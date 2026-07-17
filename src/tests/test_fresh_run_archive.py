from __future__ import annotations

from typing import Any

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.sessions import write_json_atomic
from loopy_loop.state_store import StateStore


def test_terminal_state_is_archived_on_fresh_start(
    repo_builder: Any, monkeypatch: Any, state_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    store = StateStore(repo_root=repo_root)
    terminal_state = state_factory(status="failed", stop_reason="failed")
    store.write_state(state=terminal_state)
    assert store.state_path is not None
    write_json_atomic(
        path=store.state_path.parent / "session.json",
        payload={"session_id": terminal_state.active_session_id},
    )

    create_coordinator_app(repo_root=repo_root, resume=False)
    archives = sorted(
        (repo_root / ".loopy_loop" / "sessions").rglob("state.json.archive_*.json")
    )
    state = StateStore(repo_root=repo_root).read_state()

    assert len(archives) == 1
    assert state is not None
    assert state.status == "running"
    assert state.active_session_id != "20260419_143022_cdbf6975e8a3_ab12cd34"
