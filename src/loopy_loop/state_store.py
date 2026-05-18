from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
from typing import TypeVar

from filelock import FileLock

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.models import DEFAULT_LOCK_TIMEOUT_SECONDS
from loopy_loop.models import LoopState
from loopy_loop.models import utc_now
from loopy_loop.sessions import latest_top_level_state_path
from loopy_loop.sessions import state_path as session_state_path

STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.json.lock"
TEMP_FILENAME = "state.json.tmp"
ARCHIVE_PREFIX = "state.json.archive_"
TERMINAL_STATUSES = {"stopped", "goal_met", "failed", "max_turns"}

T = TypeVar("T")


class StateStore:
    def __init__(
        self,
        *,
        repo_root: Path,
        state_path: Path | None = None,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.repo_root = repo_root
        self.loopy_dir = repo_root / LOOPY_DIRNAME
        self.state_path = state_path
        self.lock_timeout_seconds = lock_timeout_seconds

    def read_state(self) -> LoopState | None:
        with self._lock():
            return self._read_state_unlocked()

    def write_state(self, *, state: LoopState) -> LoopState:
        with self._lock():
            self._write_state_unlocked(state=state)
            return state

    def mutate(self, mutator: Callable[[LoopState | None], tuple[LoopState, T]]) -> T:
        with self._lock():
            current = self._read_state_unlocked()
            next_state, result = mutator(current)
            self._write_state_unlocked(state=next_state)
            return result

    def archive_state(self) -> Path | None:
        with self._lock():
            current = self._read_state_unlocked()
            if current is None or self.state_path is None:
                return None
            archive_path = self._archive_path()
            os.replace(self.state_path, archive_path)
            return archive_path

    def clear_state(self) -> None:
        with self._lock():
            if self.state_path is not None and self.state_path.exists():
                self.state_path.unlink()

    def is_terminal_state(self, *, state: LoopState) -> bool:
        return state.status in TERMINAL_STATUSES

    def _lock(self) -> FileLock:
        self.loopy_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._effective_state_path()
        if lock_path is None:
            lock_path = self.loopy_dir / LOCK_FILENAME
        else:
            lock_path = lock_path.with_name(LOCK_FILENAME)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(lock_path), timeout=self.lock_timeout_seconds)

    def _read_state_unlocked(self) -> LoopState | None:
        state_path = self._effective_state_path()
        if state_path is None or not state_path.exists():
            return None
        self.state_path = state_path
        raw = state_path.read_text(encoding="utf-8")
        return LoopState.model_validate_json(raw)

    def _write_state_unlocked(self, *, state: LoopState) -> None:
        if self.state_path is None:
            self.state_path = session_state_path(
                repo_root=self.repo_root, session_id=state.active_session_id
            )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_name(TEMP_FILENAME)
        payload = state.model_dump_json(indent=2)
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self.state_path)

    def _archive_path(self) -> Path:
        if self.state_path is None:
            raise RuntimeError("Cannot archive without a state path")
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        return self.state_path.with_name(f"{ARCHIVE_PREFIX}{stamp}.json")

    def _effective_state_path(self) -> Path | None:
        if self.state_path is not None:
            return self.state_path
        return latest_top_level_state_path(repo_root=self.repo_root)
