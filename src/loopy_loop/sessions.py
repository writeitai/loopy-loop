from __future__ import annotations

import json
from pathlib import Path
import uuid

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.models import utc_now

SESSIONS_DIRNAME = "sessions"
ITERATIONS_DIRNAME = "iterations"
SESSION_METADATA_FILENAME = "session.json"
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
CONTROL_FILENAME = "control.json"
GOAL_CHECK_FILENAME = "goal_check.json"


def create_session_id(*, goal_hash: str) -> str:
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    unique = uuid.uuid4().hex[:8]
    return f"{goal_hash}_{stamp}_{unique}"


def create_session_dir(*, repo_root: Path, session_id: str, goal_hash: str) -> Path:
    session_dir = session_dir_path(repo_root=repo_root, session_id=session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if not metadata_path.exists():
        payload = {
            "session_id": session_id,
            "goal_hash": goal_hash,
            "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        }
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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


def session_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return repo_root / LOOPY_DIRNAME / SESSIONS_DIRNAME / session_id


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


def iteration_harness_output_root(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return harness_outputs_dir_path(
        repo_root=repo_root, session_id=session_id
    ) / iteration_dir_name(iteration=iteration, workflow_id=workflow_id)


def ensure_iteration_dir(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    iteration_dir = iterations_dir_path(
        repo_root=repo_root, session_id=session_id
    ) / iteration_dir_name(iteration=iteration, workflow_id=workflow_id)
    iteration_dir.mkdir(parents=True, exist_ok=True)
    return iteration_dir


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
