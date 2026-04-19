from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

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
