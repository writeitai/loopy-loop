from __future__ import annotations

from typing import Any

from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.state_store import StateStore


def test_terminal_state_is_archived_on_fresh_start(
    repo_builder: Any, monkeypatch: Any, state_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    store = StateStore(repo_root=repo_root)
    store.write_state(state=state_factory(status="failed", stop_reason="failed"))

    create_coordinator_app(repo_root=repo_root, resume=False)
    archives = sorted((repo_root / ".loopy_loop").glob("state.json.archive_*.json"))
    state = store.read_state()

    assert len(archives) == 1
    assert state is not None
    assert state.status == "running"
    assert state.active_session_id != "cdbf6975e8a3_20260419_143022_ab12cd34"
