"""Tests for P0.1: durable session-stack / active-child crash recovery.

A coordinator restart used to reopen the latest TOP-LEVEL session, silently
orphaning a running child (its request file was already consumed, its state
stayed "running" forever, and the parent could dispatch duplicate work). The
parent now records a durable ``active_child_session_id`` pointer, startup
walks the pointer chain to the deepest live session, and every crash window
in the dispatch transition reconciles deterministically.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest

from loopy_loop.config import ConfigError
from loopy_loop.coordinator_app import ChildLedgerError
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.recovery import RecoveryOutcome
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import state_path
from loopy_loop.state_store import StateInvariantError
from loopy_loop.state_store import StateStore
from tests.protocol_helpers import v2_finished_body
from tests.protocol_helpers import v2_register_body

CHILD_WORKFLOW_CONFIG = "\n".join(
    [
        "enabled: true",
        "run_every: 1",
        "must_follow: null",
        "not_before_iteration: 0",
        "description: Child work",
    ]
)


def _build_repo_with_child_set(repo_builder: Any) -> Any:
    repo_root = repo_builder()
    child_workflow_dir = (
        repo_root
        / ".loopy_loop"
        / "workflow_sets"
        / "child_set"
        / "workflows"
        / "child_work"
    )
    child_workflow_dir.mkdir(parents=True)
    child_workflow_dir.joinpath("prompt.txt").write_text(
        "Do the child work.", encoding="utf-8"
    )
    child_workflow_dir.joinpath("config.yaml").write_text(
        CHILD_WORKFLOW_CONFIG + "\n", encoding="utf-8"
    )
    return repo_root


def _dispatch_child(client: TestClient, repo_root: Any) -> tuple[dict, dict]:
    """Drive the loop until a child session is dispatched; return (parent, child)."""
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("child.json").write_text(
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Handle a focused child task.",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    child_task = client.post(
        "/finished",
        json=v2_finished_body(parent_task, success=True, text="parent planned child"),
    ).json()
    assert child_task["workflow_set"] == "child_set"
    return parent_task, child_task


def _write_state_without_commit_validation(repo_root: Any, state: Any) -> None:
    """Simulate an already-persisted corrupt/crash projection for startup tests."""
    state_path(repo_root=repo_root, session_id=state.active_session_id).write_text(
        state.model_dump_json(indent=2), encoding="utf-8"
    )


def _v2_child_request_body(
    *, parent_task: dict[str, Any], request_id: str, goal: str
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "request_id": request_id,
        "workflow_set": "child_set",
        "origin": {
            "parent_attempt_id": parent_task["attempt_id"],
            "parent_work_item_id": f"work-{request_id}",
        },
        "assignment": {
            "goal": goal,
            "completion_criteria": ["done"],
            "stop_criteria": [],
            "constraints": [],
            "deliverables": [],
            "required_evidence": [],
        },
        "inputs": [],
    }


def _parent_state(repo_root: Any, parent_session_id: str):
    return StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=parent_session_id),
    ).read_state()


def _stub_recovery(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.recover_interrupted_iteration",
        lambda **kwargs: RecoveryOutcome(),
    )


# ---------------------------------------------------------------------------
# The durable pointer itself
# ---------------------------------------------------------------------------


def test_child_dispatch_records_parent_pointer_and_request_file(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id == child_task["session_id"]
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert records["children"][0]["request_file"] == "child.json"


def test_runtime_child_completion_clears_pointer(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    control_path(repo_root=repo_root, session_id=child_task["session_id"]).write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "child complete",
                "stop_reason": "goal_met",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    resumed = client.post(
        "/finished", json=v2_finished_body(child_task, success=True, text="child done")
    ).json()
    assert resumed["session_id"] == parent_task["session_id"]
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id is None


# ---------------------------------------------------------------------------
# Coordinator restart scenarios (the crash gap this feature closes)
# ---------------------------------------------------------------------------


def test_restart_mid_child_resumes_the_child_not_the_parent(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    # Coordinator "crashes": a brand-new app resumes from disk.
    _stub_recovery(monkeypatch)
    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=v2_register_body(repo_root)).json()

    # The CHILD's work continues — previously the parent was silently resumed
    # and the running child orphaned forever.
    assert response["action"] == "run"
    assert response["workflow_set"] == "child_set"
    assert response["session_id"] == child_task["session_id"]
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id == child_task["session_id"]


def test_restart_after_child_terminal_finalizes_and_resumes_parent(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    # The child reached a terminal state on disk, but the coordinator died
    # before resuming the parent (the exact window Codex's earlier review
    # flagged as untested).
    child_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child_task["session_id"]),
    )
    child = child_store.read_state()
    assert child is not None
    child.status = "goal_met"
    child.stop_reason = "goal_met"
    child.current_task = None
    child_store.write_state(state=child)

    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=v2_register_body(repo_root)).json()

    assert response["action"] == "run"
    assert response["session_id"] == parent_task["session_id"]
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id is None
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert records["children"][0]["status"] == "goal_met"


def test_restart_refuses_pointer_without_exact_ledger_edge(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # A durable pointer cannot be cleared merely because its target is absent:
    # without the exact ledger edge there is no evidence this is an interrupted
    # dispatch rather than state/ledger corruption.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.current_task = None
    missing_child_id = "20990101_000000_deadbeef_missing0"
    state.active_child_session_id = missing_child_id
    store.write_state(state=state)

    with pytest.raises(ChildLedgerError, match="requires exactly one ledger edge"):
        create_coordinator_app(repo_root=repo_root, resume=True)
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id == "20990101_000000_deadbeef_missing0"


def test_restart_adopts_dispatching_child_when_pointer_never_committed(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # Crash window: child fully created + children.json recorded, but the
    # parent state commit (which carries the pointer) never landed.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    parent.active_child_session_id = None  # simulate the lost commit
    parent_store.write_state(state=parent)
    ledger_path = children_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["children"][0]["status"] = "dispatching"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    _stub_recovery(monkeypatch)
    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=v2_register_body(repo_root)).json()

    assert response["session_id"] == child_task["session_id"]
    repaired = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert repaired["children"][0]["status"] == "running"
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id == child_task["session_id"]  # re-adopted
    assert (
        json.loads(ledger_path.read_text(encoding="utf-8"))["children"][0]["status"]
        == "running"
    )


def test_restart_hydrates_legacy_nested_state_identity_from_physical_tree(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    parent_root = session_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    child_root = session_dir_path(
        repo_root=repo_root, session_id=child_task["session_id"]
    )
    for manifest_path in (parent_root / "session.json", child_root / "session.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest.pop("root_session_id", None)
        manifest.pop("depth", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ledger_path = parent_root / "children.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["schema_version"] = 1
    ledger.pop("parent_session_id", None)
    ledger.pop("revision", None)
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    for state_file, nested in (
        (parent_root / "state.json", False),
        (child_root / "state.json", True),
    ):
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        payload.pop("root_session_id", None)
        payload.pop("depth", None)
        if nested:
            payload.pop("parent_session_id", None)
        state_file.write_text(json.dumps(payload), encoding="utf-8")

    create_coordinator_app(repo_root=repo_root, resume=True)

    hydrated = json.loads((child_root / "state.json").read_text(encoding="utf-8"))
    assert hydrated["parent_session_id"] == parent_task["session_id"]
    assert hydrated["root_session_id"] == parent_task["session_id"]
    assert hydrated["depth"] == 1


def test_restart_repairs_false_terminal_ledger_record_for_live_child(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    parent.active_child_session_id = None
    parent_store.write_state(state=parent)
    ledger_path = children_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["children"][0].update(
        {
            "status": "goal_met",
            "completed_at": "2026-01-01T00:00:00Z",
            "usage": {},
            "outcome_ref": "session:/child_outcomes/decoy.json",
        }
    )
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=v2_register_body(repo_root)).json()

    assert response["session_id"] == child_task["session_id"]


def test_resume_rejects_terminal_root_that_still_points_to_live_child(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    assert parent.active_child_session_id == child_task["session_id"]
    parent.status = "goal_met"
    parent.goal_met = True
    parent.stop_reason = "goal_met"
    _write_state_without_commit_validation(repo_root, parent)

    with pytest.raises(
        ConfigError, match="terminal v2 state cannot retain.*active child"
    ):
        create_coordinator_app(repo_root=repo_root, resume=True)

    child = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child_task["session_id"]),
    ).read_state()
    assert child is not None
    assert child.current_task is not None


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("owner", "no worker owner"),
        ("attempt", "no safe attempt identity"),
        ("repository", "no repository identity"),
        ("completion", "completion contract v2"),
        ("assignment", "no frozen assignment hash"),
        ("containing_session", "session does not match"),
        ("snapshot_identity", "snapshot identity contradicts"),
    ],
)
def test_resume_rejects_corrupt_v2_current_task_contract(
    repo_builder: Any, monkeypatch: Any, corruption: str, message: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=v2_register_body(repo_root)).json()
    path = state_path(repo_root=repo_root, session_id=task["session_id"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    current = payload["current_task"]
    if corruption == "owner":
        current["worker"] = None
    elif corruption == "attempt":
        current["attempt_id"] = "../unsafe"
    elif corruption == "repository":
        current["repository_id"] = None
    elif corruption == "completion":
        current["completion_contract_version"] = 1
    elif corruption == "assignment":
        current["assignment_sha256"] = "sha256:not-a-digest"
    elif corruption == "containing_session":
        current["session_id"] = "wrong-session"
    elif corruption == "snapshot_identity":
        current["workflow_snapshot"]["attempt_id"] = "wrong-attempt"
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(corruption)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        create_coordinator_app(repo_root=repo_root, resume=True)


def test_v2_commit_rejects_current_task_containing_state_mismatch(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=v2_register_body(repo_root)).json()
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    assert state.current_task is not None
    state.current_task.session_id = "wrong-session"

    with pytest.raises(StateInvariantError, match="session does not match"):
        store.write_state(state=state)

    persisted = store.read_state()
    assert persisted is not None
    assert persisted.current_task is not None
    assert persisted.current_task.session_id == task["session_id"]


# ---------------------------------------------------------------------------
# Request-file idempotency and rejection
# ---------------------------------------------------------------------------


def test_leftover_request_file_never_dispatches_twice(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # Crash window: children.json recorded the child but the request unlink
    # never happened. After the child completes, the leftover file must not
    # spawn a duplicate child.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.joinpath("child.json").write_text(  # resurrect the consumed file
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Handle a focused child task.",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    control_path(repo_root=repo_root, session_id=child_task["session_id"]).write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "child complete",
                "stop_reason": "goal_met",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    resumed = client.post(
        "/finished", json=v2_finished_body(child_task, success=True, text="child done")
    ).json()
    assert resumed["session_id"] == parent_task["session_id"]
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert len(records["children"]) == 1  # no duplicate child
    assert not request_dir.joinpath("child.json").exists()  # hygiene


def test_invalid_child_request_is_rejected_terminally(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("broken.json").write_text("{not json", encoding="utf-8")

    response = client.post(
        "/finished", json=v2_finished_body(parent_task, success=True)
    ).json()
    assert response["action"] == "run"
    assert response["workflow_set"] == "main"  # normal dispatch, no child
    assert not request_dir.joinpath("broken.json").exists()
    assert request_dir.joinpath("broken.json.rejected").exists()


def test_accepted_request_crash_replay_dispatches_exact_body(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    request_path = (
        child_requests_dir_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        )
        / "accepted-crash.json"
    )
    request_path.write_text(
        json.dumps(
            _v2_child_request_body(
                parent_task=parent_task,
                request_id="accepted-crash",
                goal="Resume the exact accepted request",
            )
        ),
        encoding="utf-8",
    )
    app.state.service._archive_accepted_request(
        parent_session_id=parent_task["session_id"],
        request_id="accepted-crash",
        request_path=request_path,
    )

    child_task = client.post(
        "/finished", json=v2_finished_body(parent_task, success=True)
    ).json()

    assert child_task["workflow_set"] == "child_set"
    ledger = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert [record["request_id"] for record in ledger["children"]] == ["accepted-crash"]


def test_conflicting_request_id_is_rejected_without_wedging_finished(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    request_path = (
        child_requests_dir_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        )
        / "conflict.json"
    )
    original = _v2_child_request_body(
        parent_task=parent_task,
        request_id="stable-request",
        goal="Original immutable goal",
    )
    request_path.write_text(json.dumps(original), encoding="utf-8")
    app.state.service._archive_accepted_request(
        parent_session_id=parent_task["session_id"],
        request_id="stable-request",
        request_path=request_path,
    )
    conflicting = dict(original)
    conflicting["assignment"] = {**original["assignment"], "goal": "Conflicting goal"}
    request_path.write_text(json.dumps(conflicting), encoding="utf-8")

    response = client.post(
        "/finished", json=v2_finished_body(parent_task, success=True)
    )

    assert response.status_code == 200
    assert response.json()["workflow_set"] == "main"
    receipts = list(request_path.parent.joinpath("rejected").glob("*.receipt.json"))
    assert any(
        "reused with a different body"
        in json.loads(path.read_text(encoding="utf-8"))["reason"]
        for path in receipts
    )


def test_child_request_rejects_unsafe_workflow_set_before_path_resolution(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    request_path = (
        child_requests_dir_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        )
        / "unsafe-workflow.json"
    )
    body = _v2_child_request_body(
        parent_task=parent_task,
        request_id="unsafe-workflow",
        goal="Must not escape the workflow-set root",
    )
    body["workflow_set"] = "../outside"
    request_path.write_text(json.dumps(body), encoding="utf-8")

    response = client.post(
        "/finished", json=v2_finished_body(parent_task, success=True)
    )

    assert response.status_code == 200
    receipt = next(request_path.parent.joinpath("rejected").glob("*.receipt.json"))
    assert (
        "filesystem-safe" in json.loads(receipt.read_text(encoding="utf-8"))["reason"]
    )


# ---------------------------------------------------------------------------
# Attempt ids
# ---------------------------------------------------------------------------


def test_dispatch_carries_attempt_id_and_stale_attempt_is_not_processed(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=v2_register_body(repo_root)).json()
    assert task["attempt_id"], "every dispatch must carry an attempt id"

    # Same coordinates, WRONG attempt: a late /finished from a superseded
    # attempt must be treated as stale (owner gets the live-task replay,
    # state is not mutated), never processed as the current result.
    stale = client.post(
        "/finished",
        json=v2_finished_body(
            task,
            success=True,
            text="late result from a previous attempt",
            attempt_id="superseded0000",
        ),
    ).json()
    assert stale["action"] == "run"
    assert stale["attempt_id"] == task["attempt_id"]  # the live attempt
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.history == []  # nothing was recorded

    # Correct attempt: processed normally.
    done = client.post("/finished", json=v2_finished_body(task, success=True)).json()
    assert done["action"] == "run"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert len(state.history) == 1


# ---------------------------------------------------------------------------
# Review-driven coverage (Codex P0.1 review: C1, M1-M6, m1)
# ---------------------------------------------------------------------------


def test_duplicate_finished_never_gives_suspended_parent_a_task(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # C1: overlapping /finished retries. The first dispatches the child and
    # commits the suspended parent; a duplicate retry must NOT advance the
    # parent (parent task + child task live simultaneously). It gets the
    # child's live task instead — idempotent with the first response.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    duplicate = client.post(
        "/finished",
        json=v2_finished_body(parent_task, success=True, text="parent planned child"),
    ).json()
    assert duplicate["action"] == "run"
    assert duplicate["session_id"] == child_task["session_id"]  # child, not parent
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.current_task is None  # the invariant C1 violated
    assert parent.active_child_session_id == child_task["session_id"]


def test_completed_childs_request_filename_is_reusable(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M1: the tombstone must apply only while the record is running — a later,
    # genuinely new request under the same filename must dispatch.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    # Two independent parent workflows: the default repo's goal_check would
    # be dispatched after the resume and fail (no goal_check.json), blocking
    # the after-success child scan this test needs; a single workflow would
    # starve the resume (run_every not yet satisfied).
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Plan",
                },
            },
            "implement": {
                "prompt": "Implement.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement",
                },
            },
        }
    )
    child_workflow_dir = (
        repo_root
        / ".loopy_loop"
        / "workflow_sets"
        / "child_set"
        / "workflows"
        / "child_work"
    )
    child_workflow_dir.mkdir(parents=True)
    child_workflow_dir.joinpath("prompt.txt").write_text(
        "Do the child work.", encoding="utf-8"
    )
    child_workflow_dir.joinpath("config.yaml").write_text(
        CHILD_WORKFLOW_CONFIG + "\n", encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    control_path(repo_root=repo_root, session_id=child_task["session_id"]).write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "done",
                "stop_reason": "goal_met",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    resumed_parent = client.post(
        "/finished",
        json=v2_finished_body(child_task, success=True, text="child A done"),
    ).json()
    assert resumed_parent["session_id"] == parent_task["session_id"]

    # A NEW request reusing the same filename for new work:
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.joinpath("child.json").write_text(
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Second work item.",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    second_child = client.post(
        "/finished",
        json=v2_finished_body(resumed_parent, success=True, text="planned item B"),
    ).json()
    assert second_child["workflow_set"] == "child_set"
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert len(records["children"]) == 2  # B was dispatched, not swallowed


def test_restart_reconciles_record_without_child_state(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M2 window (record lands before child state): a running record whose
    # child state is missing is marked failed_dispatch and its request file
    # redispatches exactly once.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    # Simulate the crash: erase the child's state (record + request remain).
    child_state_path = state_path(
        repo_root=repo_root, session_id=child_task["session_id"]
    )
    child_state_path.unlink()
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    parent.active_child_session_id = None  # pointer commit never landed
    parent_store.write_state(state=parent)
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.joinpath("child.json").write_text(  # unlink never happened
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Handle a focused child task.",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    _stub_recovery(monkeypatch)
    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=v2_register_body(repo_root)).json()
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert len(records["children"]) == 1
    assert records["children"][0]["status"] == "running"
    assert records["children"][0]["dispatch_failures"][0]["reason"] == (
        "child state was never written"
    )
    assert response["session_id"] == child_task["session_id"]
    physical_children = [
        path
        for path in session_dir_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        )
        .joinpath("children")
        .iterdir()
        if path.is_dir() and not path.name.startswith(".staging-")
    ]
    assert [path.name for path in physical_children] == [child_task["session_id"]]
    # The request redispatches (either already in this response or on the
    # next successful parent iteration) — the file must not be tombstoned.
    assert response["action"] == "run"


def test_restart_finalizes_terminal_child_without_pointer(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M3: terminal child + running record + NO pointer (pre-pointer version
    # crash) was ignored forever. Reconciliation must finalize it.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    child_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child_task["session_id"]),
    )
    child = child_store.read_state()
    assert child is not None
    child.status = "goal_met"
    child.stop_reason = "goal_met"
    child.current_task = None
    child_store.write_state(state=child)
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    parent.active_child_session_id = None  # pre-pointer state
    parent_store.write_state(state=parent)

    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=v2_register_body(repo_root)).json()
    assert response["session_id"] == parent_task["session_id"]
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert records["children"][0]["status"] == "goal_met"  # finalized, not stuck


@pytest.mark.parametrize(
    "corruption",
    ["outcome_ref", "stop_reason", "usage", "completed_at", "outcome_file"],
)
def test_terminal_child_projection_mismatch_is_reconstructed_exactly(
    repo_builder: Any, monkeypatch: Any, corruption: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)

    child_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child_task["session_id"]),
    )
    child = child_store.read_state()
    assert child is not None
    child.status = "goal_met"
    child.goal_met = True
    child.stop_reason = "goal_met"
    child.current_task = None
    child_store.write_state(state=child)
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    parent.active_child_session_id = None
    parent_store.write_state(state=parent)

    app = create_coordinator_app(repo_root=repo_root, resume=True)
    ledger_path = children_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    corrupted = json.loads(ledger_path.read_text(encoding="utf-8"))
    record = corrupted["children"][0]
    request_id = record["request_id"]
    outcome_path = (
        session_dir_path(repo_root=repo_root, session_id=parent_task["session_id"])
        / "child_outcomes"
        / f"{request_id}.json"
    )
    if corruption == "outcome_ref":
        record["outcome_ref"] = "session:/child_outcomes/decoy.json"
    elif corruption == "stop_reason":
        record["stop_reason"] = "forged-reason"
    elif corruption == "usage":
        record["usage"]["prompt_tokens"] = 999
    elif corruption == "completed_at":
        record["completed_at"] = "2020-01-01T00:00:00Z"
    elif corruption == "outcome_file":
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        outcome["lifecycle"]["status"] = "failed"
        outcome_path.write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(corruption)
    ledger_path.write_text(json.dumps(corrupted, indent=2), encoding="utf-8")

    repaired = app.state.service._read_or_repair_children_payload(path=ledger_path)
    repaired_record = repaired["children"][0]
    canonical_ref = f"session:/child_outcomes/{request_id}.json"
    assert repaired_record["status"] == "goal_met"
    assert repaired_record["stop_reason"] == "goal_met"
    assert repaired_record["completed_at"] != "2020-01-01T00:00:00Z"
    assert repaired_record["outcome_ref"] == canonical_ref
    assert repaired_record["usage"]["prompt_tokens"] == 0
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    assert outcome["lifecycle"] == {
        "status": "goal_met",
        "stop_reason": "goal_met",
        "completed_at": repaired_record["completed_at"],
    }
    assert outcome["usage"] == repaired_record["usage"]


def test_terminal_child_completed_at_is_derived_from_terminal_history(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    child_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child_task["session_id"]),
    )
    child = child_store.read_state()
    assert child is not None
    child.stop_requested = True
    child_store.write_state(state=child)

    response = client.post("/finished", json=v2_finished_body(child_task, success=True))
    assert response.status_code == 200
    terminal = child_store.read_state()
    assert terminal is not None
    assert terminal.status == "stopped"
    assert terminal.history
    expected = terminal.history[-1].finished_at.isoformat().replace("+00:00", "Z")
    ledger = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert ledger["children"][0]["completed_at"] == expected


def test_first_child_task_carries_an_attempt_id(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M4: the initial child dispatch bypassed _advance and shipped a null
    # attempt, leaving the child's first iteration unfenced.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    _, child_task = _dispatch_child(client, repo_root)
    assert child_task["attempt_id"]


def test_stale_result_artifact_cannot_complete_a_new_attempt(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M5 case 3: a stale pending file is rejected, but the stale result.json
    # right next to it must not slip through as the NEW attempt's completion.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=v2_register_body(repo_root)).json()

    from loopy_loop.sessions import ensure_iteration_dir

    iteration_dir = ensure_iteration_dir(
        repo_root=repo_root,
        session_id=task["session_id"],
        iteration=task["iteration"],
        workflow_id=task["workflow_id"],
    )
    iteration_dir.joinpath("result.json").write_text(
        json.dumps(
            {
                "success": True,
                "text": "stale result from a superseded attempt",
                "error": None,
                "harness_run_id": "old",
                "harness_output_dir": "",
                "attempt_id": "superseded0000",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.is_worker_alive", lambda identity: False
    )
    _stub_recovery(monkeypatch)
    response = client.post("/register", json=v2_register_body(repo_root)).json()
    assert response["action"] == "run"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    # The stale artifact was NOT recorded as a successful completion:
    assert state.history[0].success is False
    assert state.history[0].error in {"abandoned", "abandoned_after_drain"}


def test_semantically_invalid_child_request_is_rejected_not_wedging(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M6: a schema-valid request naming an unknown workflow set previously
    # raised out of the mutator — HTTP 500 on EVERY completion, forever.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post("/register", json=v2_register_body(repo_root)).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("bad.json").write_text(
        json.dumps(
            {"workflow_set": "does_not_exist", "goal": "x", "schema_version": 1}
        ),
        encoding="utf-8",
    )
    response = client.post(
        "/finished", json=v2_finished_body(parent_task, success=True)
    )
    assert response.status_code == 200  # the completion is committed
    assert response.json()["action"] == "run"
    assert not request_dir.joinpath("bad.json").exists()
    rejected = list(request_dir.glob("bad.json*.rejected")) + list(
        request_dir.glob("bad.json.rejected")
    )
    assert rejected, "the unusable request must be terminally rejected"


def test_double_finalization_keeps_first_completed_at(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # m1: crash-replayed finalization must not rewrite the audit timestamp.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task, child_task = _dispatch_child(client, repo_root)
    control_path(repo_root=repo_root, session_id=child_task["session_id"]).write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "done",
                "stop_reason": "goal_met",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    client.post(
        "/finished", json=v2_finished_body(child_task, success=True, text="child done")
    )
    children_file = children_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    first = json.loads(children_file.read_text())["children"][0]["completed_at"]

    # Simulate the crash-replay: re-finalize at startup (pointer re-set).
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    )
    parent = parent_store.read_state()
    assert parent is not None
    parent.current_task = None
    parent.active_child_session_id = child_task["session_id"]
    parent_store.write_state(state=parent)
    TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    second = json.loads(children_file.read_text())["children"][0]["completed_at"]
    assert second == first
