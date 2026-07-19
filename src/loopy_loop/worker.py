from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import sysconfig
import tempfile
import time
import traceback
from typing import Literal
import uuid

import httpx

from loopy_loop.assignments import AssignmentContractError
from loopy_loop.assignments import build_attempt_assignment
from loopy_loop.assignments import repository_id
from loopy_loop.assignments import verify_workflow_snapshot
from loopy_loop.assignments import write_attempt_assignment
from loopy_loop.config import ConfigError
from loopy_loop.config import load_workflow_config
from loopy_loop.config import load_workflow_set_preamble
from loopy_loop.config import workflow_set_workflows_dir_path
from loopy_loop.git_evidence import capture_git_evidence
from loopy_loop.git_evidence import GitEvidenceError
from loopy_loop.harness_runner import run_harness_iteration
from loopy_loop.harness_runner import write_iteration_artifacts
from loopy_loop.harness_runner import write_iteration_inputs
from loopy_loop.models import AttemptAssignment
from loopy_loop.models import CurrentTask
from loopy_loop.models import FinishedRequest
from loopy_loop.models import IterationResult
from loopy_loop.models import IterationUsage
from loopy_loop.models import LOOPY_WORKER_CAPABILITIES
from loopy_loop.models import RegisterRequest
from loopy_loop.models import REQUIRED_V3_WORKER_CAPABILITIES
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import TaskResponse
from loopy_loop.models import utc_now
from loopy_loop.models import WORKER_PROTOCOL_VERSION
from loopy_loop.models import WorkerIdentity
from loopy_loop.sessions import append_jsonl_record
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import ensure_iteration_dir
from loopy_loop.sessions import eval_checks_dir_path
from loopy_loop.sessions import eval_readiness_dir_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import finished_path
from loopy_loop.sessions import git_receipt_path
from loopy_loop.sessions import git_receipt_ref
from loopy_loop.sessions import GOAL_CHECK_FILENAME
from loopy_loop.sessions import harness_outputs_dir_path
from loopy_loop.sessions import iteration_harness_output_root
from loopy_loop.sessions import PATHS_FILENAME
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.sessions import project_state_dir_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import session_goal_path
from loopy_loop.sessions import session_layout
from loopy_loop.sessions import SESSION_LAYOUT_FOLDED
from loopy_loop.sessions import traces_root_path
from loopy_loop.sessions import updates_from_user_path
from loopy_loop.sessions import user_updates_journal_path
from loopy_loop.sessions import WORKER_SESSIONS_FILENAME
from loopy_loop.sessions import write_json_atomic
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import import_harness_artifacts
from loopy_loop.tracing import TRACE_MANIFEST_FILENAME
from loopy_loop.tracing import trace_write_json
from loopy_loop.tracing import trace_write_text
from loopy_loop.tracing import update_trace_manifest
from loopy_loop.worker_identity import current_worker_identity

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


def _join_trace_errors(*, current: str | None, addition: str) -> str:
    """Append one trace failure detail without losing an earlier diagnosis."""

    return f"{current}; {addition}" if current else addition


def _bundled_cli_scripts_dir() -> str:
    """Directory holding the console scripts of loopy-loop's bundled CLI deps.

    Derived from where eval-banana's script was actually installed (the
    package RECORD), because sysconfig's default scheme is wrong whenever the
    install used another scheme — e.g. `pip install --user` under a system
    interpreter puts scripts in the user scheme's bin while the default
    scheme points at the prefix bin. sysconfig is only the fallback for
    installs that ship no file record.
    """
    try:
        dist = importlib.metadata.distribution("eval-banana")
    except importlib.metadata.PackageNotFoundError:
        dist = None
    if dist is not None:
        for file in dist.files or []:
            if file.name in ("eval-banana", "eval-banana.exe"):
                located = str(dist.locate_file(file))
                return os.path.dirname(os.path.normpath(located))
    # Not Path(sys.executable).resolve(): resolving follows the venv symlink
    # to the base interpreter's bin, which does not hold the scripts.
    return sysconfig.get_path("scripts")


def ensure_interpreter_scripts_on_path(environ: MutableMapping[str, str]) -> None:
    """Make loopy-loop's bundled dependency CLIs findable by harness agents.

    Harness agent processes inherit this worker's environment. CLIs shipped as
    loopy-loop dependencies (e.g. eval-banana) install into the environment's
    scripts directory, which is not on PATH under `uv tool install` or `pipx`
    (those expose only the primary package's entry points). Appending — not
    prepending — keeps existing resolution intact: agents running `python` in
    the target repo must not pick up loopy-loop's interpreter, and an
    eval-banana already on PATH keeps winning. Existing entries are preserved
    verbatim (an empty entry is a valid "current directory" component); a
    missing PATH starts from the platform default search path.
    """
    scripts_dir = _bundled_cli_scripts_dir()
    entries = environ.get("PATH", os.defpath).split(os.pathsep)
    if scripts_dir not in entries:
        environ["PATH"] = os.pathsep.join([*entries, scripts_dir])


