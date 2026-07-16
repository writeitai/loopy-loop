from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.models import SAFE_DURABLE_ID_PATTERN
from loopy_loop.models import utc_now

SESSIONS_DIRNAME = "sessions"
ITERATIONS_DIRNAME = "iterations"
CHILDREN_DIRNAME = "children"
CHILD_REQUESTS_DIRNAME = "child_requests"
CHILD_REQUEST_PENDING_DIRNAME = "pending"
CHILD_REQUEST_ACCEPTED_DIRNAME = "accepted"
CHILD_REQUEST_REJECTED_DIRNAME = "rejected"
SESSION_METADATA_FILENAME = "session.json"
STATE_FILENAME = "state.json"
CHILDREN_FILENAME = "children.json"
PARENT_FILENAME = "parent.json"
GOAL_FILENAME = "goal.md"
GOAL_CONTRACT_FILENAME = "goal_contract.json"
WORKFLOW_CONTRACT_FILENAME = "workflow_contract.json"
EVENTS_FILENAME = "events.jsonl"
PROJECT_STATE_DIRNAME = "project_state"
EVAL_CHECKS_DIRNAME = "eval_checks"
EVAL_READINESS_DIRNAME = "eval_readiness"
EVAL_RECEIPTS_DIRNAME = "eval_receipts"
CHILD_OUTCOMES_DIRNAME = "child_outcomes"
PARENT_ACCEPTANCE_DIRNAME = "parent_acceptance"
GIT_RECEIPTS_DIRNAME = "git_receipts"
DELIVERY_RECEIPTS_DIRNAME = "delivery_receipts"
CONTROL_REJECTED_DIRNAME = "control_rejected"
PROTOCOL_FAILURES_DIRNAME = "protocol_failures"
TRACE_SEALS_DIRNAME = "trace_seals"
INPUTS_DIRNAME = "inputs"
USER_UPDATES_JOURNAL_FILENAME = "user_updates.jsonl"
HARNESS_OUTPUTS_DIRNAME = "harness_outputs"
TRACES_DIRNAME = "traces"
TRACE_FINALIZATION_OUTBOX_DIRNAME = "trace_finalization_outbox"
UPDATES_FROM_USER_FILENAME = "updates_from_user.md"
FINISHED_FILENAME = "finished.md"
PROMPT_FILENAME = "prompt.txt"
RESULT_FILENAME = "result.json"
RESULT_TEXT_FILENAME = "result_text.txt"
HARNESS_RUN_ID_FILENAME = "harness_run_id.txt"
PENDING_FINISHED_REQUEST_FILENAME = "pending_finished_request.json"
CONTROL_FILENAME = "control.json"
GOAL_CHECK_FILENAME = "goal_check.json"
ASSIGNMENT_FILENAME = "assignment.json"
WORKFLOW_SNAPSHOT_DIRNAME = "workflow_snapshot"
TRACE_REF_FILENAME = "trace_ref.json"


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


