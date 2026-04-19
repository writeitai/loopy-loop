from __future__ import annotations

from typing import Any

from loopy_loop.models import LoopState
from loopy_loop.state_store import StateStore


def test_state_store_reads_and_mutates_state(
    repo_root: Any, state_factory: Any
) -> None:
    store = StateStore(repo_root=repo_root)
    state = state_factory()
    store.write_state(state=state)

    def mutator(current: LoopState | None) -> tuple[LoopState, bool]:
        assert current is not None
        current.stop_requested = True
        return current, current.stop_requested

    result = store.mutate(mutator)
    loaded = store.read_state()

    assert result is True
    assert loaded is not None
    assert loaded.stop_requested is True


def test_archive_state_moves_file(repo_root: Any, state_factory: Any) -> None:
    store = StateStore(repo_root=repo_root)
    store.write_state(state=state_factory(status="failed", stop_reason="failed"))

    archive_path = store.archive_state()

    assert archive_path is not None
    assert archive_path.exists()
    assert store.read_state() is None
