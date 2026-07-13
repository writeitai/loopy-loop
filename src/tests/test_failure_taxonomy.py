"""Tests for the failure taxonomy and per-workflow failure cap (P2.3).

Every iteration failure is classified (transient / deterministic / crash /
unknown) and recorded in history, and each workflow has a consecutive-failure
circuit breaker: at the cap the loop stops with
stop_reason="workflow_failure_cap" instead of retrying a wedged workflow
until max_turns.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
import pytest

from loopy_loop.config import ConfigError
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.harness_runner import classify_failure_detail
from loopy_loop.state_store import StateStore

REGISTER_BODY = {"worker": {"hostname": "test-host", "pid": 999983, "starttime": None}}

PLANNER_ONLY = {
    "planner": {
        "prompt": "Plan the next repo change.",
        "config": {
            "enabled": True,
            "run_every": 1,
            "must_follow": None,
            "not_before_iteration": 0,
            "description": "Plan work.",
        },
    }
}


def _finished_body(
    task: dict[str, Any], *, success: bool, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": task["iteration"],
        "attempt_id": task["attempt_id"],
        "success": success,
        "text": "done" if success else None,
        "error": None if success else "harness exploded",
        "worker": REGISTER_BODY["worker"],
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# classify_failure_detail
# ---------------------------------------------------------------------------


def test_classify_retryable_true_is_transient() -> None:
    detail = {"kind": "coordinator_api", "status_code": 429, "retryable": True}
    assert classify_failure_detail(detail=detail) == "transient"


def test_classify_retryable_false_is_deterministic() -> None:
    detail = {"kind": "coordinator_auth", "status_code": 401, "retryable": False}
    assert classify_failure_detail(detail=detail) == "deterministic"


def test_classify_agent_failure_without_signal_is_unknown() -> None:
    # build_worker_failure_detail payloads carry no retryable key.
    detail = {"summary": "agent exited 1", "agent_summary": {"agent_id": "codex-1"}}
    assert classify_failure_detail(detail=detail) == "unknown"


def test_classify_missing_detail_is_unknown() -> None:
    assert classify_failure_detail(detail=None) == "unknown"


# ---------------------------------------------------------------------------
# per-workflow failure cap via the HTTP surface
# ---------------------------------------------------------------------------


def test_consecutive_failures_stop_at_cap(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"workflow_consecutive_failures_cap": 3}, workflows=PLANNER_ONLY
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    for expected_action in ("run", "run", "stop"):
        response = client.post(
            "/finished", json=_finished_body(task, success=False)
        ).json()
        assert response["action"] == expected_action
        task = response

    assert response["stop_reason"] == "workflow_failure_cap"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.status == "failed"
    assert state.stop_reason == "workflow_failure_cap"
    assert state.workflow_consecutive_failures == {"planner": 3}


def test_success_resets_the_workflow_counter(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"workflow_consecutive_failures_cap": 3}, workflows=PLANNER_ONLY
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    def run_iteration(*, success: bool) -> None:
        # Seed the live task directly: a single-workflow set with run_every=1
        # legitimately stops with no_eligible_workflow right after a success,
        # so the scheduler cannot drive a fail/succeed/fail sequence itself.
        state = store.read_state()
        assert state is not None
        state.status = "running"
        state.stop_reason = None
        iteration = state.iteration_count + 1
        state.current_task = current_task_factory(
            workflow_id="planner",
            session_id=state.active_session_id,
            iteration=iteration,
        )
        store.write_state(state=state)
        client.post(
            "/finished",
            json={
                "workflow_id": "planner",
                "session_id": state.active_session_id,
                "iteration": iteration,
                "success": success,
                "text": "t",
                "error": None if success else "boom",
            },
        )

    for success in (False, False, True, False, False):
        run_iteration(success=success)

    state = store.read_state()
    assert state is not None
    # The success popped the counter; only the two post-success failures count.
    assert state.workflow_consecutive_failures == {"planner": 2}
    assert state.stop_reason != "workflow_failure_cap"

    run_iteration(success=False)
    state = store.read_state()
    assert state is not None
    assert state.status == "failed"
    assert state.stop_reason == "workflow_failure_cap"
    assert state.workflow_consecutive_failures == {"planner": 3}


def test_counter_survives_coordinator_restart(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"workflow_consecutive_failures_cap": 3}, workflows=PLANNER_ONLY
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=REGISTER_BODY).json()
    for _ in range(2):
        task = client.post("/finished", json=_finished_body(task, success=False)).json()

    resumed = TestClient(create_coordinator_app(repo_root=repo_root, resume=True))
    # The dispatched task is still live; its worker (pid 999983) is not
    # verifiably alive, nothing is recoverable, and the recorded worker ran on
    # another host, so the register records a crash-abandoned iteration —
    # the third consecutive planner failure.
    response = resumed.post("/register", json=REGISTER_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "stop"
    assert body["stop_reason"] == "workflow_failure_cap"

    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.workflow_consecutive_failures == {"planner": 3}
    assert state.history[-1].failure_kind == "crash"
    assert state.history[-1].error == "abandoned"


def test_goal_check_broken_wins_over_workflow_cap(
    repo_builder: Any,
    monkeypatch: Any,
    current_task_factory: Any,
    history_entry_factory: Any,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={
            "goal_check_consecutive_failures_cap": 1,
            "workflow_consecutive_failures_cap": 1,
        }
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.history.append(history_entry_factory(workflow_id="planner", success=True))
    state.iteration_count = 1
    state.current_task = current_task_factory(
        workflow_id="goal_check", session_id=state.active_session_id, iteration=2
    )
    store.write_state(state=state)

    # No goal_check.json written — both caps trip on the same iteration; the
    # goal_check_broken decision must not be overwritten by the workflow cap.
    response = client.post(
        "/finished",
        json={
            "workflow_id": "goal_check",
            "session_id": state.active_session_id,
            "iteration": 2,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_check_broken"
    updated = store.read_state()
    assert updated is not None
    assert updated.status == "failed"
    assert updated.workflow_consecutive_failures == {"goal_check": 1}


# ---------------------------------------------------------------------------
# failure_kind recording
# ---------------------------------------------------------------------------


def test_failure_kind_recorded_in_history(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    task = client.post(
        "/finished", json=_finished_body(task, success=False, failure_kind="transient")
    ).json()
    # A pre-taxonomy caller (no failure_kind) still works and records None.
    client.post("/finished", json=_finished_body(task, success=False)).json()

    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert [entry.failure_kind for entry in state.history] == ["transient", None]


def test_invalid_failure_kind_is_rejected(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    response = client.post(
        "/finished", json=_finished_body(task, success=False, failure_kind="oops")
    )
    assert response.status_code == 422


def test_cap_is_not_sent_in_wire_snapshot(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"workflow_consecutive_failures_cap": 7}, workflows=PLANNER_ONLY
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    # Released workers validate the snapshot with extra="forbid"; the cap is a
    # coordinator-side setting and must stay off the wire.
    assert "workflow_consecutive_failures_cap" not in task["config_snapshot"]


def test_cap_below_one_is_rejected(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"workflow_consecutive_failures_cap": 0}, workflows=PLANNER_ONLY
    )
    with pytest.raises(ConfigError):
        create_coordinator_app(repo_root=repo_root, resume=False)


def test_register_resumes_parent_when_child_trips_cap(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Review M1: a child terminalized during /register recovery must be
    finalized and its parent resumed — without a coordinator restart."""
    import json as _json

    from loopy_loop.sessions import child_requests_dir_path
    from loopy_loop.sessions import children_path
    from loopy_loop.sessions import state_path

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"workflow_consecutive_failures_cap": 1})
    child_dir = (
        repo_root
        / ".loopy_loop"
        / "workflow_sets"
        / "child_set"
        / "workflows"
        / "child_work"
    )
    child_dir.mkdir(parents=True)
    child_dir.joinpath("prompt.txt").write_text("Child work.", encoding="utf-8")
    child_dir.joinpath("config.yaml").write_text(
        "enabled: true\nrun_every: 1\n", encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    parent_task = client.post("/register", json=REGISTER_BODY).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("child.json").write_text(
        _json.dumps(
            {"workflow_set": "child_set", "goal": "Child goal.", "schema_version": 1}
        ),
        encoding="utf-8",
    )
    child_task = client.post(
        "/finished", json=_finished_body(parent_task, success=True)
    ).json()
    assert child_task["workflow_set"] == "child_set"

    # The child's worker dies; the recorded worker ran on another host, so the
    # replacement /register records a crash-abandoned iteration — the child's
    # first and (cap=1) final failure. The register must come back with the
    # PARENT's next work, not the dead child's stop.
    response = client.post("/register", json=REGISTER_BODY).json()

    assert response["session_id"] == parent_task["session_id"]
    child_state = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child_task["session_id"]),
    ).read_state()
    assert child_state is not None
    assert child_state.status == "failed"
    assert child_state.stop_reason == "workflow_failure_cap"
    payload = _json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text()
    )
    assert payload["children"][0]["status"] == "failed"
    assert payload["children"][0]["stop_reason"] == "workflow_failure_cap"
    parent_state = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    ).read_state()
    assert parent_state is not None
    assert parent_state.active_child_session_id is None


