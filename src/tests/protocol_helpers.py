from __future__ import annotations

from pathlib import Path
from typing import Any

from loopy_loop.assignments import build_attempt_assignment
from loopy_loop.assignments import repository_id
from loopy_loop.assignments import write_attempt_assignment
from loopy_loop.models import CurrentTask
from loopy_loop.models import utc_now
from loopy_loop.models import WorkerIdentity
from loopy_loop.models import WorkflowSnapshotDescriptor
from loopy_loop.sessions import file_sha256

V2_CAPABILITIES = [
    "assignment_v1",
    "frozen_workflow_v1",
    "trace_manifest_v1",
    "caller_run_record_v1",
    "coordinator_input_v1",
    "spawn_assignment_v1",
    "nested_caller_context_v1",
]

DEFAULT_WORKER = {"hostname": "test-host", "pid": 999983, "starttime": None}


def v2_register_body(
    repo_root: Path, *, worker: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the repository-bound handshake required by a fresh v2 session."""
    return {
        "worker": worker or DEFAULT_WORKER,
        "worker_protocol_version": 2,
        "capabilities": V2_CAPABILITIES,
        "repo_root": str(repo_root.resolve()),
        "repository_id": repository_id(repo_root=repo_root),
    }


def v2_completion_binding(
    task: dict[str, Any], *, worker: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Echo the exact owner, repository, and immutable assignment binding."""
    assignment_path = task.get("assignment_path")
    if not isinstance(assignment_path, str):
        raise AssertionError("v2 task response is missing assignment_path")
    assignment_file = Path(assignment_path)
    if not assignment_file.is_file():
        _materialize_test_assignment(task=task, worker=worker)
    return {
        "worker": worker or DEFAULT_WORKER,
        "repository_id": task["repository_id"],
        "assignment_sha256": file_sha256(assignment_file),
    }


def v2_finished_body(
    task: dict[str, Any],
    *,
    success: bool,
    worker: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": task["iteration"],
        "attempt_id": task["attempt_id"],
        "success": success,
        "text": "done" if success else None,
        "error": None if success else "boom",
        **v2_completion_binding(task, worker=worker),
    }
    body.update(extra)
    return body


def _materialize_test_assignment(
    *, task: dict[str, Any], worker: dict[str, Any] | None
) -> None:
    """Perform the worker's pre-harness assignment step for direct API tests."""
    repo_root = Path(task["repo_root"])
    attempt_id = task["attempt_id"]
    descriptor = WorkflowSnapshotDescriptor.model_validate(task["workflow_snapshot"])
    current_task = CurrentTask(
        workflow_set=task["workflow_set"],
        workflow_id=task["workflow_id"],
        session_id=task["session_id"],
        iteration=task["iteration"],
        started_at=utc_now(),
        worker=WorkerIdentity.model_validate(worker or DEFAULT_WORKER),
        attempt_id=attempt_id,
        workflow_snapshot=descriptor,
        repository_id=task["repository_id"],
        completion_contract_version=2,
    )
    assignment = build_attempt_assignment(
        repo_root=repo_root,
        task=current_task,
        descriptor=descriptor,
        trace_root=repo_root / ".loopy_loop" / "test_traces" / attempt_id,
        git_before_ref=f"session:/git_receipts/test-{attempt_id}.json",
    )
    write_attempt_assignment(path=Path(task["assignment_path"]), assignment=assignment)