def run_worker_loop(*, repo_root: Path, coordinator_url: str) -> None:
    """Register one worker and execute assignments sequentially until stopped."""

    ensure_interpreter_scripts_on_path(environ=os.environ)
    base_url = coordinator_url.rstrip("/")
    identity = current_worker_identity()
    with httpx.Client(timeout=30.0) as client:
        task = _post_register(
            client=client,
            coordinator_url=base_url,
            identity=identity,
            repo_root=repo_root,
        )
        while task.action == "run":
            try:
                finished_assignment = _run_task(
                    repo_root=repo_root, task=task, identity=identity
                )
            except FatalAssignmentError as exc:
                # Exit WITHOUT posting /finished: posting would make the
                # coordinator dispatch the next task to this about-to-exit
                # worker, and the replacement worker's /register would then
                # record that never-started assignment as a second (phantom)
                # crash failure. The pending file stays in place; the next
                # /register recovers this completion exactly once.
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            task = _post_finished(
                client=client,
                coordinator_url=base_url,
                request=finished_assignment.request,
            )
            _clear_pending_finished_request(path=finished_assignment.pending_path)


def _post_register(
    *,
    client: httpx.Client,
    coordinator_url: str,
    identity: WorkerIdentity,
    repo_root: Path,
) -> TaskResponse:
    """Register this process with its protocol and repository identity."""

    request = RegisterRequest(
        worker=identity,
        worker_protocol_version=WORKER_PROTOCOL_VERSION,
        capabilities=sorted(_worker_capabilities()),
        repo_root=str(repo_root.resolve()),
        repository_id=repository_id(repo_root=repo_root),
    )
    # Unbounded read for /register ONLY: registration may legitimately block
    # while the coordinator drains a crashed predecessor's orphaned agents.
    # /finished keeps the bounded default so a wedged response cannot leave
    # this worker alive-but-stuck forever (which would 409 all reclaims).
    response = client.post(
        f"{coordinator_url}/register",
        json=request.model_dump(),
        timeout=httpx.Timeout(30.0, read=None),
    )
    _exit_if_busy(response=response)
    response.raise_for_status()
    return TaskResponse.model_validate(response.json())


def _exit_if_busy(response: httpx.Response) -> None:
    """409 means another worker verifiably holds the current task (D7)."""
    if response.status_code != 409:
        return
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    print(f"Coordinator refused this worker: {detail}", file=sys.stderr)
    sys.exit(3)


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


