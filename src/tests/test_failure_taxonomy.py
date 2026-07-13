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
