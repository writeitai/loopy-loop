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
WORKFLOW_ROSTER_FILENAME = "workflow_roster.json"
HARNESS_CAPABILITY_ROSTER_FILENAME = "harness_capability_roster.json"
SESSION_OUTCOME_FILENAME = "session_outcome.json"
EVENTS_FILENAME = "events.jsonl"
PROJECT_STATE_DIRNAME = "project_state"
PLAN_FILENAME = "plan.md"
TASKS_DIRNAME = "tasks"
CURRENT_STATE_FILENAME = "current_state.md"
DECISIONS_DIRNAME = "decisions"
EVAL_STATE_FILENAME = "eval_state.md"
HANDOFF_FILENAME = "handoff.json"
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
EVAL_REQUEST_FILENAME = "eval_request.md"
PATHS_FILENAME = "paths.json"
WORKER_SESSIONS_FILENAME = "worker_sessions.json"
ASSIGNMENT_FILENAME = "assignment.json"
WORKFLOW_SNAPSHOT_DIRNAME = "workflow_snapshot"
SCHEDULER_VIEW_FILENAME = "scheduler_view.json"
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
    """Crash-safely serialize one JSON document."""

    write_text_atomic(path=path, content=json.dumps(payload, indent=2))


def _write_json_if_absent_or_equal(*, path: Path, payload: object) -> None:
    """Create an immutable JSON artifact or verify its existing value."""

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"immutable JSON artifact is unreadable: {path}") from exc
        if existing != payload:
            raise ValueError(
                f"immutable JSON artifact contradicts existing value: {path}"
            )
        return
    write_json_atomic(path=path, payload=payload)


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
    workflow_roster_payload: dict[str, object] | None = None,
    harness_capability_roster_payload: dict[str, object] | None = None,
    session_protocol_version: int | None = None,
    schema_version: int = 1,
) -> Path:
    """Create or idempotently materialize a durable session directory.

    ``schema_version`` selects the engine's persisted state shape, while
    ``session_protocol_version`` selects the agent-facing contract. When the
    latter is omitted, historical callers retain their matching v1/v2 layout.
    """

    effective_protocol_version = (
        schema_version if session_protocol_version is None else session_protocol_version
    )
    if effective_protocol_version not in {1, 2, 3}:
        raise ValueError("session_protocol_version must be one of 1, 2, or 3")
    if effective_protocol_version >= 2 and schema_version < 2:
        raise ValueError("protocol v2/v3 sessions require state schema v2")

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
                path=parent_session_dir / SESSION_METADATA_FILENAME
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
        goal_contract_hash = file_sha256(path=goal_contract_path_value)
    workflow_contract_hash: str | None = None
    if workflow_contract is not None:
        contract_path = session_dir / WORKFLOW_CONTRACT_FILENAME
        if not contract_path.exists():
            write_json_atomic(path=contract_path, payload=workflow_contract)
        if schema_version >= 2:
            workflow_contract_hash = file_sha256(path=contract_path)
    if workflow_roster_payload is not None:
        _write_json_if_absent_or_equal(
            path=session_dir / WORKFLOW_ROSTER_FILENAME, payload=workflow_roster_payload
        )
    if harness_capability_roster_payload is not None:
        capability_roster_session_id = (
            session_id if parent_session_id is None else str(root_session_id)
        )
        _write_json_if_absent_or_equal(
            path=(
                session_dir
                if capability_roster_session_id == session_id
                else session_dir_path(
                    repo_root=repo_root, session_id=capability_roster_session_id
                )
            )
            / HARNESS_CAPABILITY_ROSTER_FILENAME,
            payload=harness_capability_roster_payload,
        )

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
    if effective_protocol_version == 3:
        _create_v3_semantic_spine(
            repo_root=repo_root,
            session_id=session_id,
            goal=goal,
            goal_hash=goal_hash,
            created_at=created_at,
        )
    for durable_dir in (
        eval_checks_dir_path(repo_root=repo_root, session_id=session_id),
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
    if effective_protocol_version < 3:
        eval_readiness_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
            parents=True, exist_ok=True
        )
    harness_outputs_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    iterations_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    return session_dir


