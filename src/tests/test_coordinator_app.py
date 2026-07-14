from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.sessions import result_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.state_store import StateStore

REGISTER_BODY = {"worker": {"hostname": "test-host", "pid": 999983, "starttime": None}}


def test_register_returns_run_response(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    response = client.post("/register", json=REGISTER_BODY).json()

    assert response["action"] == "run"


def test_register_response_has_correct_fields(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    response = client.post("/register", json=REGISTER_BODY).json()

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

    response = client.post("/register", json=REGISTER_BODY).json()
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

    reg = client.post("/register", json=REGISTER_BODY).json()
    client.post(
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

    reg = client.post("/register", json=REGISTER_BODY).json()
    finished = client.post(
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

    assert finished["action"] == "run"
    assert finished["iteration"] == 2


def test_child_session_runs_inside_parent_and_resumes_parent(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
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
        "\n".join(
            [
                "enabled: true",
                "run_every: 1",
                "must_follow: null",
                "not_before_iteration: 0",
                "description: Child work",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    parent_task = client.post("/register", json=REGISTER_BODY).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
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
            "attempt_id": parent_task.get("attempt_id"),
            "success": True,
            "text": "parent planned child",
            "error": None,
        },
    ).json()

    assert child_task["action"] == "run"
    assert child_task["workflow_set"] == "child_set"
    assert child_task["workflow_id"] == "child_work"
    assert child_task["session_id"] != parent_task["session_id"]
    child_dir = session_dir_path(
        repo_root=repo_root, session_id=child_task["session_id"]
    )
    assert child_dir.parent.name == "children"
    assert child_dir.joinpath("goal.md").read_text(encoding="utf-8") == (
        "Handle a focused child task.\n"
    )
    parent_metadata = json.loads(child_dir.joinpath("parent.json").read_text())
    assert parent_metadata["parent_session_id"] == parent_task["session_id"]

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
    resumed_parent = client.post(
        "/finished",
        json={
            "workflow_id": child_task["workflow_id"],
            "session_id": child_task["session_id"],
            "iteration": child_task["iteration"],
            "attempt_id": child_task.get("attempt_id"),
            "success": True,
            "text": "child done",
            "error": None,
        },
    ).json()

    assert resumed_parent["action"] == "run"
    assert resumed_parent["workflow_set"] == "main"
    assert resumed_parent["session_id"] == parent_task["session_id"]
    state = store.read_state()
    assert state is not None
    assert state.current_task is not None
    assert state.current_task.session_id == parent_task["session_id"]
    records = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert records["schema_version"] == 1
    assert records["children"][0]["session_id"] == child_task["session_id"]
    assert records["children"][0]["status"] == "goal_met"


def test_failed_parent_iteration_does_not_dispatch_child_request(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
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
        "enabled: true\nrun_every: 1\n", encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    parent_task = client.post("/register", json=REGISTER_BODY).json()
    request_path = (
        child_requests_dir_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        )
        / "child.json"
    )
    request_path.write_text(
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Handle a focused child task.",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    next_task = client.post(
        "/finished",
        json={
            "workflow_id": parent_task["workflow_id"],
            "session_id": parent_task["session_id"],
            "iteration": parent_task["iteration"],
            "attempt_id": parent_task.get("attempt_id"),
            "success": False,
            "text": None,
            "error": "failed before child dispatch",
        },
    ).json()

    assert next_task["action"] == "run"
    assert next_task["workflow_set"] == "main"
    assert next_task["session_id"] == parent_task["session_id"]
    assert request_path.exists()


def test_finished_stale_mismatch_does_not_mutate(
    repo_builder: Any, monkeypatch: Any
) -> None:
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
            "session_id": "wrong-session-id",
            "iteration": reg["iteration"],
            "attempt_id": reg.get("attempt_id"),
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
            "iteration": 1,
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
            "iteration": 1,
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

    response = client.post("/register", json=REGISTER_BODY).json()
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


def test_register_recovers_completed_task_from_pending_finished_request(
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
    state.stop_requested = True
    store.write_state(state=state)
    pending_path = pending_finished_request_path(
        repo_root=repo_root,
        session_id=current_task.session_id,
        iteration=current_task.iteration,
        workflow_id=current_task.workflow_id,
    )
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(
            {
                "workflow_id": current_task.workflow_id,
                "session_id": current_task.session_id,
                "iteration": current_task.iteration,
                "success": True,
                "text": "done",
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    response = client.post("/register", json=REGISTER_BODY).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "stop_requested"
    assert updated is not None
    assert updated.status == "stopped"
    assert updated.current_task is None
    assert updated.history[0].iteration == current_task.iteration
    assert updated.history[0].workflow_id == current_task.workflow_id
    assert updated.history[0].success is True
    assert updated.history[0].error is None
    assert not pending_path.exists()


def test_register_recovers_completed_task_from_result_json(
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
    state.stop_requested = True
    store.write_state(state=state)
    result_json_path = result_path(
        repo_root=repo_root,
        session_id=current_task.session_id,
        iteration=current_task.iteration,
        workflow_id=current_task.workflow_id,
    )
    result_json_path.parent.mkdir(parents=True, exist_ok=True)
    result_json_path.write_text(
        json.dumps(
            {
                "success": True,
                "text": "done",
                "error": None,
                "error_detail": None,
                "harness_run_id": "run-1",
                "harness_output_dir": "",
            }
        ),
        encoding="utf-8",
    )

    response = client.post("/register", json=REGISTER_BODY).json()
    updated = store.read_state()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "stop_requested"
    assert updated is not None
    assert updated.status == "stopped"
    assert updated.current_task is None
    assert updated.history[0].iteration == current_task.iteration
    assert updated.history[0].success is True
    assert updated.history[0].error is None


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

    response = client.post("/register", json=REGISTER_BODY).json()
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

    response = client.post("/register", json=REGISTER_BODY).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"


def test_finished_stop_after_max_turns(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"max_turns": 1})
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)

    reg = client.post("/register", json=REGISTER_BODY).json()
    response = client.post(
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

    response = client.post("/register", json=REGISTER_BODY).json()

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
            "iteration": current_task.iteration,
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

    response = client.post("/register", json=REGISTER_BODY).json()
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

    response = client.post("/register", json=REGISTER_BODY).json()
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
            "iteration": current_task.iteration,
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
            "iteration": current_task.iteration,
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
            "iteration": current_task.iteration,
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
            "iteration": current_task.iteration,
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

    response = client.post("/register", json=REGISTER_BODY).json()

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

    response = client.post("/register", json=REGISTER_BODY).json()

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
    register_response = resumed_client.post("/register", json=REGISTER_BODY).json()

    assert first_app is not None
    assert resumed_state is not None
    assert resumed_state.active_session_id == original_session_id
    assert register_response["session_id"] == original_session_id
    assert list((repo_root / ".loopy_loop").glob("state.json.archive_*.json")) == []


def test_child_snapshot_inherits_parent_config_despite_yaml_edit(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """A child session must run under the PARENT's frozen execution config
    (P0.3/D9): a mid-session edit of loopy_loop_config.yaml must not leak a
    different model into the child's snapshot."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
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
    child_workflow_dir.joinpath("prompt.txt").write_text("Child.", encoding="utf-8")
    child_workflow_dir.joinpath("config.yaml").write_text(
        "enabled: true\n", encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    parent_task = client.post("/register", json=REGISTER_BODY).json()
    assert parent_task["config_snapshot"]["team_harness_model"] == "gpt-5.5"

    config_path = repo_root / "loopy_loop_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "team_harness_model: gpt-5.5", "team_harness_model: mutated-model"
        ),
        encoding="utf-8",
    )
    child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    ).joinpath("child.json").write_text(
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Focused child goal.",
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
            "attempt_id": parent_task.get("attempt_id"),
            "success": True,
            "text": "parent planned child",
            "error": None,
        },
    ).json()

    assert child_task["action"] == "run"
    snapshot = child_task["config_snapshot"]
    assert snapshot["team_harness_model"] == "gpt-5.5"
    assert snapshot["goal"] == "Focused child goal."
    assert snapshot["workflow_set"] == "child_set"
    assert snapshot["goal_hash"] != parent_task["config_snapshot"]["goal_hash"]