def write_bytes_atomic(*, path: Path, content: bytes) -> None:
    """Crash-safe binary counterpart used for immutable input snapshots."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
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


def append_jsonl_record(*, path: Path, payload: object) -> None:
    """Durably append one compact JSON record without rewriting history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


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
    root_session_id: str | None = None,
    depth: int | None = None,
    layer_kind: str = "work",
    completion_criteria: list[str] | None = None,
    stop_criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    deliverables: list[str] | None = None,
    required_evidence: list[str] | None = None,
    origin_request_id: str | None = None,
    accepted_request_ref: str | None = None,
    accepted_request_sha256: str | None = None,
    inputs: list[dict[str, str]] | None = None,
    frozen_input_files: dict[str, bytes] | None = None,
    origin: dict[str, object] | None = None,
    workflow_contract: dict[str, object] | None = None,
    schema_version: int = 1,
) -> Path:
    created_at = utc_now().isoformat().replace("+00:00", "Z")
    publish_from_staging = False
    published_session_dir: Path | None = None
    if parent_session_id is None:
        session_dir = sessions_root_path(repo_root=repo_root) / session_id
        parent_session_dir: Path | None = None
    else:
        parent_session_dir = session_dir_path(
            repo_root=repo_root, session_id=parent_session_id
        )
        session_dir = parent_session_dir / CHILDREN_DIRNAME / session_id
        if not session_dir.exists():
            # Publish a child topology node atomically.  A crash after mkdir
            # but before session.json used to make the resolver reject the
            # entire tree forever.  Immutable identity files are now built in
            # an ignored sibling and the complete directory is renamed into
            # its canonical name in one filesystem operation.
            published_session_dir = session_dir
            session_dir = (
                parent_session_dir
                / CHILDREN_DIRNAME
                / (f".staging-{session_id}-{uuid.uuid4().hex[:8]}")
            )
            publish_from_staging = True
    session_dir.mkdir(parents=True, exist_ok=True)
    if root_session_id is None or depth is None:
        if parent_session_id is None:
            root_session_id = root_session_id or session_id
            depth = 0 if depth is None else depth
        else:
            assert parent_session_dir is not None
            parent_manifest = _read_json_mapping(
                parent_session_dir / SESSION_METADATA_FILENAME
            )
            root_session_id = root_session_id or str(
                parent_manifest.get("root_session_id") or parent_session_id
            )
            parent_depth = parent_manifest.get("depth")
            depth = (
                int(parent_depth) + 1
                if depth is None and isinstance(parent_depth, int)
                else (1 if depth is None else depth)
            )
    goal_contract_hash: str | None = None
    if schema_version >= 2:
        inputs_root = session_dir / INPUTS_DIRNAME
        for relative, content in sorted((frozen_input_files or {}).items()):
            relative_path = Path(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or "\\" in relative
            ):
                raise ValueError(f"unsafe frozen session input path: {relative!r}")
            target = inputs_root / relative_path
            if target.exists():
                if not target.is_file() or target.read_bytes() != content:
                    raise ValueError(
                        f"frozen session input contradicts existing bytes: {target}"
                    )
            else:
                write_bytes_atomic(path=target, content=content)
        goal_contract_path_value = session_dir / GOAL_CONTRACT_FILENAME
        if not goal_contract_path_value.exists():
            goal_contract_payload = {
                "schema_version": 1,
                "session_id": session_id,
                "goal": goal,
                "goal_hash": goal_hash,
                "completion_criteria": completion_criteria or [],
                "stop_criteria": stop_criteria or [],
                "constraints": constraints or [],
                "deliverables": deliverables or [],
                "required_evidence": required_evidence or [],
                "terminal_blocker_policy_ref": f"workflow:{workflow_set}",
                "origin_request_id": origin_request_id,
                "accepted_request_ref": accepted_request_ref,
                "accepted_request_sha256": accepted_request_sha256,
                "inputs": inputs or [],
                "created_at": created_at,
            }
            write_json_atomic(
                path=goal_contract_path_value, payload=goal_contract_payload
            )
        goal_contract_hash = file_sha256(goal_contract_path_value)
    workflow_contract_hash: str | None = None
    if workflow_contract is not None:
        contract_path = session_dir / WORKFLOW_CONTRACT_FILENAME
        if not contract_path.exists():
            write_json_atomic(path=contract_path, payload=workflow_contract)
        if schema_version >= 2:
            workflow_contract_hash = file_sha256(contract_path)

    metadata_path = session_dir / SESSION_METADATA_FILENAME
    if not metadata_path.exists():
        if schema_version >= 2:
            payload = {
                "schema_version": 2,
                "session_id": session_id,
                "root_session_id": root_session_id,
                "parent_session_id": parent_session_id,
                "depth": depth,
                "workflow_set": workflow_set,
                "layer_kind": layer_kind,
                "goal_hash": goal_hash,
                "goal_contract_hash": goal_contract_hash,
                "workflow_contract_hash": workflow_contract_hash,
                "origin": origin or {},
                "created_at": created_at,
            }
        else:
            payload = {
                "session_id": session_id,
                "goal_hash": goal_hash,
                "workflow_set": workflow_set,
                "parent_session_id": parent_session_id,
                "created_at": created_at,
            }
        write_json_atomic(path=metadata_path, payload=payload)
    if parent_session_id is not None:
        parent_path = session_dir / PARENT_FILENAME
        if not parent_path.exists():
            payload = {
                "schema_version": 1,
                "parent_session_id": parent_session_id,
                "parent_relative_path": "../..",
                "created_at": created_at,
            }
            write_json_atomic(path=parent_path, payload=payload)
    goal_path = session_dir / GOAL_FILENAME
    if goal and not goal_path.exists():
        write_text_atomic(path=goal_path, content=goal.rstrip() + "\n")
    if publish_from_staging:
        assert published_session_dir is not None
        os.replace(session_dir, published_session_dir)
        session_dir = published_session_dir
    children = session_dir / CHILDREN_FILENAME
    if not children.exists():
        payload = (
            {
                "schema_version": 2,
                "parent_session_id": session_id,
                "revision": 0,
                "children": [],
            }
            if schema_version >= 2
            else {"schema_version": 1, "children": []}
        )
        write_json_atomic(path=children, payload=payload)
    requests_root = child_requests_dir_path(repo_root=repo_root, session_id=session_id)
    requests_root.mkdir(parents=True, exist_ok=True)
    for request_dir in (
        child_requests_pending_dir_path(repo_root=repo_root, session_id=session_id),
        child_requests_accepted_dir_path(repo_root=repo_root, session_id=session_id),
        child_requests_rejected_dir_path(repo_root=repo_root, session_id=session_id),
    ):
        request_dir.mkdir(parents=True, exist_ok=True)
    events_path = session_dir / EVENTS_FILENAME
    if not events_path.exists():
        write_text_atomic(path=events_path, content="")
    control_path(repo_root=repo_root, session_id=session_id)
    updates_path = updates_from_user_path(repo_root=repo_root, session_id=session_id)
    if not updates_path.exists():
        write_text_atomic(path=updates_path, content="")
    updates_journal = user_updates_journal_path(
        repo_root=repo_root, session_id=session_id
    )
    updates_journal.parent.mkdir(parents=True, exist_ok=True)
    updates_journal.touch(exist_ok=True)
    project_state_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    finished = finished_path(repo_root=repo_root, session_id=session_id)
    if not finished.exists():
        write_text_atomic(path=finished, content="# Finished Work\n")
    for durable_dir in (
        eval_checks_dir_path(repo_root=repo_root, session_id=session_id),
        eval_readiness_dir_path(repo_root=repo_root, session_id=session_id),
        eval_receipts_dir_path(repo_root=repo_root, session_id=session_id),
        child_outcomes_dir_path(repo_root=repo_root, session_id=session_id),
        parent_acceptance_dir_path(repo_root=repo_root, session_id=session_id),
        git_receipts_dir_path(repo_root=repo_root, session_id=session_id),
        delivery_receipts_dir_path(repo_root=repo_root, session_id=session_id),
        control_rejected_dir_path(repo_root=repo_root, session_id=session_id),
        protocol_failures_dir_path(repo_root=repo_root, session_id=session_id),
        trace_seals_dir_path(repo_root=repo_root, session_id=session_id),
    ):
        durable_dir.mkdir(parents=True, exist_ok=True)
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
    if not root.exists():
        return direct
    # Existing sessions are resolved through the same topology validator used
    # for durable logical references. This prevents the old ``rglob`` helper
    # from silently selecting one of two duplicate IDs or following a
    # contradictory/symlinked session tree. Unknown IDs still return the
    # top-level candidate so callers can probe a dangling crash pointer or
    # initialize a new root session.
    from loopy_loop.references import LogicalReferenceError
    from loopy_loop.references import LogicalReferenceResolver

    try:
        resolver = LogicalReferenceResolver.for_session(
            repo_root=repo_root, session_id=session_id
        )
    except LogicalReferenceError as exc:
        if str(exc) == f"unknown session ID: {session_id}":
            return direct
        raise
    return resolver.current.directory


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