def _run_task(
    *, repo_root: Path, task: TaskResponse, identity: WorkerIdentity | None = None
) -> FinishedAssignment:
    """Execute one frozen assignment and persist its raw local trace artifacts."""

    if (
        task.session_id is None
        or task.workflow_set is None
        or task.workflow_id is None
        or task.iteration is None
        or task.config_snapshot is None
    ):
        raise ConfigError("Incomplete run payload from coordinator")

    root = repo_root.resolve()
    config_snapshot = RootConfigSnapshot.model_validate(
        task.config_snapshot.model_dump()
    )
    if task.repo_root is not None and Path(task.repo_root).resolve() != root:
        raise ConfigError(
            f"Coordinator assignment belongs to {task.repo_root}, not worker checkout {root}"
        )
    local_repository_id = repository_id(repo_root=root)
    if task.repository_id is not None and task.repository_id != local_repository_id:
        raise ConfigError("Coordinator assignment repository identity does not match")
    missing_capabilities = set(task.required_capabilities) - _worker_capabilities()
    if missing_capabilities:
        raise ConfigError(
            "Worker does not support coordinator capabilities: "
            + ", ".join(sorted(missing_capabilities))
        )
    iteration_dir = ensure_iteration_dir(
        repo_root=root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
    )
    trace_root: Path | None = None
    scratch_dir: Path | None = None
    assignment: AttemptAssignment | None = None
    assignment_file: Path | None = None
    caller_context: dict[str, object] | None = None
    fatal_error: str | None = None
    started = time.monotonic()
    try:
        if task.workflow_snapshot is not None:
            metadata = _read_session_metadata(
                repo_root=root, session_id=task.session_id
            )
            layout = session_layout(repo_root=root, session_id=task.session_id)
            attempt_id = (
                task.attempt_id or f"legacy-{task.iteration}-{task.workflow_id}"
            )
            trace_root, _ = create_attempt_trace(
                repo_root=root,
                root_session_id=str(metadata.get("root_session_id") or task.session_id),
                session_id=task.session_id,
                request_id=_optional_string(
                    value=metadata.get("origin"), key="request_id"
                ),
                work_item_id=_optional_string(
                    value=metadata.get("origin"), key="parent_work_item_id"
                ),
                workflow_set=task.workflow_set,
                workflow_id=task.workflow_id,
                iteration=task.iteration,
                attempt_id=attempt_id,
                layout=layout,
            )
            trace_write_json(
                trace_root=trace_root,
                relative_path="protocol/task_response.json",
                payload=task.model_dump(mode="json"),
            )
            attempt_task = CurrentTask(
                workflow_set=task.workflow_set,
                workflow_id=task.workflow_id,
                session_id=task.session_id,
                iteration=task.iteration,
                started_at=utc_now(),
                attempt_id=attempt_id,
                workflow_snapshot=task.workflow_snapshot,
                repository_id=task.repository_id,
            )
            (config_payload, prompt_text, _, frozen_config_snapshot) = (
                verify_workflow_snapshot(
                    descriptor=task.workflow_snapshot,
                    repo_root=root,
                    expected_task=attempt_task,
                )
            )
            if config_snapshot != frozen_config_snapshot:
                raise AssignmentContractError(
                    "coordinator config snapshot does not match the frozen "
                    "attempt snapshot"
                )
            from loopy_loop.config import WorkflowConfig

            workflow_config = WorkflowConfig.model_validate(config_payload)
            git_before = _capture_git_boundary(
                repo_root=root,
                session_id=task.session_id,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
                attempt_id=attempt_id,
                phase="before",
                trace_root=trace_root,
            )
            git_before_ref = git_receipt_ref(
                session_id=None,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
                attempt_id=attempt_id,
                phase="before",
                layout=layout,
            )
            assignment = build_attempt_assignment(
                repo_root=root,
                task=attempt_task,
                descriptor=task.workflow_snapshot,
                trace_root=trace_root,
                git_before_ref=git_before_ref,
            )
            assignment_file = assignment_path(
                repo_root=root,
                session_id=task.session_id,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
                attempt_id=attempt_id,
            ).resolve()
            if task.assignment_path is not None and (
                Path(task.assignment_path).resolve() != assignment_file
            ):
                raise AssignmentContractError(
                    "coordinator assignment path does not match the attempt identity"
                )
            if task.assignment_sha256 is None:
                raise AssignmentContractError(
                    "v2 coordinator response omitted the frozen assignment hash"
                )
            if (
                not assignment_file.is_file()
                or file_sha256(path=assignment_file) != task.assignment_sha256
            ):
                # Detection, not a filesystem fence (D8): restore the exact
                # engine-derived envelope so the failed attempt can still post
                # a provenance-valid completion and the next iteration can
                # repair the work.
                write_attempt_assignment(path=assignment_file, assignment=assignment)
                if file_sha256(path=assignment_file) != task.assignment_sha256:
                    raise AssignmentContractError(
                        "coordinator-frozen assignment cannot be reconstructed"
                    )
                raise AssignmentContractError(
                    "coordinator-frozen assignment changed before execution; "
                    "the canonical envelope was restored"
                )
            persisted_assignment = AttemptAssignment.model_validate_json(
                assignment_file.read_text(encoding="utf-8")
            )
            if persisted_assignment != assignment:
                raise AssignmentContractError(
                    "coordinator-frozen assignment content contradicts its session"
                )
            assignment = persisted_assignment
            trace_write_json(
                trace_root=trace_root,
                relative_path="protocol/assignment.json",
                payload=assignment.model_dump(mode="json"),
            )
            trace_write_json(
                trace_root=trace_root,
                relative_path="git/before-receipt.json",
                payload=git_before,
            )
            harness_output_root = trace_root / "harness"
            if layout == SESSION_LAYOUT_FOLDED:
                # The whole raw iteration dir is the agent's scratch space; the
                # team-harness run lives in its harness/ subdir.
                scratch_dir = trace_root
            relevant_state_paths = [
                assignment.absolute_paths.get(name)
                for name in (
                    "layer_plan",
                    "layer_tasks",
                    "layer_current_state",
                    "layer_decisions",
                    "layer_finished_ledger",
                    "layer_eval_state",
                    "layer_handoff",
                    "workflow_roster",
                    "scheduler_view",
                    "harness_capability_roster",
                    "eval_receipts",
                )
            ]
            capability_roster_path = assignment.absolute_paths.get(
                "harness_capability_roster"
            )
            capability_roster_summary = assignment.context.get(
                "harness_capability_roster"
            )
            capability_roster_sha256 = assignment.provenance.get(
                "harness_capability_roster_sha256"
            )
            caller_context = {
                "schema_version": 1,
                "trace_root": str(harness_output_root.resolve()),
                "parent_assignment_path": str(assignment_file),
                "parent_attempt_id": attempt_id,
                "root_session_id": assignment.identity["root_session_id"],
                "session_id": task.session_id,
                "session_depth": assignment.identity["depth"],
                "workflow_role": task.workflow_id,
                "relevant_state_paths": [
                    path for path in relevant_state_paths if path is not None
                ],
            }
            if (
                capability_roster_path is not None
                and capability_roster_sha256 is not None
                and isinstance(capability_roster_summary, dict)
            ):
                caller_context.update(
                    {
                        "capability_roster_path": capability_roster_path,
                        "capability_roster_sha256": capability_roster_sha256,
                        "capability_roster_summary": capability_roster_summary,
                    }
                )
        else:
            workflow_dir = (
                workflow_set_workflows_dir_path(
                    repo_root=root, workflow_set=task.workflow_set
                )
                / task.workflow_id
            )
            workflow_config = load_workflow_config(workflow_dir=workflow_dir)
            prompt_text = (workflow_dir / "prompt.txt").read_text(encoding="utf-8")
            harness_output_root = iteration_harness_output_root(
                repo_root=root,
                session_id=task.session_id,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
            )
        rendered_prompt = _render_prompt(
            config_snapshot=config_snapshot,
            session_id=task.session_id,
            workflow_set=task.workflow_set,
            iteration=task.iteration,
            workflow_id=task.workflow_id,
            iteration_dir=iteration_dir,
            harness_output_root=harness_output_root,
            scratch_dir=scratch_dir or harness_output_root,
            workflow_prompt=prompt_text,
            emits_goal_check=workflow_config.emits_goal_check,
            repo_root=root,
            assignment=assignment,
            assignment_file=assignment_file,
        )
        write_iteration_inputs(
            iteration_dir=iteration_dir, rendered_prompt=rendered_prompt
        )
        if trace_root is not None:
            trace_write_text(
                trace_root=trace_root,
                relative_path="protocol/rendered_prompt.txt",
                content=rendered_prompt,
            )
            update_trace_manifest(
                trace_root=trace_root,
                updates={"channels": {"loopy_assignment": "complete"}},
            )
        iteration_result = run_harness_iteration(
            repo_root=root,
            config_snapshot=config_snapshot,
            rendered_prompt=rendered_prompt,
            harness_output_root=harness_output_root,
            caller_context=caller_context,
        )
    except (ConfigError, AssignmentContractError) as exc:
        fatal_error = str(exc)
        iteration_result = IterationResult(
            success=False,
            text=None,
            error=fatal_error,
            failure_kind="deterministic",
            harness_run_id="",
        )
    except Exception as exc:
        traceback.print_exc()
        iteration_result = IterationResult(
            success=False,
            text=None,
            error=str(exc),
            failure_kind="unknown",
            harness_run_id="",
        )
    rendered_prompt = locals().get("rendered_prompt", "")
    usage = _read_harness_usage(
        run_json_path=iteration_result.harness_run_json_path,
        harness_output_dir=iteration_result.harness_output_dir,
    )
    trace_problem: str | None = None
    # Folded raw dirs have no manifest; leave the manifest path empty so the
    # coordinator's folded completion path (which never seals) is used.
    trace_manifest_path = (
        str((trace_root / TRACE_MANIFEST_FILENAME).resolve())
        if trace_root is not None and (trace_root / TRACE_MANIFEST_FILENAME).is_file()
        else ""
    )
    if trace_root is not None:
        try:
            import_harness_artifacts(
                trace_root=trace_root,
                run_json_path=iteration_result.harness_run_json_path,
                session_output_dir=iteration_result.harness_output_dir,
                harness_run_id=iteration_result.harness_run_id,
            )
            git_after = _capture_git_boundary(
                repo_root=root,
                session_id=task.session_id,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
                attempt_id=task.attempt_id or "legacy",
                phase="after",
                trace_root=trace_root,
            )
            trace_write_json(
                trace_root=trace_root,
                relative_path="git/after-receipt.json",
                payload=git_after,
            )
            update_trace_manifest(
                trace_root=trace_root,
                updates={
                    "channels": {
                        "git": "complete",
                        "eval": _eval_trace_channel_status(trace_root=trace_root),
                    }
                },
            )
        except Exception as exc:
            traceback.print_exc()
            trace_problem = str(exc)
    assignment_changed_during_run = False
    if (
        assignment_file is not None
        and assignment is not None
        and task.assignment_sha256 is not None
        and (
            not assignment_file.is_file()
            or file_sha256(path=assignment_file) != task.assignment_sha256
        )
    ):
        assignment_changed_during_run = True
        try:
            write_attempt_assignment(path=assignment_file, assignment=assignment)
        except OSError as exc:
            trace_problem = _join_trace_errors(
                current=trace_problem,
                addition=f"cannot restore changed assignment: {exc}",
            )
        else:
            iteration_result = iteration_result.model_copy(
                update={
                    "success": False,
                    "error": "immutable assignment changed during harness execution",
                    "failure_kind": "deterministic",
                }
            )
    completion_assignment_sha256 = (
        task.assignment_sha256
        if assignment_file is not None
        and assignment_file.is_file()
        and task.assignment_sha256 is not None
        and file_sha256(path=assignment_file) == task.assignment_sha256
        else None
    )
    completion_repository_id = task.repository_id or local_repository_id
    iteration_result = iteration_result.model_copy(
        update={
            "attempt_id": task.attempt_id,
            "usage": usage,
            "duration_s": round(time.monotonic() - started, 3),
            "trace_manifest_path": trace_manifest_path,
            "worker": identity,
            "repository_id": completion_repository_id,
            "assignment_sha256": completion_assignment_sha256,
            "trace_incomplete": bool(
                fatal_error or trace_problem or assignment_changed_during_run
            ),
            "trace_error": trace_problem,
        }
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
        failure_kind=iteration_result.failure_kind,
        usage=iteration_result.usage,
        duration_s=iteration_result.duration_s,
        worker=identity,
        attempt_id=task.attempt_id,
        repository_id=completion_repository_id,
        assignment_sha256=completion_assignment_sha256,
        harness_run_id=iteration_result.harness_run_id,
        trace_manifest_path=iteration_result.trace_manifest_path,
        trace_incomplete=iteration_result.trace_incomplete,
        trace_error=iteration_result.trace_error,
    )
    if trace_root is not None:
        try:
            trace_write_json(
                trace_root=trace_root,
                relative_path="protocol/iteration_result.json",
                payload=iteration_result.model_dump(mode="json"),
            )
            trace_write_json(
                trace_root=trace_root,
                relative_path="protocol/finished_request.json",
                payload=finished_request.model_dump(mode="json"),
            )
            update_trace_manifest(
                trace_root=trace_root,
                updates={
                    "channels": {
                        "service": "finished_request_captured_response_pending"
                    }
                },
            )
        except Exception as exc:
            # D3: trace capture cannot reinterpret a successfully completed
            # harness run as semantic success/failure. Leave the active trace
            # visibly incomplete and retain the recovery-journal result.
            traceback.print_exc()
            trace_problem = _join_trace_errors(
                current=trace_problem,
                addition=f"cannot persist completion protocol trace: {exc}",
            )
            iteration_result = iteration_result.model_copy(
                update={"trace_incomplete": True, "trace_error": trace_problem}
            )
            finished_request = finished_request.model_copy(
                update={"trace_incomplete": True, "trace_error": trace_problem}
            )
            # The compact recovery journal is correctness-critical and must
            # reflect the trace failure even when the trace writer itself is
            # unavailable. This does not change D3 semantic success.
            write_iteration_artifacts(
                iteration_dir=iteration_dir,
                rendered_prompt=rendered_prompt,
                iteration_result=iteration_result,
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
    # Crash-safe: this file is what post-crash recovery trusts as proof of a
    # completed task — it must never exist truncated.
    write_json_atomic(path=path, payload=request.model_dump())
    return path


def _clear_pending_finished_request(*, path: Path) -> None:
    path.unlink(missing_ok=True)


def _read_harness_usage(
    *, harness_output_dir: str, run_json_path: str = ""
) -> IterationUsage | None:
    """Sum coordinator-model token usage from team-harness's run.json.

    Returns None (usage UNKNOWN, distinct from zero) when the run produced no
    record, the record is unreadable, or no turn carries usage — e.g. the
    codex provider without usage in its responses. Agent-CLI subprocess usage
    is never included; it is not measurable here.
    """
    if run_json_path:
        path = Path(run_json_path)
    elif harness_output_dir:
        path = Path(harness_output_dir) / "run.json"
    else:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    turns = record.get("turns")
    if not isinstance(turns, list):
        return None
    prompt_tokens = 0
    completion_tokens = 0
    turns_with_usage = 0
    turns_without_usage = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        usage = turn.get("usage")
        if not isinstance(usage, dict) or not usage:
            turns_without_usage += 1
            continue
        try:
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            turns_without_usage += 1
            continue
        turns_with_usage += 1
    if turns_with_usage == 0:
        return None
    return IterationUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        turns=turns_with_usage,
        turns_without_usage=turns_without_usage,
    )


