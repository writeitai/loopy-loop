from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import uuid

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.models import utc_now

SESSIONS_DIRNAME = "sessions"
ITERATIONS_DIRNAME = "iterations"
CHILDREN_DIRNAME = "children"
CHILD_REQUESTS_DIRNAME = "child_requests"
SESSION_METADATA_FILENAME = "session.json"
STATE_FILENAME = "state.json"
CHILDREN_FILENAME = "children.json"
PARENT_FILENAME = "parent.json"
GOAL_FILENAME = "goal.md"
EVENTS_FILENAME = "events.jsonl"
PROJECT_STATE_DIRNAME = "project_state"
EVAL_CHECKS_DIRNAME = "eval_checks"
HARNESS_OUTPUTS_DIRNAME = "harness_outputs"
UPDATES_FROM_USER_FILENAME = "updates_from_user.md"
FINISHED_FILENAME = "finished.md"
PROMPT_FILENAME = "prompt.txt"
RESULT_FILENAME = "result.json"
RESULT_TEXT_FILENAME = "result_text.txt"
HARNESS_RUN_ID_FILENAME = "harness_run_id.txt"
PENDING_FINISHED_REQUEST_FILENAME = "pending_finished_request.json"
CONTROL_FILENAME = "control.json"
GOAL_CHECK_FILENAME = "goal_check.json"


def write_text_atomic(*, path: Path, content: str) -> None:
    """Crash-safe file write: unique temp in the same directory + rename.

    Recovery decisions are made from these artifacts, so a crash mid-write
    must never leave a truncated file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_json_atomic(*, path: Path, payload: object) -> None:
    write_text_atomic(path=path, content=json.dumps(payload, indent=2))


def create_session_id(*, goal_hash: str) -> str:
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    unique = uuid.uuid4().hex[:8]
    return f"{stamp}_{goal_hash}_{unique}"


def create_session_dir(
    *,
    repo_root: Path,
    session_id: str,
    goal_hash: str,
    goal: str = "",
    workflow_set: str,
    parent_session_id: str | None = None,
) -> Path:
    created_at = utc_now().isoformat().replace("+00:00", "Z")
    if parent_session_id is None:
        session_dir = sessions_root_path(repo_root=repo_root) / session_id
    else:
        session_dir = (
            session_dir_path(repo_root=repo_root, session_id=parent_session_id)
            / CHILDREN_DIRNAME
            / session_id
        )
    session_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if not metadata_path.exists():
        payload = {
            "session_id": session_id,
            "goal_hash": goal_hash,
            "workflow_set": workflow_set,
            "parent_session_id": parent_session_id,
            "created_at": created_at,
        }
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if parent_session_id is not None:
        parent_path = session_dir / PARENT_FILENAME
        if not parent_path.exists():
            payload = {
                "schema_version": 1,
                "parent_session_id": parent_session_id,
                "parent_relative_path": "../..",
                "created_at": created_at,
            }
            parent_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    goal_path = session_dir / GOAL_FILENAME
    if goal and not goal_path.exists():
        goal_path.write_text(goal.rstrip() + "\n", encoding="utf-8")
    children = session_dir / CHILDREN_FILENAME
    if not children.exists():
        payload = {"schema_version": 1, "children": []}
        children.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    child_requests_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    events_path = session_dir / EVENTS_FILENAME
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8")
    control_path(repo_root=repo_root, session_id=session_id)
    updates_path = updates_from_user_path(repo_root=repo_root, session_id=session_id)
    if not updates_path.exists():
        updates_path.write_text("", encoding="utf-8")
    project_state_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    finished = finished_path(repo_root=repo_root, session_id=session_id)
    if not finished.exists():
        finished.write_text("# Finished Work\n", encoding="utf-8")
    eval_checks_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    harness_outputs_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    iterations_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    return session_dir


def sessions_root_path(*, repo_root: Path) -> Path:
    return repo_root / LOOPY_DIRNAME / SESSIONS_DIRNAME


def session_dir_path(*, repo_root: Path, session_id: str) -> Path:
    root = sessions_root_path(repo_root=repo_root)
    direct = root / session_id
    if direct.exists():
        return direct
    if root.exists():
        for candidate in sorted(root.rglob(session_id)):
            if candidate.is_dir() and candidate.name == session_id:
                return candidate
    return direct


def state_path(*, repo_root: Path, session_id: str) -> Path:
    return session_dir_path(repo_root=repo_root, session_id=session_id) / STATE_FILENAME


def children_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id) / CHILDREN_FILENAME
    )


def parent_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id) / PARENT_FILENAME
    )


def child_requests_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_REQUESTS_DIRNAME
    )


def child_sessions_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id) / CHILDREN_DIRNAME
    )


def session_goal_path(*, repo_root: Path, session_id: str) -> Path:
    return session_dir_path(repo_root=repo_root, session_id=session_id) / GOAL_FILENAME


def latest_top_level_state_path(*, repo_root: Path) -> Path | None:
    root = sessions_root_path(repo_root=repo_root)
    if not root.exists():
        return None
    candidates = [
        path / STATE_FILENAME
        for path in root.iterdir()
        if path.is_dir() and (path / STATE_FILENAME).exists()
    ]
    return sorted(candidates)[-1] if candidates else None


def latest_state_path(*, repo_root: Path) -> Path | None:
    root = sessions_root_path(repo_root=repo_root)
    if not root.exists():
        return None
    candidates = sorted(root.rglob(STATE_FILENAME))
    return candidates[-1] if candidates else None


def iterations_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / ITERATIONS_DIRNAME
    )


def project_state_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / PROJECT_STATE_DIRNAME
    )


def eval_checks_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_CHECKS_DIRNAME
    )


def harness_outputs_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / HARNESS_OUTPUTS_DIRNAME
    )


def updates_from_user_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / UPDATES_FROM_USER_FILENAME
    )


def finished_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / FINISHED_FILENAME
    )


def iteration_dir_name(*, iteration: int, workflow_id: str) -> str:
    return f"{iteration:04d}_{workflow_id}"


def iteration_dir_path(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return iterations_dir_path(repo_root=repo_root, session_id=session_id) / (
        iteration_dir_name(iteration=iteration, workflow_id=workflow_id)
    )


def iteration_harness_output_root(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return harness_outputs_dir_path(
        repo_root=repo_root, session_id=session_id
    ) / iteration_dir_name(iteration=iteration, workflow_id=workflow_id)


def ensure_iteration_dir(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    iteration_dir = iteration_dir_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=iteration,
        workflow_id=workflow_id,
    )
    iteration_dir.mkdir(parents=True, exist_ok=True)
    return iteration_dir


def pending_finished_request_path(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return (
        iteration_dir_path(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
        )
        / PENDING_FINISHED_REQUEST_FILENAME
    )


def result_path(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return (
        iteration_dir_path(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
        )
        / RESULT_FILENAME
    )


def control_path(*, repo_root: Path, session_id: str) -> Path:
    path = (
        session_dir_path(repo_root=repo_root, session_id=session_id) / CONTROL_FILENAME
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": "running",
            "reason": "session active",
            "stop_reason": None,
            "schema_version": 1,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def goal_check_path(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str = "goal_check"
) -> Path:
    return (
        ensure_iteration_dir(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
        )
        / GOAL_CHECK_FILENAME
    )
