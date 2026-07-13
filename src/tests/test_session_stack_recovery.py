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

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.recovery import RecoveryOutcome
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import state_path
from loopy_loop.state_store import StateStore

REGISTER_BODY = {"worker": {"hostname": "test-host", "pid": 999983, "starttime": None}}

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
    parent_task = client.post("/register", json=REGISTER_BODY).json()
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
        json={
            "workflow_id": parent_task["workflow_id"],
            "session_id": parent_task["session_id"],
            "iteration": parent_task["iteration"],
            "success": True,
            "text": "parent planned child",
            "worker": REGISTER_BODY["worker"],
            "attempt_id": parent_task["attempt_id"],
        },
    ).json()
    assert child_task["workflow_set"] == "child_set"
    return parent_task, child_task


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
        "/finished",
        json={
            "workflow_id": child_task["workflow_id"],
            "session_id": child_task["session_id"],
            "iteration": child_task["iteration"],
            "success": True,
            "text": "child done",
            "worker": REGISTER_BODY["worker"],
            "attempt_id": child_task["attempt_id"],
        },
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
    response = restarted.post("/register", json=REGISTER_BODY).json()

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
    response = restarted.post("/register", json=REGISTER_BODY).json()

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


def test_restart_with_dangling_pointer_clears_it(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # The dispatch crashed between commits: the pointer exists but the child
    # state was never written. The parent must recover cleanly.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_repo_with_child_set(repo_builder)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post("/register", json=REGISTER_BODY).json()
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.active_child_session_id = "20990101_000000_deadbeef_missing0"
    store.write_state(state=state)

    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    _stub_recovery(monkeypatch)
    response = restarted.post("/register", json=REGISTER_BODY).json()
    assert response["action"] == "run"
    assert response["session_id"] == parent_task["session_id"]
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id is None


def test_restart_adopts_running_child_when_pointer_never_committed(
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

    _stub_recovery(monkeypatch)
    restarted = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    response = restarted.post("/register", json=REGISTER_BODY).json()

    assert response["session_id"] == child_task["session_id"]
    parent = _parent_state(repo_root, parent_task["session_id"])
    assert parent is not None
    assert parent.active_child_session_id == child_task["session_id"]  # re-adopted


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
        "/finished",
        json={
            "workflow_id": child_task["workflow_id"],
            "session_id": child_task["session_id"],
            "iteration": child_task["iteration"],
            "success": True,
            "text": "child done",
            "worker": REGISTER_BODY["worker"],
            "attempt_id": child_task["attempt_id"],
        },
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
    parent_task = client.post("/register", json=REGISTER_BODY).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("broken.json").write_text("{not json", encoding="utf-8")

    response = client.post(
        "/finished",
        json={
            "workflow_id": parent_task["workflow_id"],
            "session_id": parent_task["session_id"],
            "iteration": parent_task["iteration"],
            "success": True,
            "text": "done",
            "worker": REGISTER_BODY["worker"],
            "attempt_id": parent_task["attempt_id"],
        },
    ).json()
    assert response["action"] == "run"
    assert response["workflow_set"] == "main"  # normal dispatch, no child
    assert not request_dir.joinpath("broken.json").exists()
    assert request_dir.joinpath("broken.json.rejected").exists()


# ---------------------------------------------------------------------------
# Attempt ids
# ---------------------------------------------------------------------------


def test_dispatch_carries_attempt_id_and_stale_attempt_is_not_processed(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=REGISTER_BODY).json()
    assert task["attempt_id"], "every dispatch must carry an attempt id"

    # Same coordinates, WRONG attempt: a late /finished from a superseded
    # attempt must be treated as stale (owner gets the live-task replay,
    # state is not mutated), never processed as the current result.
    stale = client.post(
        "/finished",
        json={
            "workflow_id": task["workflow_id"],
            "session_id": task["session_id"],
            "iteration": task["iteration"],
            "success": True,
            "text": "late result from a previous attempt",
            "worker": REGISTER_BODY["worker"],
            "attempt_id": "superseded0000",
        },
    ).json()
    assert stale["action"] == "run"
    assert stale["attempt_id"] == task["attempt_id"]  # the live attempt
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.history == []  # nothing was recorded

    # Correct attempt: processed normally.
    done = client.post(
        "/finished",
        json={
            "workflow_id": task["workflow_id"],
            "session_id": task["session_id"],
            "iteration": task["iteration"],
            "success": True,
            "text": "done",
            "worker": REGISTER_BODY["worker"],
            "attempt_id": task["attempt_id"],
        },
    ).json()
    assert done["action"] == "run"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert len(state.history) == 1