def _render_prompt(
    *,
    config_snapshot: RootConfigSnapshot,
    session_id: str,
    workflow_set: str,
    iteration: int,
    workflow_id: str,
    iteration_dir: Path,
    harness_output_root: Path,
    scratch_dir: Path | None = None,
    workflow_prompt: str,
    emits_goal_check: bool = False,
    repo_root: Path | None = None,
    assignment: AttemptAssignment | None = None,
    assignment_file: Path | None = None,
) -> str:
    """Render the diet iteration header plus the workflow body.

    The header before "Workflow body:" is a coordination contract the workflow
    templates are written against (single-goal-assignments.md §3): one goal, the
    optional criteria sections, and a short key-paths block. The full machine
    path map, frozen rosters, scheduler view, and prior worker-session manifest
    move into a sibling paths.json referenced from the header — never inlined.
    """

    root = repo_root or Path.cwd()
    session_dir = session_dir_path(repo_root=root, session_id=session_id)
    scratch = scratch_dir or harness_output_root
    attempt_id = (
        str(assignment.identity.get("attempt_id"))
        if assignment is not None
        else f"legacy-{iteration}-{workflow_id}"
    )
    # Preserve the append-only user-input delivery mechanism (its journaling
    # side effect and the surfacing of not-yet-acknowledged inputs); this is
    # runtime context, distinct from the fixed header scaffolding.
    semantic_context = _semantic_prompt_context(
        repo_root=root, session_id=session_id, attempt_id=attempt_id
    )
    paths_json_path = (iteration_dir / PATHS_FILENAME).resolve()
    _write_iteration_paths(
        path=paths_json_path,
        repo_root=root,
        session_id=session_id,
        workflow_set=workflow_set,
        workflow_id=workflow_id,
        iteration=iteration,
        iteration_dir=iteration_dir,
        session_dir=session_dir,
        harness_output_root=harness_output_root,
        emits_goal_check=emits_goal_check,
        assignment=assignment,
        assignment_file=assignment_file,
    )
    preamble = load_workflow_set_preamble(repo_root=root, workflow_set=workflow_set)

    header_line = (
        f"loopy-loop assignment — iteration {iteration:04d}, "
        f"role: {workflow_id}, session: {session_id}"
    )
    key_paths = [
        "You are inside a durable looping session. Key paths:",
        f"- session dir: {session_dir.resolve()}   (paths below are relative to it)",
        "- project_state/            durable working state for your role",
        "- child_requests/pending/   publish child requests here",
        "- control.json              terminal control",
        f"- scratch dir (this iteration): {scratch.resolve()}   "
        "(raw/verbose output only; evidence goes in the durable tree)",
        f"- paths.json: {paths_json_path}    "
        "full path map, rosters, scheduler view — read if needed",
    ]
    criteria: list[str] = []
    if config_snapshot.completion_criteria:
        criteria.append("Completion criteria:")
        criteria.extend(f"- {item}" for item in config_snapshot.completion_criteria)
    if config_snapshot.stop_criteria:
        criteria.append("Stop criteria:")
        criteria.extend(f"- {item}" for item in config_snapshot.stop_criteria)

    blocks: list[list[str]] = [[header_line], ["Goal:", config_snapshot.goal]]
    if criteria:
        blocks.append(criteria)
    blocks.append(key_paths)
    if preamble is not None and preamble.strip():
        blocks.append(["Shared ground rules:", preamble.rstrip()])
    if semantic_context:
        blocks.append(["Current layer inputs and receipts:", semantic_context])
    blocks.append(["Workflow body:", workflow_prompt])
    rendered = "\n\n".join("\n".join(block) for block in blocks)
    return rendered.rstrip() + "\n"


