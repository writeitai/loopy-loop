from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.sessions import control_path
from loopy_loop.state_store import StateStore


def test_register_returns_run_response(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    response = client.post("/register", json={}).json()

    assert response["action"] == "run"


def test_register_response_has_correct_fields(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    response = client.post("/register", json={}).json()

    assert response["action"] == "run"
    assert response["workflow_id"] == "planner"
    assert response["session_id"] is not None
    assert response["iteration"] == 1
    assert response["config_snapshot"] is not None
    assert response["config_snapshot"]["goal_hash"] == "71393ee22450"
    assert response["config_snapshot"]["team_harness_model"] == "gpt-5.5"
    assert response["stop_reason"] is None


def test_register_sets_current_task(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    response = client.post("/register", json={}).json()
    state = store.read_state()

    assert state is not None
    assert state.current_task is not None
    assert state.current_task.workflow_id == response["workflow_id"]
    assert state.current_task.session_id == response["session_id"]
    assert state.current_task.iteration == response["iteration"]


def test_finished_records_history(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json={}).json()
    client.post(
        "/finished",
        json={
            "workflow_id": reg["workflow_id"],
            "session_id": reg["session_id"],
            "success": True,
            "text": "done",
            "error": None,
        },
    )
    state = store.read_state()

    assert state is not None
    assert len(state.history) == 1
    assert state.history[0].workflow_id == reg["workflow_id"]
    assert state.history[0].session_id == reg["session_id"]
    assert state.history[0].iteration == 1
    assert state.history[0].success is True
    # current_task is set to the next dispatched task (coordinator dispatches fresh work
    # immediately as part of the /finished response).
    assert state.current_task is not None


def test_finished_returns_next_run(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    reg = client.post("/register", json={}).json()
    finished = client.post(
        "/finished",
        json={
            "workflow_id": reg["workflow_id"],
            "session_id": reg["session_id"],
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()

    assert finished["action"] == "run"
    assert finished["iteration"] == 2


def test_finished_stale_mismatch_does_not_mutate(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json={}).json()
    # Send /finished with wrong session_id — stale mismatch.
    stale = client.post(
        "/finished",
        json={
            "workflow_id": reg["workflow_id"],
            "session_id": "wrong-session-id",
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    state = store.read_state()

    # History must not be modified.
    assert state is not None
    assert len(state.history) == 0
    # current_task must remain unchanged.
    assert state.current_task is not None
    assert state.current_task.workflow_id == reg["workflow_id"]
    # Returns the current running task's info.
    assert stale["action"] == "run"
    assert stale["workflow_id"] == reg["workflow_id"]
    assert stale["session_id"] == reg["session_id"]
    assert stale["iteration"] == reg["iteration"]


def test_finished_stale_no_current_task_dispatches_fresh(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    # Ensure current_task is None (no active task).
    assert state.current_task is None

    # /finished with no active task acts like /register: dispatches fresh work.
    response = client.post(
        "/finished",
        json={
            "workflow_id": "planner",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()

    assert response["action"] == "run"
    assert response["workflow_id"] is not None
    assert response["iteration"] is not None


def test_finished_stale_no_current_task_terminal_returns_stop(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    # Put state into terminal condition with no current_task.
    state.goal_met = True
    store.write_state(state=state)

    response = client.post(
        "/finished",
        json={
            "workflow_id": "planner",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"


def test_register_recovers_abandoned_task(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    # Use two independent workflows so that after abandoning "implement",
    # "planner" (which has no prior history) is still eligible for dispatch.
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
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    # Simulate an orphaned current_task (implement crashed before calling /finished).
    orphaned = current_task_factory(
        workflow_id="implement", session_id=state.active_session_id, iteration=1
    )
    state.current_task = orphaned
    store.write_state(state=state)

    response = client.post("/register", json={}).json()
    updated = store.read_state()

    # Abandoned task should be recorded in history.
    assert updated is not None
    assert len(updated.history) == 1
    assert updated.history[0].workflow_id == "implement"
    assert updated.history[0].error == "abandoned"
    assert updated.history[0].success is False
    # Fresh task dispatched (planner has no prior history so it is eligible).
    assert response["action"] == "run"
    assert response["iteration"] == 2


def test_register_terminal_plus_abandoned_task_cleanup_first(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    """Scenario: state is terminal AND current_task is set.

    Abandoned cleanup (step 3) must always run before the stop check (step 4),
    so the orphaned task is always recorded in history even when terminal.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    # Set both terminal state and an orphaned current_task.
    state.goal_met = True
    state.status = "goal_met"
    state.stop_reason = "goal_met"
    orphaned = current_task_factory(
        workflow_id="planner", session_id=state.active_session_id, iteration=1
    )
    state.current_task = orphaned
    store.write_state(state=state)

    response = client.post("/register", json={}).json()
    updated = store.read_state()

    # Abandoned entry must be recorded even though state was terminal.
    assert updated is not None
    assert len(updated.history) == 1
    assert updated.history[0].error == "abandoned"
    # Response is stop (terminal state).
    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"


def test_register_stop_when_terminal(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.goal_met = True
    store.write_state(state=state)

    response = client.post("/register", json={}).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"


def test_finished_stop_after_max_turns(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"max_turns": 1})
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json={}).json()
    response = client.post(
        "/finished",
        json={
            "workflow_id": reg["workflow_id"],
            "session_id": reg["session_id"],
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "max_turns"
    assert updated is not None
    assert updated.current_task is None


def test_stop_precedence_goal_met(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.goal_met = True
    state.stop_requested = True
    store.write_state(state=state)

    response = client.post("/register", json={}).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"


def test_session_control_signal_sets_unresolvable_error(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    current_task = current_task_factory(
        workflow_id="planner", session_id=state.active_session_id, iteration=1
    )
    state.current_task = current_task
    store.write_state(state=state)
    control = control_path(repo_root=repo_root, session_id=state.active_session_id)
    control.write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "missing secret",
                "stop_reason": "unresolvable_error",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/finished",
        json={
            "workflow_id": "planner",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "unresolvable_error"
    assert updated is not None
    assert updated.unresolvable_error is True


def test_session_control_signal_sets_goal_met(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    control_path(repo_root=repo_root, session_id=state.active_session_id).write_text(
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

    response = client.post("/register", json={}).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"
    assert updated is not None
    assert updated.goal_met is True


def test_invalid_session_control_signal_stops(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    control_path(repo_root=repo_root, session_id=state.active_session_id).write_text(
        json.dumps(
            {"state": "stopped", "reason": "missing stop reason", "schema_version": 1}
        ),
        encoding="utf-8",
    )

    response = client.post("/register", json={}).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "invalid_control_output"
    assert updated is not None
    assert updated.status == "failed"


def test_invalid_goal_check_output_stops_at_failure_cap(
    repo_builder: Any,
    monkeypatch: Any,
    current_task_factory: Any,
    history_entry_factory: Any,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"goal_check_consecutive_failures_cap": 1})
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.history.append(history_entry_factory(workflow_id="planner", success=True))
    state.iteration_count = 1
    current_task = current_task_factory(
        workflow_id="goal_check", session_id=state.active_session_id, iteration=2
    )
    state.current_task = current_task
    store.write_state(state=state)

    # No goal_check.json written — triggers invalid_goal_check_output.
    response = client.post(
        "/finished",
        json={
            "workflow_id": "goal_check",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_check_broken"
    assert updated is not None
    assert updated.goal_check_consecutive_failures == 1


def test_goal_check_does_not_stop_without_session_control(
    repo_builder: Any,
    monkeypatch: Any,
    current_task_factory: Any,
    history_entry_factory: Any,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.history.append(history_entry_factory(workflow_id="planner", success=True))
    state.iteration_count = 1
    current_task = current_task_factory(
        workflow_id="goal_check", session_id=state.active_session_id, iteration=2
    )
    state.current_task = current_task
    store.write_state(state=state)
    goal_check_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / state.active_session_id
        / "iterations"
        / "0002_goal_check"
        / "goal_check.json"
    )
    goal_check_path.parent.mkdir(parents=True, exist_ok=True)
    goal_check_path.write_text(
        json.dumps({"goal_met": True, "reason": "done", "schema_version": 1}),
        encoding="utf-8",
    )

    response = client.post(
        "/finished",
        json={
            "workflow_id": "goal_check",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["action"] == "run"
    assert updated is not None
    assert updated.goal_met is False


def test_emits_goal_check_workflow_stops_with_session_control(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "eval_runner": {
                "prompt": "Run evals",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "emits_goal_check": True,
                    "description": "",
                },
            }
        }
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    current_task = current_task_factory(
        workflow_id="eval_runner", session_id=state.active_session_id, iteration=1
    )
    state.current_task = current_task
    store.write_state(state=state)
    goal_check_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / state.active_session_id
        / "iterations"
        / "0001_eval_runner"
        / "goal_check.json"
    )
    goal_check_path.parent.mkdir(parents=True, exist_ok=True)
    goal_check_path.write_text(
        json.dumps({"goal_met": True, "reason": "evals passed", "schema_version": 1}),
        encoding="utf-8",
    )
    control_path(repo_root=repo_root, session_id=state.active_session_id).write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "evals passed",
                "stop_reason": "goal_met",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/finished",
        json={
            "workflow_id": "eval_runner",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"
    assert updated is not None
    assert updated.goal_met is True


def test_invalid_emits_goal_check_output_stops_at_failure_cap(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"goal_check_consecutive_failures_cap": 1},
        workflows={
            "eval_runner": {
                "prompt": "Run evals",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "emits_goal_check": True,
                    "description": "",
                },
            }
        },
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    current_task = current_task_factory(
        workflow_id="eval_runner", session_id=state.active_session_id, iteration=1
    )
    state.current_task = current_task
    store.write_state(state=state)

    response = client.post(
        "/finished",
        json={
            "workflow_id": "eval_runner",
            "session_id": state.active_session_id,
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_check_broken"
    assert updated is not None
    assert updated.goal_check_consecutive_failures == 1


def test_no_eligible_workflow_stops(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    # Only goal_check workflow, which requires iteration >= 1. At iteration 0,
    # no workflow is eligible.
    repo_root = repo_builder(
        workflows={
            "goal_check": {
                "prompt": "Check",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 1,
                    "description": "",
                },
            }
        }
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    response = client.post("/register", json={}).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "no_eligible_workflow"


@pytest.mark.parametrize(
    ("workflows", "state_updates", "expected_stop_reason"),
    [
        (None, {"goal_met": True, "stop_requested": True}, "goal_met"),
        (None, {"stop_requested": True, "unresolvable_error": True}, "stop_requested"),
        (
            None,
            {"unresolvable_error": True, "iteration_count": 20},
            "unresolvable_error",
        ),
        (None, {"iteration_count": 20}, "max_turns"),
        (
            {
                "goal_check": {
                    "prompt": "Check",
                    "config": {
                        "enabled": True,
                        "run_every": 1,
                        "must_follow": None,
                        "not_before_iteration": 1,
                        "description": "",
                    },
                }
            },
            {},
            "no_eligible_workflow",
        ),
    ],
)
def test_stop_precedence_matrix(
    repo_builder: Any,
    monkeypatch: Any,
    workflows: dict[str, dict[str, Any]] | None,
    state_updates: dict[str, Any],
    expected_stop_reason: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=workflows)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    for key, value in state_updates.items():
        setattr(state, key, value)
    store.write_state(state=state)

    response = client.post("/register", json={}).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == expected_stop_reason


def test_resume_reuses_in_progress_session(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    first_app = create_coordinator_app(repo_root=repo_root, resume=False)
    first_store = StateStore(repo_root=repo_root)
    first_state = first_store.read_state()
    assert first_state is not None
    original_session_id = first_state.active_session_id

    resumed_app = create_coordinator_app(repo_root=repo_root, resume=True)
    resumed_client = TestClient(resumed_app)
    resumed_store = StateStore(repo_root=repo_root)
    resumed_state = resumed_store.read_state()
    register_response = resumed_client.post("/register", json={}).json()

    assert first_app is not None
    assert resumed_state is not None
    assert resumed_state.active_session_id == original_session_id
    assert register_response["session_id"] == original_session_id
    assert list((repo_root / ".loopy_loop").glob("state.json.archive_*.json")) == []
