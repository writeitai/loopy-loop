from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.state_store import StateStore


def test_duplicate_finished_is_idempotent(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    worker_id = client.post("/workers/register").json()["worker_id"]
    first_run = client.post(f"/workers/{worker_id}/next").json()
    finished_body = {
        "assignment_id": first_run["assignment_id"],
        "session_id": first_run["session_id"],
        "workflow_id": first_run["workflow_id"],
        "success": True,
        "text": "done",
        "error": None,
    }

    first_finished = client.post(
        f"/workers/{worker_id}/finished", json=finished_body
    ).json()
    second_finished = client.post(
        f"/workers/{worker_id}/finished", json=finished_body
    ).json()
    state = store.read_state()

    assert first_finished["action"] == "run"
    assert first_finished == second_finished
    assert state is not None
    assert len(state.history) == 1
    assert state.history[0].assignment_id == first_run["assignment_id"]


def test_stale_finished_returns_current_next_action(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    worker_id = client.post("/workers/register").json()["worker_id"]
    first_run = client.post(f"/workers/{worker_id}/next").json()

    first_finished = client.post(
        f"/workers/{worker_id}/finished",
        json={
            "assignment_id": first_run["assignment_id"],
            "session_id": first_run["session_id"],
            "workflow_id": first_run["workflow_id"],
            "success": True,
            "text": "done",
            "error": None,
        },
    ).json()
    stale_finished = client.post(
        f"/workers/{worker_id}/finished",
        json={
            "assignment_id": first_run["assignment_id"],
            "session_id": first_run["session_id"],
            "workflow_id": first_run["workflow_id"],
            "success": True,
            "text": "done again",
            "error": None,
        },
    ).json()
    state = store.read_state()

    assert first_finished["action"] == "run"
    assert stale_finished["action"] == "run"
    assert state is not None
    assert state.active_assignment is not None
    assert stale_finished["assignment_id"] == state.active_assignment.assignment_id
    assert stale_finished["assignment_id"] == first_finished["assignment_id"]
    assert stale_finished["assignment_id"] != first_run["assignment_id"]
    assert len(state.history) == 1
    assert state.history[0].assignment_id == first_run["assignment_id"]