def test_coordinator_flip_overrides_stale_failure_kind(
    repo_builder: Any,
    monkeypatch: Any,
    current_task_factory: Any,
    history_entry_factory: Any,
) -> None:
    """Review M3: when the coordinator flips a result to a protocol failure,
    the recorded kind must describe that flip, not the harness outcome."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.history.append(history_entry_factory(workflow_id="planner", success=True))
    state.iteration_count = 1
    state.current_task = current_task_factory(
        workflow_id="goal_check", session_id=state.active_session_id, iteration=2
    )
    store.write_state(state=state)

    # Harness "succeeded" but no goal_check.json exists -> flipped to failure.
    client.post(
        "/finished",
        json={
            "workflow_id": "goal_check",
            "session_id": state.active_session_id,
            "iteration": 2,
            "success": True,
            "text": "done",
            "error": None,
            "failure_kind": "transient",
        },
    )

    updated = store.read_state()
    assert updated is not None
    entry = updated.history[-1]
    assert entry.error == "invalid_goal_check_output"
    assert entry.failure_kind == "unknown"


def test_workflow_cap_wins_over_simultaneous_max_turns(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Review minor-1: the more specific diagnosis must not be relabeled."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"workflow_consecutive_failures_cap": 1, "max_turns": 1},
        workflows=PLANNER_ONLY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    response = client.post("/finished", json=_finished_body(task, success=False)).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "workflow_failure_cap"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.status == "failed"
    assert state.stop_reason == "workflow_failure_cap"


def test_classify_unconfirmed_retryable_false_is_unknown() -> None:
    """Review M4: protocol/stream failures default retryable=False; without
    corroboration they must not be called deterministic."""
    assert (
        classify_failure_detail(
            detail={"kind": "coordinator_api", "retryable": False, "status_code": None}
        )
        == "unknown"
    )
    assert (
        classify_failure_detail(
            detail={"kind": "coordinator_api", "retryable": False, "status_code": 500}
        )
        == "unknown"
    )
    assert (
        classify_failure_detail(
            detail={"kind": "coordinator_api", "retryable": False, "status_code": 404}
        )
        == "deterministic"
    )
    assert (
        classify_failure_detail(
            detail={"kind": "coordinator_auth", "retryable": False, "status_code": 401}
        )
        == "deterministic"
    )


def test_run_harness_iteration_normalizes_systemexit(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    """Review M4: SDK-side sys.exit must become a deterministic failed result,
    not kill the worker (which would later read as a crash)."""
    from loopy_loop.harness_runner import run_harness_iteration

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)

    class ExitingHarness:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, task: str) -> Any:
            raise SystemExit(2)

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="prompt",
        harness_factory=ExitingHarness,
    )

    assert result.success is False
    assert result.failure_kind == "deterministic"
    assert "exited during run" in (result.error or "")