def child_requests_pending_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        child_requests_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_REQUEST_PENDING_DIRNAME
    )


def child_requests_accepted_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        child_requests_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_REQUEST_ACCEPTED_DIRNAME
    )


def child_requests_rejected_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        child_requests_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_REQUEST_REJECTED_DIRNAME
    )


def child_sessions_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id) / CHILDREN_DIRNAME
    )


def session_goal_path(*, repo_root: Path, session_id: str) -> Path:
    return session_dir_path(repo_root=repo_root, session_id=session_id) / GOAL_FILENAME


def goal_contract_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / GOAL_CONTRACT_FILENAME
    )


def workflow_contract_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / WORKFLOW_CONTRACT_FILENAME
    )


def latest_top_level_state_path(*, repo_root: Path) -> Path | None:
    """Return the newest state owned by a valid top-level session identity.

    The sessions directory can also contain operator backups or incomplete
    crash debris.  A directory is authoritative only when its manifest binds
    its safe session ID to the directory name and identifies it as a root.
    Legacy manifests have no ``schema_version`` or v2 topology fields, so this
    intentionally relies only on the identity fields present since v1.
    """

    root = sessions_root_path(repo_root=repo_root)
    if not root.exists():
        return None
    candidates = [
        path / STATE_FILENAME
        for path in root.iterdir()
        if _is_valid_top_level_session_directory(path=path)
        and not (path / STATE_FILENAME).is_symlink()
        and (path / STATE_FILENAME).is_file()
    ]
    return sorted(candidates)[-1] if candidates else None


