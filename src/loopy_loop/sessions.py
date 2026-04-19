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
PROMPT_FILENAME = "prompt.txt"
RESULT_FILENAME = "result.json"
RESULT_TEXT_FILENAME = "result_text.txt"
HARNESS_RUN_ID_FILENAME = "harness_run_id.txt"
CONTROL_FILENAME = "control.json"
GOAL_CHECK_FILENAME = "goal_check.json"


def create_session_id(*, goal_slug: str) -> str:
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    unique = uuid.uuid4().hex[:8]
    return f"{goal_slug}_{stamp}_{unique}"


def create_session_dir(*, repo_root: Path, session_id: str, goal_slug: str) -> Path:
    session_dir = session_dir_path(repo_root=repo_root, session_id=session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if not metadata_path.exists():
        payload = {
            "session_id": session_id,
            "goal_slug": goal_slug,
            "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        }
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    events_path = session_dir / EVENTS_FILENAME
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8")
    iterations_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    return session_dir


def session_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return repo_root / LOOPY_DIRNAME / SESSIONS_DIRNAME / session_id


def iterations_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return session_dir_path(repo_root=repo_root, session_id=session_id) / ITERATIONS_DIRNAME


def iteration_dir_name(*, iteration: int, workflow_id: str) -> str:
    return f"{iteration:04d}_{workflow_id}"


def ensure_iteration_dir(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    iteration_dir = (
        iterations_dir_path(repo_root=repo_root, session_id=session_id)
        / iteration_dir_name(iteration=iteration, workflow_id=workflow_id)
    )
    iteration_dir.mkdir(parents=True, exist_ok=True)
    return iteration_dir


def control_path(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return ensure_iteration_dir(
        repo_root=repo_root,
        session_id=session_id,
        iteration=iteration,
        workflow_id=workflow_id,
    ) / CONTROL_FILENAME


def goal_check_path(*, repo_root: Path, session_id: str, iteration: int) -> Path:
    return ensure_iteration_dir(
        repo_root=repo_root,
        session_id=session_id,
        iteration=iteration,
        workflow_id="goal_check",
    ) / GOAL_CHECK_FILENAME