def _write_iteration_paths(
    *,
    path: Path,
    repo_root: Path,
    session_id: str,
    workflow_set: str,
    workflow_id: str,
    iteration: int,
    iteration_dir: Path,
    session_dir: Path,
    harness_output_root: Path,
    emits_goal_check: bool,
    assignment: AttemptAssignment | None,
    assignment_file: Path | None,
) -> None:
    """Write the full machine path map the diet header references by name.

    Holds every absolute path the old header inlined plus the complete v3
    assignment path map (rosters, scheduler view, workflow contract as files),
    and previous_worker_sessions: the prior iteration's team-harness
    worker_sessions.json for selective session reuse (context-and-eval-economy
    A4), or null when none exists.
    """

    goal_check_output = (
        str((iteration_dir / GOAL_CHECK_FILENAME).resolve())
        if (workflow_id == "goal_check" or emits_goal_check)
        else None
    )
    parent_session_dir = (
        str(session_dir.parent.parent.resolve())
        if session_dir.parent.name == "children"
        else None
    )
    root_session_id = (
        str(assignment.identity.get("root_session_id"))
        if assignment is not None and assignment.identity.get("root_session_id")
        else None
    )
    previous_worker_sessions = (
        _previous_worker_sessions_path(
            repo_root=repo_root,
            root_session_id=root_session_id,
            session_id=session_id,
            iteration=iteration,
        )
        if root_session_id is not None
        else None
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "session_id": session_id,
        "workflow_set": workflow_set,
        "workflow_id": workflow_id,
        "iteration": iteration,
        "session_dir": str(session_dir.resolve()),
        "iteration_dir": str(iteration_dir.resolve()),
        "scratch_dir": str(harness_output_root.resolve()),
        "assignment_envelope": (
            str(assignment_file.resolve()) if assignment_file is not None else None
        ),
        "goal_check_output": goal_check_output,
        "parent_session_dir": parent_session_dir,
        "previous_worker_sessions": (
            str(previous_worker_sessions)
            if previous_worker_sessions is not None
            else None
        ),
        "session_paths": {
            "goal": str(
                session_goal_path(repo_root=repo_root, session_id=session_id).resolve()
            ),
            "project_state": str(
                project_state_dir_path(
                    repo_root=repo_root, session_id=session_id
                ).resolve()
            ),
            "eval_checks": str(
                eval_checks_dir_path(
                    repo_root=repo_root, session_id=session_id
                ).resolve()
            ),
            "updates_from_user": str(
                updates_from_user_path(
                    repo_root=repo_root, session_id=session_id
                ).resolve()
            ),
            "user_inputs_journal": str(
                user_updates_journal_path(
                    repo_root=repo_root, session_id=session_id
                ).resolve()
            ),
            "child_requests": str(
                child_requests_dir_path(
                    repo_root=repo_root, session_id=session_id
                ).resolve()
            ),
            "control": str(
                control_path(repo_root=repo_root, session_id=session_id).resolve()
            ),
            "finished_ledger": str(
                finished_path(repo_root=repo_root, session_id=session_id).resolve()
            ),
            "harness_outputs": str(
                harness_outputs_dir_path(
                    repo_root=repo_root, session_id=session_id
                ).resolve()
            ),
        },
        "envelope_paths": (
            dict(assignment.absolute_paths) if assignment is not None else None
        ),
    }
    write_json_atomic(path=path, payload=payload)


