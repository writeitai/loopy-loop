from __future__ import annotations

from typing import Any

from loopy_loop.models import LoopState
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import state_path
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


def test_state_store_default_discovers_top_level_state(
    repo_root: Any, state_factory: Any
) -> None:
    parent = state_factory(active_session_id="20260419_143022_cdbf6975e8a3_ab12cd34")
    child = state_factory(
        active_session_id="20260419_143123_cdbf6975e8a3_cd34ef56",
        parent_session_id=parent.active_session_id,
    )
    create_session_dir(
        repo_root=repo_root,
        session_id=parent.active_session_id,
        goal_hash=parent.goal_hash,
        workflow_set=parent.workflow_set,
    )
    create_session_dir(
        repo_root=repo_root,
        session_id=child.active_session_id,
        goal_hash=child.goal_hash,
        workflow_set=child.workflow_set,
        parent_session_id=parent.active_session_id,
    )
    StateStore(repo_root=repo_root).write_state(state=parent)
    StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=child.active_session_id),
    ).write_state(state=child)

    loaded = StateStore(repo_root=repo_root).read_state()

    assert loaded is not None
    assert loaded.active_session_id == parent.active_session_id