def _is_valid_top_level_session_directory(*, path: Path) -> bool:
    """Return whether ``path`` carries a valid v1-compatible root identity."""

    if path.is_symlink() or not path.is_dir():
        return False
    parent_manifest_path = path / PARENT_FILENAME
    if parent_manifest_path.exists() or parent_manifest_path.is_symlink():
        return False
    manifest_path = path / SESSION_METADATA_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    session_id = payload.get("session_id")
    if (
        not isinstance(session_id, str)
        or SAFE_DURABLE_ID_PATTERN.fullmatch(session_id) is None
        or session_id != path.name
    ):
        return False
    schema_version = payload.get("schema_version", 1)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        return False
    parent_session_id = payload.get("parent_session_id")
    if parent_session_id is not None:
        return False
    root_session_id = payload.get("root_session_id")
    if root_session_id is not None and root_session_id != session_id:
        return False
    depth = payload.get("depth")
    if depth is not None and (
        isinstance(depth, bool) or not isinstance(depth, int) or depth != 0
    ):
        return False
    if schema_version >= 2:
        return root_session_id == session_id and depth == 0
    return True


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


def eval_readiness_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_READINESS_DIRNAME
    )


def eval_receipts_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_RECEIPTS_DIRNAME
    )


def child_outcomes_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_OUTCOMES_DIRNAME
    )


def parent_acceptance_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / PARENT_ACCEPTANCE_DIRNAME
    )


def git_receipts_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / GIT_RECEIPTS_DIRNAME
    )


def delivery_receipts_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / DELIVERY_RECEIPTS_DIRNAME
    )


def control_rejected_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / CONTROL_REJECTED_DIRNAME
    )


def protocol_failures_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / PROTOCOL_FAILURES_DIRNAME
    )


def trace_seals_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / TRACE_SEALS_DIRNAME
    )


def trace_seal_receipt_path(
    *, repo_root: Path, session_id: str, attempt_id: str
) -> Path:
    return trace_seals_dir_path(repo_root=repo_root, session_id=session_id) / (
        f"{attempt_id}.json"
    )


def user_updates_journal_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / INPUTS_DIRNAME
        / USER_UPDATES_JOURNAL_FILENAME
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


def assignment_path(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    attempt_id: str,
) -> Path:
    return (
        workflow_snapshot_dir_path(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
            attempt_id=attempt_id,
        )
        / ASSIGNMENT_FILENAME
    )


def workflow_snapshot_dir_path(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    attempt_id: str,
) -> Path:
    return (
        ensure_iteration_dir(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
        )
        / WORKFLOW_SNAPSHOT_DIRNAME
        / attempt_id
    )


def trace_ref_path(
    *, repo_root: Path, session_id: str, iteration: int, workflow_id: str
) -> Path:
    return (
        ensure_iteration_dir(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
        )
        / TRACE_REF_FILENAME
    )


def traces_root_path(*, repo_root: Path) -> Path:
    return repo_root / LOOPY_DIRNAME / TRACES_DIRNAME


def attempt_trace_dir_path(
    *, repo_root: Path, root_session_id: str, session_id: str, attempt_id: str
) -> Path:
    return (
        traces_root_path(repo_root=repo_root)
        / root_session_id
        / "sessions"
        / session_id
        / "attempts"
        / attempt_id
    )


def trace_finalization_outbox_dir_path(*, repo_root: Path) -> Path:
    return repo_root / LOOPY_DIRNAME / TRACE_FINALIZATION_OUTBOX_DIRNAME


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _read_json_mapping(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
