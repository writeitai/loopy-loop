from __future__ import annotations

from pathlib import Path
import time
import traceback

import httpx

from loopy_loop.config import ConfigError
from loopy_loop.config import load_workflow_config
from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.harness_runner import run_harness_iteration
from loopy_loop.harness_runner import write_iteration_artifacts
from loopy_loop.models import DEFAULT_FINISHED_RETRY_ATTEMPTS
from loopy_loop.models import DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS
from loopy_loop.models import DEFAULT_POLL_INTERVAL_SECONDS
from loopy_loop.models import FinishedRequest
from loopy_loop.models import IterationResult
from loopy_loop.models import NextActionResponse
from loopy_loop.models import RegisterWorkerResponse
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.sessions import ensure_iteration_dir
from loopy_loop.sessions import GOAL_CHECK_FILENAME


def run_worker_loop(
    *,
    repo_root: Path,
    coordinator_url: str,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    finished_retry_attempts: int = DEFAULT_FINISHED_RETRY_ATTEMPTS,
    finished_retry_backoff_seconds: float = DEFAULT_FINISHED_RETRY_BACKOFF_SECONDS,
) -> None:
    base_url = coordinator_url.rstrip("/")
    with httpx.Client(timeout=30.0) as client:
        worker_id = _register_worker(client=client, coordinator_url=base_url)
        while True:
            next_action = _post_next(
                client=client, coordinator_url=base_url, worker_id=worker_id
            )
            if next_action.action == "wait":
                time.sleep(poll_interval_seconds)
                continue
            if next_action.action == "stop":
                return
            finished_request = _run_assignment(
                repo_root=repo_root, next_action=next_action
            )
            next_after_finish = _post_finished_with_retry(
                client=client,
                coordinator_url=base_url,
                worker_id=worker_id,
                request=finished_request,
                attempts=finished_retry_attempts,
                backoff_seconds=finished_retry_backoff_seconds,
            )
            if next_after_finish.action == "stop":
                return


def _register_worker(*, client: httpx.Client, coordinator_url: str) -> str:
    response = client.post(f"{coordinator_url}/workers/register", json={})
    response.raise_for_status()
    payload = RegisterWorkerResponse.model_validate(response.json())
    return payload.worker_id


def _post_next(
    *, client: httpx.Client, coordinator_url: str, worker_id: str
) -> NextActionResponse:
    response = client.post(f"{coordinator_url}/workers/{worker_id}/next", json={})
    response.raise_for_status()
    return NextActionResponse.model_validate(response.json())


def _post_finished_with_retry(
    *,
    client: httpx.Client,
    coordinator_url: str,
    worker_id: str,
    request: FinishedRequest,
    attempts: int,
    backoff_seconds: float,
) -> NextActionResponse:
    for attempt in range(1, attempts + 1):
        try:
            response = client.post(
                f"{coordinator_url}/workers/{worker_id}/finished",
                json=request.model_dump(),
            )
            response.raise_for_status()
            return NextActionResponse.model_validate(response.json())
        except httpx.HTTPError:
            traceback.print_exc()
            if attempt == attempts:
                raise
            time.sleep(backoff_seconds * attempt)
    raise RuntimeError("unreachable")


def _run_assignment(
    *, repo_root: Path, next_action: NextActionResponse
) -> FinishedRequest:
    if (
        next_action.assignment_id is None
        or next_action.session_id is None
        or next_action.workflow_id is None
        or next_action.iteration is None
        or next_action.config_snapshot is None
    ):
        raise ConfigError("Incomplete run payload from coordinator")

    config_snapshot = RootConfigSnapshot.model_validate(
        next_action.config_snapshot.model_dump()
    )
    workflow_dir = repo_root / LOOPY_DIRNAME / "workflows" / next_action.workflow_id
    load_workflow_config(workflow_dir=workflow_dir)
    prompt_text = (workflow_dir / "prompt.txt").read_text(encoding="utf-8")
    iteration_dir = ensure_iteration_dir(
        repo_root=repo_root,
        session_id=next_action.session_id,
        iteration=next_action.iteration,
        workflow_id=next_action.workflow_id,
    )
    rendered_prompt = _render_prompt(
        config_snapshot=config_snapshot,
        assignment_id=next_action.assignment_id,
        session_id=next_action.session_id,
        iteration=next_action.iteration,
        workflow_id=next_action.workflow_id,
        iteration_dir=iteration_dir,
        workflow_prompt=prompt_text,
    )
    try:
        iteration_result = run_harness_iteration(
            repo_root=repo_root,
            config_snapshot=config_snapshot,
            rendered_prompt=rendered_prompt,
        )
    except ConfigError as exc:
        traceback.print_exc()
        iteration_result = IterationResult(
            success=False, text=None, error=str(exc), harness_run_id=""
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
    return FinishedRequest(
        assignment_id=next_action.assignment_id,
        session_id=next_action.session_id,
        workflow_id=next_action.workflow_id,
        success=iteration_result.success,
        text=iteration_result.text,
        error=iteration_result.error,
    )


def _render_prompt(
    *,
    config_snapshot: RootConfigSnapshot,
    assignment_id: str,
    session_id: str,
    iteration: int,
    workflow_id: str,
    iteration_dir: Path,
    workflow_prompt: str,
) -> str:
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
        f"Assignment ID: {assignment_id}",
        f"Iteration: {iteration}",
        f"Workflow ID: {workflow_id}",
        f"Iteration directory: {iteration_dir}",
    ]
    if workflow_id == "goal_check":
        lines.append(
            f"goal_check.json output path: {iteration_dir / GOAL_CHECK_FILENAME}"
        )
    lines.extend(["", "Workflow body:", workflow_prompt])
    return "\n".join(lines).rstrip() + "\n"
