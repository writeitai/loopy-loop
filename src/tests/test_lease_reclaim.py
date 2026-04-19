from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.models import utc_now
from loopy_loop.models import WorkerState
from loopy_loop.state_store import StateStore


def test_stale_lease_is_reclaimed(
    repo_builder: Any, monkeypatch: Any, assignment_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "implement": {
                "prompt": "Implement",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.workers["worker_old"] = WorkerState(
        status="busy",
        registered_at=utc_now() - timedelta(minutes=20),
        last_seen_at=utc_now() - timedelta(minutes=20),
    )
    state.active_assignment = assignment_factory(
        worker_id="worker_old",
        session_id=state.active_session_id,
        assigned_at=utc_now() - timedelta(minutes=20),
        lease_seconds=60,
    )
    store.write_state(state=state)
    worker_new = client.post("/workers/register").json()["worker_id"]

    response = client.post(f"/workers/{worker_new}/next").json()
    updated = store.read_state()

    assert response["action"] == "run"
    assert response["workflow_id"] == "implement"
    assert updated is not None
    assert updated.history[-1].error == "lease_expired"