def _create_v3_semantic_spine(
    *, repo_root: Path, session_id: str, goal: str, goal_hash: str, created_at: str
) -> None:
    """Materialize the compact protocol-v3 state skeleton without interpreting it."""

    tasks_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    decisions_dir_path(repo_root=repo_root, session_id=session_id).mkdir(
        parents=True, exist_ok=True
    )
    goal_summary = " ".join(goal.strip().splitlines()) or goal_hash
    plan = layer_plan_path(repo_root=repo_root, session_id=session_id)
    if not plan.exists():
        write_text_atomic(
            path=plan,
            content=(
                "# Layer Plan\n\n"
                "- Revision: 0\n"
                f"- Layer goal: {goal_summary}\n"
                "- Current milestone: none\n\n"
                "## Outcomes\n\n"
                "| ID | Outcome | Status | Dependencies | Evidence |\n"
                "| --- | --- | --- | --- | --- |\n\n"
                "## Active selection\n\n"
                "No leaf or child outcome is selected yet.\n\n"
                "## Risks, assumptions, and replanning triggers\n\n"
                "None recorded yet.\n"
            ),
        )
    current_state = current_state_path(repo_root=repo_root, session_id=session_id)
    if not current_state.exists():
        write_text_atomic(
            path=current_state,
            content=(
                "# Current State\n\n"
                "- Current outcome: none selected\n"
                "- Active leaf or child: none\n"
                "- Blockers: none recorded\n"
                "- Risks: none recorded\n"
                "- Next decision: initialize the layer plan\n"
            ),
        )
    eval_state = eval_state_path(repo_root=repo_root, session_id=session_id)
    if not eval_state.exists():
        write_text_atomic(
            path=eval_state,
            content=(
                "# Evaluation State\n\n"
                "No evaluation has been created or run for this layer.\n"
            ),
        )
    handoff = handoff_path(repo_root=repo_root, session_id=session_id)
    if not handoff.exists():
        write_json_atomic(
            path=handoff,
            payload={
                "schema_version": 1,
                "session_id": session_id,
                "goal_sha256": goal_hash,
                "revision": 0,
                "producer": None,
                "summary": "No orchestrator handoff has been published yet.",
                "accepted_outcomes": [],
                "open_work": [],
                "risks": [],
                "decision_refs": [],
                "evidence_refs": [],
                "delivery_refs": [],
                "eval_refs": [],
                "updated_at": created_at,
            },
        )


def sessions_root_path(*, repo_root: Path) -> Path:
    """Return the root containing all durable session trees."""

    return repo_root / LOOPY_DIRNAME / SESSIONS_DIRNAME


def session_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Resolve a session ID through the validated recursive topology."""

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
    """Return the directory for child requests awaiting dispatch."""

    return (
        child_requests_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_REQUEST_PENDING_DIRNAME
    )


def child_requests_accepted_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for child requests accepted for dispatch."""

    return (
        child_requests_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_REQUEST_ACCEPTED_DIRNAME
    )


def child_requests_rejected_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for invalid or conflicting child requests."""

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
    """Return the immutable goal-contract path for a session."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / GOAL_CONTRACT_FILENAME
    )


def workflow_contract_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the agent-visible workflow-contract projection path."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / WORKFLOW_CONTRACT_FILENAME
    )


def workflow_roster_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the session-frozen scheduled-role roster path."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / WORKFLOW_ROSTER_FILENAME
    )


def harness_capability_roster_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the tree-root harness capability roster path."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / HARNESS_CAPABILITY_ROSTER_FILENAME
    )


def session_outcome_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the topology-neutral terminal session outcome path."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / SESSION_OUTCOME_FILENAME
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
    """Return the compact semantic-state directory for one layer."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / PROJECT_STATE_DIRNAME
    )


def eval_request_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the orchestrator's requested-eval marker path for a session.

    Its mere existence is the coordination contract for `run_when_requested`
    scheduling: the orchestrator writes it to request an eval iteration and the
    eval role archives it once served.
    """

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_REQUEST_FILENAME
    )


