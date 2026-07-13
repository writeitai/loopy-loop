from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.state_store import StateStore

REGISTER_BODY = {"worker": {"hostname": "test-host", "pid": 999983, "starttime": None}}


def test_stale_finished_mismatch_does_not_record_history_twice(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Calling /finished with stale ids does not double-append history."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json=REGISTER_BODY).json()
    # First /finished — legitimate call, processes the task.
    first = client.post(
        "/finished",
        json={
            "workflow_id": reg["workflow_id"],
            "session_id": reg["session_id"],
            "iteration": reg["iteration"],
            "attempt_id": reg.get("attempt_id"),
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    state_after_first = store.read_state()

    # Second /finished with SAME ids — now stale (current_task is None or different).
    # This should not add a second history entry.
    client.post(
        "/finished",
        json={
            "workflow_id": reg["workflow_id"],
            "session_id": reg["session_id"],
            "iteration": reg["iteration"],
            "attempt_id": reg.get("attempt_id"),
            "success": True,
            "text": "done again",
            "error": None,
        },
    )
    state_after_second = store.read_state()

    assert first["action"] == "run"
    assert state_after_first is not None
    assert len(state_after_first.history) == 1
    assert state_after_second is not None
    # History must not have grown — the stale call dispatches a fresh task but
    # does not double-record the already-processed result.
    assert state_after_second.history[0].workflow_id == reg["workflow_id"]


def test_stale_finished_returns_current_task_run_response(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Stale /finished with mismatched ids returns the CURRENT running task's info."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json=REGISTER_BODY).json()
    # Send /finished with wrong session_id — stale mismatch FROM THE OWNER
    # (the live-task replay is only served to the task's recorded worker).
    stale = client.post(
        "/finished",
        json={
            "worker": REGISTER_BODY["worker"],
            "workflow_id": reg["workflow_id"],
            "session_id": "stale-session-id",
            "iteration": reg["iteration"],
            "attempt_id": reg.get("attempt_id"),
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    state = store.read_state()

    # The stale call returns the current task (same one that /register returned).
    assert stale["action"] == "run"
    assert stale["workflow_id"] == reg["workflow_id"]
    assert stale["session_id"] == reg["session_id"]
    assert stale["iteration"] == reg["iteration"]
    # State must not be mutated.
    assert state is not None
    assert len(state.history) == 0
    assert state.current_task is not None


def test_finished_no_current_task_dispatches_fresh(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """/finished with no current_task dispatches the next available task."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    # Verify no current task is active.
    assert state.current_task is None

    response = client.post(
        "/finished",
        json={
            "workflow_id": "planner",
            "session_id": state.active_session_id,
            "iteration": 1,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()

    assert response["action"] == "run"
    assert response["workflow_id"] is not None
    assert response["session_id"] is not None
    assert response["iteration"] is not None


def test_finished_no_current_task_when_terminal_returns_stop(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """/finished with no current_task AND terminal state returns stop."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    # Set terminal state, ensure no current_task.
    state.goal_met = True
    assert state.current_task is None
    store.write_state(state=state)

    response = client.post(
        "/finished",
        json={
            "workflow_id": "planner",
            "session_id": state.active_session_id,
            "iteration": 1,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"
