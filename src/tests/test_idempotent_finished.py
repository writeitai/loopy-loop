from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.sessions import trace_finalization_outbox_dir_path
from loopy_loop.state_store import StateStore
from tests.protocol_helpers import v2_finished_body
from tests.protocol_helpers import v2_register_body


def test_stale_finished_mismatch_does_not_record_history_twice(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Calling /finished with stale ids does not double-append history."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json=v2_register_body(repo_root)).json()
    # First /finished — legitimate call, processes the task.
    first = client.post("/finished", json=v2_finished_body(reg, success=True)).json()
    state_after_first = store.read_state()

    # Second /finished with SAME ids — now stale (current_task is None or different).
    # This should not add a second history entry.
    second = client.post(
        "/finished", json=v2_finished_body(reg, success=True, text="done again")
    ).json()
    state_after_second = store.read_state()

    assert first["action"] == "run"
    assert state_after_first is not None
    assert len(state_after_first.history) == 1
    assert state_after_second is not None
    # History must not have grown — the stale call dispatches a fresh task but
    # does not double-record the already-processed result.
    assert second["action"] == "run"
    assert len(state_after_second.history) == 1
    assert state_after_second.history[0].workflow_id == reg["workflow_id"]
    assert (
        list(trace_finalization_outbox_dir_path(repo_root=repo_root).glob("*.json"))
        == []
    )


def test_stale_finished_returns_current_task_run_response(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Stale /finished with mismatched ids returns the CURRENT running task's info."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json=v2_register_body(repo_root)).json()
    # Send /finished with wrong session_id — stale mismatch FROM THE OWNER
    # (the live-task replay is only served to the task's recorded worker).
    stale = client.post(
        "/finished",
        json=v2_finished_body(reg, success=True, session_id="stale-session-id"),
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


def test_v2_finished_no_current_task_requires_validated_handshake(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """An unhandshaked v2 /finished cannot manufacture a fresh assignment."""
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
    )

    assert response.status_code == 426
    assert "call /register with protocol v2" in response.json()["detail"]
    unchanged = store.read_state()
    assert unchanged is not None
    assert unchanged.current_task is None


def test_legacy_finished_no_current_task_keeps_v1_dispatch_behavior(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.schema_version = 1
    store.write_state(state=state)

    response = client.post(
        "/finished",
        json={
            "workflow_id": "planner",
            "session_id": state.active_session_id,
            "iteration": 1,
            "success": True,
        },
    )

    assert response.status_code == 200
    task = response.json()
    assert task["action"] == "run"
    assert task["coordinator_protocol_version"] == 1
    assert task["workflow_snapshot"] is None

    completed = client.post(
        "/finished",
        json={
            "workflow_id": task["workflow_id"],
            "session_id": task["session_id"],
            "iteration": task["iteration"],
            "attempt_id": task["attempt_id"],
            "success": True,
            "text": "legacy task complete",
        },
    )

    assert completed.status_code == 200
    assert (
        list(trace_finalization_outbox_dir_path(repo_root=repo_root).glob("*.json"))
        == []
    )


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
