from __future__ import annotations

import json
import shutil
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


def test_default_discovery_ignores_stray_state_without_session_manifest(
    repo_root: Any, state_factory: Any
) -> None:
    """A later-sorted non-session state cannot wedge root reads or mutations."""

    state = state_factory()
    session_dir = create_session_dir(
        repo_root=repo_root,
        session_id=state.active_session_id,
        goal_hash=state.goal_hash,
        workflow_set=state.workflow_set,
    )
    StateStore(repo_root=repo_root, state_path=session_dir / "state.json").write_state(
        state=state
    )
    stray_dir = session_dir.parent / "zzzz-stray"
    stray_dir.mkdir()
    stray_dir.joinpath("state.json").write_text("{", encoding="utf-8")

    store = StateStore(repo_root=repo_root)
    loaded = store.read_state()

    assert loaded is not None
    assert loaded.active_session_id == state.active_session_id

    def request_stop(current: LoopState | None) -> tuple[LoopState, None]:
        """Apply the same shared root mutation used by the stop command."""

        assert current is not None
        current.stop_requested = True
        return current, None

    store.mutate(mutator=request_stop)
    persisted = StateStore(
        repo_root=repo_root, state_path=session_dir / "state.json"
    ).read_state()
    assert persisted is not None
    assert persisted.stop_requested


def test_default_discovery_ignores_copied_session_backup(
    repo_root: Any, state_factory: Any
) -> None:
    """A copied root whose manifest ID mismatches its directory is not a run."""

    state = state_factory()
    session_dir = create_session_dir(
        repo_root=repo_root,
        session_id=state.active_session_id,
        goal_hash=state.goal_hash,
        workflow_set=state.workflow_set,
    )
    canonical_store = StateStore(
        repo_root=repo_root, state_path=session_dir / "state.json"
    )
    canonical_store.write_state(state=state)
    backup_dir = session_dir.parent / "zzzz-session-backup"
    shutil.copytree(src=session_dir, dst=backup_dir)
    backup_state_path = backup_dir / "state.json"
    backup_state = json.loads(backup_state_path.read_text(encoding="utf-8"))
    backup_state["iteration_count"] = 999
    backup_state_path.write_text(json.dumps(backup_state), encoding="utf-8")

    discovered_store = StateStore(repo_root=repo_root)
    loaded = discovered_store.read_state()

    assert loaded is not None
    assert loaded.iteration_count == 0
    assert discovered_store.state_path == session_dir / "state.json"
