from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
import traceback

import httpx

from loopy_loop.config import ConfigError
from loopy_loop.config import load_workflow_config
from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.harness_runner import run_harness_iteration
from loopy_loop.harness_runner import write_iteration_artifacts
from loopy_loop.models import FinishedRequest
from loopy_loop.models import IterationResult
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import TaskResponse
from loopy_loop.sessions import control_path
from loopy_loop.sessions import ensure_iteration_dir
from loopy_loop.sessions import eval_checks_dir_path
from loopy_loop.sessions import finished_path
from loopy_loop.sessions import GOAL_CHECK_FILENAME
from loopy_loop.sessions import harness_outputs_dir_path
from loopy_loop.sessions import iteration_harness_output_root
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.sessions import project_state_dir_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import updates_from_user_path

# Internal retry constants for /finished — not configurable externally.
# If all retries fail, the exception propagates and the process exits;
# the next invocation recovers via the completed-task or abandoned-task path in /register.
_FINISHED_RETRY_ATTEMPTS = 2
_FINISHED_RETRY_BACKOFF_SECONDS = 1.0


class FatalAssignmentError(Exception):
    def __init__(
        self, *, finished_assignment: FinishedAssignment, message: str
    ) -> None:
        super().__init__(message)
        self.finished_assignment = finished_assignment


@dataclass(frozen=True)
class FinishedAssignment:
    request: FinishedRequest
    pending_path: Path


def run_worker_loop(*, repo_root: Path, coordinator_url: str) -> None:
    base_url = coordinator_url.rstrip("/")
    with httpx.Client(timeout=30.0) as client:
        task = _post_register(client=client, coordinator_url=base_url)
        while task.action == "run":
            try:
                finished_assignment = _run_task(repo_root=repo_root, task=task)
            except FatalAssignmentError as exc:
                print(str(exc), file=sys.stderr)
                _post_finished(
                    client=client,
                    coordinator_url=base_url,
                    request=exc.finished_assignment.request,
                )
                _clear_pending_finished_request(
                    path=exc.finished_assignment.pending_path
                )
                sys.exit(2)
            task = _post_finished(
                client=client,
                coordinator_url=base_url,
                request=finished_assignment.request,
            )
            _clear_pending_finished_request(path=finished_assignment.pending_path)


def _post_register(*, client: httpx.Client, coordinator_url: str) -> TaskResponse:
    response = client.post(f"{coordinator_url}/register", json={})
    response.raise_for_status()
    return TaskResponse.model_validate(response.json())


