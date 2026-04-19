from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import os
from typing import TypeVar

from filelock import FileLock

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.models import DEFAULT_LOCK_TIMEOUT_SECONDS
from loopy_loop.models import LoopState
from loopy_loop.models import utc_now

STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.json.lock"
TEMP_FILENAME = "state.json.tmp"
ARCHIVE_PREFIX = "state.json.archive_"
TERMINAL_STATUSES = {"stopped", "goal_met", "failed", "max_turns"}

T = TypeVar("T")


class StateStore:
    def __init__(
        self, *, repo_root: Path, lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    ) -> None:
        self.repo_root = repo_root
        self.loopy_dir = repo_root / LOOPY_DIRNAME
        self.state_path = self.loopy_dir / STATE_FILENAME
        self.lock_path = self.loopy_dir / LOCK_FILENAME
        self.temp_path = self.loopy_dir / TEMP_FILENAME
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
            if current is None:
                return None
            archive_path = self._archive_path()
            os.replace(self.state_path, archive_path)
            return archive_path

    def clear_state(self) -> None:
        with self._lock():
            if self.state_path.exists():
                self.state_path.unlink()

    def is_terminal_state(self, *, state: LoopState) -> bool:
        return state.status in TERMINAL_STATUSES

    def _lock(self) -> FileLock:
        self.loopy_dir.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.lock_path), timeout=self.lock_timeout_seconds)

    def _read_state_unlocked(self) -> LoopState | None:
        if not self.state_path.exists():
            return None
        raw = self.state_path.read_text(encoding="utf-8")
        return LoopState.model_validate_json(raw)

    def _write_state_unlocked(self, *, state: LoopState) -> None:
        self.loopy_dir.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2)
        self.temp_path.write_text(payload, encoding="utf-8")
        os.replace(self.temp_path, self.state_path)

    def _archive_path(self) -> Path:
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        return self.loopy_dir / f"{ARCHIVE_PREFIX}{stamp}.json"
