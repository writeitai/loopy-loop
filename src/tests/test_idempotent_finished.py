from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from loopy_loop.coordinator_app import create_coordinator_app


def test_duplicate_finished_is_idempotent(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
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

    assert first_finished["action"] == "run"
    assert first_finished == second_finished