def _post_finished(
    *, client: httpx.Client, coordinator_url: str, request: FinishedRequest
) -> TaskResponse:
    for attempt in range(1, _FINISHED_RETRY_ATTEMPTS + 1):
        try:
            response = client.post(
                f"{coordinator_url}/finished", json=request.model_dump()
            )
            response.raise_for_status()
            return TaskResponse.model_validate(response.json())
        except httpx.HTTPError:
            traceback.print_exc()
            if attempt == _FINISHED_RETRY_ATTEMPTS:
                raise
            time.sleep(_FINISHED_RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError("unreachable")


def _run_task(*, repo_root: Path, task: TaskResponse) -> FinishedAssignment:
    if (
        task.session_id is None
        or task.workflow_id is None
        or task.iteration is None
        or task.config_snapshot is None
    ):
        raise ConfigError("Incomplete run payload from coordinator")

    config_snapshot = RootConfigSnapshot.model_validate(
        task.config_snapshot.model_dump()
    )
    workflow_dir = repo_root / LOOPY_DIRNAME / "workflows" / task.workflow_id
    workflow_config = load_workflow_config(workflow_dir=workflow_dir)
    prompt_text = (workflow_dir / "prompt.txt").read_text(encoding="utf-8")
    iteration_dir = ensure_iteration_dir(
        repo_root=repo_root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
    )
    harness_output_root = iteration_harness_output_root(
        repo_root=repo_root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
    )
    rendered_prompt = _render_prompt(
        config_snapshot=config_snapshot,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
        iteration_dir=iteration_dir,
        harness_output_root=harness_output_root,
        workflow_prompt=prompt_text,
        emits_goal_check=workflow_config.emits_goal_check,
        repo_root=repo_root,
    )
    fatal_error: str | None = None
    try:
        iteration_result = run_harness_iteration(
            repo_root=repo_root,
            config_snapshot=config_snapshot,
            rendered_prompt=rendered_prompt,
            harness_output_root=harness_output_root,
        )
    except ConfigError as exc:
        fatal_error = str(exc)
        iteration_result = IterationResult(
            success=False, text=None, error=fatal_error, harness_run_id=""
        )
    except Exception as exc:
        traceback.print_exc()
        iteration_result = IterationResult(
            success=False, text=None, error=str(exc), harness_run_id=""
        )
    write_iteration_artifacts(
        iteration_dir=iteration_dir,
        rendered_prompt=rendered_prompt,
        iteration_result=iteration_result,
    )
    finished_request = FinishedRequest(
        session_id=task.session_id,
        workflow_id=task.workflow_id,
        iteration=task.iteration,
        success=iteration_result.success,
        text=iteration_result.text,
        error=iteration_result.error,
    )
    pending_path = _write_pending_finished_request(
        repo_root=repo_root, request=finished_request
    )
    finished_assignment = FinishedAssignment(
        request=finished_request, pending_path=pending_path
    )
    if fatal_error is not None:
        raise FatalAssignmentError(
            finished_assignment=finished_assignment, message=fatal_error
        )
    return finished_assignment


def _write_pending_finished_request(
    *, repo_root: Path, request: FinishedRequest
) -> Path:
    path = pending_finished_request_path(
        repo_root=repo_root,
        session_id=request.session_id,
        iteration=request.iteration,
        workflow_id=request.workflow_id,
    )
    path.write_text(json.dumps(request.model_dump(), indent=2), encoding="utf-8")
    return path


def _clear_pending_finished_request(*, path: Path) -> None:
    path.unlink(missing_ok=True)


def _render_prompt(
    *,
    config_snapshot: RootConfigSnapshot,
    session_id: str,
    iteration: int,
    workflow_id: str,
    iteration_dir: Path,
    harness_output_root: Path,
    workflow_prompt: str,
    emits_goal_check: bool = False,
    repo_root: Path | None = None,
) -> str:
    root = repo_root or Path.cwd()
    lines = [
        "loopy-loop assignment",
        "",
        f"Goal: {config_snapshot.goal}",
        "Completion criteria:",
        *[f"- {item}" for item in config_snapshot.completion_criteria],
        "Stop criteria:",
        *[f"- {item}" for item in config_snapshot.stop_criteria],
        "",
        f"Session ID: {session_id}",
        f"Iteration: {iteration}",
        f"Workflow ID: {workflow_id}",
        f"Session directory: {session_dir_path(repo_root=root, session_id=session_id)}",
        "Session project_state directory: "
        f"{project_state_dir_path(repo_root=root, session_id=session_id)}",
        "Session eval_checks directory: "
        f"{eval_checks_dir_path(repo_root=root, session_id=session_id)}",
        "Session updates_from_user path: "
        f"{updates_from_user_path(repo_root=root, session_id=session_id)}",
        f"Session control path: {control_path(repo_root=root, session_id=session_id)}",
        "Session finished ledger path: "
        f"{finished_path(repo_root=root, session_id=session_id)}",
        "Session harness outputs directory: "
        f"{harness_outputs_dir_path(repo_root=root, session_id=session_id)}",
        f"Iteration directory: {iteration_dir}",
        f"Iteration harness output root: {harness_output_root}",
    ]
    if workflow_id == "goal_check" or emits_goal_check:
        lines.append(
            f"goal_check.json output path: {iteration_dir / GOAL_CHECK_FILENAME}"
        )
    lines.extend(["", "Workflow body:", workflow_prompt])
    return "\n".join(lines).rstrip() + "\n"
