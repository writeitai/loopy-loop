from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.models import utc_now
from loopy_loop.models import WorkerState
from loopy_loop.state_store import StateStore


def test_register_and_next_wait_behavior(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    worker_one = client.post("/workers/register").json()["worker_id"]
    worker_two = client.post("/workers/register").json()["worker_id"]
    first = client.post(f"/workers/{worker_one}/next").json()
    second = client.post(f"/workers/{worker_two}/next").json()

    assert first["action"] == "run"
    assert second == {
        "action": "wait",
        "stop_reason": None,
        "assignment_id": None,
        "workflow_id": None,
        "session_id": None,
        "iteration": None,
        "config_snapshot": None,
    }


def test_next_run_response_contains_full_assignment_contract(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    worker_id = client.post("/workers/register").json()["worker_id"]

    response = client.post(f"/workers/{worker_id}/next").json()
    state = store.read_state()

    assert response["action"] == "run"
    assert response["assignment_id"]
    assert response["session_id"]
    assert response["iteration"] == 1
    assert response["workflow_id"] == "planner"
    assert response["config_snapshot"] is not None
    assert response["config_snapshot"]["goal_slug"] == "ship-landing-page"
    assert response["config_snapshot"]["model"] == "gpt-5.4"
    assert state is not None
    assert state.active_assignment is not None
    assert response["assignment_id"] == state.active_assignment.assignment_id
    assert response["session_id"] == state.active_assignment.session_id
    assert response["workflow_id"] == state.active_assignment.workflow_id


def test_control_signal_sets_unresolvable_error(
    repo_builder: Any, monkeypatch: Any, assignment_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.workers["worker_1"] = WorkerState(
        status="busy", registered_at=utc_now(), last_seen_at=utc_now()
    )
    state.active_assignment = assignment_factory(
        worker_id="worker_1",
        workflow_id="planner",
        session_id=state.active_session_id,
        iteration=1,
    )
    store.write_state(state=state)
    control_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / state.active_session_id
        / "iterations"
        / "0001_planner"
        / "control.json"
    )
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps(
            {
                "unresolvable_error": True,
                "reason": "missing secret",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/workers/worker_1/finished",
        json={
            "assignment_id": state.active_assignment.assignment_id,
            "session_id": state.active_assignment.session_id,
            "workflow_id": "planner",
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


def test_control_json_requires_schema_version(
    repo_builder: Any, monkeypatch: Any, assignment_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.workers["worker_1"] = WorkerState(
        status="busy", registered_at=utc_now(), last_seen_at=utc_now()
    )
    state.active_assignment = assignment_factory(
        worker_id="worker_1",
        workflow_id="planner",
        session_id=state.active_session_id,
        iteration=1,
    )
    store.write_state(state=state)
    control_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / state.active_session_id
        / "iterations"
        / "0001_planner"
        / "control.json"
    )
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps(
            {
                "unresolvable_error": True,
                "reason": "missing schema version",
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/workers/worker_1/finished",
        json={
            "assignment_id": state.active_assignment.assignment_id,
            "session_id": state.active_assignment.session_id,
            "workflow_id": "planner",
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    updated = store.read_state()

    assert response["stop_reason"] != "unresolvable_error"
    assert updated is not None
    assert updated.unresolvable_error is False


def test_invalid_goal_check_output_stops_at_failure_cap(
    repo_builder: Any,
    monkeypatch: Any,
    assignment_factory: Any,
    history_entry_factory: Any,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"goal_check_consecutive_failures_cap": 1})
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.history.append(history_entry_factory(workflow_id="planner", success=True))
    state.iteration_count = 1
    state.workers["worker_1"] = WorkerState(
        status="busy", registered_at=utc_now(), last_seen_at=utc_now()
    )
    state.active_assignment = assignment_factory(
        worker_id="worker_1",
        workflow_id="goal_check",
        session_id=state.active_session_id,
        iteration=2,
    )
    store.write_state(state=state)

    response = client.post(
        "/workers/worker_1/finished",
        json={
            "assignment_id": state.active_assignment.assignment_id,
            "session_id": state.active_assignment.session_id,
            "workflow_id": "goal_check",
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


def test_goal_check_success_reads_only_current_iteration_artifact(
    repo_builder: Any,
    monkeypatch: Any,
    assignment_factory: Any,
    history_entry_factory: Any,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.history.append(history_entry_factory(workflow_id="planner", success=True))
    state.iteration_count = 1
    state.workers["worker_1"] = WorkerState(
        status="busy", registered_at=utc_now(), last_seen_at=utc_now()
    )
    state.active_assignment = assignment_factory(
        worker_id="worker_1",
        workflow_id="goal_check",
        session_id=state.active_session_id,
        iteration=2,
    )
    store.write_state(state=state)
    wrong_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / state.active_session_id
        / "goal_check.json"
    )
    wrong_path.write_text(
        json.dumps(
            {"goal_met": False, "reason": "stale", "schema_version": 1}, indent=2
        ),
        encoding="utf-8",
    )
    current_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / state.active_session_id
        / "iterations"
        / "0002_goal_check"
        / "goal_check.json"
    )
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text(
        json.dumps(
            {"goal_met": True, "reason": "current artifact", "schema_version": 1},
            indent=2,
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/workers/worker_1/finished",
        json={
            "assignment_id": state.active_assignment.assignment_id,
            "session_id": state.active_assignment.session_id,
            "workflow_id": "goal_check",
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


@pytest.mark.parametrize(
    ("workflows", "state_updates", "expected_stop_reason"),
    [
        (None, {"goal_met": True, "stop_requested": True}, "goal_met"),
        (None, {"stop_requested": True, "unresolvable_error": True}, "stop_requested"),
        (None, {"unresolvable_error": True, "iteration_count": 20}, "unresolvable_error"),
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
    worker_id = client.post("/workers/register").json()["worker_id"]
    state = store.read_state()
    assert state is not None
    for key, value in state_updates.items():
        setattr(state, key, value)
    store.write_state(state=state)

    response = client.post(f"/workers/{worker_id}/next").json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == expected_stop_reason


def test_stop_ordering_prefers_goal_met_over_stop_requested(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    worker_id = client.post("/workers/register").json()["worker_id"]
    state = store.read_state()
    assert state is not None
    state.goal_met = True
    state.stop_requested = True
    store.write_state(state=state)

    response = client.post(f"/workers/{worker_id}/next").json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"


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
    worker_id = resumed_client.post("/workers/register").json()["worker_id"]
    next_response = resumed_client.post(f"/workers/{worker_id}/next").json()

    assert first_app is not None
    assert resumed_state is not None
    assert resumed_state.active_session_id == original_session_id
    assert next_response["session_id"] == original_session_id
    assert list((repo_root / ".loopy_loop").glob("state.json.archive_*.json")) == []
