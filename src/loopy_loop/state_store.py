from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import re
from typing import TypeVar

from filelock import FileLock

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.models import DEFAULT_LOCK_TIMEOUT_SECONDS
from loopy_loop.models import LoopState
from loopy_loop.models import SAFE_DURABLE_ID_PATTERN
from loopy_loop.models import utc_now
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import latest_top_level_state_path
from loopy_loop.sessions import state_path as session_state_path
from loopy_loop.sessions import workflow_snapshot_dir_path

STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.json.lock"
TEMP_FILENAME = "state.json.tmp"
ARCHIVE_PREFIX = "state.json.archive_"
TERMINAL_STATUSES = {"stopped", "goal_met", "failed", "max_turns"}

T = TypeVar("T")
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class StateInvariantError(ValueError):
    """A v2 state file contradicts its containing session or frozen attempt."""


class AttemptArtifactInvariantError(StateInvariantError):
    """A live task's frozen files were removed or changed after commit."""


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
            current = self._read_state_unlocked()
            validated = LoopState.model_validate(state.model_dump())
            if validated.schema_version >= 2:
                _validate_committed_shape(validated, repo_root=self.repo_root)
                validated.state_revision = (
                    current.state_revision + 1 if current is not None else 0
                )
                validated = LoopState.model_validate(validated.model_dump())
            self._write_state_unlocked(state=validated)
            return validated

    def mutate(self, mutator: Callable[[LoopState | None], tuple[LoopState, T]]) -> T:
        with self._lock():
            current = self._read_state_unlocked()
            next_state, result = mutator(current)
            if next_state.schema_version >= 2:
                _validate_committed_shape(next_state, repo_root=self.repo_root)
                prior_revision = current.state_revision if current is not None else -1
                next_state.state_revision = prior_revision + 1
                # Revalidate after the complete mutation, never halfway
                # through a staged multi-file transition.
                next_state = LoopState.model_validate(next_state.model_dump())
            self._write_state_unlocked(state=next_state)
            return result

    def validate_committed_state(self, *, state: LoopState) -> None:
        """Validate a state read for startup/recovery without rewriting it."""
        if state.schema_version >= 2:
            _validate_committed_shape(state, repo_root=self.repo_root)

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


def _validate_committed_shape(state: LoopState, *, repo_root: Path) -> None:
    """Reject impossible v2 phase and containing-state contradictions."""
    if state.workflow_contract is None:
        raise StateInvariantError(
            "v2 state has no engine-owned workflow contract trust root"
        )
    terminal = state.status in TERMINAL_STATUSES
    if terminal and (
        state.current_task is not None or state.active_child_session_id is not None
    ):
        raise StateInvariantError(
            "terminal v2 state cannot retain a current task or active child"
        )
    if state.current_task is not None and state.active_child_session_id is not None:
        raise StateInvariantError(
            "v2 state cannot execute a task while a child is active"
        )
    task = state.current_task
    if task is None:
        return

    if task.session_id != state.active_session_id:
        raise StateInvariantError(
            "v2 current task session does not match its containing state"
        )
    if task.workflow_set != state.workflow_set:
        raise StateInvariantError(
            "v2 current task workflow set does not match its containing state"
        )
    if task.iteration != state.iteration_count + 1:
        raise StateInvariantError(
            "v2 current task iteration does not follow its containing state"
        )
    if task.worker is None:
        raise StateInvariantError("v2 current task has no worker owner")
    if task.attempt_id is None or not SAFE_DURABLE_ID_PATTERN.fullmatch(
        task.attempt_id
    ):
        raise StateInvariantError("v2 current task has no safe attempt identity")
    if not task.repository_id or not task.repository_id.strip():
        raise StateInvariantError("v2 current task has no repository identity")
    if task.completion_contract_version != 2:
        raise StateInvariantError("v2 current task must use completion contract v2")
    if task.assignment_sha256 is None or not _SHA256_PATTERN.fullmatch(
        task.assignment_sha256
    ):
        raise StateInvariantError("v2 current task has no frozen assignment hash")

    descriptor = task.workflow_snapshot
    if descriptor is None:
        raise StateInvariantError("v2 current task has no frozen workflow snapshot")
    if descriptor.schema_version != 1:
        raise StateInvariantError("v2 current task has an unknown snapshot schema")
    expected_identity = {
        "session_id": task.session_id,
        "workflow_set": task.workflow_set,
        "workflow_id": task.workflow_id,
        "iteration": task.iteration,
        "attempt_id": task.attempt_id,
    }
    observed_identity = {
        "session_id": descriptor.session_id,
        "workflow_set": descriptor.workflow_set,
        "workflow_id": descriptor.workflow_id,
        "iteration": descriptor.iteration,
        "attempt_id": descriptor.attempt_id,
    }
    if observed_identity != expected_identity:
        raise StateInvariantError(
            "v2 workflow snapshot identity contradicts its current task"
        )

    root = repo_root.resolve()
    expected_snapshot_root = workflow_snapshot_dir_path(
        repo_root=root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
        attempt_id=task.attempt_id,
    ).resolve()
    try:
        observed_snapshot_root = Path(descriptor.snapshot_root).resolve(strict=True)
    except OSError as exc:
        raise AttemptArtifactInvariantError(
            f"v2 workflow snapshot root is unavailable: {exc}"
        ) from exc
    if not Path(descriptor.snapshot_root).is_absolute() or (
        observed_snapshot_root != expected_snapshot_root
    ):
        raise StateInvariantError(
            "v2 workflow snapshot root is not canonical for its current task"
        )

    members = {
        "workflow_config_path": ("config.yaml", descriptor.workflow_config_sha256),
        "workflow_prompt_path": ("prompt.txt", descriptor.workflow_prompt_sha256),
        "workflow_contract_path": (
            "workflow_contract.yaml",
            descriptor.workflow_contract_sha256,
        ),
        "root_config_snapshot_path": (
            "root_config_snapshot.json",
            descriptor.root_config_snapshot_sha256,
        ),
    }
    for field, (filename, expected_hash) in members.items():
        raw_path = getattr(descriptor, field)
        expected_path = expected_snapshot_root / filename
        path = Path(raw_path)
        if not path.is_absolute() or path.resolve() != expected_path:
            raise StateInvariantError(
                f"v2 workflow snapshot member {field!r} is not canonical"
            )
        if not _SHA256_PATTERN.fullmatch(expected_hash):
            raise StateInvariantError(
                f"v2 workflow snapshot member {field!r} has an invalid hash"
            )
        if not path.is_file() or file_sha256(path) != expected_hash:
            raise AttemptArtifactInvariantError(
                f"v2 workflow snapshot member {field!r} changed or is missing"
            )

    expected_assignment = assignment_path(
        repo_root=root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
        attempt_id=task.attempt_id,
    ).resolve()
    if (
        not expected_assignment.is_file()
        or file_sha256(expected_assignment) != task.assignment_sha256
    ):
        raise AttemptArtifactInvariantError(
            "v2 current task assignment changed or is missing"
        )

    repository_path = root / ".loopy_loop" / "repository.json"
    try:
        repository = json.loads(repository_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateInvariantError(
            f"v2 repository identity is unavailable: {exc}"
        ) from exc
    if (
        not isinstance(repository, dict)
        or repository.get("repository_id") != task.repository_id
    ):
        raise StateInvariantError(
            "v2 current task repository identity contradicts this checkout"
        )