def layer_plan_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the layer orchestrator's canonical plan path."""

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / PLAN_FILENAME
    )


def tasks_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the stable per-task semantic ledger directory."""

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / TASKS_DIRNAME
    )


def current_state_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the layer's compact resumption-state path."""

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / CURRENT_STATE_FILENAME
    )


def decisions_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for durable layer decisions."""

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / DECISIONS_DIRNAME
    )


def eval_state_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the layer's optional evaluation-evidence index path."""

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_STATE_FILENAME
    )


def handoff_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the rolling semantic handoff path for one layer."""

    return (
        project_state_dir_path(repo_root=repo_root, session_id=session_id)
        / HANDOFF_FILENAME
    )


def inputs_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the session-local immutable and append-only input directory."""

    return session_dir_path(repo_root=repo_root, session_id=session_id) / INPUTS_DIRNAME


def eval_checks_dir_path(*, repo_root: Path, session_id: str) -> Path:
    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_CHECKS_DIRNAME
    )


def eval_readiness_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for eval-readiness declarations."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_READINESS_DIRNAME
    )


def eval_receipts_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for validated evaluation receipts."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / EVAL_RECEIPTS_DIRNAME
    )


def child_outcomes_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for outcomes reported by child sessions."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / CHILD_OUTCOMES_DIRNAME
    )


def parent_acceptance_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for a parent's child-outcome decisions."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / PARENT_ACCEPTANCE_DIRNAME
    )


def git_receipts_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for session-bound Git evidence receipts."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / GIT_RECEIPTS_DIRNAME
    )


def delivery_receipts_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for branch and PR delivery receipts."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / DELIVERY_RECEIPTS_DIRNAME
    )


def control_rejected_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the archive for rejected control signals."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / CONTROL_REJECTED_DIRNAME
    )


def protocol_failures_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the archive for repairable protocol failures."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / PROTOCOL_FAILURES_DIRNAME
    )


def trace_seals_dir_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the directory for committed attempt trace seals."""

    return (
        session_dir_path(repo_root=repo_root, session_id=session_id)
        / TRACE_SEALS_DIRNAME
    )


def trace_seal_receipt_path(
    *, repo_root: Path, session_id: str, attempt_id: str
) -> Path:
    """Return the trace-seal receipt path for one attempt."""

    return trace_seals_dir_path(repo_root=repo_root, session_id=session_id) / (
        f"{attempt_id}.json"
    )


def user_updates_journal_path(*, repo_root: Path, session_id: str) -> Path:
    """Return the append-only user-input journal path for a session."""

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
    """Return the frozen assignment-envelope path for one attempt."""

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


def scheduler_view_path(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    attempt_id: str,
) -> Path:
    """Return the attempt-frozen conditional scheduler-view path."""

    return (
        workflow_snapshot_dir_path(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
            attempt_id=attempt_id,
        )
        / SCHEDULER_VIEW_FILENAME
    )


def workflow_snapshot_dir_path(
    *,
    repo_root: Path,
    session_id: str,
    iteration: int,
    workflow_id: str,
    attempt_id: str,
) -> Path:
    """Return the immutable workflow snapshot directory for one attempt."""

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
    """Return the iteration's logical reference to its raw trace."""

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
    """Return the repository-local root for ignored raw traces."""

    return repo_root / LOOPY_DIRNAME / TRACES_DIRNAME


def attempt_trace_dir_path(
    *, repo_root: Path, root_session_id: str, session_id: str, attempt_id: str
) -> Path:
    """Return the raw trace directory owned by a specific attempt."""

    return (
        traces_root_path(repo_root=repo_root)
        / root_session_id
        / "sessions"
        / session_id
        / "attempts"
        / attempt_id
    )


def trace_finalization_outbox_dir_path(*, repo_root: Path) -> Path:
    """Return the crash-recovery outbox for pending trace finalization."""

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
    """Return a file's SHA-256 digest with its algorithm prefix."""

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _read_json_mapping(path: Path) -> dict[str, object]:
    """Read a JSON object, returning an empty mapping on invalid input."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