def _previous_worker_sessions_path(
    *, repo_root: Path, root_session_id: str, session_id: str, iteration: int
) -> Path | None:
    """Return the newest prior iteration's team-harness worker_sessions.json.

    Scans this session's attempt traces, reads each trace manifest's iteration,
    and returns the worker_sessions.json belonging to the highest iteration
    strictly below the current one. None when no earlier attempt produced it.
    """

    attempts_root = (
        traces_root_path(repo_root=repo_root)
        / root_session_id
        / "sessions"
        / session_id
        / "attempts"
    )
    if not attempts_root.is_dir():
        return None
    best_iteration = -1
    best_path: Path | None = None
    for attempt_dir in sorted(attempts_root.iterdir()):
        if not attempt_dir.is_dir():
            continue
        try:
            manifest = json.loads(
                (attempt_dir / TRACE_MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        identity = manifest.get("identity") if isinstance(manifest, dict) else None
        attempt_iteration = (
            identity.get("iteration") if isinstance(identity, dict) else None
        )
        if not isinstance(attempt_iteration, int) or attempt_iteration >= iteration:
            continue
        if attempt_iteration <= best_iteration:
            continue
        sessions_file = next(
            iter(sorted((attempt_dir / "harness").rglob(WORKER_SESSIONS_FILENAME))),
            None,
        )
        if sessions_file is None:
            continue
        best_iteration = attempt_iteration
        best_path = sessions_file.resolve()
    return best_path


def _read_session_metadata(*, repo_root: Path, session_id: str) -> dict[str, object]:
    """Read and validate the session manifest used by worker-side binding."""

    path = session_dir_path(repo_root=repo_root, session_id=session_id) / "session.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssignmentContractError(
            f"session manifest is unreadable at {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssignmentContractError(f"session manifest is not an object at {path}")
    return payload


def _optional_string(*, value: object, key: str) -> str | None:
    """Read a non-empty optional string from a loosely typed JSON object."""

    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _capture_git_boundary(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    attempt_id: str,
    phase: Literal["before", "after"],
    trace_root: Path,
) -> dict[str, object]:
    """Capture one Git boundary and persist its compact session receipt.

    The compact receipt name and location follow the session layout: folded
    sessions get a self-describing ``receipts/<NNNN>_<workflow>_git_<phase>``
    name, mirror sessions keep the historical ``git_receipts/`` hash name.
    """

    if phase not in {"before", "after"}:
        raise AssignmentContractError(f"invalid git evidence phase: {phase}")
    status_path = trace_root / "git" / f"{phase}-status.jsonl"
    diff_path = trace_root / "git" / f"{phase}-diff.patch"
    try:
        # git_evidence writes byte-exact diagnostic output. Stage those bytes
        # outside the trace, then copy decoded text into the local trace;
        # compact receipt facts never depend on the verbose files.
        with tempfile.TemporaryDirectory(prefix="loopy-git-evidence-") as temp_dir:
            staging_root = Path(temp_dir)
            staged_status = staging_root / "status.jsonl"
            staged_diff = staging_root / "diff.patch"
            receipt = capture_git_evidence(
                repo_root=repo_root,
                phase=phase,
                attempt_id=attempt_id,
                verbose_status_path=staged_status,
                verbose_diff_path=staged_diff,
            )
            trace_write_text(
                trace_root=trace_root,
                relative_path=str(status_path.relative_to(trace_root)),
                content=staged_status.read_text(encoding="utf-8", errors="replace"),
            )
            trace_write_text(
                trace_root=trace_root,
                relative_path=str(diff_path.relative_to(trace_root)),
                content=staged_diff.read_text(encoding="utf-8", errors="replace"),
            )
    except GitEvidenceError as exc:
        raise AssignmentContractError(
            f"cannot capture {phase} git evidence for {attempt_id}: {exc}"
        ) from exc
    payload = receipt.to_dict()
    payload["verbose_status_path"] = str(status_path.resolve())
    payload["verbose_diff_path"] = str(diff_path.resolve())
    compact = dict(payload)
    compact["verbose_status_path"] = None
    compact["verbose_diff_path"] = None
    receipt_path = git_receipt_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=iteration,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        phase=phase,
    )
    write_json_atomic(path=receipt_path, payload=compact)
    return payload


def _worker_capabilities() -> frozenset[str]:
    """Advertise harness features by name; never infer them from a version."""
    harness_capabilities: frozenset[str] = frozenset()
    try:
        harness_module = importlib.import_module("team_harness")
        get_capabilities = harness_module.get_capabilities
        harness_capabilities = frozenset(get_capabilities().capabilities)
    except (ImportError, AttributeError):
        pass
    return LOOPY_WORKER_CAPABILITIES | (
        harness_capabilities & REQUIRED_V3_WORKER_CAPABILITIES
    )


def _semantic_prompt_context(
    *, repo_root: Path, session_id: str, attempt_id: str
) -> str:
    """Render pending user inputs and the latest usable eval-readiness receipt."""

    sections: list[str] = []
    journal = user_updates_journal_path(repo_root=repo_root, session_id=session_id)
    records: list[dict[str, object]] = []
    if journal.exists():
        for line_number, line in enumerate(
            journal.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise ConfigError(
                    f"invalid user-input journal record at {journal}:{line_number}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ConfigError(
                    f"user-input journal record is not an object at "
                    f"{journal}:{line_number}"
                )
            records.append(payload)
    acknowledged = {
        str(record.get("input_id"))
        for record in records
        if record.get("record_type") == "user_input_acknowledgement"
        and record.get("input_id")
    }
    pending = [
        record
        for record in records
        if record.get("record_type") == "user_input"
        and str(record.get("input_id")) not in acknowledged
    ]
    delivered = {
        (str(record.get("input_id")), str(record.get("attempt_id")))
        for record in records
        if record.get("record_type") == "input_delivery"
    }
    for record in pending:
        input_id = str(record.get("input_id"))
        if (input_id, attempt_id) in delivered:
            continue
        append_jsonl_record(
            path=journal,
            payload={
                "schema_version": 1,
                "record_type": "input_delivery",
                "delivery_id": f"delivery-{uuid.uuid4().hex}",
                "input_id": input_id,
                "attempt_id": attempt_id,
                "delivered_at": utc_now().isoformat().replace("+00:00", "Z"),
            },
        )
    if pending:
        sections.extend(
            [
                "Pending append-only user inputs:",
                json.dumps(pending, indent=2, ensure_ascii=False),
                "After acting on an input, append a new JSONL record to "
                f"{journal.resolve()} with record_type "
                "'user_input_acknowledgement', its input_id, this attempt_id, "
                "acknowledged_at, and a concise disposition. Never edit an "
                "earlier record.",
            ]
        )

    readiness_root = eval_readiness_dir_path(repo_root=repo_root, session_id=session_id)
    readiness_files = sorted(
        readiness_root.glob("*.json"),
        key=lambda path: (_safe_mtime_ns(path=path), path.name),
        reverse=True,
    )
    skipped_readiness: list[str] = []
    for latest in readiness_files:
        try:
            readiness = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            skipped_readiness.append(f"{latest.resolve()}: {exc}")
            continue
        if not isinstance(readiness, dict):
            skipped_readiness.append(f"{latest.resolve()}: receipt is not an object")
            continue
        sections.extend(
            [
                f"Latest task-acceptance/eval-readiness receipt ({latest.resolve()}):",
                json.dumps(readiness, indent=2, ensure_ascii=False),
                "This is semantic context only; it does not force scheduler eligibility.",
            ]
        )
        break
    if skipped_readiness:
        sections.extend(
            [
                "Ignored malformed eval-readiness receipts (semantic context remains repairable):",
                "\n".join(f"- {item}" for item in skipped_readiness),
            ]
        )
    return "\n".join(sections)


def _safe_mtime_ns(*, path: Path) -> int:
    """Return a sortable mtime, placing unreadable artifacts last."""

    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _eval_trace_channel_status(*, trace_root: Path) -> str:
    """Project the explicit eval-banana output contract into trace status.

    A canonical ``eval/report.json`` is the only complete eval output. Stray
    files or directories are retained for diagnosis but do not masquerade as
    a completed evaluator channel.
    """

    eval_root = trace_root / "eval"
    report = eval_root / "report.json"
    if report.is_file() and not report.is_symlink():
        return "complete"
    try:
        has_partial_output = any(eval_root.iterdir())
    except OSError:
        return "incomplete"
    return "incomplete" if has_partial_output else "not_produced"
