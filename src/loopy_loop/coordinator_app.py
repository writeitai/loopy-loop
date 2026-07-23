from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import socket
import threading
from typing import Any
from typing import Literal
from typing import TypeVar
import uuid

from eval_banana.runner import compute_check_definition_sha256
from fastapi import FastAPI
from fastapi import HTTPException
from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel
from pydantic import ValidationError
import yaml

from loopy_loop.assignments import AssignmentContractError
from loopy_loop.assignments import build_attempt_assignment
from loopy_loop.assignments import ensure_repository_identity
from loopy_loop.assignments import materialize_workflow_snapshot
from loopy_loop.assignments import verify_workflow_snapshot
from loopy_loop.assignments import write_attempt_assignment
from loopy_loop.config import build_harness_capability_roster
from loopy_loop.config import ConfigError
from loopy_loop.config import derive_full_goal_hash
from loopy_loop.config import estimate_cost_usd
from loopy_loop.config import PreflightResult
from loopy_loop.config import run_preflight
from loopy_loop.config import WorkflowDefinition
from loopy_loop.events import append_events
from loopy_loop.git_evidence import capture_git_evidence
from loopy_loop.git_evidence import GitEvidenceError
from loopy_loop.models import AcceptedEvalReceiptSeal
from loopy_loop.models import AcceptedHandoffSnapshot
from loopy_loop.models import AcceptedTerminalControlSnapshot
from loopy_loop.models import ArtifactInputRef
from loopy_loop.models import ChildSessionRecord
from loopy_loop.models import ChildSessionRequest
from loopy_loop.models import ControlSignal
from loopy_loop.models import CurrentTask
from loopy_loop.models import EvalReceipt
from loopy_loop.models import FinishedRequest
from loopy_loop.models import GoalCheckSignal
from loopy_loop.models import HistoryEntry
from loopy_loop.models import IterationResult
from loopy_loop.models import LayerHandoff
from loopy_loop.models import LoopState
from loopy_loop.models import OutcomeArtifactRef
from loopy_loop.models import OutcomeFallbackSummary
from loopy_loop.models import OutcomeHandoff
from loopy_loop.models import RegisterRequest
from loopy_loop.models import REQUIRED_V2_WORKER_CAPABILITIES
from loopy_loop.models import REQUIRED_V3_WORKER_CAPABILITIES
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import SAFE_DURABLE_ID_PATTERN
from loopy_loop.models import SchedulerForecast
from loopy_loop.models import SchedulerView
from loopy_loop.models import SessionOutcome
from loopy_loop.models import SessionUsageTotals
from loopy_loop.models import SignalProducer
from loopy_loop.models import STOP_ACTION
from loopy_loop.models import TaskResponse
from loopy_loop.models import utc_now
from loopy_loop.models import WorkerIdentity
from loopy_loop.models import WorkflowRoster
from loopy_loop.models import WorkflowRosterRole
from loopy_loop.models import WorkflowSetContract
from loopy_loop.recovery import recover_interrupted_iteration
from loopy_loop.recovery import RecoveryIncompleteError
from loopy_loop.recovery import RecoveryOutcome
from loopy_loop.recovery import RecoveryRefusedError
from loopy_loop.references import LogicalReferenceError
from loopy_loop.references import LogicalReferenceResolver
from loopy_loop.references import resolve_logical_reference
from loopy_loop.scheduler import choose_next_workflow
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import attempt_trace_dir_path
from loopy_loop.sessions import child_outcomes_dir_path
from loopy_loop.sessions import child_requests_accepted_dir_path
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import child_requests_pending_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import control_rejected_dir_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import delivery_receipts_dir_path
from loopy_loop.sessions import eval_request_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import git_receipt_path
from loopy_loop.sessions import git_receipt_ref
from loopy_loop.sessions import git_receipts_dir_path
from loopy_loop.sessions import goal_check_path
from loopy_loop.sessions import goal_contract_path
from loopy_loop.sessions import handoff_path
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.sessions import preflight_reload_request_path
from loopy_loop.sessions import protocol_failures_dir_path
from loopy_loop.sessions import raw_attempt_dir_path
from loopy_loop.sessions import result_path
from loopy_loop.sessions import scheduler_view_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import session_layout
from loopy_loop.sessions import SESSION_LAYOUT_FOLDED
from loopy_loop.sessions import SESSION_LAYOUT_MIRROR
from loopy_loop.sessions import session_outcome_path
from loopy_loop.sessions import state_path
from loopy_loop.sessions import trace_finalization_outbox_dir_path
from loopy_loop.sessions import trace_seal_receipt_path
from loopy_loop.sessions import workflow_contract_path
from loopy_loop.sessions import workflow_roster_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.sessions import write_text_atomic
from loopy_loop.state_store import AttemptArtifactInvariantError
from loopy_loop.state_store import StateInvariantError
from loopy_loop.state_store import StateStore
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import read_trace_manifest
from loopy_loop.tracing import seal_attempt_trace
from loopy_loop.tracing import trace_write_json
from loopy_loop.tracing import TraceError
from loopy_loop.tracing import update_trace_manifest
from loopy_loop.tracing import verify_trace_integrity
from loopy_loop.worker_identity import is_worker_alive

logger = logging.getLogger(__name__)

# Coordinator-operational settings that must never enter the wire config
# snapshot: released workers validate the snapshot with extra="forbid", so any
# new response field would crash them at parse time (protocol compatibility).
_COORDINATOR_ONLY_FIELDS = {
    "recovery_policy",
    "recovery_drain_timeout_s",
    "workflow_consecutive_failures_cap",
    "max_cost_usd",
    "model_prices",
    # Tier declarations resolve at config load into the existing snapshot
    # fields (agent models/efforts + system prompt extension); the raw
    # declarations themselves stay coordinator-side.
    "model_tiers",
    "default_tier",
}

# These root settings are operational coordinator policy, not part of a
# session's frozen worker config or model/capability roster. An explicit
# `loopy reload` may refresh only these fields plus workflow prompt text.
_HOT_RELOADABLE_ROOT_FIELDS = {
    "recovery_policy",
    "recovery_drain_timeout_s",
    "workflow_consecutive_failures_cap",
    "max_cost_usd",
    "model_prices",
}

_LIVE_CHILD_STATUSES = frozenset({"dispatching", "running"})
_TERMINAL_CHILD_STATUSES = frozenset({"stopped", "goal_met", "failed", "max_turns"})
_VALID_CHILD_STATUSES = (
    _LIVE_CHILD_STATUSES | _TERMINAL_CHILD_STATUSES | {"failed_dispatch"}
)
_CONTROL_REPAIR_PLACEHOLDER_KIND = "invalid_control_archived"


def _model_payload(*, model: BaseModel) -> dict[str, Any]:
    """Return the exact JSON-mode payload used for durable exchange binding."""

    return model.model_dump(mode="json")


def _payload_sha256(*, payload: object) -> str:
    """Hash one canonical JSON payload with an explicit SHA-256 prefix."""

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _model_sha256(*, model: BaseModel) -> str:
    """Hash an exact model payload for idempotent request/response binding."""

    return _payload_sha256(payload=_model_payload(model=model))


def session_tree_usage_totals(
    *,
    repo_root: Path,
    state: LoopState,
    children_reader: Callable[..., dict[str, Any]] | None = None,
) -> SessionUsageTotals:
    """The session's own ledger plus its finalized children's recorded totals.

    A RUNNING child's spend is not visible here until it finalizes — the
    parent is suspended while a child runs, so its own budget checks are not
    executing anyway; the child enforces the budget against its own subtree
    in the meantime.
    """
    totals = state.usage_totals.model_copy(deep=True)
    read_children = children_reader or _read_children_payload
    payload = read_children(
        path=children_path(repo_root=repo_root, session_id=state.active_session_id)
    )
    for record in payload["children"]:
        usage = record.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            child_totals = SessionUsageTotals.model_validate(usage)
        except ValidationError:
            continue
        totals.prompt_tokens += child_totals.prompt_tokens
        totals.completion_tokens += child_totals.completion_tokens
        totals.iterations_with_usage += child_totals.iterations_with_usage
        totals.iterations_without_usage += child_totals.iterations_without_usage
        totals.duration_s += child_totals.duration_s
    return totals


def root_tree_usage_totals(
    *,
    repo_root: Path,
    state: LoopState,
    children_reader: Callable[..., dict[str, Any]] | None = None,
) -> SessionUsageTotals:
    """Known usage across the whole root tree, including the active path.

    Finalized child records carry their subtree total. A currently active
    child is read from its state instead, so the same spend is never counted
    from both a record and a live state.
    """
    root_id = state.root_session_id or state.active_session_id
    root_state = (
        state
        if state.active_session_id == root_id
        else StateStore(
            repo_root=repo_root,
            state_path=state_path(repo_root=repo_root, session_id=root_id),
        ).read_state()
    )
    if root_state is None:
        return state.usage_totals.model_copy(deep=True)
    seen: set[str] = set()

    read_children = children_reader or _read_children_payload

    def accumulate(node: LoopState) -> SessionUsageTotals:
        """Accumulate one session subtree without double-counting live children."""

        if node.active_session_id in seen:
            raise ChildLedgerError(
                f"cycle detected in usage projection at {node.active_session_id}"
            )
        seen.add(node.active_session_id)
        total = node.usage_totals.model_copy(deep=True)
        payload = read_children(
            path=children_path(repo_root=repo_root, session_id=node.active_session_id)
        )
        if node.active_child_session_id is not None:
            pointer_matches = [
                record
                for record in payload["children"]
                if record.get("session_id") == node.active_child_session_id
            ]
            if len(pointer_matches) != 1:
                raise ChildLedgerError(
                    f"usage projection requires exactly one edge for active child "
                    f"{node.active_child_session_id}; found {len(pointer_matches)}"
                )
        for record in payload["children"]:
            child_id = record.get("session_id")
            if child_id == node.active_child_session_id:
                # This projection runs inside the active session's mutate
                # callback. Re-reading that exact state through a new
                # StateStore would re-acquire the same file lock and deadlock.
                child = (
                    state
                    if child_id == state.active_session_id
                    else StateStore(
                        repo_root=repo_root,
                        state_path=state_path(repo_root=repo_root, session_id=child_id),
                    ).read_state()
                )
                if child is None:
                    raise ChildLedgerError(
                        f"active child {child_id} has no state for usage projection"
                    )
                _add_usage(total=total, addition=accumulate(node=child))
                continue
            usage = record.get("usage")
            if isinstance(usage, dict):
                try:
                    _add_usage(
                        total=total, addition=SessionUsageTotals.model_validate(usage)
                    )
                except ValidationError:
                    total.iterations_without_usage += 1
        return total

    return accumulate(node=root_state)


def _add_usage(*, total: SessionUsageTotals, addition: SessionUsageTotals) -> None:
    """Add one usage subtotal to an existing aggregate in place."""

    total.prompt_tokens += addition.prompt_tokens
    total.completion_tokens += addition.completion_tokens
    total.iterations_with_usage += addition.iterations_with_usage
    total.iterations_without_usage += addition.iterations_without_usage
    total.duration_s += addition.duration_s


class WorkerBusyError(RuntimeError):
    """The current task's worker is verifiably still alive — do not reclaim."""


class WorkerUpgradeRequired(RuntimeError):
    """The active session contract cannot be served to this worker version."""


def _serialized_json_sha256(*, payload: object) -> str:
    """Hash bytes produced by the durable indented-JSON writer."""

    encoded = json.dumps(payload, indent=2).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _build_workflow_roster(
    *, session_id: str, preflight: PreflightResult, created_at: datetime
) -> WorkflowRoster:
    """Derive one inspectable role roster from a frozen workflow set."""

    contract = preflight.workflow_contract
    orchestration = contract.orchestration
    roles: list[WorkflowRosterRole] = []
    for workflow in preflight.workflows:
        authorities: list[str] = []
        expected_outputs: list[str] = []
        if orchestration is not None:
            owner_fields = {
                "completion": orchestration.completion_role,
                "plan": orchestration.plan_owner,
                "handoff": orchestration.handoff_owner,
                "task_acceptance": orchestration.task_acceptance_owner,
                "child_acceptance": orchestration.child_acceptance_owner,
            }
            authorities.extend(
                label for label, owner in owner_fields.items() if owner == workflow.id
            )
            if workflow.id == orchestration.plan_owner:
                expected_outputs.extend(
                    [
                        "project_state/plan.md",
                        "project_state/current_state.md",
                        "project_state/finished.md",
                    ]
                )
            if workflow.id == orchestration.handoff_owner:
                expected_outputs.append("project_state/handoff.json")
        if workflow.id in contract.check_author_roles:
            authorities.append("eval_check_author")
            expected_outputs.extend(["eval_checks/", "project_state/eval_state.md"])
        if workflow.id in contract.check_runner_roles:
            authorities.append("eval_check_runner")
            # v3 evaluation is agent-authored advisory evidence
            # (project_state/eval_results.md, written by the check-runner role);
            # the engine no longer advertises the retired eval_receipts/ output.
            # Sessions whose frozen roster predates this keep their own
            # advertisement, and the receipt-sealing machinery below stays
            # contract-gated so their receipts are still accepted.
            expected_outputs.append("project_state/eval_state.md")
        if workflow.id in contract.terminal_blocker_reporting_roles:
            authorities.append("terminal_blocker_reporting")
        run_after = (
            workflow.run_after_successes.model_dump(mode="json")
            if workflow.run_after_successes is not None
            else None
        )
        role = contract.roles.get(workflow.id)
        roles.append(
            WorkflowRosterRole(
                workflow_id=workflow.id,
                responsibility=(
                    role.responsibility if role is not None else workflow.description
                ),
                cadence={
                    "enabled": workflow.enabled,
                    "priority": workflow.priority,
                    "run_every": workflow.run_every,
                    "run_on_start": workflow.run_on_start,
                    "not_before_iteration": workflow.not_before_iteration,
                    "must_follow": workflow.must_follow,
                    "run_after_successes": run_after,
                },
                expected_outputs=list(dict.fromkeys(expected_outputs)),
                authorities=list(dict.fromkeys(authorities)),
            )
        )
    contract_payload = contract.model_dump(mode="json")
    return WorkflowRoster(
        session_id=session_id,
        workflow_contract_sha256=_serialized_json_sha256(payload=contract_payload),
        created_at=created_at,
        completion_role=contract.completion_role,
        roles=roles,
    )


def create_coordinator_app(
    *,
    repo_root: Path,
    resume: bool,
    workflow_set: str | None = None,
    goal_file: Path | None = None,
) -> FastAPI:
    """Create the HTTP coordinator bound to one repository checkout."""

    preflight = run_preflight(
        repo_root=repo_root, workflow_set=workflow_set, goal_file=goal_file
    )
    store = StateStore(repo_root=repo_root)
    service = CoordinatorService(
        repo_root=repo_root, preflight=preflight, state_store=store, resume=resume
    )
    app = FastAPI()
    app.state.service = service

    @app.post("/register", response_model=TaskResponse)
    def register_worker(request: RegisterRequest | None = None) -> TaskResponse:
        """Validate a worker and return its current or next assignment."""

        # Breaking change (0.3): identity is required. It guarantees every
        # dispatched task has a recorded owner, so liveness verification and
        # the stale-/finished owner check are always possible. Old workers
        # fail fast here with a clear message instead of degrading silently.
        if request is None or request.worker is None:
            raise HTTPException(
                status_code=400,
                detail="worker identity is required; upgrade the worker CLI "
                "(loopy-loop >= 0.3)",
            )
        try:
            return service.register_worker(request=request)
        except WorkerUpgradeRequired as exc:
            raise HTTPException(status_code=426, detail=str(exc)) from exc
        except (WorkerBusyError, RecoveryRefusedError, RecoveryIncompleteError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileLockTimeout as exc:
            raise HTTPException(
                status_code=503,
                detail="coordinator state is briefly locked (crash recovery "
                "or a concurrent request); retry shortly",
            ) from exc

    @app.post("/finished", response_model=TaskResponse)
    def finish_assignment(request: FinishedRequest) -> TaskResponse:
        """Accept one attempt completion and advance its active session."""

        try:
            return service.finish_assignment(request=request)
        except WorkerUpgradeRequired as exc:
            raise HTTPException(status_code=426, detail=str(exc)) from exc
        except WorkerBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileLockTimeout as exc:
            raise HTTPException(
                status_code=503,
                detail="coordinator state is briefly locked (crash recovery "
                "or a concurrent request); retry shortly",
            ) from exc

    return app


class CoordinatorService:
    def __init__(
        self,
        *,
        repo_root: Path,
        preflight: PreflightResult,
        state_store: StateStore,
        resume: bool,
    ) -> None:
        """Initialize scheduling state and recover pending trace finalizations."""

        self.repo_root = repo_root
        self.repo_root = repo_root.resolve()
        self.repository_identity = ensure_repository_identity(repo_root=self.repo_root)
        self._worker_contracts: dict[tuple[str, int, str | None], int] = {}
        self.preflight = preflight
        self.preflights: dict[str, PreflightResult] = {
            preflight.workflow_set: preflight
        }
        # A request already present at startup is already reflected by the
        # startup preflight. Only a later request invalidates this cache.
        self._preflight_reload_request_id = self._read_preflight_reload_request_id()
        self.state_store = state_store
        # Serializes cross-store transitions (parent<->child handoff and the
        # phase-B commits): FastAPI runs sync endpoints in a threadpool, so
        # overlapping requests could otherwise race on self.state_store and
        # the multi-file dispatch transition (C1). Reentrant because a child's
        # terminal /finished resumes the parent by calling register_worker()
        # on the same thread. Phase-A recovery (the long drain) deliberately
        # runs OUTSIDE this lock on the initial register path.
        self._transition_lock = threading.RLock()
        # Buffered (session_id, type, payload) events; appended to the
        # session's events.jsonl AFTER the producing mutation commits
        # (best-effort — see events.py). Guarded by _transition_lock on
        # every request path that touches it.
        self._pending_events: list[tuple[str, str, dict]] = []
        self._prepare_state(resume=resume)
        self._retry_trace_finalizations(allow_unavailable_responses=True)
        self._flush_pending_events()

    def register_worker(
        self, *, request: RegisterRequest | None = None
    ) -> TaskResponse:
        """Recover interrupted work and dispatch the next eligible assignment."""

        if request is not None:
            self._validate_worker_handshake(request=request)
        caller = request.worker if request is not None else None
        # Two-phase recovery: the potentially long drain of an interrupted
        # task's agent processes (up to recovery_drain_timeout_s) runs in phase A,
        # OUTSIDE the state lock, so `loopy status`/`stop` and /finished stay
        # responsive. Phase B re-validates under the lock and retries from
        # phase A when the state moved in between.
        recovered_completion_traces: list[FinishedRequest] = []
        for _ in range(3):
            recovery = self._plan_orphan_recovery()
            selected_response: TaskResponse | None = None
            with self._transition_lock:
                response = self._register_attempt(
                    caller=caller,
                    recovery=recovery,
                    recovered_completion_traces=recovered_completion_traces,
                )
                if response is not None and response.action == STOP_ACTION:
                    terminal_state = self.state_store.read_state()
                    if (
                        terminal_state is not None
                        and self._protocol_version_for_state(state=terminal_state) >= 3
                        and self.state_store.is_terminal_state(state=terminal_state)
                    ):
                        self._ensure_session_outcome(state=terminal_state)
                    # Recovery can make the ACTIVE CHILD terminal (an
                    # abandoned iteration tripping the failure cap or
                    # max_turns). Without this, the parent's pointer and
                    # children.json stay pointing at a finished child until a
                    # coordinator restart, and every register keeps returning
                    # the child's stop instead of resuming the parent — the
                    # exact finalize/resume step /finished already performs.
                    parent_response = self._resume_parent_if_active_child_completed(
                        caller=caller,
                        recovered_completion_traces=recovered_completion_traces,
                    )
                    if parent_response is not None:
                        selected_response = parent_response
                if selected_response is None:
                    selected_response = response
            if selected_response is not None:
                for recovered_request in recovered_completion_traces:
                    if recovered_request.assignment_sha256 is not None:
                        try:
                            self._replace_finished_response_binding(
                                request=recovered_request, response=selected_response
                            )
                        except Exception:
                            # The semantic completion already committed. Keep
                            # returning the selected scheduler response, but do
                            # not let an unbound outbox response become sealed
                            # evidence; _completion_is_committed will reject it
                            # until state repair restores this hash binding.
                            logger.warning(
                                "failed to bind recovered completion response for "
                                "attempt %s",
                                recovered_request.attempt_id,
                                exc_info=True,
                            )
                    self._queue_trace_finalization(
                        request=recovered_request,
                        response=selected_response,
                        error=None,
                    )
                    finalized = self._finalize_completion_trace(
                        request=recovered_request, response=selected_response
                    )
                    if finalized and self._refresh_terminal_child_trace_projection(
                        request=recovered_request
                    ):
                        self._clear_trace_finalization(request=recovered_request)
                return selected_response
        raise WorkerBusyError(
            "crash recovery is contended (state changed repeatedly while "
            "recovering); retry shortly"
        )

    def _validate_worker_handshake(self, *, request: RegisterRequest) -> None:
        """Validate worker protocol, capability, and repository identity binding."""

        top_state = StateStore(repo_root=self.repo_root).read_state()
        required_version = (
            max(
                2 if top_state.schema_version >= 2 else 1,
                self._protocol_version_for_state(state=top_state),
            )
            if top_state is not None
            else 1
        )
        version = request.worker_protocol_version or 1
        if version < required_version:
            raise WorkerUpgradeRequired(
                f"this session tree requires worker protocol v{required_version}; "
                "upgrade the worker CLI"
            )
        required = (
            REQUIRED_V3_WORKER_CAPABILITIES
            if required_version >= 3
            else REQUIRED_V2_WORKER_CAPABILITIES
        )
        missing = required - set(request.capabilities)
        if required_version >= 2 and missing:
            raise WorkerUpgradeRequired(
                "worker is missing required protocol capabilities: "
                + ", ".join(sorted(missing))
            )
        if required_version >= 2 and (
            request.repo_root is None or request.repository_id is None
        ):
            raise WorkerUpgradeRequired(
                "this session tree requires repository "
                "binding (repo_root and repository_id)"
            )
        if request.repo_root is not None:
            try:
                worker_root = Path(request.repo_root).resolve()
            except OSError as exc:
                raise WorkerBusyError(
                    f"worker repository path is invalid: {exc}"
                ) from exc
            if worker_root != self.repo_root:
                raise WorkerBusyError(
                    f"worker is in the wrong checkout: expected {self.repo_root}, "
                    f"got {worker_root}"
                )
        expected_repo_id = str(self.repository_identity["repository_id"])
        if (
            request.repository_id is not None
            and request.repository_id != expected_repo_id
        ):
            raise WorkerBusyError(
                "worker repository identity does not match this coordinator"
            )
        if request.worker is not None:
            key = (
                request.worker.hostname,
                request.worker.pid,
                request.worker.starttime,
            )
            self._worker_contracts[key] = (
                required_version
                if version >= required_version
                and not missing
                and request.repo_root is not None
                and request.repository_id is not None
                else 1
            )

    @staticmethod
    def _protocol_version_for_state(*, state: LoopState) -> int:
        """Return the semantic protocol frozen in engine-owned session state."""

        if state.workflow_contract is None:
            return 1
        return state.workflow_contract.session_protocol_version

    @staticmethod
    def _worker_contract_key(worker: WorkerIdentity) -> tuple[str, int, str | None]:
        """Return the process identity key used for worker protocol tracking."""

        return (worker.hostname, worker.pid, worker.starttime)

    def _remember_v2_completion_caller(
        self, *, active: CurrentTask, request: FinishedRequest
    ) -> None:
        """Restore the caller handshake after a coordinator restart.

        A completion of a frozen v2 assignment has already proved the exact
        worker owner, repository, and assignment hash.  Remembering that fact
        before scheduling keeps the response-dispatched next task on v2 even
        when the original /register handshake lived in an earlier process.
        """
        if active.completion_contract_version < 2:
            return
        caller = request.worker
        assert caller is not None  # checked by _validate_finished_binding
        self._worker_contracts[self._worker_contract_key(worker=caller)] = (
            active.completion_contract_version
        )

    def _emit(self, *, session_id: str, event_type: str, payload: dict) -> None:
        """Buffer one session event until its producing mutation commits."""

        self._pending_events.append((session_id, event_type, payload))

    def _flush_pending_events(self) -> None:
        buffered, self._pending_events = self._pending_events, []
        by_session: dict[str, list[tuple[str, dict]]] = {}
        for session_id, event_type, payload in buffered:
            by_session.setdefault(session_id, []).append((event_type, payload))
        for session_id, items in by_session.items():
            try:
                append_events(
                    repo_root=self.repo_root, session_id=session_id, events=items
                )
            except OSError:
                logger.warning(
                    "failed to append events for session %s", session_id, exc_info=True
                )

    def _emit_stop_transition(self, *, state: LoopState, was_terminal: bool) -> None:
        """Emit the session-stopped event only for a new terminal transition."""

        if not was_terminal and self.state_store.is_terminal_state(state=state):
            if (
                self._protocol_version_for_state(state=state) >= 3
                and state.terminal_state_revision is None
            ):
                # StateStore increments the revision only after this mutator
                # returns, so the committed terminal revision is one greater.
                state.terminal_state_revision = state.state_revision + 1
                state.terminal_at = utc_now()
            self._emit(
                session_id=state.active_session_id,
                event_type="session_stopped",
                payload={"status": state.status, "stop_reason": state.stop_reason},
            )

    def _plan_orphan_recovery(self) -> tuple[CurrentTask, RecoveryOutcome] | None:
        """Phase A: inspect state and, if needed, drain/reap OUTSIDE the lock.

        Raises WorkerBusyError when the current task's worker is verifiably
        alive, and RecoveryRefusedError when team-harness's parent guard finds
        the interrupted run's owner still living (both -> HTTP 409).
        """
        state = self.state_store.read_state()
        if state is None or state.current_task is None:
            return None
        orphaned = state.current_task
        self._raise_if_worker_alive(current_task=orphaned)
        if self._read_recoverable_finished_request(current_task=orphaned) is not None:
            # A completed result exists: phase B recovers it under the lock;
            # nothing to reap (the worker finished its harness run).
            return None
        return orphaned, self._recover_orphaned_agents(current_task=orphaned)

    def _register_attempt(
        self,
        *,
        caller: WorkerIdentity | None,
        recovery: tuple[CurrentTask, RecoveryOutcome] | None,
        recovered_completion_traces: list[FinishedRequest] | None = None,
    ) -> TaskResponse | None:
        """Phase B: commit under the state lock; None means retry from phase A."""
        recovered_pending_paths: list[Path] = []
        recovered_requests: list[FinishedRequest] = []

        def mutator(state: LoopState | None) -> tuple[LoopState, TaskResponse | None]:
            """Commit recovery and scheduling against the latest session state."""

            current = _require_state(state=state)
            was_terminal = self.state_store.is_terminal_state(state=current)
            now = utc_now()

            # Step 3: If current_task is set (crash recovery from previous worker crash),
            # recover a locally completed result before calling it abandoned.
            # Important: this increment can itself trigger max_turns — that is correct
            # behaviour. The stop check (step 4) will catch it immediately after.
            if current.current_task is not None:
                orphaned = current.current_task
                # Verified-alive worker: its task is NOT abandoned. Refuse this
                # register instead of dispatching duplicate work (D7). Unknown
                # (None) falls through to the pre-existing recovery behavior.
                self._raise_if_worker_alive(current_task=orphaned)
                recovered = self._read_recoverable_finished_request(
                    current_task=orphaned
                )
                if recovered is not None:
                    (recovered_request, pending_path, frozen_workflow_contract) = (
                        recovered
                    )
                    if pending_path is not None:
                        recovered_pending_paths.append(pending_path)
                    self._queue_trace_finalization(
                        request=recovered_request, response=None, error=None
                    )
                    self._record_finished_task(
                        state=current,
                        active=orphaned,
                        request=recovered_request,
                        now=now,
                        workflow_contract=frozen_workflow_contract,
                    )
                    recovered_requests.append(recovered_request)
                elif recovery is not None and _same_task(a=orphaned, b=recovery[0]):
                    # The interrupted task's agent processes were handled in
                    # phase A (outside the lock); commit the abandonment.
                    outcome = recovery[1]
                    error = "abandoned"
                    if outcome.salvaged:
                        error = f"abandoned_after_{outcome.policy or 'drain'}"
                    self._queue_abandoned_trace_finalization(task=orphaned, error=error)
                    current.history.append(
                        HistoryEntry(
                            iteration=orphaned.iteration,
                            workflow_set=orphaned.workflow_set,
                            workflow_id=orphaned.workflow_id,
                            session_id=orphaned.session_id,
                            success=False,
                            error=error,
                            failure_kind="crash",
                            started_at=orphaned.started_at,
                            finished_at=now,
                            attempt_id=orphaned.attempt_id,
                            assignment_sha256=orphaned.assignment_sha256,
                        )
                    )
                    self._track_workflow_failure_cap(
                        state=current, workflow_id=orphaned.workflow_id, success=False
                    )
                    current.usage_totals.iterations_without_usage += 1
                    self._emit(
                        session_id=current.active_session_id,
                        event_type="iteration_abandoned",
                        payload={
                            "workflow_id": orphaned.workflow_id,
                            "iteration": orphaned.iteration,
                            "attempt_id": orphaned.attempt_id,
                            "error": error,
                        },
                    )
                    current.iteration_count += 1
                    current.current_task = None
                else:
                    # The state moved between phase A and phase B (a stale
                    # /finished landed, or another register recovered first):
                    # do not act on a stale plan — replan.
                    return current, None

            # A crash can publish a child and its live ledger edge before the
            # parent mutation (which clears the producing current_task and
            # stores the stack pointer) commits.  After recovering that parent
            # completion, adopt the already-published child, commit the parent
            # pointer, switch stores, and make register re-plan recovery for
            # the child's own worker rather than replaying it to a new owner.
            if current.active_child_session_id is None:
                adoptable = self._adoptable_child_id(parent_state=current)
                if adoptable is not None:
                    _, child_store, _ = self._validated_child_edge(
                        parent_state=current, child_session_id=adoptable
                    )
                    current.active_child_session_id = adoptable
                    self.state_store = child_store
                    return current, None

            # Step 4+: stop conditions, child dispatch, next workflow.
            response = self._advance(state=current, caller=caller, now=now)
            self._emit_stop_transition(state=current, was_terminal=was_terminal)
            return current, response

        checkpoint = len(self._pending_events)
        try:
            response = self.state_store.mutate(mutator)
        except BaseException:
            # Drop only THIS mutation's events: earlier buffered events (e.g.
            # a child_finished whose children.json write already committed)
            # must survive to the next flush.
            del self._pending_events[checkpoint:]
            raise
        self._flush_pending_events()
        if recovered_completion_traces is not None:
            recovered_completion_traces.extend(recovered_requests)
        for path in recovered_pending_paths:
            path.unlink(missing_ok=True)
        self._retry_trace_finalizations()
        return response

    def _suspended_parent_response(self, *, state: LoopState) -> TaskResponse | None:
        """A parent with a live child must NEVER acquire its own current_task.

        Reachable via overlapping /finished retries: the first call dispatches
        the child and commits the suspended parent; a duplicate retry then
        reads that parent with current_task=None and would otherwise advance
        it — putting a parent task and a child task live simultaneously (the
        C1 race from review). The duplicate gets the child's live task instead
        (idempotent with the first response); a terminal child is finalized so
        the advance continues as the legitimate parent resume.
        """
        child_id = state.active_child_session_id
        if child_id is None:
            return None
        _, child_store, child_state = self._validated_child_edge(
            parent_state=state, child_session_id=child_id
        )
        if child_state is None:
            self._mark_child_record_failed_dispatch(
                parent_session_id=state.active_session_id,
                child_session_id=child_id,
                reason="child state was never written",
            )
            state.active_child_session_id = None
            return None
        child_state = self._hydrate_legacy_state_identity(
            store=child_store, state=child_state
        )
        if child_store.is_terminal_state(state=child_state):
            # Legitimate resume: finalize and let the advance continue.
            self._mark_child_record_complete(
                child_state=child_state, parent_state=state
            )
            state.active_child_session_id = None
            return None
        if child_state.current_task is not None:
            return _build_run_response(
                current_task=child_state.current_task,
                config_snapshot=child_state.config_snapshot,
                repo_root=self.repo_root,
            )
        raise WorkerBusyError(
            f"child session {child_id} is active; its next task is dispatched "
            "through the child session, not the parent"
        )

    def _advance(
        self, *, state: LoopState, caller: WorkerIdentity | None, now: datetime
    ) -> TaskResponse:
        """The single scheduling step shared by every dispatch path.

        Order: stop conditions -> pending child dispatch -> next workflow ->
        stamp a fresh CurrentTask (new attempt id, caller as owner). Extracted
        so the three former copies (register, finished no-task, finished
        matched) cannot drift apart.
        """
        self._reload_preflights_if_requested()
        suspended = self._suspended_parent_response(state=state)
        if suspended is not None:
            return suspended
        stop_response = self._stop_response_if_needed(state=state)
        if stop_response is not None:
            return stop_response
        child_response = self._dispatch_child_session_after_success(
            state=state, caller=caller
        )
        if child_response is not None:
            return child_response
        workflows = self._workflows_for(workflow_set=state.workflow_set)
        workflow = choose_next_workflow(
            workflows=workflows,
            history=state.history,
            iteration_count=state.iteration_count,
            eval_requested=self._eval_requested(state=state),
        )
        if workflow is None:
            state.stop_reason = "no_eligible_workflow"
            state.status = "failed"
            return TaskResponse(action=STOP_ACTION, stop_reason="no_eligible_workflow")
        state.current_task = self._create_current_task(
            state=state,
            workflow=workflow,
            iteration=state.iteration_count + 1,
            caller=caller,
            now=now,
        )
        self._emit_task_dispatched(
            session_id=state.active_session_id, task=state.current_task
        )
        return _build_run_response(
            current_task=state.current_task,
            config_snapshot=state.config_snapshot,
            repo_root=self.repo_root,
        )

    def _create_current_task(
        self,
        *,
        state: LoopState,
        workflow: WorkflowDefinition,
        iteration: int,
        caller: WorkerIdentity | None,
        now: datetime,
    ) -> CurrentTask:
        """Create and freeze the next task for a validated worker caller."""

        completion_contract_version = max(
            2 if state.schema_version >= 2 else 1,
            self._protocol_version_for_state(state=state),
        )
        if completion_contract_version >= 2:
            if caller is None or (
                self._worker_contracts.get(self._worker_contract_key(worker=caller), 1)
                < completion_contract_version
            ):
                raise WorkerUpgradeRequired(
                    f"v{completion_contract_version} task dispatch requires a "
                    "validated worker handshake; register this worker before "
                    "requesting work"
                )
        task = CurrentTask(
            workflow_set=state.workflow_set,
            workflow_id=workflow.id,
            session_id=state.active_session_id,
            iteration=iteration,
            started_at=now,
            worker=caller,
            attempt_id=_new_attempt_id(),
            repository_id=str(self.repository_identity["repository_id"]),
            completion_contract_version=completion_contract_version,
        )
        if state.schema_version >= 2:
            # Reconcile agent-visible session projections from the durable
            # engine trust root before freezing the next attempt.  Without
            # this step, a consistent rewrite of workflow_contract.json plus
            # its manifest hash could become the next attempt's snapshot.
            self._workflow_contract_for_state(state=state)
            if completion_contract_version >= 3:
                self._restore_v3_context_projections(state=state)
            preflight = self._preflight_for(workflow_set=state.workflow_set)
            task.workflow_snapshot = materialize_workflow_snapshot(
                repo_root=self.repo_root,
                task=task,
                workflow=workflow,
                preflight=preflight,
                config_snapshot=state.config_snapshot,
            )
            root_session_id = state.root_session_id or state.active_session_id
            trace_root, _ = create_attempt_trace(
                repo_root=self.repo_root,
                root_session_id=root_session_id,
                session_id=state.active_session_id,
                request_id=state.request_id,
                work_item_id=state.work_item_id,
                workflow_set=state.workflow_set,
                workflow_id=workflow.id,
                iteration=iteration,
                attempt_id=task.attempt_id or "legacy",
                layout=session_layout(
                    repo_root=self.repo_root, session_id=state.active_session_id
                ),
            )
            assignment_file = assignment_path(
                repo_root=self.repo_root,
                session_id=task.session_id,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
                attempt_id=task.attempt_id or "legacy",
            ).resolve()
            if completion_contract_version >= 3:
                self._write_scheduler_view(state=state, task=task, captured_at=now)
            assignment = build_attempt_assignment(
                repo_root=self.repo_root,
                task=task,
                descriptor=task.workflow_snapshot,
                trace_root=trace_root,
                git_before_ref=git_receipt_ref(
                    session_id=None,
                    iteration=iteration,
                    workflow_id=workflow.id,
                    attempt_id=task.attempt_id,
                    phase="before",
                    layout=session_layout(
                        repo_root=self.repo_root, session_id=state.active_session_id
                    ),
                ),
            )
            write_attempt_assignment(path=assignment_file, assignment=assignment)
            task.assignment_sha256 = file_sha256(path=assignment_file)
        return task

    def _restore_v3_context_projections(self, *, state: LoopState) -> None:
        """Restore agent-visible frozen rosters from engine-owned state."""

        if state.workflow_roster is None or state.harness_capability_roster is None:
            raise ConfigError("protocol v3 state is missing its frozen context rosters")
        projections = {
            workflow_roster_path(
                repo_root=self.repo_root, session_id=state.active_session_id
            ): state.workflow_roster.model_dump(mode="json"),
            (
                session_dir_path(
                    repo_root=self.repo_root,
                    session_id=state.root_session_id or state.active_session_id,
                )
                / "harness_capability_roster.json"
            ): state.harness_capability_roster.model_dump(mode="json"),
        }
        for path, payload in projections.items():
            expected = _serialized_json_sha256(payload=payload)
            if not path.is_file() or file_sha256(path=path) != expected:
                self._emit(
                    session_id=state.active_session_id,
                    event_type="context_projection_restored",
                    payload={"path": str(path), "expected_sha256": expected},
                )
                write_json_atomic(path=path, payload=payload)

    def _write_scheduler_view(
        self, *, state: LoopState, task: CurrentTask, captured_at: datetime
    ) -> None:
        """Freeze a conditional next-role forecast without changing scheduling."""

        roster_path = workflow_roster_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not roster_path.is_file():
            raise ConfigError(
                f"protocol v3 session has no workflow roster at {roster_path}"
            )
        assumed_success = HistoryEntry(
            iteration=task.iteration,
            workflow_set=task.workflow_set,
            workflow_id=task.workflow_id,
            session_id=task.session_id,
            success=True,
            started_at=task.started_at,
            finished_at=captured_at,
            attempt_id=task.attempt_id,
        )
        projected_history = [*state.history, assumed_success]
        next_workflow = choose_next_workflow(
            workflows=self._workflows_for(workflow_set=state.workflow_set),
            history=projected_history,
            iteration_count=task.iteration,
            eval_requested=self._eval_requested(state=state),
        )
        reasons = (
            [
                "selected by the unchanged mechanical scheduler after the "
                "simulated successful current attempt"
            ]
            if next_workflow is not None
            else ["no workflow is mechanically eligible under these assumptions"]
        )
        view = SchedulerView(
            session_id=state.active_session_id,
            state_revision=(
                state.state_revision + 1
                if state_path(
                    repo_root=self.repo_root, session_id=state.active_session_id
                ).is_file()
                else 0
            ),
            attempt_id=task.attempt_id or "legacy",
            workflow_roster_sha256=file_sha256(path=roster_path),
            history_watermark=state.iteration_count,
            captured_at=captured_at,
            recent_history=[
                entry.model_dump(mode="json") for entry in state.history[-12:]
            ],
            conditional_forecast=SchedulerForecast(
                next_workflow_id=(
                    next_workflow.id if next_workflow is not None else None
                ),
                reasons=reasons,
                assumptions=[
                    "current attempt returns normally",
                    "current attempt is recorded as a mechanical success",
                    "no terminal control or child request is accepted",
                    "no stop, failure, user update, or recovery changes state",
                ],
            ),
        )
        write_json_atomic(
            path=scheduler_view_path(
                repo_root=self.repo_root,
                session_id=task.session_id,
                iteration=task.iteration,
                workflow_id=task.workflow_id,
                attempt_id=task.attempt_id or "legacy",
            ),
            payload=view.model_dump(mode="json"),
        )

    def _emit_task_dispatched(self, *, session_id: str, task: CurrentTask) -> None:
        """Emit the compact ownership record for a newly dispatched task."""

        self._emit(
            session_id=session_id,
            event_type="task_dispatched",
            payload={
                "workflow_id": task.workflow_id,
                "iteration": task.iteration,
                "attempt_id": task.attempt_id,
                "worker": (
                    {"hostname": task.worker.hostname, "pid": task.worker.pid}
                    if task.worker is not None
                    else None
                ),
            },
        )

    def _raise_if_worker_alive(self, *, current_task: CurrentTask) -> None:
        """Refuse recovery while the task's recorded worker is verifiably alive."""

        if is_worker_alive(identity=current_task.worker) is not True:
            return
        worker = current_task.worker
        assert worker is not None
        raise WorkerBusyError(
            f"worker pid={worker.pid} on {worker.hostname} is still running "
            f"iteration {current_task.iteration} ({current_task.workflow_id}); "
            "refusing to dispatch duplicate work. If that worker is hung, "
            "kill the process and register again."
        )

    def finish_assignment(self, *, request: FinishedRequest) -> TaskResponse:
        """Commit an attempt result and finalize its externally visible trace."""

        caller = request.worker
        with self._transition_lock:
            response, completion_accepted = self._finish_assignment_locked(
                request=request, caller=caller
            )
        if not completion_accepted:
            # A stale /finished still receives the current scheduler response,
            # but it did not create that response and must not rewrite the
            # immutable exchange of its earlier attempt.
            return response
        self._queue_trace_finalization(request=request, response=response, error=None)
        finalized = self._finalize_completion_trace(request=request, response=response)
        if finalized:
            with self._transition_lock:
                projected = self._refresh_terminal_child_trace_projection(
                    request=request
                )
            if projected:
                self._clear_trace_finalization(request=request)
        return response

    def _finish_assignment_locked(
        self, *, request: FinishedRequest, caller: WorkerIdentity | None
    ) -> tuple[TaskResponse, bool]:
        """Apply one completion while holding the coordinator transition lock."""

        intent_path = self._trace_finalization_path(request=request)
        intent_preexisting = intent_path is not None and intent_path.is_file()
        intent_created_by_call = False

        def mutator(
            state: LoopState | None,
        ) -> tuple[LoopState, tuple[TaskResponse, bool]]:
            """Resolve the completion against the latest durable loop state."""

            nonlocal intent_created_by_call
            current = _require_state(state=state)
            was_terminal = self.state_store.is_terminal_state(state=current)
            now = utc_now()

            def finish(
                response: TaskResponse, *, completion_accepted: bool = False
            ) -> tuple[LoopState, tuple[TaskResponse, bool]]:
                """Bind an accepted response and return the state mutation result."""

                if completion_accepted:
                    if (
                        not current.history
                        or current.history[-1].attempt_id != request.attempt_id
                    ):
                        raise StateInvariantError(
                            "accepted completion has no matching history entry"
                        )
                    current.history[-1].finished_response_sha256 = _model_sha256(
                        model=response
                    )
                self._emit_stop_transition(state=current, was_terminal=was_terminal)
                return current, (response, completion_accepted)

            # Step 3: No active task — stale call. Dispatch as if /register was called.
            # This handles the post-crash stale retry scenario safely.
            if current.current_task is None:
                if current.schema_version >= 2:
                    # A stop response assigns no work, so it is safe to return
                    # without a handshake.  Any response that could dispatch
                    # a task must be backed by the v2 /register contract held
                    # in this coordinator process.
                    if current.active_child_session_id is None:
                        stop_response = self._stop_response_if_needed(state=current)
                        if stop_response is not None:
                            return finish(response=stop_response)
                    if caller is None or (
                        (
                            self._worker_contracts.get(
                                self._worker_contract_key(worker=caller)
                            )
                            or 0
                        )
                        < 2
                    ):
                        raise WorkerUpgradeRequired(
                            "a durable /finished retry cannot dispatch work without "
                            "a validated protocol-v2-or-newer worker handshake; "
                            "call /register with protocol v2 or newer first"
                        )
                return finish(
                    response=self._advance(state=current, caller=caller, now=now)
                )

            # Step 4: Mismatch check — stale call for a different task.
            # Do NOT mutate state; return the current task's run response so the
            # caller knows what is actually running. Note: the returned workflow_id
            # and iteration belong to the CURRENT (live) task, not the stale caller's
            # completed task — this is intentional and safe in the single-worker model.
            active = current.current_task
            if (
                request.session_id != active.session_id
                or request.workflow_id != active.workflow_id
                or request.iteration != active.iteration
                or (
                    # A live task WITH an attempt id accepts only an exact
                    # echo: a missing or different attempt means a superseded
                    # or unversioned completion — its work was already
                    # recovered/abandoned and redispatched. The wildcard
                    # applies only when the persisted task itself predates
                    # attempt ids (M5: legacy tolerance belongs to the OLD
                    # task, never to an unversioned artifact vs a NEW task).
                    active.attempt_id is not None
                    and request.attempt_id != active.attempt_id
                )
            ):
                # Replaying the live task is only safe to its recorded owner:
                # handing it to anyone else would start a second executor of
                # the same task. Identity is required at /register, so every
                # task dispatched by this version has an owner; a None owner
                # can only come from pre-upgrade persisted state and keeps
                # the legacy replay behavior for that one resume.
                if active.worker is not None and (
                    caller is None
                    or caller.hostname != active.worker.hostname
                    or caller.pid != active.worker.pid
                    or caller.starttime != active.worker.starttime
                ):
                    caller_desc = (
                        f"pid={caller.pid} on {caller.hostname}"
                        if caller is not None
                        else "an unidentified caller"
                    )
                    raise WorkerBusyError(
                        f"stale /finished from {caller_desc}: iteration "
                        f"{active.iteration} ({active.workflow_id}) belongs "
                        f"to worker pid={active.worker.pid} on "
                        f"{active.worker.hostname}"
                    )
                return finish(
                    response=_build_run_response(
                        current_task=active,
                        config_snapshot=current.config_snapshot,
                        repo_root=self.repo_root,
                    )
                )

            # Step 5: Match confirmed — process result.
            frozen_workflow_contract = self._validate_finished_binding(
                active=active, request=request
            )
            self._remember_v2_completion_caller(active=active, request=request)
            # Write-ahead trace intent: if the process dies after the state
            # commit but before the HTTP response/final seal, startup can
            # still close the trace honestly as response-unavailable.
            self._queue_trace_finalization(request=request, response=None, error=None)
            intent_created_by_call = (
                not intent_preexisting
                and intent_path is not None
                and intent_path.is_file()
            )
            self._record_finished_task(
                state=current,
                active=active,
                request=request,
                now=now,
                workflow_contract=frozen_workflow_contract,
            )

            # Step 6: Special cases that stop immediately.
            if current.stop_reason == "goal_check_broken":
                return finish(
                    response=TaskResponse(
                        action=STOP_ACTION, stop_reason="goal_check_broken"
                    ),
                    completion_accepted=True,
                )

            # Step 7+: stop conditions, child dispatch, next workflow.
            return finish(
                response=self._advance(state=current, caller=caller, now=now),
                completion_accepted=True,
            )

        checkpoint = len(self._pending_events)
        try:
            response, completion_accepted = self.state_store.mutate(mutator=mutator)
        except BaseException:
            del self._pending_events[checkpoint:]
            if intent_created_by_call:
                self._clear_trace_finalization(request=request)
            raise
        self._flush_pending_events()
        if completion_accepted and response.action == STOP_ACTION:
            committed_state = self.state_store.read_state()
            if (
                committed_state is not None
                and self._protocol_version_for_state(state=committed_state) >= 3
                and self.state_store.is_terminal_state(state=committed_state)
            ):
                self._ensure_session_outcome(state=committed_state)
        if response.action == STOP_ACTION:
            parent_response = self._resume_parent_if_active_child_completed(
                caller=caller
            )
            if parent_response is not None:
                self._replace_finished_response_binding(
                    request=request, response=parent_response
                )
                return parent_response, completion_accepted
        return response, completion_accepted

    def _replace_finished_response_binding(
        self, *, request: FinishedRequest, response: TaskResponse
    ) -> None:
        """Bind a child completion to the parent response returned on unwind.

        A terminal child's local scheduler response is ``stop``. The public
        endpoint can instead resume its parent and return that parent's next
        assignment in the same call, so the child history must bind the exact
        externally observable response after this substitution.
        """

        store = StateStore(
            repo_root=self.repo_root,
            state_path=state_path(
                repo_root=self.repo_root, session_id=request.session_id
            ),
        )

        def mutator(state: LoopState | None) -> tuple[LoopState, None]:
            """Replace the stored response binding for the matching attempt."""

            current = _require_state(state=state)
            matches = [
                entry
                for entry in current.history
                if entry.attempt_id == request.attempt_id
                and entry.session_id == request.session_id
                and entry.workflow_id == request.workflow_id
                and entry.iteration == request.iteration
            ]
            if len(matches) != 1:
                raise StateInvariantError(
                    "resumed completion has no unique history response binding"
                )
            matches[0].finished_response_sha256 = _model_sha256(model=response)
            return current, None

        store.mutate(mutator=mutator)

    def _record_finished_task(
        self,
        *,
        state: LoopState,
        active: CurrentTask,
        request: FinishedRequest,
        now: datetime,
        workflow_contract: WorkflowSetContract | None = None,
    ) -> None:
        """Record one finished attempt and evaluate its protocol-owned signals."""

        effective_workflow_contract = (
            workflow_contract or self._workflow_contract_for_state(state=state)
        )
        protocol_version = effective_workflow_contract.session_protocol_version
        success = request.success
        error = request.error
        # The taxonomy must describe the FINAL recorded failure: when the
        # coordinator flips a harness success to a protocol failure below,
        # an incoming harness kind (or None) would misattribute the cause.
        failure_kind = request.failure_kind if not success else None

        if protocol_version >= 3:
            self._accept_current_eval_receipts(
                state=state,
                active=active,
                workflow_contract=effective_workflow_contract,
            )
            self._observe_layer_handoff(
                state=state,
                active=active,
                workflow_contract=effective_workflow_contract,
            )
            # D13 (A2) / D14: currency diagnostics run only after a mechanically
            # successful attempt so a crashed attempt never fails against, or
            # archives, the prior valid handoff. These are pure diagnostics — no
            # success flip, no cap, no counter.
            if request.success:
                self._diagnose_currency_outputs(
                    state=state,
                    active=active,
                    workflow_contract=effective_workflow_contract,
                )
        elif self._workflow_expects_goal_check_signal(current_task=active):
            goal_signal_errors: list[str] = []
            goal_signal = self._read_goal_check_signal(
                current_task=active,
                state=state,
                workflow_contract=workflow_contract,
                receipt_validation_errors=goal_signal_errors,
            )
            if goal_signal is None:
                success = False
                error = "invalid_goal_check_output"
                if goal_signal_errors:
                    error += ": " + "; ".join(goal_signal_errors)
                failure_kind = "unknown"
                state.goal_check_consecutive_failures += 1
                self._emit(
                    session_id=state.active_session_id,
                    event_type="goal_check",
                    payload={"valid": False, "iteration": active.iteration},
                )
                if (
                    state.goal_check_consecutive_failures
                    >= state.config_snapshot.goal_check_consecutive_failures_cap
                ):
                    state.stop_reason = "goal_check_broken"
                    state.status = "failed"
            else:
                state.goal_check_consecutive_failures = 0
                self._emit(
                    session_id=state.active_session_id,
                    event_type="goal_check",
                    payload={
                        "valid": True,
                        "goal_met": goal_signal.goal_met,
                        "reason": goal_signal.reason,
                        "iteration": active.iteration,
                    },
                )

        if state.stop_reason != "goal_check_broken":
            control_result = self._apply_session_control(
                state=state, workflow_contract=effective_workflow_contract
            )
            if control_result in {"invalid_v1", "invalid_v2", "invalid_v3"}:
                success = False
                error = "invalid_control_output"
                failure_kind = "unknown"

        state.history.append(
            HistoryEntry(
                iteration=active.iteration,
                workflow_set=active.workflow_set,
                workflow_id=active.workflow_id,
                session_id=active.session_id,
                success=success,
                error=error,
                failure_kind=failure_kind,
                started_at=active.started_at,
                finished_at=now,
                attempt_id=active.attempt_id,
                harness_run_id=request.harness_run_id,
                assignment_sha256=active.assignment_sha256,
                finished_request_sha256=_model_sha256(model=request),
                trace_manifest_ref=(
                    f"trace:trace-{active.attempt_id}:/trace_manifest.json"
                    if state.schema_version >= 2 and active.attempt_id is not None
                    else None
                ),
                trace_manifest_path=(
                    request.trace_manifest_path if state.schema_version == 1 else None
                ),
            )
        )
        self._track_workflow_failure_cap(
            state=state, workflow_id=active.workflow_id, success=success
        )
        totals = state.usage_totals
        if request.usage is not None:
            totals.prompt_tokens += request.usage.prompt_tokens
            totals.completion_tokens += request.usage.completion_tokens
            # "with usage" means FULLY measured: a run where some coordinator
            # turns carried no usage record keeps its measured subtotal but
            # counts as not-fully-known, so a cost budget's blind spot stays
            # visible instead of masquerading as complete accounting.
            if request.usage.turns_without_usage == 0:
                totals.iterations_with_usage += 1
            else:
                totals.iterations_without_usage += 1
        else:
            totals.iterations_without_usage += 1
        if request.duration_s:
            totals.duration_s += request.duration_s
        self._emit(
            session_id=state.active_session_id,
            event_type="task_finished",
            payload={
                "workflow_id": active.workflow_id,
                "iteration": active.iteration,
                "attempt_id": active.attempt_id,
                "success": success,
                "error": error,
                "failure_kind": failure_kind,
                "prompt_tokens": (
                    request.usage.prompt_tokens if request.usage else None
                ),
                "completion_tokens": (
                    request.usage.completion_tokens if request.usage else None
                ),
                "duration_s": request.duration_s,
            },
        )
        state.iteration_count += 1
        state.current_task = None

    def _validate_finished_binding(
        self, *, active: CurrentTask, request: FinishedRequest
    ) -> WorkflowSetContract | None:
        """Apply the provenance fence and return the attempt-frozen contract.

        A v2 completion must be interpreted using the contract that was
        selected when that exact attempt was dispatched.  The live session
        copies remain agent-visible repair surfaces; reading them here would
        let a worker consistently rewrite ``workflow_contract.json`` and
        ``session.json`` to downgrade terminal-control semantics after it had
        already received a v2 assignment.  Returning the hash-verified
        snapshot also makes recovery use the same contract as the live
        ``/finished`` path.
        """
        frozen_workflow_contract: WorkflowSetContract | None = None
        caller = request.worker
        if (
            active.worker is not None
            and caller is not None
            and (
                caller.hostname != active.worker.hostname
                or caller.pid != active.worker.pid
                or caller.starttime != active.worker.starttime
            )
        ):
            raise WorkerBusyError(
                "matching /finished coordinates were posted by a worker "
                "that does not own this assignment"
            )
        if active.completion_contract_version >= 2:
            if caller is None or active.worker is None:
                raise WorkerBusyError(
                    "v2 finished assignment must echo its exact worker owner"
                )
            if request.repository_id != active.repository_id:
                raise WorkerBusyError(
                    "v2 finished assignment must echo its repository identity"
                )
            assignment_file = assignment_path(
                repo_root=self.repo_root,
                session_id=active.session_id,
                iteration=active.iteration,
                workflow_id=active.workflow_id,
                attempt_id=active.attempt_id or "legacy",
            )
            if (
                request.assignment_sha256 is None
                or active.assignment_sha256 is None
                or request.assignment_sha256 != active.assignment_sha256
                or not assignment_file.is_file()
                or active.assignment_sha256 != file_sha256(path=assignment_file)
            ):
                raise WorkerBusyError(
                    "v2 finished assignment does not match its immutable "
                    "assignment envelope"
                )
            if active.workflow_snapshot is None:
                raise WorkerBusyError("v2 finished assignment has no frozen snapshot")
            try:
                _, _, frozen_workflow_contract, _ = verify_workflow_snapshot(
                    descriptor=active.workflow_snapshot,
                    repo_root=self.repo_root,
                    expected_task=active,
                )
            except AssignmentContractError as exc:
                raise WorkerBusyError(
                    "v2 finished assignment snapshot changed during execution"
                ) from exc
        if (
            active.repository_id is not None
            and request.repository_id is not None
            and request.repository_id != active.repository_id
        ):
            raise WorkerBusyError(
                "finished assignment repository identity does not match"
            )
        return frozen_workflow_contract

    def _root_session_id_from_frozen_assignment(
        self,
        *,
        session_id: str,
        workflow_id: str,
        iteration: int,
        attempt_id: str,
        assignment_sha256: str | None,
    ) -> str:
        """Recover trace topology from the assignment accepted with the task.

        ``session.json`` is agent-visible durable state and therefore cannot
        redirect an engine seal after dispatch. The assignment hash was bound
        into current-task/history state before execution, so it is the stable
        source for the root identity used to derive the canonical trace path.
        """

        if assignment_sha256 is None:
            raise TraceError("attempt has no frozen assignment hash")
        path = assignment_path(
            repo_root=self.repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
            attempt_id=attempt_id,
        )
        if not path.is_file() or file_sha256(path=path) != assignment_sha256:
            raise TraceError("attempt assignment no longer matches its accepted hash")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TraceError("attempt assignment is unreadable") from exc
        identity = payload.get("identity") if isinstance(payload, dict) else None
        if not isinstance(identity, dict):
            raise TraceError("attempt assignment has no identity")
        expected = {
            "session_id": session_id,
            "workflow_id": workflow_id,
            "iteration": iteration,
            "attempt_id": attempt_id,
        }
        if any(identity.get(field) != value for field, value in expected.items()):
            raise TraceError("attempt assignment identity does not match completion")
        root_session_id = identity.get("root_session_id")
        if not isinstance(
            root_session_id, str
        ) or not SAFE_DURABLE_ID_PATTERN.fullmatch(root_session_id):
            raise TraceError("attempt assignment has no safe root session identity")
        return root_session_id

    def _finalize_completion_trace(
        self,
        *,
        request: FinishedRequest,
        response: TaskResponse | None,
        queue_on_failure: bool = True,
    ) -> bool:
        """Capture the observable service response, then seal the attempt trace.

        State acceptance has already committed when this runs.  Trace capture
        is deliberately best-effort and cannot change D3 result semantics.
        The coordinator derives the canonical trace from durable session and
        attempt identity. A missing or mismatched caller-supplied path is
        recorded as trace incompleteness; it cannot redirect finalization.
        """
        if not request.attempt_id or not request.assignment_sha256:
            return False
        if (
            session_layout(repo_root=self.repo_root, session_id=request.session_id)
            == SESSION_LAYOUT_FOLDED
        ):
            return self._finalize_completion_raw(request=request, response=response)
        try:
            if not self._completion_is_committed(request=request, response=response):
                raise TraceError(
                    "completion exchange is not bound to committed history"
                )
            root_session_id = self._root_session_id_from_frozen_assignment(
                session_id=request.session_id,
                workflow_id=request.workflow_id,
                iteration=request.iteration,
                attempt_id=request.attempt_id,
                assignment_sha256=request.assignment_sha256,
            )
            trace_root = attempt_trace_dir_path(
                repo_root=self.repo_root,
                root_session_id=root_session_id,
                session_id=request.session_id,
                attempt_id=request.attempt_id,
            ).resolve()
            manifest_path = trace_root / "trace_manifest.json"
            trace_protocol_errors: list[str] = []
            if response is None:
                trace_protocol_errors.append(
                    "coordinator response was interrupted after state acceptance"
                )
            if not request.trace_manifest_path:
                trace_protocol_errors.append(
                    "finished request omitted the canonical trace manifest path"
                )
            else:
                supplied_path = Path(request.trace_manifest_path)
                if (
                    not supplied_path.is_absolute()
                    or supplied_path.resolve() != manifest_path
                ):
                    trace_protocol_errors.append(
                        "finished request trace manifest path was not canonical"
                    )
            manifest = read_trace_manifest(manifest_path=manifest_path)
            identity = manifest.get("identity")
            if not isinstance(identity, dict):
                raise TraceError("canonical trace manifest has no identity")
            expected_identity = {
                "root_session_id": root_session_id,
                "session_id": request.session_id,
                "workflow_id": request.workflow_id,
                "iteration": request.iteration,
                "attempt_id": request.attempt_id,
            }
            if any(
                identity.get(field) != expected
                for field, expected in expected_identity.items()
            ):
                raise TraceError(
                    "canonical trace manifest identity does not match completion"
                )
            if manifest.get("lifecycle") in {"sealed", "incomplete"}:
                receipt_path = trace_seal_receipt_path(
                    repo_root=self.repo_root,
                    session_id=request.session_id,
                    attempt_id=request.attempt_id,
                )
                if receipt_path.is_file():
                    integrity = verify_trace_integrity(
                        trace_root=trace_root, repo_root=self.repo_root
                    )
                    if integrity.get("status") != "verified":
                        raise TraceError(
                            "previously anchored completion trace no longer verifies"
                        )
                    self._verify_finished_exchange(
                        trace_root=trace_root, request=request, response=response
                    )
                    return True
                # A workflow/tool may write inside the active trace under D8,
                # including changing its manifest. Without the compact
                # session-plane receipt this lifecycle claim is not an engine
                # seal. Reopen, retain the files, and recompute an incomplete
                # inventory after recording the exact exchange.
                trace_protocol_errors.append(
                    "trace claimed finalization before coordinator completion"
                )
                manifest["lifecycle"] = "active"
                manifest["sealed_at"] = None
                manifest["inventory"] = []
                manifest["incompleteness_reasons"] = []
                write_json_atomic(path=manifest_path, payload=manifest)
            if manifest.get("lifecycle") != "active":
                raise TraceError("canonical trace manifest lifecycle is invalid")
            trace_write_json(
                trace_root=trace_root,
                relative_path="service/finished_exchange.json",
                payload={
                    "schema_version": 1,
                    "request": request.model_dump(mode="json"),
                    "response": (
                        response.model_dump(mode="json")
                        if response is not None
                        else None
                    ),
                    "response_status": (
                        "complete" if response is not None else "unavailable"
                    ),
                },
            )
            if response is not None:
                trace_write_json(
                    trace_root=trace_root,
                    relative_path="protocol/finished_response.json",
                    payload=response.model_dump(mode="json"),
                )
            update_trace_manifest(
                trace_root=trace_root,
                updates={
                    "channels": {
                        "service": (
                            "complete" if response is not None else "incomplete"
                        )
                    }
                },
            )
            failure = None
            if (
                not request.success
                or request.trace_error is not None
                or trace_protocol_errors
            ):
                trace_errors = [
                    value
                    for value in [request.trace_error, *trace_protocol_errors]
                    if value
                ]
                failure = {
                    "error": request.error,
                    "failure_kind": request.failure_kind,
                    "trace_error": "; ".join(trace_errors) or None,
                }
            seal_attempt_trace(
                trace_root=trace_root,
                usage=(
                    request.usage.model_dump(mode="json")
                    if request.usage is not None
                    else None
                ),
                failure=failure,
                incomplete=request.trace_incomplete or bool(trace_protocol_errors),
                repo_root=self.repo_root,
            )
            return True
        except Exception as exc:
            logger.warning(
                "failed to finalize trace for attempt %s",
                request.attempt_id,
                exc_info=True,
            )
            if queue_on_failure:
                self._queue_trace_finalization(
                    request=request, response=response, error=str(exc)
                )
            return False

    def _finalize_completion_raw(
        self, *, request: FinishedRequest, response: TaskResponse | None
    ) -> bool:
        """Record a folded session's completion exchange in its raw dir.

        A folded session is never sealed. The completion is already durable in
        the iteration's result.json and recovery journal; this only mirrors the
        service exchange into the prunable raw dir for observability, best
        effort. Failure here cannot change the accepted D3 result.
        """

        if not self._completion_is_committed(request=request, response=response):
            return False
        try:
            raw_root = raw_attempt_dir_path(
                repo_root=self.repo_root,
                session_id=request.session_id,
                iteration=request.iteration,
                workflow_id=request.workflow_id,
            )
            trace_write_json(
                trace_root=raw_root,
                relative_path="service/finished_exchange.json",
                payload={
                    "schema_version": 1,
                    "request": request.model_dump(mode="json"),
                    "response": (
                        response.model_dump(mode="json")
                        if response is not None
                        else None
                    ),
                    "response_status": (
                        "complete" if response is not None else "unavailable"
                    ),
                },
            )
            if response is not None:
                trace_write_json(
                    trace_root=raw_root,
                    relative_path="protocol/finished_response.json",
                    payload=response.model_dump(mode="json"),
                )
        except Exception:
            logger.warning(
                "failed to record folded completion raw for attempt %s",
                request.attempt_id,
                exc_info=True,
            )
        return True

    @staticmethod
    def _verify_finished_exchange(
        *, trace_root: Path, request: FinishedRequest, response: TaskResponse | None
    ) -> None:
        """Verify that a trace preserves the exact finished request and response."""

        try:
            exchange = json.loads(
                trace_root.joinpath("service/finished_exchange.json").read_text(
                    encoding="utf-8"
                )
            )
            response_payload = (
                json.loads(
                    trace_root.joinpath("protocol/finished_response.json").read_text(
                        encoding="utf-8"
                    )
                )
                if response is not None
                else None
            )
        except (OSError, ValueError) as exc:
            raise TraceError(
                "anchored completion trace lacks its exact finished exchange"
            ) from exc
        expected_request = _model_payload(model=request)
        expected_response = (
            _model_payload(model=response) if response is not None else None
        )
        if response_payload != expected_response or exchange != {
            "schema_version": 1,
            "request": expected_request,
            "response": expected_response,
            "response_status": "complete" if response is not None else "unavailable",
        }:
            raise TraceError(
                "anchored completion trace finished exchange does not match"
            )

    def _trace_finalization_path(self, *, request: FinishedRequest) -> Path | None:
        """Return the durable finalization-intent path for a v2 completion.

        Folded sessions have no seal to finalize: their raw artifacts are
        written in place and never sealed, so there is no crash-recovery
        outbox intent for them.
        """

        if not request.attempt_id or not request.assignment_sha256:
            return None
        if (
            session_layout(repo_root=self.repo_root, session_id=request.session_id)
            == SESSION_LAYOUT_FOLDED
        ):
            return None
        outbox = trace_finalization_outbox_dir_path(repo_root=self.repo_root)
        return outbox / f"{request.session_id}--{request.attempt_id}--finished.json"

    def _queue_trace_finalization(
        self,
        *,
        request: FinishedRequest,
        response: TaskResponse | None,
        error: str | None,
    ) -> None:
        """Persist a raw completion exchange for crash-safe trace finalization."""

        path = self._trace_finalization_path(request=request)
        if path is None:
            return
        try:
            existing: dict[str, Any] = {}
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("outbox record is not an object")
                existing = loaded
            payload = {
                "schema_version": 1,
                "kind": "finished_exchange",
                "request": request.model_dump(mode="json"),
                "response": (
                    response.model_dump(mode="json")
                    if response is not None
                    else existing.get("response")
                ),
                "last_error": error
                if error is not None
                else existing.get("last_error"),
                "queued_at": existing.get("queued_at")
                or utc_now().isoformat().replace("+00:00", "Z"),
            }
            write_json_atomic(path=path, payload=payload)
        except (OSError, ValueError):
            logger.warning(
                "failed to journal completion-trace finalization for attempt %s",
                request.attempt_id,
                exc_info=True,
            )

    def _clear_trace_finalization(self, *, request: FinishedRequest) -> None:
        """Remove a completion's finalization intent after successful sealing."""

        path = self._trace_finalization_path(request=request)
        if path is not None:
            path.unlink(missing_ok=True)

    def _retry_trace_finalizations(
        self, *, allow_unavailable_responses: bool = False
    ) -> None:
        """Retry durable completion and abandonment finalization intents."""

        outbox = trace_finalization_outbox_dir_path(repo_root=self.repo_root)
        if not outbox.is_dir():
            return
        for path in sorted(outbox.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("kind") == "abandoned_attempt":
                    task = CurrentTask.model_validate(payload["task"])
                    error = str(payload["error"])
                    if self._abandonment_is_committed(task=task) and (
                        self._finalize_abandoned_trace(task=task, error=error)
                    ):
                        path.unlink(missing_ok=True)
                        # A crash before the state mutation can leave the
                        # earlier write-ahead finished intent beside the later
                        # abandonment intent. Crash history proves that
                        # completion never committed, so it cannot remain a
                        # permanently unresolvable outbox record.
                        outbox.joinpath(
                            f"{task.session_id}--{task.attempt_id}--finished.json"
                        ).unlink(missing_ok=True)
                    continue
                request = FinishedRequest.model_validate(payload["request"])
                response_payload = payload.get("response")
                response = (
                    TaskResponse.model_validate(response_payload)
                    if response_payload is not None
                    else None
                )
            except (OSError, KeyError, TypeError, ValueError, ValidationError):
                logger.warning("invalid trace-finalization outbox record: %s", path)
                continue
            if response is None and not allow_unavailable_responses:
                # A matching /finished writes its intent inside the state
                # mutation. Nested parent-resume scheduling can revisit the
                # outbox before that same request has selected its response;
                # only a fresh coordinator process may conclude that the
                # response was genuinely interrupted and unavailable.
                continue
            if not self._completion_is_committed(request=request, response=response):
                continue
            if self._finalize_completion_trace(
                request=request, response=response, queue_on_failure=False
            ):
                with self._transition_lock:
                    projected = self._refresh_terminal_child_trace_projection(
                        request=request
                    )
                if projected:
                    path.unlink(missing_ok=True)

    def _queue_abandoned_trace_finalization(
        self, *, task: CurrentTask, error: str
    ) -> None:
        """Journal crash-trace finalization before the state transition commits."""

        if task.attempt_id is None or task.assignment_sha256 is None:
            return
        if (
            session_layout(repo_root=self.repo_root, session_id=task.session_id)
            == SESSION_LAYOUT_FOLDED
        ):
            # Folded sessions never seal; a crashed attempt's raw dir is left
            # in place and the durable recovery journal records the crash.
            return
        path = trace_finalization_outbox_dir_path(repo_root=self.repo_root) / (
            f"{task.session_id}--{task.attempt_id}--abandoned.json"
        )
        if path.exists():
            return
        try:
            payload = {
                "schema_version": 1,
                "kind": "abandoned_attempt",
                "task": task.model_dump(mode="json"),
                "error": error,
                "queued_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
            write_json_atomic(path=path, payload=payload)
        except OSError:
            logger.warning(
                "failed to journal crash-trace finalization for attempt %s",
                task.attempt_id,
                exc_info=True,
            )

    def _completion_is_committed(
        self, *, request: FinishedRequest, response: TaskResponse | None = None
    ) -> bool:
        """Distinguish a write-ahead intent from an accepted completion."""
        try:
            states = self._current_and_archived_states(session_id=request.session_id)
        except Exception:
            logger.warning(
                "cannot inspect state for completion attempt %s",
                request.attempt_id,
                exc_info=True,
            )
            return False
        if not states:
            return False
        current = states[0]
        if (
            current.current_task is not None
            and current.current_task.attempt_id == request.attempt_id
        ):
            return False
        request_sha256 = _model_sha256(model=request)
        response_sha256 = (
            _model_sha256(model=response) if response is not None else None
        )
        for state in states:
            for entry in state.history:
                if not (
                    entry.attempt_id == request.attempt_id
                    and entry.session_id == request.session_id
                    and entry.workflow_id == request.workflow_id
                    and entry.iteration == request.iteration
                    and entry.failure_kind != "crash"
                ):
                    continue
                if (
                    entry.assignment_sha256 is not None
                    and entry.assignment_sha256 != request.assignment_sha256
                ):
                    continue
                if (
                    entry.finished_request_sha256 is not None
                    and entry.finished_request_sha256 != request_sha256
                ):
                    continue
                if (
                    response_sha256 is not None
                    and entry.finished_request_sha256 is not None
                ):
                    if entry.finished_response_sha256 != response_sha256:
                        continue
                return True
        return False

    def _current_and_archived_states(self, *, session_id: str) -> list[LoopState]:
        """Read durable session state, including terminal startup archives.

        Trace-finalization intents can outlive the session's live ``state.json``.
        Startup archives terminal state before retrying the outbox, so the
        committed history in that archive remains part of the acceptance
        proof and must be consulted.
        """
        session_root = session_dir_path(repo_root=self.repo_root, session_id=session_id)
        live_path = state_path(repo_root=self.repo_root, session_id=session_id)
        states: list[LoopState] = []
        live = StateStore(repo_root=self.repo_root, state_path=live_path).read_state()
        if live is not None:
            states.append(live)
        for archive_path in sorted(
            session_root.glob("state.json.archive_*.json"), reverse=True
        ):
            payload = json.loads(archive_path.read_text(encoding="utf-8"))
            states.append(LoopState.model_validate(payload))
        return states

    def _refresh_terminal_child_trace_projection(
        self, *, request: FinishedRequest
    ) -> bool:
        """Refresh terminal outcome projections after an attempt trace seals."""
        try:
            child_store = StateStore(
                repo_root=self.repo_root,
                state_path=state_path(
                    repo_root=self.repo_root, session_id=request.session_id
                ),
            )
            child_state = child_store.read_state()
            if child_state is None or not child_store.is_terminal_state(
                state=child_state
            ):
                return True
            if self._protocol_version_for_state(state=child_state) >= 3:
                self._ensure_session_outcome(state=child_state)
            if child_state.parent_session_id is None:
                return True
            self._mark_child_record_complete(child_state=child_state)
            return True
        except Exception:
            # Trace projection remains retriable through the still-durable
            # seal receipt; it must not change the accepted loop transition.
            logger.warning(
                "failed to refresh child trace projection for attempt %s",
                request.attempt_id,
                exc_info=True,
            )
            return False

    def _ensure_session_outcome(self, *, state: LoopState) -> SessionOutcome | None:
        """Write the topology-neutral result for any terminal v3 lifecycle."""

        if state.status == "running" or state.stop_reason is None:
            return None
        if state.terminal_state_revision is None or state.terminal_at is None:
            raise StateInvariantError(
                "terminal v3 state has no frozen transition revision or timestamp"
            )
        session_id = state.active_session_id
        terminal_control_path = control_path(
            repo_root=self.repo_root, session_id=session_id
        )
        terminal_signal: ControlSignal | None = None
        control_ref: OutcomeArtifactRef | None = None
        control_snapshot = state.accepted_terminal_control
        if control_snapshot is not None:
            try:
                terminal_signal = ControlSignal.model_validate(control_snapshot.payload)
                raw_payload = json.loads(control_snapshot.raw_json)
            except (ValidationError, ValueError) as exc:
                raise StateInvariantError(
                    f"accepted terminal control snapshot is invalid: {exc}"
                ) from exc
            expected_digest = (
                "sha256:"
                + hashlib.sha256(control_snapshot.raw_json.encode("utf-8")).hexdigest()
            )
            if (
                raw_payload != control_snapshot.payload
                or expected_digest != control_snapshot.sha256
                or terminal_signal.schema_version != 3
                or terminal_signal.state != "stopped"
                or terminal_signal.stop_reason != state.stop_reason
            ):
                raise StateInvariantError(
                    "accepted terminal control snapshot contradicts terminal state"
                )
            if (
                not terminal_control_path.is_file()
                or file_sha256(path=terminal_control_path) != control_snapshot.sha256
            ):
                write_text_atomic(
                    path=terminal_control_path, content=control_snapshot.raw_json
                )
            if file_sha256(path=terminal_control_path) != control_snapshot.sha256:
                raise StateInvariantError(
                    "restored terminal control bytes do not match snapshot"
                )
            control_ref = OutcomeArtifactRef(
                ref="session:/control.json", sha256=control_snapshot.sha256
            )
        elif state.stop_reason in {"goal_met", "unresolvable_error"}:
            raise StateInvariantError(
                "control-owned terminal state has no accepted v3 control snapshot"
            )
        evidence_refs = terminal_signal.evidence_refs if terminal_signal else []
        observed_handoff = self._terminal_handoff_projection(state=state)
        fallback = (
            None
            if observed_handoff.status == "valid"
            else OutcomeFallbackSummary(
                source=(
                    "control_reason"
                    if terminal_signal is not None
                    else "engine_stop_reason"
                ),
                text=(
                    terminal_signal.reason
                    if terminal_signal is not None
                    else state.stop_reason
                ),
            )
        )
        session_root = session_dir_path(repo_root=self.repo_root, session_id=session_id)
        delivery_refs = list(
            dict.fromkeys(
                reference
                for entry in state.history
                if entry.attempt_id is not None
                for reference in [
                    self._delivery_ref_for_attempt(
                        session_id=session_id,
                        attempt_id=entry.attempt_id,
                        evidence_refs=evidence_refs,
                    )
                ]
                if reference is not None
            )
        )
        trace_seal_refs = [
            f"session:/trace_seals/{path.name}"
            for path in sorted((session_root / "trace_seals").glob("*.json"))
            if path.is_file()
        ]
        cited_eval_refs: list[str] = (
            list(terminal_signal.eval_receipt_refs)
            if terminal_signal is not None
            else []
        )
        eval_refs: list[str] = list(
            dict.fromkeys([*cited_eval_refs, *state.accepted_eval_receipt_seals])
        )
        outcome_file = session_outcome_path(
            repo_root=self.repo_root, session_id=session_id
        )
        outcome = SessionOutcome(
            session_id=session_id,
            root_session_id=state.root_session_id or session_id,
            goal_sha256=state.goal_hash,
            terminal_status=state.status,
            stop_reason=state.stop_reason,
            terminal_state_revision=state.terminal_state_revision,
            control=control_ref,
            handoff=observed_handoff,
            fallback_summary=fallback,
            evidence_refs=evidence_refs,
            delivery_refs=delivery_refs,
            eval_refs=eval_refs,
            trace_seal_refs=trace_seal_refs,
            created_at=state.terminal_at,
        )
        write_json_atomic(path=outcome_file, payload=outcome.model_dump(mode="json"))
        return outcome

    def _terminal_handoff_projection(self, *, state: LoopState) -> OutcomeHandoff:
        """Project the last engine observation without trusting later file edits."""

        observation = state.latest_handoff_observation
        if observation is None:
            return OutcomeHandoff(status="missing")
        if observation.status != "valid":
            return observation.model_copy(deep=True)
        snapshot = state.accepted_handoff_snapshot
        if (
            snapshot is None
            or snapshot.sha256 != observation.sha256
            or snapshot.handoff.revision != observation.revision
        ):
            return OutcomeHandoff(status="invalid")
        path = handoff_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not path.is_file() or file_sha256(path=path) != snapshot.sha256:
            write_text_atomic(path=path, content=snapshot.raw_json)
        if file_sha256(path=path) != snapshot.sha256:
            raise StateInvariantError("restored handoff bytes do not match snapshot")
        return observation.model_copy(deep=True)

    def _abandonment_is_committed(self, *, task: CurrentTask) -> bool:
        """Return whether durable state records this exact task as abandoned."""

        try:
            states = self._current_and_archived_states(session_id=task.session_id)
        except Exception:
            logger.warning(
                "cannot inspect state for abandoned attempt %s",
                task.attempt_id,
                exc_info=True,
            )
            return False
        if not states:
            return False
        current = states[0]
        if current.current_task is not None and _same_task(
            a=current.current_task, b=task
        ):
            return False
        return any(
            any(
                entry.attempt_id == task.attempt_id
                and entry.session_id == task.session_id
                and entry.failure_kind == "crash"
                and (
                    entry.assignment_sha256 is None
                    or entry.assignment_sha256 == task.assignment_sha256
                )
                for entry in state.history
            )
            for state in states
        )

    def _finalize_abandoned_trace(self, *, task: CurrentTask, error: str) -> bool:
        """Seal a recovered crash attempt as incomplete forensic evidence."""

        if task.attempt_id is None or task.assignment_sha256 is None:
            return False
        try:
            if not self._abandonment_is_committed(task=task):
                raise TraceError("abandonment is not bound to committed history")
            root_session_id = self._root_session_id_from_frozen_assignment(
                session_id=task.session_id,
                workflow_id=task.workflow_id,
                iteration=task.iteration,
                attempt_id=task.attempt_id,
                assignment_sha256=task.assignment_sha256,
            )
            trace_root = attempt_trace_dir_path(
                repo_root=self.repo_root,
                root_session_id=root_session_id,
                session_id=task.session_id,
                attempt_id=task.attempt_id,
            ).resolve()
            manifest = read_trace_manifest(
                manifest_path=trace_root / "trace_manifest.json"
            )
            identity = manifest.get("identity")
            if not isinstance(identity, dict) or any(
                identity.get(field) != expected
                for field, expected in {
                    "root_session_id": root_session_id,
                    "session_id": task.session_id,
                    "workflow_id": task.workflow_id,
                    "iteration": task.iteration,
                    "attempt_id": task.attempt_id,
                }.items()
            ):
                return False
            receipt_path = trace_seal_receipt_path(
                repo_root=self.repo_root,
                session_id=task.session_id,
                attempt_id=task.attempt_id,
            )
            if manifest.get("lifecycle") in {"sealed", "incomplete"}:
                if receipt_path.is_file():
                    self._verify_abandoned_trace(
                        trace_root=trace_root, task=task, error=error
                    )
                    return True
                # A workflow can write anywhere under its trace (D8), including
                # claiming that its own manifest is final. Only the compact
                # session-plane receipt proves an engine-owned seal. Reopen an
                # unanchored claim and record the coordinator's crash decision.
                manifest["lifecycle"] = "active"
                manifest["sealed_at"] = None
                manifest["inventory"] = []
                manifest["incompleteness_reasons"] = []
                manifest["usage"] = None
                manifest["failure"] = None
                write_json_atomic(
                    path=trace_root / "trace_manifest.json", payload=manifest
                )
                trace_protocol_errors = [
                    "trace claimed finalization before coordinator abandonment"
                ]
            else:
                trace_protocol_errors = []
            if manifest.get("lifecycle") != "active":
                return False
            trace_write_json(
                trace_root=trace_root,
                relative_path="service/recovery.json",
                payload={
                    "schema_version": 1,
                    "kind": "iteration_abandoned",
                    "attempt_id": task.attempt_id,
                    "error": error,
                    "trace_protocol_errors": trace_protocol_errors,
                    "recorded_at": utc_now().isoformat().replace("+00:00", "Z"),
                },
            )
            update_trace_manifest(
                trace_root=trace_root, updates={"channels": {"service": "incomplete"}}
            )
            seal_attempt_trace(
                trace_root=trace_root,
                usage=None,
                failure={"error": error, "failure_kind": "crash"},
                incomplete=True,
                repo_root=self.repo_root,
            )
            self._verify_abandoned_trace(trace_root=trace_root, task=task, error=error)
            return True
        except Exception:
            logger.warning(
                "failed to finalize abandoned trace for attempt %s",
                task.attempt_id,
                exc_info=True,
            )
            return False

    def _verify_abandoned_trace(
        self, *, trace_root: Path, task: CurrentTask, error: str
    ) -> None:
        """Verify that an anchored trace records this exact crash decision."""

        manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
        if manifest.get("lifecycle") != "incomplete":
            raise TraceError("anchored abandoned trace is not incomplete")
        integrity = verify_trace_integrity(
            trace_root=trace_root, repo_root=self.repo_root
        )
        if integrity.get("status") != "verified":
            raise TraceError("anchored abandoned trace no longer verifies")
        try:
            recovery = json.loads(
                trace_root.joinpath("service/recovery.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise TraceError(
                "anchored abandoned trace lacks its recovery record"
            ) from exc
        if not isinstance(recovery, dict) or any(
            recovery.get(field) != expected
            for field, expected in {
                "schema_version": 1,
                "kind": "iteration_abandoned",
                "attempt_id": task.attempt_id,
                "error": error,
            }.items()
        ):
            raise TraceError("anchored abandoned trace recovery record does not match")
        failure = manifest.get("failure")
        if not isinstance(failure, dict) or any(
            failure.get(field) != expected
            for field, expected in {"error": error, "failure_kind": "crash"}.items()
        ):
            raise TraceError("anchored abandoned trace failure does not match")

    def _track_workflow_failure_cap(
        self, *, state: LoopState, workflow_id: str, success: bool
    ) -> None:
        """Per-workflow circuit breaker (P2.3).

        Counts consecutive failed iterations per workflow id (including
        crash-abandoned iterations recorded during /register recovery); any
        success of that workflow resets its counter. At the cap the loop
        stops terminally instead of retrying a wedged workflow until
        max_turns. Does not overwrite a stop decision already made in this
        mutation (e.g. goal_check_broken).
        """
        if success:
            state.workflow_consecutive_failures.pop(workflow_id, None)
            return
        count = state.workflow_consecutive_failures.get(workflow_id, 0) + 1
        state.workflow_consecutive_failures[workflow_id] = count
        cap = self.preflight.root_config.workflow_consecutive_failures_cap
        if count >= cap and state.status == "running" and state.stop_reason is None:
            state.status = "failed"
            state.stop_reason = "workflow_failure_cap"

    def _recover_orphaned_agents(self, *, current_task: CurrentTask) -> RecoveryOutcome:
        """Apply the configured recovery policy to an interrupted task's agents.

        Runs in phase A, OUTSIDE the state lock — draining can take up to the
        configured timeout without blocking status/stop or /finished.

        Host gate: process signals only reach this host. When the recorded
        worker ran elsewhere (shared-filesystem deployments), reaping here
        would probe/kill the wrong host's pids and could falsely report the
        orphans handled — skip instead and leave a plain abandonment.
        """
        config = self.preflight.root_config
        worker = current_task.worker
        if worker is not None and worker.hostname != socket.gethostname():
            logger.warning(
                "worker for iteration %04d_%s ran on %s (not this host); "
                "skipping orphan recovery — its agent processes cannot be "
                "reached from here",
                current_task.iteration,
                current_task.workflow_id,
                worker.hostname,
            )
            return RecoveryOutcome(policy=config.recovery_policy)
        return recover_interrupted_iteration(
            repo_root=self.repo_root,
            session_id=current_task.session_id,
            iteration=current_task.iteration,
            workflow_id=current_task.workflow_id,
            policy=config.recovery_policy,
            drain_timeout_s=config.recovery_drain_timeout_s,
            attempt_id=current_task.attempt_id,
        )

    def _read_recoverable_finished_request(
        self, *, current_task: CurrentTask
    ) -> tuple[FinishedRequest, Path | None, WorkflowSetContract | None] | None:
        """Recover a provenance-valid completion from worker outbox artifacts."""

        pending = pending_finished_request_path(
            repo_root=self.repo_root,
            session_id=current_task.session_id,
            iteration=current_task.iteration,
            workflow_id=current_task.workflow_id,
        )
        request = _read_signal(path=pending, model=FinishedRequest)
        if request is not None and _matches_current_task(
            request=request, current_task=current_task
        ):
            try:
                frozen_workflow_contract = self._validate_finished_binding(
                    active=current_task, request=request
                )
            except WorkerBusyError:
                logger.warning(
                    "ignoring completion recovery file with invalid provenance: %s",
                    pending,
                )
            else:
                return request, pending, frozen_workflow_contract

        result = _read_signal(
            path=result_path(
                repo_root=self.repo_root,
                session_id=current_task.session_id,
                iteration=current_task.iteration,
                workflow_id=current_task.workflow_id,
            ),
            model=IterationResult,
        )
        if result is None:
            return None
        if (
            current_task.attempt_id is not None
            and result.attempt_id != current_task.attempt_id
        ):
            # The artifact belongs to a superseded (or unversioned) attempt:
            # accepting it would let a stale result complete the NEW attempt
            # right after its stale pending file was correctly rejected (M5).
            return None
        recovered_request = FinishedRequest(
            session_id=current_task.session_id,
            workflow_id=current_task.workflow_id,
            iteration=current_task.iteration,
            success=result.success,
            text=result.text,
            error=result.error,
            attempt_id=result.attempt_id,
            failure_kind=result.failure_kind,
            usage=result.usage,
            duration_s=result.duration_s,
            worker=result.worker,
            repository_id=result.repository_id,
            assignment_sha256=result.assignment_sha256,
            harness_run_id=result.harness_run_id,
            trace_manifest_path=result.trace_manifest_path,
            trace_incomplete=result.trace_incomplete,
            trace_error=result.trace_error,
        )
        try:
            frozen_workflow_contract = self._validate_finished_binding(
                active=current_task, request=recovered_request
            )
        except WorkerBusyError:
            logger.warning(
                "ignoring result recovery file with invalid provenance for attempt %s",
                current_task.attempt_id,
            )
            return None
        return recovered_request, None, frozen_workflow_contract

    def _prepare_state(self, *, resume: bool) -> None:
        """Create fresh state or reconstruct the active session stack on resume."""

        existing_state = self.state_store.read_state()
        if existing_state is None:
            self._write_fresh_state()
            return
        self._validate_resumable_state(
            store=self.state_store, state=existing_state, context="top-level session"
        )
        terminal_with_inflight_projection = self.state_store.is_terminal_state(
            state=existing_state
        ) and (
            existing_state.current_task is not None
            or existing_state.active_child_session_id is not None
        )
        if (
            self.state_store.is_terminal_state(state=existing_state)
            and not terminal_with_inflight_projection
        ):
            if self._protocol_version_for_state(state=existing_state) >= 3:
                self._ensure_session_outcome(state=existing_state)
            self.state_store.archive_state()
            self._write_fresh_state()
            return
        if not resume:
            raise ConfigError(
                "Found running loopy-loop state. Restart with --resume to continue "
                "the in-progress session."
            )
        active_state = self._reconstruct_session_stack(top_state=existing_state)
        if active_state.schema_version >= 2:
            self._validate_active_path_contracts(state=active_state)
        else:
            legacy_contract = self._preflight_for(
                workflow_set=active_state.workflow_set
            ).workflow_contract.model_copy(update={"session_protocol_version": 1})
            create_session_dir(
                repo_root=self.repo_root,
                session_id=active_state.active_session_id,
                goal_hash=active_state.goal_hash,
                goal=active_state.config_snapshot.goal,
                workflow_set=active_state.workflow_set,
                parent_session_id=active_state.parent_session_id,
                workflow_contract=legacy_contract.model_dump(),
                schema_version=1,
            )

    def _validate_resumable_state(
        self, *, store: StateStore, state: LoopState, context: str
    ) -> None:
        """Turn impossible persisted v2 shapes into a legible startup error."""
        try:
            store.validate_committed_state(state=state)
        except AttemptArtifactInvariantError as exc:
            # Frozen-file tamper is an attempt failure with an autonomous
            # abandonment/reissue path, not a reason to wedge coordinator
            # startup before recovery can run.
            logger.warning(
                "live attempt artifacts changed in %s %s; recovery will "
                "abandon and reissue the attempt: %s",
                context,
                state.active_session_id,
                exc,
            )
            return
        except StateInvariantError as exc:
            raise ConfigError(
                f"Cannot resume {context} {state.active_session_id}: {exc}. "
                "The v2 state contradicts its frozen assignment; inspect the "
                "state and protocol artifacts before retrying."
            ) from exc

    def _validate_session_contract(self, *, state: LoopState) -> None:
        """Verify v2 identity and restore its agent-visible contract projection.

        The complete workflow contract stored in ``LoopState`` is the
        coordinator-owned trust root.  ``session.json`` and
        ``workflow_contract.json`` are intentionally visible to agents, so a
        consistent rewrite of both files must not change protocol semantics.
        When only that projection drifts, restore it from state before the
        next attempt; unrelated identity contradictions still fail visibly.
        """

        session_root = session_dir_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        manifest_path = session_root / "session.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"v2 session manifest is missing or unreadable at {manifest_path}: {exc}"
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
            raise ConfigError(f"v2 session has an invalid manifest at {manifest_path}")
        expected_identity = {
            "session_id": state.active_session_id,
            "root_session_id": state.root_session_id,
            "parent_session_id": state.parent_session_id,
            "depth": state.depth,
            "workflow_set": state.workflow_set,
            "goal_hash": state.goal_hash,
        }
        for field, expected in expected_identity.items():
            if manifest.get(field) != expected:
                raise ConfigError(
                    f"session manifest field {field!r} contradicts state for "
                    f"{state.active_session_id}"
                )
        trusted_contract = state.workflow_contract
        if trusted_contract is None:
            raise ConfigError(
                "v2 session state has no engine-owned workflow contract trust root"
            )
        goal_path = goal_contract_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        contract_path = workflow_contract_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not goal_path.is_file():
            raise ConfigError(f"immutable v2 session artifact is missing: {goal_path}")
        projected_contract: WorkflowSetContract | None = None
        try:
            projected_contract = self._read_workflow_contract(
                session_id=state.active_session_id
            )
        except ConfigError:
            pass
        projected_hash = (
            file_sha256(path=contract_path) if contract_path.is_file() else None
        )
        if (
            projected_contract != trusted_contract
            or manifest.get("workflow_contract_hash") != projected_hash
        ):
            write_json_atomic(
                path=contract_path, payload=trusted_contract.model_dump(mode="json")
            )
            manifest["workflow_contract_hash"] = file_sha256(path=contract_path)
            write_json_atomic(path=manifest_path, payload=manifest)
            logger.warning(
                "restored workflow contract projection for v2 session %s from "
                "engine-owned state",
                state.active_session_id,
            )
        expected_hash = manifest.get("goal_contract_hash")
        if expected_hash != file_sha256(path=goal_path):
            raise ConfigError(
                f"immutable goal contract hash mismatch for session "
                f"{state.active_session_id}"
            )
        expected_workflow_hash = manifest.get("workflow_contract_hash")
        if expected_workflow_hash != file_sha256(path=contract_path):
            raise ConfigError(
                f"immutable workflow contract hash mismatch for session "
                f"{state.active_session_id}"
            )
        try:
            goal_contract = json.loads(goal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # pragma: no cover - hash read succeeded
            raise ConfigError(f"invalid goal contract at {goal_path}: {exc}") from exc
        if (
            goal_contract.get("session_id") != state.active_session_id
            or goal_contract.get("goal_hash") != state.goal_hash
            or goal_contract.get("goal") != state.config_snapshot.goal
        ):
            raise ConfigError(
                f"session state identity contradicts immutable goal contract at "
                f"{goal_path}"
            )
        if (
            state.config_snapshot.goal != goal_contract.get("goal")
            or state.config_snapshot.goal_hash != state.goal_hash
            or state.config_snapshot.workflow_set != state.workflow_set
        ):
            raise ConfigError(
                f"frozen execution config contradicts session identity for "
                f"{state.active_session_id}"
            )

    def _validate_active_path_contracts(self, *, state: LoopState) -> None:
        """Validate every session contract on the active child-to-root path."""

        current = state
        seen: set[str] = set()
        while True:
            if current.active_session_id in seen:
                raise ConfigError("cycle detected in active session parent chain")
            seen.add(current.active_session_id)
            current_store = self._store_for(session_id=current.active_session_id)
            self._validate_resumable_state(
                store=current_store, state=current, context="active session path"
            )
            self._validate_session_contract(state=current)
            if current.parent_session_id is None:
                return
            parent_state = self._store_for(
                session_id=current.parent_session_id
            ).read_state()
            if parent_state is None:
                raise ConfigError(
                    "active session parent state is missing for "
                    f"{state.active_session_id}"
                )
            current = parent_state

    def _reconstruct_session_stack(self, *, top_state: LoopState) -> LoopState:
        """Walk the durable parent->child pointers to the deepest live session.

        A restarted coordinator previously reopened the latest TOP-LEVEL
        session, silently orphaning a running child. Now:

        - a non-terminal child pointed at by its parent becomes the active
          session (the parent stays suspended, exactly as before the crash);
        - a terminal child is finalized (children.json completed, pointer
          cleared) and the walk resumes the parent;
        - a dangling pointer (child state missing — the dispatch crashed
          between commits) is cleared so the parent redispatches cleanly;
        - a parent with NO pointer but a running children.json record whose
          child is live ADOPTS it (the crash window where the child was fully
          created but the parent commit never landed).
        """
        top_store = self._store_for(session_id=top_state.active_session_id)
        state = self._hydrate_legacy_state_identity(store=top_store, state=top_state)
        while True:
            self._validate_resumable_state(
                store=self._store_for(session_id=state.active_session_id),
                state=state,
                context="session stack",
            )
            if (
                state.schema_version >= 2
                and state.current_task is not None
                and state.active_child_session_id is not None
            ):
                raise ChildLedgerError(
                    f"session {state.active_session_id} cannot own a live task "
                    "and an active child at the same committed revision"
                )
            parent_store = self._store_for(session_id=state.active_session_id)
            child_id = state.active_child_session_id
            if child_id is None:
                # The parent attempt may itself need completion recovery.  A
                # live child edge without a pointer is the cross-file dispatch
                # crash window; `_register_attempt` first commits the parent
                # result, then adopts and re-plans the child's worker recovery.
                if state.current_task is not None:
                    return state
                child_id = self._adoptable_child_id(parent_state=state)
            if child_id is None:
                return state
            record, child_store, child_state = self._validated_child_edge(
                parent_state=state, child_session_id=child_id
            )
            if child_state is None:
                if record.get("status") not in {
                    "dispatching",
                    "running",
                    "failed_dispatch",
                }:
                    raise ChildLedgerError(
                        f"parent {state.active_session_id} points at terminal child "
                        f"{child_id}, but its state is missing"
                    )
                logger.warning(
                    "session %s points at child %s whose state is missing; "
                    "marking its exact ledger edge failed_dispatch and clearing "
                    "the pointer",
                    state.active_session_id,
                    child_id,
                )
                self._mark_child_record_failed_dispatch(
                    parent_session_id=state.active_session_id,
                    child_session_id=child_id,
                    reason="child state was never written",
                )
                state = self._set_child_pointer(
                    store=parent_store, child_session_id=None
                )
                return state
            child_state = self._hydrate_legacy_state_identity(
                store=child_store, state=child_state
            )
            self._validate_resumable_state(
                store=child_store, state=child_state, context="child session"
            )
            self._validate_child_state_identity(
                parent_state=state,
                child_state=child_state,
                record=record,
                child_manifest=self._read_child_manifest_for_edge(
                    parent_session_id=state.active_session_id, child_session_id=child_id
                ),
            )
            if child_store.is_terminal_state(state=child_state):
                self._mark_child_record_complete(
                    child_state=child_state, parent_state=state
                )
                self._clear_child_pointer(
                    parent_store=parent_store,
                    parent_session_id=state.active_session_id,
                    child_session_id=child_id,
                )
                refreshed = parent_store.read_state()
                if refreshed is None:  # pragma: no cover - state just existed
                    raise RuntimeError("parent state vanished during recovery")
                return refreshed
            # Ensure the adopted case is persisted as a real pointer.
            if state.active_child_session_id != child_id:
                state = self._set_child_pointer(
                    store=parent_store, child_session_id=child_id
                )
            self.state_store = child_store
            state = child_state

    def _adoptable_child_id(self, *, parent_state: LoopState) -> str | None:
        """Reconcile EVERY running-projected child record, then return the
        adoptable live child (if any).

        Handles all the projections a crash (or a pre-pointer version of
        loopy-loop) can leave behind:
        - record running, child state TERMINAL -> finalize the record, remove
          its leftover request file (previously ignored forever — M3);
        - record running, child state MISSING -> mark the record
          failed_dispatch so its request file redispatches exactly once (M2);
        - record running, child state running and parent linkage correct ->
          the crash window where the child was fully created but the parent's
          pointer commit never landed: adopt it.
        """
        parent_session_id = parent_state.active_session_id
        payload = self._read_or_repair_children_payload(
            path=children_path(repo_root=self.repo_root, session_id=parent_session_id)
        )
        adoptable: str | None = None
        changed = False
        for record in payload["children"]:
            if record.get("status") not in {"dispatching", "running"}:
                continue
            child_id = record.get("session_id")
            if not child_id:
                continue
            child_store = self._physical_child_store(
                parent_session_id=parent_session_id, child_session_id=child_id
            )
            child_state = child_store.read_state()
            if child_state is None:
                record["status"] = "failed_dispatch"
                record["stop_reason"] = "child state was never written"
                changed = True
                continue
            child_state = self._hydrate_legacy_state_identity(
                store=child_store, state=child_state
            )
            self._validate_child_state_identity(
                parent_state=parent_state,
                child_state=child_state,
                record=record,
                child_manifest=self._read_child_manifest_for_edge(
                    parent_session_id=parent_session_id, child_session_id=child_id
                ),
            )
            if child_store.is_terminal_state(state=child_state):
                self._mark_child_record_complete(
                    child_state=child_state, parent_state=parent_state
                )
                request_file = record.get("request_file")
                if request_file:
                    (
                        child_requests_dir_path(
                            repo_root=self.repo_root, session_id=parent_session_id
                        )
                        / request_file
                    ).unlink(missing_ok=True)
                continue
            if adoptable is not None:
                raise ChildLedgerError(
                    "multiple live child edges survived bounded ledger validation: "
                    f"{adoptable}, {child_id}"
                )
            if record.get("status") == "dispatching":
                record["status"] = "running"
                changed = True
            adoptable = child_id
        if changed:
            _bump_children_revision(payload=payload)
            write_json_atomic(
                path=children_path(
                    repo_root=self.repo_root, session_id=parent_session_id
                ),
                payload=payload,
            )
        return adoptable

    def _store_for(self, *, session_id: str) -> StateStore:
        return StateStore(
            repo_root=self.repo_root,
            state_path=state_path(repo_root=self.repo_root, session_id=session_id),
        )

    def _set_child_pointer(
        self, *, store: StateStore, child_session_id: str | None
    ) -> LoopState:
        def mutator(state: LoopState | None) -> tuple[LoopState, LoopState]:
            current = _require_state(state=state)
            current.active_child_session_id = child_session_id
            return current, current

        return store.mutate(mutator)

    def _hydrate_legacy_state_identity(
        self, *, store: StateStore, state: LoopState
    ) -> LoopState:
        """Persist the physical tree identity omitted by legacy state files.

        ``LoopState`` historically had no root/depth fields. Its model defaults
        therefore make every deserialized legacy child look like a root. The
        topology resolver derives those facts from the immutable directory and
        parent manifests; persisting them once keeps every later edge check and
        assignment consistent without upgrading the session protocol itself.
        """
        if state.schema_version >= 2:
            return state
        try:
            identity = LogicalReferenceResolver.for_session(
                repo_root=self.repo_root, session_id=state.active_session_id
            ).current
        except LogicalReferenceError as exc:
            raise ChildLedgerError(
                f"cannot derive legacy session identity for "
                f"{state.active_session_id}: {exc}"
            ) from exc
        expected = (
            identity.parent_session_id,
            identity.root_session_id,
            identity.depth,
        )
        observed = (state.parent_session_id, state.root_session_id, state.depth)
        if observed == expected:
            return state
        state.parent_session_id = identity.parent_session_id
        state.root_session_id = identity.root_session_id
        state.depth = identity.depth
        return store.write_state(state=state)

    def _physical_child_store(
        self, *, parent_session_id: str, child_session_id: str
    ) -> StateStore:
        """Return the state store physically nested beneath a parent session."""

        if not SAFE_DURABLE_ID_PATTERN.fullmatch(child_session_id):
            raise ChildLedgerError(
                f"child ledger contains an unsafe session ID: {child_session_id!r}"
            )
        parent_root = session_dir_path(
            repo_root=self.repo_root, session_id=parent_session_id
        )
        child_state_path = parent_root / "children" / child_session_id / "state.json"
        return StateStore(repo_root=self.repo_root, state_path=child_state_path)

    def _read_child_manifest_for_edge(
        self, *, parent_session_id: str, child_session_id: str, required: bool = True
    ) -> dict[str, Any] | None:
        """Read the immutable child manifest that corroborates a ledger edge."""

        parent_root = session_dir_path(
            repo_root=self.repo_root, session_id=parent_session_id
        )
        path = parent_root / "children" / child_session_id / "session.json"
        if not path.is_file():
            if not required:
                return None
            raise ChildLedgerError(
                f"child edge {parent_session_id}->{child_session_id} has no "
                "session manifest"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ChildLedgerError(
                f"child manifest is unreadable at {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ChildLedgerError(f"child manifest is not an object at {path}")
        return payload

    def _validate_child_state_identity(
        self,
        *,
        parent_state: LoopState,
        child_state: LoopState,
        record: dict[str, Any],
        child_manifest: dict[str, Any] | None,
    ) -> None:
        """Require child state, manifest, and parent ledger identity to agree."""

        child_id = record.get("session_id")
        if child_manifest is None:
            raise ChildLedgerError(
                f"child {child_id} has state but no immutable session manifest"
            )
        expected_state = {
            "active_session_id": child_id,
            "parent_session_id": parent_state.active_session_id,
            "root_session_id": (
                parent_state.root_session_id or parent_state.active_session_id
            ),
            "depth": parent_state.depth + 1,
            "workflow_set": record.get("workflow_set"),
            "goal_hash": record.get("goal_hash"),
            "request_id": record.get("request_id"),
        }
        for field, expected in expected_state.items():
            observed = getattr(child_state, field)
            # Legacy records did not carry a stable request identity.
            if field == "request_id" and expected is None:
                continue
            if observed != expected:
                raise ChildLedgerError(
                    f"child edge {parent_state.active_session_id}->{child_id} "
                    f"contradicts child state field {field!r}: expected "
                    f"{expected!r}, got {observed!r}"
                )

        expected_manifest = {
            "session_id": child_id,
            "parent_session_id": parent_state.active_session_id,
            "workflow_set": record.get("workflow_set"),
            "goal_hash": record.get("goal_hash"),
        }
        if child_manifest.get("schema_version", 1) >= 2:
            expected_manifest.update(
                {
                    "root_session_id": expected_state["root_session_id"],
                    "depth": expected_state["depth"],
                }
            )
        for field, expected in expected_manifest.items():
            if child_manifest.get(field) != expected:
                raise ChildLedgerError(
                    f"child edge {parent_state.active_session_id}->{child_id} "
                    f"contradicts child manifest field {field!r}"
                )

        if child_manifest.get("schema_version", 1) >= 2:
            origin = child_manifest.get("origin")
            if not isinstance(origin, dict):
                raise ChildLedgerError(
                    f"v2 child {child_id} has no immutable origin object"
                )
            origin_expectations = {
                "request_id": record.get("request_id"),
                "parent_attempt_id": record.get("parent_attempt_id"),
                "parent_work_item_id": record.get("parent_work_item_id"),
                "accepted_request_sha256": record.get("accepted_request_sha256"),
            }
            for field, expected in origin_expectations.items():
                if origin.get(field) != expected:
                    raise ChildLedgerError(
                        f"child edge {parent_state.active_session_id}->{child_id} "
                        f"contradicts immutable origin field {field!r}"
                    )

    def _validated_child_edge(
        self, *, parent_state: LoopState, child_session_id: str
    ) -> tuple[dict[str, Any], StateStore, LoopState | None]:
        """Resolve a unique, identity-valid child edge and its durable state."""

        payload = self._read_or_repair_children_payload(
            path=children_path(
                repo_root=self.repo_root, session_id=parent_state.active_session_id
            )
        )
        matches = [
            record
            for record in payload["children"]
            if record.get("session_id") == child_session_id
        ]
        if len(matches) != 1:
            raise ChildLedgerError(
                f"active child pointer {parent_state.active_session_id}->"
                f"{child_session_id} requires exactly one ledger edge; found "
                f"{len(matches)}"
            )
        record = matches[0]
        child_store = self._physical_child_store(
            parent_session_id=parent_state.active_session_id,
            child_session_id=child_session_id,
        )
        child_state = child_store.read_state()
        manifest = self._read_child_manifest_for_edge(
            parent_session_id=parent_state.active_session_id,
            child_session_id=child_session_id,
            required=child_state is not None,
        )
        if child_state is not None:
            child_state = self._hydrate_legacy_state_identity(
                store=child_store, state=child_state
            )
            self._validate_child_state_identity(
                parent_state=parent_state,
                child_state=child_state,
                record=record,
                child_manifest=manifest,
            )
            terminal = child_store.is_terminal_state(state=child_state)
            if record.get("status") == "failed_dispatch":
                raise ChildLedgerError(
                    f"failed-dispatch edge {parent_state.active_session_id}->"
                    f"{child_session_id} unexpectedly has durable child state"
                )
            if record.get("status") in _TERMINAL_CHILD_STATUSES and not terminal:
                raise ChildLedgerError(
                    f"terminal ledger edge {parent_state.active_session_id}->"
                    f"{child_session_id} points at a live child"
                )
        return record, child_store, child_state

    def _mark_child_record_failed_dispatch(
        self, *, parent_session_id: str, child_session_id: str, reason: str
    ) -> None:
        """Mark a child edge failed when publication did not complete."""

        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = self._read_or_repair_children_payload(path=path)
        matches = [
            record
            for record in payload["children"]
            if record.get("session_id") == child_session_id
        ]
        if len(matches) != 1:
            raise ChildLedgerError(
                f"cannot fail child dispatch {parent_session_id}->{child_session_id}: "
                f"expected one ledger edge, found {len(matches)}"
            )
        record = matches[0]
        if record.get("status") not in {"dispatching", "running", "failed_dispatch"}:
            raise ChildLedgerError(
                f"cannot mark terminal child edge {child_session_id} failed_dispatch"
            )
        if (
            record.get("status") == "failed_dispatch"
            and record.get("stop_reason") == reason
        ):
            return
        record["status"] = "failed_dispatch"
        record["stop_reason"] = reason
        _bump_children_revision(payload=payload)
        write_json_atomic(path=path, payload=payload)

    def _write_fresh_state(self) -> None:
        """Create a new root session with frozen v2 identity contracts."""

        protocol_version = self.preflight.workflow_contract.session_protocol_version
        session_id = create_session_id(
            repo_root=self.repo_root,
            goal=self.preflight.root_config.goal,
            parent_session_id=None,
            request_id=None,
        )
        goal_hash = derive_full_goal_hash(goal=self.preflight.root_config.goal)
        layout = _layout_for_protocol(protocol_version=protocol_version)
        created_at = utc_now()
        workflow_roster = (
            _build_workflow_roster(
                session_id=session_id, preflight=self.preflight, created_at=created_at
            )
            if protocol_version >= 3
            else None
        )
        capability_roster = (
            build_harness_capability_roster(
                config=self.preflight.root_config,
                root_session_id=session_id,
                root_execution_config_sha256=_model_sha256(
                    model=self.preflight.root_config
                ),
                created_at=created_at,
            )
            if protocol_version >= 3
            else None
        )
        create_session_dir(
            repo_root=self.repo_root,
            session_id=session_id,
            goal_hash=goal_hash,
            goal=self.preflight.root_config.goal,
            workflow_set=self.preflight.workflow_set,
            root_session_id=session_id,
            depth=0,
            layer_kind=self.preflight.workflow_contract.layer_kind,
            completion_criteria=self.preflight.root_config.completion_criteria,
            stop_criteria=self.preflight.root_config.stop_criteria,
            workflow_contract=self.preflight.workflow_contract.model_dump(),
            workflow_roster_payload=(
                workflow_roster.model_dump(mode="json")
                if workflow_roster is not None
                else None
            ),
            harness_capability_roster_payload=(
                capability_roster.model_dump(mode="json")
                if capability_roster is not None
                else None
            ),
            session_protocol_version=protocol_version,
            schema_version=2,
            layout=layout,
        )
        self.state_store = StateStore(
            repo_root=self.repo_root,
            state_path=state_path(repo_root=self.repo_root, session_id=session_id),
        )
        snapshot = RootConfigSnapshot.model_validate(
            self.preflight.root_config.model_dump(exclude=_COORDINATOR_ONLY_FIELDS)
        ).model_copy(update={"goal_hash": goal_hash})
        state = LoopState(
            schema_version=2,
            status="running",
            goal_hash=goal_hash,
            workflow_set=self.preflight.workflow_set,
            max_turns=self.preflight.root_config.max_turns,
            active_session_id=session_id,
            root_session_id=session_id,
            depth=0,
            config_snapshot=snapshot,
            workflow_contract=self.preflight.workflow_contract,
            workflow_roster=workflow_roster,
            harness_capability_roster=capability_roster,
        )
        self.state_store.write_state(state=state)
        self._emit(
            session_id=session_id,
            event_type="session_started",
            payload={
                "goal_hash": goal_hash,
                "workflow_set": self.preflight.workflow_set,
                "max_turns": self.preflight.root_config.max_turns,
            },
        )
        self._flush_pending_events()

    def _preflight_for(self, *, workflow_set: str) -> PreflightResult:
        """Workflow definitions + set validation only. A child session's
        execution config never comes from here — it inherits the parent's
        frozen config_snapshot (see _dispatch_child_session_if_requested),
        so a mid-session edit of loopy_loop_config.yaml cannot split the
        session tree across different models or policies."""
        self._reload_preflights_if_requested()
        preflight = self.preflights.get(workflow_set)
        if preflight is None:
            preflight = run_preflight(
                repo_root=self.repo_root, workflow_set=workflow_set
            )
            self.preflights[workflow_set] = preflight
        return preflight

    def _read_preflight_reload_request_id(self) -> str | None:
        """Read the explicit operator reload generation, best-effort."""

        path = preflight_reload_request_path(repo_root=self.repo_root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        request_id = payload.get("request_id")
        return request_id if isinstance(request_id, str) and request_id else None

    def _reload_preflights_if_requested(self) -> None:
        """Atomically refresh mutable preflight inputs after `loopy reload`.

        Workflow prompts and coordinator-operational root settings are mutable.
        Workflow membership/config/cadence, role contracts, the session config
        snapshot, and model/capability rosters remain frozen for live sessions.
        """

        request_id = self._read_preflight_reload_request_id()
        if request_id is None or request_id == self._preflight_reload_request_id:
            return
        refreshed: dict[str, PreflightResult] = {}
        for workflow_set, cached in self.preflights.items():
            loaded = run_preflight(repo_root=self.repo_root, workflow_set=workflow_set)
            cached_by_id = {workflow.id: workflow for workflow in cached.workflows}
            loaded_by_id = {workflow.id: workflow for workflow in loaded.workflows}
            if set(cached_by_id) != set(loaded_by_id):
                raise ConfigError(
                    "hot reload cannot change the session-frozen workflow roster "
                    f"for {workflow_set!r}; restart with a new session"
                )
            workflows = [
                workflow.model_copy(
                    update={
                        "prompt_path": loaded_by_id[workflow.id].prompt_path,
                        "prompt_text": loaded_by_id[workflow.id].prompt_text,
                        "prompt_sha256": loaded_by_id[workflow.id].prompt_sha256,
                    }
                )
                for workflow in cached.workflows
            ]
            root_config = cached.root_config.model_copy(
                update={
                    field: getattr(loaded.root_config, field)
                    for field in _HOT_RELOADABLE_ROOT_FIELDS
                }
            )
            refreshed[workflow_set] = cached.model_copy(
                update={"root_config": root_config, "workflows": workflows}
            )
        self.preflights = refreshed
        self.preflight = refreshed[self.preflight.workflow_set]
        self._preflight_reload_request_id = request_id
        logger.info(
            "reloaded workflow prompts and coordinator-operational config (%s)",
            request_id,
        )

    def _workflows_for(self, *, workflow_set: str) -> list[WorkflowDefinition]:
        return self._preflight_for(workflow_set=workflow_set).workflows

    def _eval_requested(self, *, state: LoopState) -> bool:
        """Return whether the active session has a pending eval request.

        The file-existence predicate behind `run_when_requested`: a workflow so
        marked is eligible only while the orchestrator's
        project_state/eval_request.md stands.
        """

        return eval_request_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        ).exists()

    def _workflows_by_id_for(
        self, *, workflow_set: str
    ) -> dict[str, WorkflowDefinition]:
        return {
            workflow.id: workflow
            for workflow in self._workflows_for(workflow_set=workflow_set)
        }

    def _dispatch_child_session_if_requested(
        self, *, state: LoopState, caller: WorkerIdentity | None = None
    ) -> TaskResponse | None:
        """Accept at most one pending child request and dispatch its first task."""

        requests_dir = child_requests_dir_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not requests_dir.exists():
            return None
        dispatched_request_files = self._dispatched_request_files(
            parent_session_id=state.active_session_id
        )
        dispatched_request_ids = self._dispatched_request_ids(
            parent_session_id=state.active_session_id
        )
        candidates = [
            *requests_dir.glob("*.json"),
            *child_requests_pending_dir_path(
                repo_root=self.repo_root, session_id=state.active_session_id
            ).glob("*.json"),
        ]
        for request_path in sorted(candidates):
            request = _read_signal(path=request_path, model=ChildSessionRequest)
            if request is None:
                _reject_request(
                    request_path=request_path, reason="invalid JSON or schema"
                )
                continue
            if not SAFE_DURABLE_ID_PATTERN.fullmatch(request.workflow_set):
                _reject_request(
                    request_path=request_path,
                    reason="workflow_set must be a filesystem-safe durable ID",
                )
                continue
            parent_contract = self._workflow_contract_for_state(state=state)
            if (
                parent_contract.session_protocol_version >= 2
                and request.schema_version < 2
            ):
                _reject_request(
                    request_path=request_path,
                    reason="this session contract requires child request v2",
                )
                continue
            request_id = _child_request_id(request=request, path=request_path)
            if request.schema_version >= 2:
                replay_state = self._accepted_request_replay_state(
                    parent_session_id=state.active_session_id,
                    request_id=request_id,
                    request_path=request_path,
                )
                if replay_state == "conflict":
                    _reject_request(
                        request_path=request_path,
                        reason=(
                            f"child request id {request_id!r} was reused with a "
                            "different body than its immutable accepted archive"
                        ),
                    )
                    continue
                if (
                    request_id in dispatched_request_ids
                    or request_path.name in dispatched_request_files
                ):
                    if replay_state == "exact":
                        request_path.unlink(missing_ok=True)
                    else:
                        _reject_request(
                            request_path=request_path,
                            reason=(
                                "a live child tombstone exists, but its immutable "
                                "accepted request is missing"
                            ),
                        )
                    continue
            elif request_path.name in dispatched_request_files:
                # Legacy requests have no stable request ID; their live
                # filename tombstone remains the only available replay key.
                request_path.unlink(missing_ok=True)
                continue
            if request.schema_version >= 2:
                latest = state.history[-1] if state.history else None
                if (
                    latest is None
                    or not latest.success
                    or request.origin is None
                    or request.origin.parent_attempt_id != latest.attempt_id
                ):
                    _reject_request(
                        request_path=request_path,
                        reason=(
                            "child request origin.parent_attempt_id must match "
                            "the latest successful parent attempt"
                        ),
                    )
                    continue
            try:
                validated_inputs = self._validate_child_request_inputs(
                    parent_state=state, request=request
                )
            except ConfigError as exc:
                _reject_request(request_path=request_path, reason=str(exc))
                continue
            # Total transition (M6): a schema-valid request that cannot be
            # dispatched — unknown workflow set, broken workflow configs, or a
            # set with no initially eligible workflow — must be terminally
            # rejected, never left to wedge every future completion with the
            # same error.
            try:
                preflight = self._preflight_for(workflow_set=request.workflow_set)
            except ConfigError as exc:
                _reject_request(request_path=request_path, reason=str(exc))
                continue
            workflows = preflight.workflows
            workflow = choose_next_workflow(
                workflows=workflows, history=[], iteration_count=0
            )
            if workflow is None:
                _reject_request(
                    request_path=request_path,
                    reason="workflow set has no initially eligible workflow",
                )
                continue
            goal = request.effective_goal
            goal_hash = derive_full_goal_hash(goal=goal)
            child_session_id = self._reusable_failed_dispatch_child_id(
                parent_session_id=state.active_session_id,
                request_id=request_id,
                workflow_set=request.workflow_set,
                goal_hash=goal_hash,
            ) or create_session_id(
                repo_root=self.repo_root,
                goal=goal,
                parent_session_id=state.active_session_id,
                request_id=request_id,
            )
            try:
                accepted_path = self._archive_accepted_request(
                    parent_session_id=state.active_session_id,
                    request_id=request_id,
                    request_path=request_path,
                )
            except ConfigError as exc:
                _reject_request(request_path=request_path, reason=str(exc))
                continue
            assignment = request.assignment
            accepted_request_ref = (
                f"session:{state.active_session_id}:/child_requests/accepted/"
                f"{accepted_path.name}"
            )
            accepted_request_sha256 = file_sha256(path=accepted_path)
            frozen_input_files: dict[str, bytes] = {
                "accepted_request.json": accepted_path.read_bytes()
            }
            child_inputs: list[ArtifactInputRef] = []
            source_inputs: list[ArtifactInputRef] = []
            for index, (source_input, content) in enumerate(validated_inputs, start=1):
                frozen_name = (
                    f"artifacts/input-{index:04d}-"
                    f"{source_input.sha256.removeprefix('sha256:')[:12]}.artifact"
                )
                frozen_input_files[frozen_name] = content
                child_inputs.append(
                    ArtifactInputRef(
                        ref=f"session:/inputs/{frozen_name}", sha256=source_input.sha256
                    )
                )
                source_inputs.append(source_input)
            parent_attempt_id = (
                request.origin.parent_attempt_id
                if request.origin is not None
                else (state.history[-1].attempt_id if state.history else None)
            )
            parent_work_item_id = (
                request.origin.parent_work_item_id
                if request.origin is not None
                else None
            )
            self._record_child_dispatch_intent(
                parent_session_id=state.active_session_id,
                record=ChildSessionRecord(
                    session_id=child_session_id,
                    workflow_set=request.workflow_set,
                    goal_hash=goal_hash,
                    status="dispatching",
                    created_at=utc_now(),
                    request_file=request_path.name,
                    request_id=request_id,
                    accepted_request_ref=(
                        f"session:/child_requests/accepted/{accepted_path.name}"
                    ),
                    accepted_request_sha256=accepted_request_sha256,
                    parent_attempt_id=parent_attempt_id,
                    parent_work_item_id=parent_work_item_id,
                ),
            )
            child_protocol_version = (
                preflight.workflow_contract.session_protocol_version
            )
            child_workflow_roster = (
                _build_workflow_roster(
                    session_id=child_session_id,
                    preflight=preflight,
                    created_at=utc_now(),
                )
                if child_protocol_version >= 3
                else None
            )
            if child_protocol_version >= 3 and state.harness_capability_roster is None:
                raise ConfigError(
                    "protocol v3 child requires the root's frozen harness "
                    "capability roster"
                )
            create_session_dir(
                repo_root=self.repo_root,
                session_id=child_session_id,
                goal_hash=goal_hash,
                goal=goal,
                workflow_set=request.workflow_set,
                parent_session_id=state.active_session_id,
                root_session_id=state.root_session_id or state.active_session_id,
                depth=state.depth + 1,
                layer_kind=preflight.workflow_contract.layer_kind,
                completion_criteria=(
                    assignment.completion_criteria if assignment is not None else []
                ),
                stop_criteria=(
                    assignment.stop_criteria if assignment is not None else []
                ),
                constraints=assignment.constraints if assignment is not None else [],
                deliverables=assignment.deliverables if assignment is not None else [],
                required_evidence=(
                    assignment.required_evidence if assignment is not None else []
                ),
                origin_request_id=request_id,
                accepted_request_ref="session:/inputs/accepted_request.json",
                accepted_request_sha256=accepted_request_sha256,
                inputs=[item.model_dump() for item in child_inputs],
                frozen_input_files=frozen_input_files,
                origin={
                    "request_id": request_id,
                    "parent_attempt_id": parent_attempt_id,
                    "parent_work_item_id": parent_work_item_id,
                    "accepted_request_ref": accepted_request_ref,
                    "accepted_request_sha256": accepted_request_sha256,
                    "inputs": [item.model_dump() for item in source_inputs],
                    "frozen_inputs": [item.model_dump() for item in child_inputs],
                },
                workflow_contract=preflight.workflow_contract.model_dump(),
                workflow_roster_payload=(
                    child_workflow_roster.model_dump(mode="json")
                    if child_workflow_roster is not None
                    else None
                ),
                harness_capability_roster_payload=(
                    state.harness_capability_roster.model_dump(mode="json")
                    if state.harness_capability_roster is not None
                    else None
                ),
                session_protocol_version=child_protocol_version,
                schema_version=state.schema_version,
                layout=_layout_for_protocol(protocol_version=child_protocol_version),
            )
            # The child inherits the PARENT's frozen execution config — only
            # goal, goal_hash, and workflow_set change (P0.3's "children
            # inherit root config"; D9's uniform session tree). Re-deriving
            # from the on-disk YAML here would let a mid-session config edit
            # split the tree across different models and policies.
            snapshot = state.config_snapshot.model_copy(
                update={
                    "goal": goal,
                    "goal_hash": goal_hash,
                    "workflow_set": request.workflow_set,
                    "completion_criteria": (
                        assignment.completion_criteria if assignment is not None else []
                    ),
                    "stop_criteria": (
                        assignment.stop_criteria if assignment is not None else []
                    ),
                }
            )
            now = utc_now()
            child_state = LoopState(
                schema_version=state.schema_version,
                status="running",
                goal_hash=goal_hash,
                workflow_set=request.workflow_set,
                parent_session_id=state.active_session_id,
                max_turns=state.config_snapshot.max_turns,
                active_session_id=child_session_id,
                root_session_id=state.root_session_id or state.active_session_id,
                depth=state.depth + 1,
                request_id=request_id,
                work_item_id=parent_work_item_id,
                config_snapshot=snapshot,
                current_task=None,
                workflow_contract=(
                    preflight.workflow_contract if state.schema_version >= 2 else None
                ),
                workflow_roster=child_workflow_roster,
                harness_capability_roster=state.harness_capability_roster,
            )
            child_state.current_task = self._create_current_task(
                state=child_state,
                workflow=workflow,
                iteration=1,
                caller=caller,
                now=now,
            )
            child_store = StateStore(
                repo_root=self.repo_root,
                state_path=state_path(
                    repo_root=self.repo_root, session_id=child_session_id
                ),
            )
            child_store.write_state(state=child_state)
            self._mark_child_record_running(
                parent_session_id=state.active_session_id,
                child_session_id=child_session_id,
            )
            child_task = child_state.current_task
            self._emit(
                session_id=state.active_session_id,
                event_type="child_started",
                payload={
                    "child_session_id": child_session_id,
                    "workflow_set": request.workflow_set,
                    "request_file": request_path.name,
                },
            )
            self._emit(
                session_id=child_session_id,
                event_type="session_started",
                payload={
                    "workflow_set": request.workflow_set,
                    "parent_session_id": state.active_session_id,
                },
            )
            self._emit_task_dispatched(session_id=child_session_id, task=child_task)
            # The durable session-stack pointer: committed with the parent
            # state when this mutator returns, so a restarted coordinator can
            # walk parent -> child instead of resuming the parent and
            # orphaning the running child.
            state.active_child_session_id = child_session_id
            request_path.unlink(missing_ok=True)
            self.state_store = child_store
            return _build_run_response(
                current_task=child_task,
                config_snapshot=child_state.config_snapshot,
                repo_root=self.repo_root,
            )
        return None

    def _validate_child_request_inputs(
        self, *, parent_state: LoopState, request: ChildSessionRequest
    ) -> list[tuple[ArtifactInputRef, bytes]]:
        """Resolve and capture exact child-input bytes from the parent view."""

        normalized: list[tuple[ArtifactInputRef, bytes]] = []
        seen: set[str] = set()
        parent_id = parent_state.active_session_id
        for item in request.inputs:
            reference = item.ref
            if reference.startswith("parent:/"):
                reference = f"session:{parent_id}:/{reference.removeprefix('parent:/')}"
            elif reference.startswith("session:/"):
                reference = (
                    f"session:{parent_id}:/{reference.removeprefix('session:/')}"
                )
            if reference in seen:
                raise ConfigError(f"child input reference is duplicated: {reference}")
            seen.add(reference)
            try:
                path = resolve_logical_reference(
                    reference=reference, repo_root=self.repo_root, session_id=parent_id
                )
            except LogicalReferenceError as exc:
                raise ConfigError(
                    f"child input reference is invalid ({item.ref}): {exc}"
                ) from exc
            if not path.is_file():
                raise ConfigError(f"child input artifact does not exist: {item.ref}")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise ConfigError(
                    f"child input artifact cannot be read ({item.ref}): {exc}"
                ) from exc
            actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
            if actual_hash != item.sha256:
                raise ConfigError(
                    f"child input artifact hash mismatch for {item.ref}: "
                    f"expected {item.sha256}, got {actual_hash}"
                )
            normalized.append(
                (ArtifactInputRef(ref=reference, sha256=actual_hash), content)
            )
        return normalized

    def _archive_accepted_request(
        self, *, parent_session_id: str, request_id: str, request_path: Path
    ) -> Path:
        """Freeze an accepted child request under its durable request identity."""

        accepted_dir = child_requests_accepted_dir_path(
            repo_root=self.repo_root, session_id=parent_session_id
        )
        accepted_dir.mkdir(parents=True, exist_ok=True)
        accepted_path = accepted_dir / f"{request_id}.json"
        body = request_path.read_text(encoding="utf-8")
        if accepted_path.exists():
            if accepted_path.read_text(encoding="utf-8") != body:
                raise ConfigError(
                    f"child request id {request_id!r} was reused with a different body"
                )
            return accepted_path
        write_text_atomic(path=accepted_path, content=body)
        return accepted_path

    def _accepted_request_replay_state(
        self, *, parent_session_id: str, request_id: str, request_path: Path
    ) -> str:
        """Return ``absent``, ``exact``, or ``conflict`` for a v2 request ID."""
        accepted_path = (
            child_requests_accepted_dir_path(
                repo_root=self.repo_root, session_id=parent_session_id
            )
            / f"{request_id}.json"
        )
        if not accepted_path.is_file():
            return "absent"
        try:
            return (
                "exact"
                if file_sha256(path=accepted_path) == file_sha256(path=request_path)
                else "conflict"
            )
        except OSError:
            return "conflict"

    def _mark_child_record_running(
        self, *, parent_session_id: str, child_session_id: str
    ) -> None:
        """Promote a published child ledger edge from dispatching to running."""

        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = self._read_or_repair_children_payload(path=path)
        matches = [
            record
            for record in payload["children"]
            if record.get("session_id") == child_session_id
        ]
        if len(matches) != 1:
            raise ChildLedgerError(
                f"cannot mark child {child_session_id} running: expected one "
                f"ledger edge, found {len(matches)}"
            )
        record = matches[0]
        if record.get("status") not in _LIVE_CHILD_STATUSES:
            raise ChildLedgerError(
                f"cannot mark child {child_session_id} running from status "
                f"{record.get('status')!r}"
            )
        manifest = self._read_child_manifest_for_edge(
            parent_session_id=parent_session_id, child_session_id=child_session_id
        )
        record["status"] = "running"
        record["goal_contract_hash"] = manifest.get("goal_contract_hash")
        _bump_children_revision(payload=payload)
        write_json_atomic(path=path, payload=payload)

    def _dispatch_child_session_after_success(
        self, *, state: LoopState, caller: WorkerIdentity | None = None
    ) -> TaskResponse | None:
        """Dispatch a requested child only after the parent attempt succeeds."""

        if not state.history or not state.history[-1].success:
            return None
        return self._dispatch_child_session_if_requested(state=state, caller=caller)

    def _resume_parent_if_active_child_completed(
        self,
        *,
        caller: WorkerIdentity | None = None,
        recovered_completion_traces: list[FinishedRequest] | None = None,
    ) -> TaskResponse | None:
        """Iteratively finalize terminal descendants and resume an ancestor.

        This path deliberately never calls register_worker(): doing so used to
        run phase-A process drain/reap while the outer transition lock was
        still held. Every recovery plan remains outside the lock; unwind only
        performs short durable transitions.
        """
        last_response: TaskResponse | None = None
        while True:
            child_state = self.state_store.read_state()
            if child_state is None or child_state.parent_session_id is None:
                return last_response
            if not self.state_store.is_terminal_state(state=child_state):
                return last_response
            self._mark_child_record_complete(child_state=child_state)
            parent_store = StateStore(
                repo_root=self.repo_root,
                state_path=state_path(
                    repo_root=self.repo_root, session_id=child_state.parent_session_id
                ),
            )
            self._clear_child_pointer(
                parent_store=parent_store,
                parent_session_id=child_state.parent_session_id,
                child_session_id=child_state.active_session_id,
            )
            self.state_store = parent_store
            last_response = self._register_attempt(
                caller=caller,
                recovery=None,
                recovered_completion_traces=recovered_completion_traces,
            )
            if last_response is None:
                raise WorkerBusyError(
                    "parent state changed during terminal unwind; retry shortly"
                )
            if last_response.action != STOP_ACTION:
                return last_response

    def _clear_child_pointer(
        self, *, parent_store: StateStore, parent_session_id: str, child_session_id: str
    ) -> None:
        """The child reached a terminal state: the parent's stack pointer no
        longer points at live work. Also removes the originating request file
        if a crash window left it behind (its children.json record already
        prevents redispatch; this is just hygiene)."""

        payload = self._read_or_repair_children_payload(
            path=children_path(repo_root=self.repo_root, session_id=parent_session_id)
        )
        matches = [
            record
            for record in payload["children"]
            if record.get("session_id") == child_session_id
        ]
        if len(matches) != 1:
            raise ChildLedgerError(
                f"cannot clear child pointer {parent_session_id}->{child_session_id}: "
                f"expected one ledger edge, found {len(matches)}"
            )
        record = matches[0]
        if record.get("status") not in _TERMINAL_CHILD_STATUSES:
            raise ChildLedgerError(
                f"cannot clear live child pointer {parent_session_id}->"
                f"{child_session_id}"
            )

        def mutator(state: LoopState | None) -> tuple[LoopState, None]:
            """Clear a parent's active-child pointer if it still names the child."""

            parent = _require_state(state=state)
            if parent.active_child_session_id == child_session_id:
                parent.active_child_session_id = None
            elif parent.active_child_session_id is not None:
                raise ChildLedgerError(
                    f"parent {parent_session_id} points at "
                    f"{parent.active_child_session_id}, not terminal child "
                    f"{child_session_id}"
                )
            return parent, None

        parent_store.mutate(mutator)
        request_file = record.get("request_file")
        if request_file:
            leftover = (
                child_requests_dir_path(
                    repo_root=self.repo_root, session_id=parent_session_id
                )
                / request_file
            )
            leftover.unlink(missing_ok=True)

    def _dispatched_request_files(self, *, parent_session_id: str) -> set[str]:
        """Filenames suppressed by the crash-window tombstone.

        Only RUNNING records suppress: a completed child's request filename is
        legal to reuse for genuinely new work (a stable name like child.json
        is a perfectly reasonable agent protocol). The crash window this
        protects — record appended, request not yet unlinked — always has a
        running record; completed leftovers are cleaned by the completion path
        and by startup reconciliation.
        """
        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = self._read_or_repair_children_payload(path=path)
        return {
            record["request_file"]
            for record in payload["children"]
            if record.get("request_file")
            and record.get("status") in {"running", "dispatching"}
        }

    def _dispatched_request_ids(self, *, parent_session_id: str) -> set[str]:
        """Return request IDs already represented by non-retryable child edges."""

        payload = self._read_or_repair_children_payload(
            path=children_path(repo_root=self.repo_root, session_id=parent_session_id)
        )
        return {
            str(record["request_id"])
            for record in payload["children"]
            if record.get("request_id") and record.get("status") != "failed_dispatch"
        }

    def _reusable_failed_dispatch_child_id(
        self,
        *,
        parent_session_id: str,
        request_id: str,
        workflow_set: str,
        goal_hash: str,
    ) -> str | None:
        """Reuse an atomically published child identity whose state never landed."""
        payload = self._read_or_repair_children_payload(
            path=children_path(repo_root=self.repo_root, session_id=parent_session_id)
        )
        matches = [
            record
            for record in payload["children"]
            if record.get("request_id") == request_id
            and record.get("status") == "failed_dispatch"
        ]
        if len(matches) != 1:
            return None
        record = matches[0]
        child_id = record.get("session_id")
        if not isinstance(child_id, str):
            return None
        child_store = self._physical_child_store(
            parent_session_id=parent_session_id, child_session_id=child_id
        )
        manifest = self._read_child_manifest_for_edge(
            parent_session_id=parent_session_id,
            child_session_id=child_id,
            required=False,
        )
        if child_store.read_state() is not None or manifest is None:
            return None
        if (
            manifest.get("workflow_set") != workflow_set
            or manifest.get("goal_hash") != goal_hash
            or not isinstance(manifest.get("origin"), dict)
            or manifest["origin"].get("request_id") != request_id
        ):
            raise ChildLedgerError(
                f"published failed-dispatch identity {child_id} contradicts its "
                "immutable request; refusing to create a duplicate topology node"
            )
        return child_id

    def _record_child_dispatch_intent(
        self, *, parent_session_id: str, record: ChildSessionRecord
    ) -> None:
        """Persist the child edge before publishing the child session state."""

        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = self._read_or_repair_children_payload(path=path)
        serialized = json.loads(record.model_dump_json())
        for index, existing in enumerate(payload["children"]):
            if (
                existing.get("request_id") == record.request_id
                and existing.get("status") == "failed_dispatch"
            ):
                # The durable dispatch intent existed, but recovery proved
                # that no child state was ever committed. Reusing the same
                # immutable request replaces that failed projection instead
                # of manufacturing a duplicate request identity.
                failures = list(existing.get("dispatch_failures") or [])
                failures.append(
                    {
                        "session_id": existing.get("session_id"),
                        "created_at": existing.get("created_at"),
                        "failed_at": utc_now().isoformat().replace("+00:00", "Z"),
                        "reason": existing.get("stop_reason"),
                    }
                )
                serialized["dispatch_failures"] = failures
                payload["children"][index] = serialized
                break
        else:
            payload["children"].append(serialized)
        _bump_children_revision(payload=payload)
        write_json_atomic(path=path, payload=payload)

    def _mark_child_record_complete(
        self, *, child_state: LoopState, parent_state: LoopState | None = None
    ) -> None:
        """Project a terminal child result and subtree usage into its parent."""

        assert child_state.parent_session_id is not None
        path = children_path(
            repo_root=self.repo_root, session_id=child_state.parent_session_id
        )
        payload = self._read_or_repair_children_payload(path=path)
        matches = [
            record
            for record in payload["children"]
            if record.get("session_id") == child_state.active_session_id
        ]
        if len(matches) != 1:
            raise ChildLedgerError(
                f"terminal child {child_state.active_session_id} requires exactly "
                f"one parent ledger edge; found {len(matches)}"
            )
        record = matches[0]
        if parent_state is None:
            parent_store = StateStore(
                repo_root=self.repo_root, state_path=path.parent / "state.json"
            )
            parent_state = parent_store.read_state()
            if parent_state is None:
                raise ChildLedgerError(
                    f"terminal child {child_state.active_session_id} has no parent state"
                )
            parent_state = self._hydrate_legacy_state_identity(
                store=parent_store, state=parent_state
            )
        elif parent_state.active_session_id != child_state.parent_session_id:
            raise ChildLedgerError(
                f"terminal child {child_state.active_session_id} was paired with "
                "the wrong in-memory parent"
            )
        self._validate_child_state_identity(
            parent_state=parent_state,
            child_state=child_state,
            record=record,
            child_manifest=self._read_child_manifest_for_edge(
                parent_session_id=child_state.parent_session_id,
                child_session_id=child_state.active_session_id,
            ),
        )
        if child_state.status not in _TERMINAL_CHILD_STATUSES:
            raise ChildLedgerError(
                f"cannot finalize live child {child_state.active_session_id}"
            )
        if self._protocol_version_for_state(state=child_state) >= 3:
            self._ensure_session_outcome(state=child_state)
        first_finalization = record.get("status") in _LIVE_CHILD_STATUSES
        self._project_terminal_child_record(record=record, child_state=child_state)
        _bump_children_revision(payload=payload)
        write_json_atomic(path=path, payload=payload)
        if first_finalization:
            self._emit(
                session_id=child_state.parent_session_id,
                event_type="child_finished",
                payload={
                    "child_session_id": child_state.active_session_id,
                    "status": child_state.status,
                    "stop_reason": child_state.stop_reason,
                },
            )
            self._flush_pending_events()

    def _project_terminal_child_record(
        self, *, record: dict[str, Any], child_state: LoopState
    ) -> None:
        """Fill the terminal ledger projection and its evidence-rich outcome."""
        record["status"] = child_state.status
        if child_state.history:
            # A normal terminal transition is decided in the same mutation as
            # its final history entry. That durable timestamp is canonical,
            # unlike whichever restart first happens to observe the child.
            record["completed_at"] = (
                child_state.history[-1].finished_at.isoformat().replace("+00:00", "Z")
            )
        elif not record.get("completed_at"):
            # Legacy/manual terminal projections can predate history. Preserve
            # their first observed time because no stronger fact exists.
            record["completed_at"] = utc_now().isoformat().replace("+00:00", "Z")
        record["stop_reason"] = child_state.stop_reason
        # Always derive the factual projection from the child tree. Keeping a
        # pre-existing value here would preserve a syntactically valid but
        # stale/tampered subtotal across an otherwise successful repair.
        record["usage"] = session_tree_usage_totals(
            repo_root=self.repo_root,
            state=child_state,
            children_reader=self._read_or_repair_children_payload,
        ).model_dump(mode="json")
        request_id = record.get("request_id")
        if not request_id:
            return
        if child_state.parent_session_id is None:
            raise ChildLedgerError(
                f"terminal child {child_state.active_session_id} has no parent"
            )
        outcome_path = (
            child_outcomes_dir_path(
                repo_root=self.repo_root, session_id=child_state.parent_session_id
            )
            / f"{request_id}.json"
        )
        if self._protocol_version_for_state(state=child_state) >= 3:
            session_outcome = self._ensure_session_outcome(state=child_state)
            if session_outcome is None:
                raise ChildLedgerError(
                    f"terminal v3 child {child_state.active_session_id} has no "
                    "session outcome"
                )
            child_outcome_file = session_outcome_path(
                repo_root=self.repo_root, session_id=child_state.active_session_id
            )
            write_json_atomic(
                path=outcome_path,
                payload={
                    "schema_version": 2,
                    "request_id": request_id,
                    "child_session_id": child_state.active_session_id,
                    "session_outcome_ref": (
                        f"session:{child_state.active_session_id}:/session_outcome.json"
                    ),
                    "session_outcome_sha256": file_sha256(path=child_outcome_file),
                },
            )
            record["outcome_ref"] = f"session:/child_outcomes/{outcome_path.name}"
            return
        evidence_refs, trace_ref, trace_sealed = self._terminal_evidence_projection(
            child_state=child_state
        )
        outcome = {
            "schema_version": 1,
            "request_id": request_id,
            "child_session_id": child_state.active_session_id,
            "goal_hash": child_state.goal_hash,
            "lifecycle": {
                "status": child_state.status,
                "stop_reason": child_state.stop_reason,
                "completed_at": record.get("completed_at"),
            },
            "evidence_refs": evidence_refs,
            "trace_ref": trace_ref,
            "usage": record.get("usage") or {},
            "completeness": {
                "eval_receipt_present": evidence_refs["eval"] is not None,
                "delivery_receipt_present": evidence_refs["delivery"] is not None,
                "trace_sealed": trace_sealed,
            },
        }
        write_json_atomic(path=outcome_path, payload=outcome)
        record["outcome_ref"] = f"session:/child_outcomes/{outcome_path.name}"

    def _terminal_evidence_projection(
        self, *, child_state: LoopState
    ) -> tuple[dict[str, str | None], str | None, bool]:
        """Bind outcome evidence to the terminal producer, never a filename sort.

        Agent-owned receipt directories may contain retries, decoys, or later
        repair artifacts. A v2 control names the producer attempt and (for
        goal completion) its eval receipt, so only artifacts carrying that
        identity can enter the factual child outcome.
        """
        session_id = child_state.active_session_id
        handoff_ref = _artifact_ref_if_present(
            repo_root=self.repo_root,
            session_id=session_id,
            relative_path="project_state/handoff.json",
        )
        eval_ref: str | None = None
        git_ref: str | None = None
        delivery_ref: str | None = None
        trace_ref: str | None = None
        trace_sealed = False
        signal = _read_signal(
            path=control_path(repo_root=self.repo_root, session_id=session_id),
            model=ControlSignal,
        )
        producer_attempt_id: str | None = None
        if signal is not None and signal.schema_version == 2 and signal.producer:
            producer_attempt_id = signal.producer.attempt_id
            if signal.stop_reason == "goal_met" and signal.eval_receipt_ref:
                receipt = self._load_eval_receipt(
                    state_session_id=session_id, reference=signal.eval_receipt_ref
                )
                if (
                    receipt is not None
                    and receipt.producer.attempt_id == producer_attempt_id
                    and receipt.producer.workflow_id == signal.producer.workflow_id
                ):
                    eval_ref = signal.eval_receipt_ref

            git_path = (
                git_receipts_dir_path(repo_root=self.repo_root, session_id=session_id)
                / f"git-after-{producer_attempt_id}.json"
            )
            try:
                git_payload = self._read_json_object_if_present(
                    path=git_path, label="terminal git receipt"
                )
            except ChildLedgerError:
                git_payload = None
            if (
                git_payload is not None
                and git_payload.get("phase") == "after"
                and git_payload.get("attempt_id") == producer_attempt_id
            ):
                git_ref = f"session:{session_id}:/git_receipts/{git_path.name}"

            delivery_ref = self._delivery_ref_for_attempt(
                session_id=session_id,
                attempt_id=producer_attempt_id,
                evidence_refs=signal.evidence_refs,
            )

        if producer_attempt_id is not None:
            histories = [
                item
                for item in child_state.history
                if item.attempt_id == producer_attempt_id
            ]
            if len(histories) == 1:
                canonical_manifest = (
                    attempt_trace_dir_path(
                        repo_root=self.repo_root,
                        root_session_id=(
                            child_state.root_session_id or child_state.active_session_id
                        ),
                        session_id=session_id,
                        attempt_id=producer_attempt_id,
                    )
                    / "trace_manifest.json"
                )
                trace_ref, trace_sealed = _trace_outcome_projection(
                    value=str(canonical_manifest), repo_root=self.repo_root
                )
        elif child_state.schema_version == 1 and child_state.history:
            # Legacy controls did not carry producer identity; the final
            # history entry is the strongest evidence that old protocol has.
            trace_ref, trace_sealed = _trace_outcome_projection(
                value=child_state.history[-1].trace_manifest_path,
                repo_root=self.repo_root,
            )

        return (
            {
                "handoff": handoff_ref,
                "eval": eval_ref,
                "git": git_ref,
                "delivery": delivery_ref,
            },
            trace_ref,
            trace_sealed,
        )

    def _delivery_ref_for_attempt(
        self, *, session_id: str, attempt_id: str, evidence_refs: list[str]
    ) -> str | None:
        """Select the unique delivery receipt produced by a child attempt."""

        candidates: dict[Path, str] = {}
        for reference in evidence_refs:
            try:
                resolved = resolve_logical_reference(
                    reference=reference, repo_root=self.repo_root, session_id=session_id
                )
            except LogicalReferenceError:
                continue
            delivery_root = delivery_receipts_dir_path(
                repo_root=self.repo_root, session_id=session_id
            ).resolve()
            if resolved.parent == delivery_root and resolved.is_file():
                candidates[resolved] = reference
        delivery_root = delivery_receipts_dir_path(
            repo_root=self.repo_root, session_id=session_id
        )
        for path in delivery_root.glob("*.json"):
            candidates.setdefault(
                path.resolve(), f"session:{session_id}:/delivery_receipts/{path.name}"
            )
        matching: list[str] = []
        for path, reference in candidates.items():
            try:
                payload = self._read_json_object_if_present(
                    path=path, label="delivery receipt"
                )
            except ChildLedgerError:
                continue
            if payload is None:
                continue
            producer = payload.get("producer")
            observed_attempt = payload.get("attempt_id")
            if observed_attempt is None and isinstance(producer, dict):
                observed_attempt = producer.get("attempt_id")
            if observed_attempt == attempt_id:
                matching.append(reference)
        return matching[0] if len(matching) == 1 else None

    def _validate_v2_children_payload(
        self, *, path: Path, payload: dict[str, Any]
    ) -> None:
        """Validate a ledger projection against immutable topology and state.

        The JSON schema alone is insufficient: a syntactically valid record
        could otherwise claim that a live child is terminal, point at a child
        from another parent, or silently change request/workflow identity.
        Live records are allowed to lag a terminal child state because that is
        the normal crash window finalized by stack reconstruction.
        """
        if payload.get("schema_version") != 2:
            return
        parent_root = path.parent
        manifest_path = parent_root / "session.json"
        try:
            parent_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ChildLedgerError(
                f"v2 parent manifest is unreadable at {manifest_path}: {exc}"
            ) from exc
        if not isinstance(parent_manifest, dict):
            raise ChildLedgerError(
                f"v2 parent manifest is not an object at {manifest_path}"
            )
        parent_session_id = parent_manifest.get("session_id")
        if (
            parent_manifest.get("schema_version") != 2
            or not isinstance(parent_session_id, str)
            or payload.get("parent_session_id") != parent_session_id
        ):
            raise ChildLedgerError(
                f"v2 children ledger parent identity contradicts {manifest_path}"
            )
        parent_state_path = parent_root / "state.json"
        try:
            parent_state = LoopState.model_validate_json(
                parent_state_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise ChildLedgerError(
                f"v2 children ledger has no readable parent state at "
                f"{parent_state_path}: {exc}"
            ) from exc
        parent_expectations = {
            "active_session_id": parent_session_id,
            "root_session_id": parent_manifest.get("root_session_id"),
            "parent_session_id": parent_manifest.get("parent_session_id"),
            "depth": parent_manifest.get("depth"),
            "workflow_set": parent_manifest.get("workflow_set"),
            "goal_hash": parent_manifest.get("goal_hash"),
        }
        for field, expected in parent_expectations.items():
            if getattr(parent_state, field) != expected:
                raise ChildLedgerError(
                    f"v2 ledger parent state contradicts manifest field {field!r}"
                )

        for record in payload["children"]:
            try:
                validated = ChildSessionRecord.model_validate(record)
            except ValidationError as exc:
                raise ChildLedgerError(
                    f"v2 child ledger record is invalid at {path}: {exc}"
                ) from exc
            status = validated.status
            if status not in _VALID_CHILD_STATUSES:
                raise ChildLedgerError(
                    f"v2 child ledger has unknown status {status!r} at {path}"
                )
            child_id = validated.session_id
            if not SAFE_DURABLE_ID_PATTERN.fullmatch(child_id):
                raise ChildLedgerError(
                    f"v2 child ledger has unsafe session ID {child_id!r}"
                )
            child_root = parent_root / "children" / child_id
            manifest = self._read_json_object_if_present(
                path=child_root / "session.json", label="child session manifest"
            )
            child_store = StateStore(
                repo_root=self.repo_root, state_path=child_root / "state.json"
            )
            child_state = child_store.read_state()

            if manifest is not None:
                manifest_expectations = {
                    "schema_version": 2,
                    "session_id": child_id,
                    "root_session_id": parent_state.root_session_id,
                    "parent_session_id": parent_session_id,
                    "depth": parent_state.depth + 1,
                    "workflow_set": validated.workflow_set,
                    "goal_hash": validated.goal_hash,
                }
                for field, expected in manifest_expectations.items():
                    if manifest.get(field) != expected:
                        raise ChildLedgerError(
                            f"child {child_id} manifest contradicts ledger/parent "
                            f"field {field!r}"
                        )
                origin = manifest.get("origin")
                if not isinstance(origin, dict):
                    raise ChildLedgerError(f"v2 child {child_id} manifest lacks origin")
                origin_expectations = {
                    "request_id": validated.request_id,
                    "parent_attempt_id": validated.parent_attempt_id,
                    "parent_work_item_id": validated.parent_work_item_id,
                    "accepted_request_sha256": validated.accepted_request_sha256,
                    "accepted_request_ref": (
                        f"session:{parent_session_id}:/child_requests/accepted/"
                        f"{validated.request_id}.json"
                    ),
                }
                for field, expected in origin_expectations.items():
                    if origin.get(field) != expected:
                        raise ChildLedgerError(
                            f"child {child_id} origin contradicts ledger field "
                            f"{field!r}"
                        )
                if validated.goal_contract_hash is not None and (
                    manifest.get("goal_contract_hash") != validated.goal_contract_hash
                ):
                    raise ChildLedgerError(
                        f"child {child_id} goal contract hash contradicts ledger"
                    )

            if child_state is not None:
                self._validate_child_state_identity(
                    parent_state=parent_state,
                    child_state=child_state,
                    record=record,
                    child_manifest=manifest,
                )
                terminal = child_store.is_terminal_state(state=child_state)
                if status == "failed_dispatch":
                    raise ChildLedgerError(
                        f"failed-dispatch child {child_id} has durable state"
                    )
                if status in _TERMINAL_CHILD_STATUSES:
                    if not terminal or child_state.status != status:
                        raise ChildLedgerError(
                            f"terminal projection for child {child_id} "
                            "contradicts child state"
                        )
                    self._validate_terminal_child_projection(
                        parent_root=parent_root,
                        record=record,
                        validated=validated,
                        child_state=child_state,
                    )
            elif status in _TERMINAL_CHILD_STATUSES:
                raise ChildLedgerError(
                    f"terminal child {child_id} has no durable child state"
                )
            elif manifest is not None and status == "running":
                # A published identity without state is the interrupted child
                # creation window. It is repaired to failed_dispatch/retry by
                # reconciliation, never treated as a running session.
                pass

            if validated.request_id is not None:
                expected_record_ref = (
                    f"session:/child_requests/accepted/{validated.request_id}.json"
                )
                if validated.accepted_request_ref != expected_record_ref:
                    raise ChildLedgerError(
                        f"child {child_id} accepted request ref contradicts ledger"
                    )
                accepted_path = (
                    parent_root
                    / "child_requests"
                    / "accepted"
                    / f"{validated.request_id}.json"
                )
                if validated.accepted_request_sha256 is not None:
                    if not accepted_path.is_file():
                        raise ChildLedgerError(
                            f"child {child_id} accepted request is missing"
                        )
                    if (
                        file_sha256(path=accepted_path)
                        != validated.accepted_request_sha256
                    ):
                        raise ChildLedgerError(
                            f"child {child_id} accepted request hash contradicts ledger"
                        )

    def _validate_terminal_child_projection(
        self,
        *,
        parent_root: Path,
        record: dict[str, Any],
        validated: ChildSessionRecord,
        child_state: LoopState,
    ) -> None:
        """Require a terminal edge to be an exact projection of durable facts."""
        request_id = validated.request_id
        if request_id is None or not SAFE_DURABLE_ID_PATTERN.fullmatch(request_id):
            raise ChildLedgerError(
                f"terminal child {validated.session_id} has no safe request identity"
            )
        if validated.completed_at is None or not isinstance(
            record.get("completed_at"), str
        ):
            raise ChildLedgerError(
                f"terminal child {validated.session_id} lacks completion time"
            )
        if child_state.history:
            expected_completed_at = (
                child_state.history[-1].finished_at.isoformat().replace("+00:00", "Z")
            )
            if record.get("completed_at") != expected_completed_at:
                raise ChildLedgerError(
                    f"terminal child {validated.session_id} completion time "
                    "contradicts its terminal history"
                )
        if record.get("stop_reason") != child_state.stop_reason:
            raise ChildLedgerError(
                f"terminal child {validated.session_id} stop reason contradicts state"
            )

        expected_usage = session_tree_usage_totals(
            repo_root=self.repo_root,
            state=child_state,
            children_reader=self._read_or_repair_children_payload,
        ).model_dump(mode="json")
        if validated.usage is None or record.get("usage") != expected_usage:
            raise ChildLedgerError(
                f"terminal child {validated.session_id} usage contradicts its tree"
            )

        outcome_name = f"{request_id}.json"
        expected_ref = f"session:/child_outcomes/{outcome_name}"
        if validated.outcome_ref != expected_ref:
            raise ChildLedgerError(
                f"terminal child {validated.session_id} outcome ref is not canonical"
            )
        outcome_path = parent_root / "child_outcomes" / outcome_name
        outcome = self._read_json_object_if_present(
            path=outcome_path, label="terminal child outcome"
        )
        if outcome is None:
            raise ChildLedgerError(
                f"terminal child {validated.session_id} outcome file is missing"
            )
        if self._protocol_version_for_state(state=child_state) >= 3:
            child_outcome_file = session_outcome_path(
                repo_root=self.repo_root, session_id=child_state.active_session_id
            )
            expected_session_outcome = self._ensure_session_outcome(state=child_state)
            if expected_session_outcome is None:
                raise ChildLedgerError(
                    f"terminal child {validated.session_id} has no v3 outcome basis"
                )
            try:
                persisted_session_outcome = SessionOutcome.model_validate_json(
                    child_outcome_file.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, ValueError) as exc:
                raise ChildLedgerError(
                    f"terminal child {validated.session_id} outcome is invalid: {exc}"
                ) from exc
            if persisted_session_outcome != expected_session_outcome:
                raise ChildLedgerError(
                    f"terminal child {validated.session_id} outcome contradicts state"
                )
            expected_link = {
                "schema_version": 2,
                "request_id": request_id,
                "child_session_id": child_state.active_session_id,
                "session_outcome_ref": (
                    f"session:{child_state.active_session_id}:/session_outcome.json"
                ),
                "session_outcome_sha256": file_sha256(path=child_outcome_file),
            }
            if outcome != expected_link:
                raise ChildLedgerError(
                    f"terminal child {validated.session_id} outcome link "
                    "contradicts its session outcome"
                )
            return
        expected_lifecycle = {
            "status": child_state.status,
            "stop_reason": child_state.stop_reason,
            "completed_at": record["completed_at"],
        }
        expected_identity = {
            "schema_version": 1,
            "request_id": request_id,
            "child_session_id": child_state.active_session_id,
            "goal_hash": child_state.goal_hash,
            "lifecycle": expected_lifecycle,
            "usage": expected_usage,
        }
        for field, expected in expected_identity.items():
            if outcome.get(field) != expected:
                raise ChildLedgerError(
                    f"terminal child {validated.session_id} outcome field "
                    f"{field!r} contradicts its ledger/state"
                )

    @staticmethod
    def _read_json_object_if_present(
        *, path: Path, label: str
    ) -> dict[str, Any] | None:
        """Read an optional JSON object or raise a contextual ledger error."""

        if not path.exists():
            return None
        if not path.is_file():
            raise ChildLedgerError(f"{label} is not a regular file at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ChildLedgerError(f"{label} is unreadable at {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ChildLedgerError(f"{label} is not an object at {path}")
        return payload

    def _read_or_repair_children_payload(self, *, path: Path) -> dict[str, Any]:
        """Read a valid child ledger or reconstruct it from durable evidence."""

        try:
            payload = _read_children_payload(path=path)
            self._validate_v2_children_payload(path=path, payload=payload)
            return payload
        except ChildLedgerError as exc:
            return self._reconstruct_v2_children_ledger(path=path, cause=exc)

    def _reconstruct_v2_children_ledger(
        self, *, path: Path, cause: ChildLedgerError
    ) -> dict[str, Any]:
        """Bounded repair from immutable requests, manifests, and child state."""
        parent_root = path.parent
        manifest_path = parent_root / "session.json"
        try:
            parent_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as manifest_error:
            raise ChildLedgerError(
                f"{cause}; parent manifest is unavailable for reconstruction: "
                f"{manifest_error}"
            ) from cause
        if (
            not isinstance(parent_manifest, dict)
            or parent_manifest.get("schema_version", 1) < 2
        ):
            raise cause
        parent_session_id = parent_manifest.get("session_id")
        if not isinstance(parent_session_id, str):
            raise ChildLedgerError(
                f"{cause}; parent manifest has no session identity"
            ) from cause

        try:
            original_bytes = path.read_bytes()
        except OSError:
            original_bytes = b""
        original_digest = hashlib.sha256(original_bytes).hexdigest()[:16]
        failure_stem = f"children-ledger-{original_digest}"
        failures_dir = protocol_failures_dir_path(
            repo_root=self.repo_root, session_id=parent_session_id
        )
        failures_dir.mkdir(parents=True, exist_ok=True)
        preserved = failures_dir / f"{failure_stem}.original"
        if not preserved.exists():
            write_text_atomic(
                path=preserved,
                content=original_bytes.decode("utf-8", errors="backslashreplace"),
            )

        # This repair can run from inside a mutation of the same parent.  Read
        # the atomically replaced state file directly instead of acquiring a
        # second FileLock instance for ``state.json.lock`` and deadlocking on
        # our own in-flight coordinator transaction.
        parent_state_path = parent_root / "state.json"
        try:
            parent_state = LoopState.model_validate_json(
                parent_state_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as state_error:
            raise ChildLedgerError(
                f"{cause}; parent state is unavailable for reconstruction: "
                f"{state_error}"
            ) from cause
        for field in (
            "active_session_id",
            "root_session_id",
            "parent_session_id",
            "depth",
            "workflow_set",
            "goal_hash",
        ):
            manifest_field = "session_id" if field == "active_session_id" else field
            if getattr(parent_state, field) != parent_manifest.get(manifest_field):
                raise ChildLedgerError(
                    f"{cause}; parent state contradicts manifest field {field!r}"
                ) from cause

        accepted_dir = child_requests_accepted_dir_path(
            repo_root=self.repo_root, session_id=parent_session_id
        )
        pending_dir = child_requests_pending_dir_path(
            repo_root=self.repo_root, session_id=parent_session_id
        )
        accepted = {
            candidate.stem: candidate
            for candidate in accepted_dir.glob("*.json")
            if candidate.is_file()
        }
        records: list[dict[str, Any]] = []
        seen_requests: set[str] = set()
        retryable_requests: set[str] = set()
        child_root = parent_root / "children"
        for child_dir in (
            sorted(
                candidate
                for candidate in child_root.iterdir()
                if candidate.is_dir() and not candidate.name.startswith(".staging-")
            )
            if child_root.is_dir()
            else []
        ):
            child_manifest_path = child_dir / "session.json"
            if not child_manifest_path.is_file():
                # A directory without an immutable session identity is not a
                # topology node. Ignore it while rebuilding the canonical
                # ledger so accidental agent scratch cannot block terminal
                # child projection or autonomous repair.
                continue
            try:
                child_manifest = json.loads(
                    child_manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as child_error:
                raise ChildLedgerError(
                    f"{cause}; child manifest is ambiguous at {child_manifest_path}: "
                    f"{child_error}"
                ) from cause
            if (
                not isinstance(child_manifest, dict)
                or child_manifest.get("schema_version") != 2
                or child_manifest.get("parent_session_id") != parent_session_id
            ):
                raise ChildLedgerError(
                    f"{cause}; child manifest contradicts parent at "
                    f"{child_manifest_path}"
                ) from cause
            child_id = child_manifest.get("session_id")
            origin = child_manifest.get("origin")
            request_id = origin.get("request_id") if isinstance(origin, dict) else None
            if (
                not isinstance(child_id, str)
                or child_id != child_dir.name
                or not SAFE_DURABLE_ID_PATTERN.fullmatch(child_id)
                or not isinstance(request_id, str)
            ):
                raise ChildLedgerError(
                    f"{cause}; child identity/provenance is incomplete at "
                    f"{child_manifest_path}"
                ) from cause
            if request_id in seen_requests:
                raise ChildLedgerError(
                    f"{cause}; multiple children claim request {request_id!r}"
                ) from cause
            seen_requests.add(request_id)
            accepted_path = accepted.get(request_id)
            if accepted_path is None:
                raise ChildLedgerError(
                    f"{cause}; child {child_id} has no immutable accepted request"
                ) from cause
            accepted_hash = file_sha256(path=accepted_path)
            expected_manifest = {
                "root_session_id": parent_state.root_session_id,
                "depth": parent_state.depth + 1,
                "workflow_set": child_manifest.get("workflow_set"),
                "goal_hash": child_manifest.get("goal_hash"),
            }
            if (
                child_manifest.get("root_session_id")
                != expected_manifest["root_session_id"]
                or child_manifest.get("depth") != expected_manifest["depth"]
                or origin.get("accepted_request_sha256") != accepted_hash
            ):
                raise ChildLedgerError(
                    f"{cause}; child {child_id} immutable identity is contradictory"
                ) from cause
            child_store = StateStore(
                repo_root=self.repo_root, state_path=child_dir / "state.json"
            )
            child_state = child_store.read_state()
            status = (
                "failed_dispatch"
                if child_state is None
                else child_state.status
                if child_store.is_terminal_state(state=child_state)
                else "running"
            )
            record = {
                "session_id": child_id,
                "workflow_set": child_manifest.get("workflow_set"),
                "goal_hash": child_manifest.get("goal_hash"),
                "status": status,
                "created_at": child_manifest.get(
                    "created_at", utc_now().isoformat().replace("+00:00", "Z")
                ),
                "completed_at": None,
                "stop_reason": (
                    child_state.stop_reason if child_state is not None else None
                ),
                "request_file": None,
                "request_id": request_id,
                "accepted_request_ref": (
                    f"session:/child_requests/accepted/{accepted_path.name}"
                ),
                "accepted_request_sha256": accepted_hash,
                "parent_attempt_id": origin.get("parent_attempt_id"),
                "parent_work_item_id": origin.get("parent_work_item_id"),
                "goal_contract_hash": child_manifest.get("goal_contract_hash"),
                "dispatch_failures": [],
            }
            if child_state is None:
                retryable_requests.add(request_id)
            else:
                self._validate_child_state_identity(
                    parent_state=parent_state,
                    child_state=child_state,
                    record=record,
                    child_manifest=child_manifest,
                )
                if status in _TERMINAL_CHILD_STATUSES:
                    self._project_terminal_child_record(
                        record=record, child_state=child_state
                    )
            records.append(record)

        if not records and not accepted:
            failure_id = f"{failure_stem}-refused"
            receipt_path = failures_dir / f"{failure_id}.json"
            if not receipt_path.exists():
                write_json_atomic(
                    path=receipt_path,
                    payload={
                        "schema_version": 1,
                        "failure_id": failure_id,
                        "kind": "children_ledger_reconstruction_refused",
                        "original_ref": f"session:/protocol_failures/{preserved.name}",
                        "original_sha256": file_sha256(path=preserved),
                        "reason": (
                            f"{cause}; no immutable child manifest or accepted request "
                            "proves that an empty reconstruction is lossless"
                        ),
                        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                    },
                )
            raise ChildLedgerError(
                f"{cause}; refusing to reconstruct an empty children ledger "
                "without corroborating immutable artifacts"
            ) from cause

        # An accepted request with no child manifest is the crash window before
        # child creation. Requeue the exact immutable body for one idempotent
        # dispatch; do not invent a child record with a fabricated ID.
        for request_id, accepted_path in accepted.items():
            if request_id in seen_requests and request_id not in retryable_requests:
                continue
            pending_path = pending_dir / accepted_path.name
            if not pending_path.exists():
                write_text_atomic(
                    path=pending_path, content=accepted_path.read_text(encoding="utf-8")
                )

        payload = {
            "schema_version": 2,
            "parent_session_id": parent_session_id,
            "revision": 1,
            "children": records,
        }
        write_json_atomic(path=path, payload=payload)
        try:
            repaired = _read_children_payload(path=path)
            self._validate_v2_children_payload(path=path, payload=repaired)
        except ChildLedgerError as repair_error:
            raise ChildLedgerError(
                f"{cause}; bounded reconstruction was contradictory: {repair_error}"
            ) from cause
        failure_id = f"{failure_stem}-reconstructed"
        receipt_path = failures_dir / f"{failure_id}.json"
        if not receipt_path.exists():
            write_json_atomic(
                path=receipt_path,
                payload={
                    "schema_version": 1,
                    "failure_id": failure_id,
                    "kind": "children_ledger_reconstructed",
                    "original_ref": f"session:/protocol_failures/{preserved.name}",
                    "original_sha256": file_sha256(path=preserved),
                    "reason": str(cause),
                    "reconstructed_child_ids": [
                        record["session_id"] for record in records
                    ],
                    "requeued_request_ids": sorted(
                        (set(accepted) - seen_requests) | retryable_requests
                    ),
                    "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                },
            )
        return repaired

    def _apply_stop_precedence(self, *, state: LoopState) -> str | None:
        """Apply terminal conditions in their documented precedence order."""

        if state.goal_met:
            state.status = "goal_met"
            state.stop_reason = "goal_met"
            return "goal_met"
        if state.stop_requested:
            state.status = "stopped"
            state.stop_reason = "stop_requested"
            return "stop_requested"
        if state.unresolvable_error:
            state.status = "failed"
            state.stop_reason = "unresolvable_error"
            return "unresolvable_error"
        if state.iteration_count >= state.max_turns and state.status == "running":
            # Only label a still-running loop: a stop decided in the same
            # mutation (workflow_failure_cap, goal_check_broken) is the more
            # specific diagnosis and must not be rewritten to max_turns.
            state.status = "max_turns"
            state.stop_reason = "max_turns"
            return "max_turns"
        budget = self.preflight.root_config.max_cost_usd
        if budget is not None and state.status == "running":
            totals = root_tree_usage_totals(
                repo_root=self.repo_root,
                state=state,
                # The active state lock is already held. The pure projector
                # validates pointer cardinality and typed usage without
                # recursively re-locking the active child through the ledger
                # repair path.
                children_reader=_read_children_payload,
            )
            cost = estimate_cost_usd(
                prompt_tokens=totals.prompt_tokens,
                completion_tokens=totals.completion_tokens,
                prices=self.preflight.root_config.model_prices,
            )
            if cost is not None and cost >= budget:
                state.status = "stopped"
                state.stop_reason = "max_cost_usd"
                return "max_cost_usd"
        if state.status in {"stopped", "goal_met", "failed", "max_turns"}:
            return state.stop_reason or state.status
        return None

    def _stop_response_if_needed(self, *, state: LoopState) -> TaskResponse | None:
        """Return a stop response when root or active-session state is terminal."""

        root_id = state.root_session_id or state.active_session_id
        if root_id != state.active_session_id:
            root_state = self._store_for(session_id=root_id).read_state()
            if root_state is not None and root_state.stop_requested:
                state.stop_requested = True
        # A terminal control is consumed while its producing task is still
        # current.  Do not re-validate that same file after task recording has
        # cleared current_task: v2 deliberately rejects historical producers.
        if not state.goal_met and not state.unresolvable_error:
            self._apply_session_control(state=state)
        stop_reason = self._apply_stop_precedence(state=state)
        if stop_reason is not None:
            return TaskResponse(action=STOP_ACTION, stop_reason=stop_reason)
        return None

    def _read_goal_check_signal(
        self,
        *,
        current_task: CurrentTask,
        state: LoopState,
        workflow_contract: WorkflowSetContract | None = None,
        receipt_validation_errors: list[str] | None = None,
    ) -> GoalCheckSignal | None:
        """Read a goal-check signal bound to the current attempt and eval receipt."""

        path = goal_check_path(
            repo_root=self.repo_root,
            session_id=current_task.session_id,
            iteration=current_task.iteration,
            workflow_id=current_task.workflow_id,
        )
        signal = _read_signal(path=path, model=GoalCheckSignal)
        if signal is None:
            return signal
        effective_workflow_contract = (
            workflow_contract or self._workflow_contract_for_state(state=state)
        )
        if signal.schema_version == 1:
            return (
                signal
                if effective_workflow_contract.session_protocol_version < 2
                else None
            )
        receipt = self._load_eval_receipt(
            state_session_id=current_task.session_id,
            reference=signal.eval_receipt_ref,
            validation_errors=receipt_validation_errors,
        )
        if receipt is None:
            return None
        if receipt.producer.workflow_id != current_task.workflow_id:
            return None
        if receipt.producer.attempt_id != current_task.attempt_id:
            return None
        if receipt.producer.iteration != current_task.iteration:
            return None
        if receipt.subject.session_id != current_task.session_id:
            return None
        if receipt.subject.root_session_id != (
            state.root_session_id or state.active_session_id
        ):
            return None
        if receipt.subject.goal_hash != state.goal_hash:
            return None
        if receipt.verdict.goal_met != signal.goal_met:
            return None
        if receipt.verdict.reason != signal.reason:
            return None
        if self._validate_eval_receipt_artifacts(
            session_id=current_task.session_id, receipt=receipt
        ):
            return None
        return signal

    def _workflow_expects_goal_check_signal(self, *, current_task: CurrentTask) -> bool:
        """Return whether this frozen workflow is responsible for goal checking."""

        if current_task.workflow_id == "goal_check":
            return True
        descriptor = current_task.workflow_snapshot
        if descriptor is not None:
            try:
                payload = yaml.safe_load(
                    Path(descriptor.workflow_config_path).read_text(encoding="utf-8")
                )
            except (OSError, yaml.YAMLError):
                return True
            return isinstance(payload, dict) and bool(payload.get("emits_goal_check"))
        workflow = self._workflows_by_id_for(
            workflow_set=current_task.workflow_set
        ).get(current_task.workflow_id)
        return workflow is not None and workflow.emits_goal_check

    def _apply_session_control(
        self, *, state: LoopState, workflow_contract: WorkflowSetContract | None = None
    ) -> str | None:
        """Validate and apply the active session's role-bound terminal control."""

        path = control_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        effective_workflow_contract = (
            workflow_contract or self._workflow_contract_for_state(state=state)
        )
        protocol_version = effective_workflow_contract.session_protocol_version
        signal = _read_signal(path=path, model=ControlSignal)
        if signal is None:
            if protocol_version >= 2:
                self._reject_v2_control(
                    state=state,
                    path=path,
                    raw=raw,
                    reasons=[f"invalid control v{protocol_version} JSON or schema"],
                    schema_version=protocol_version,
                )
                return f"invalid_v{protocol_version}"
            state.status = "failed"
            state.stop_reason = "invalid_control_output"
            return "invalid_v1"
        if signal.state == "running":
            if protocol_version >= 2:
                is_engine_placeholder, rejected_attempt_id = (
                    _control_repair_placeholder_identity(raw=raw)
                )
                active_attempt_id = (
                    state.current_task.attempt_id
                    if state.current_task is not None
                    else None
                )
                # The coordinator calls this method again during the same
                # /finished transition.  Its own running placeholder is not
                # evidence of repair and must not erase the just-incremented
                # bounded failure count.  A later, different task completing
                # without another invalid terminal record is a real clean
                # iteration and resets the consecutive counter.
                if not is_engine_placeholder or (
                    active_attempt_id is not None
                    and active_attempt_id != rejected_attempt_id
                ):
                    state.control_protocol_consecutive_failures = 0
            return None
        if signal.schema_version != protocol_version:
            self._reject_v2_control(
                state=state,
                path=path,
                raw=raw,
                reasons=[
                    f"this session contract requires terminal control "
                    f"v{protocol_version}, got v{signal.schema_version}"
                ],
                schema_version=protocol_version,
            )
            return f"invalid_v{protocol_version}"
        if protocol_version == 2:
            reasons = self._validate_v2_control(
                state=state,
                signal=signal,
                workflow_contract=effective_workflow_contract,
            )
            if reasons:
                self._reject_v2_control(
                    state=state, path=path, raw=raw, reasons=reasons, schema_version=2
                )
                return "invalid_v2"
            state.control_protocol_consecutive_failures = 0
        elif protocol_version == 3:
            reasons = self._validate_v3_control(
                state=state,
                signal=signal,
                workflow_contract=effective_workflow_contract,
            )
            if reasons:
                self._reject_v2_control(
                    state=state, path=path, raw=raw, reasons=reasons, schema_version=3
                )
                return "invalid_v3"
            self._snapshot_accepted_terminal_control(
                state=state, path=path, signal=signal
            )
            state.control_protocol_consecutive_failures = 0
        if signal.stop_reason == "goal_met":
            state.goal_met = True
            return None
        if signal.stop_reason == "unresolvable_error":
            state.unresolvable_error = True
            return None
        raise RuntimeError("unreachable")

    def _snapshot_accepted_terminal_control(
        self, *, state: LoopState, path: Path, signal: ControlSignal
    ) -> None:
        """Freeze canonical accepted v3 control bytes in engine-owned state."""

        payload = signal.model_dump(mode="json")
        raw_json = json.dumps(payload, indent=2)
        write_text_atomic(path=path, content=raw_json)
        state.accepted_terminal_control = AcceptedTerminalControlSnapshot(
            payload=payload,
            raw_json=raw_json,
            sha256=file_sha256(path=path),
            accepted_at=utc_now(),
        )

    def _validate_v3_control(
        self,
        *,
        state: LoopState,
        signal: ControlSignal,
        workflow_contract: WorkflowSetContract,
    ) -> list[str]:
        """Return every false identity or provenance claim in v3 control."""

        reasons: list[str] = []
        producer = signal.producer
        if producer is None:
            return ["producer is required"]
        current_task = state.current_task
        owns_current_attempt = (
            current_task is not None
            and producer.session_id == state.active_session_id
            and current_task.session_id == producer.session_id
            and current_task.workflow_id == producer.workflow_id
            and current_task.attempt_id == producer.attempt_id
        )
        if not owns_current_attempt:
            reasons.append(
                "producer attempt is not owned by this session's exact current task"
            )
        if signal.stop_reason == "goal_met":
            if workflow_contract.completion_role != producer.workflow_id:
                reasons.append("producer is not the declared completion role")
            for reference in signal.evidence_refs:
                reasons.extend(
                    self._validate_asserted_artifact_reference(
                        session_id=state.active_session_id,
                        reference=reference,
                        label="evidence",
                    )
                )
            for reference in signal.eval_receipt_refs:
                reasons.extend(
                    self._validate_accepted_eval_reference(
                        state=state,
                        workflow_contract=workflow_contract,
                        reference=reference,
                    )
                )
            if signal.handoff_ref is not None:
                reasons.extend(
                    self._validate_control_handoff_reference(
                        state=state, reference=signal.handoff_ref
                    )
                )
            # D13 (A1): when the contract makes the handoff a currency output, a
            # terminal completion must be backed by a handoff the *completing*
            # attempt brought current. This runs independent of whether
            # handoff_ref is cited (handoff_ref is optional, so a citation-only
            # check is bypassable); a currency contract additionally requires the
            # citation so completion always names the handoff it rests on.
            if workflow_contract.currency_handoff_owner is not None:
                reasons.extend(
                    self._validate_completion_handoff_currency(
                        state=state, producer=producer, cited=signal.handoff_ref
                    )
                )
        elif signal.stop_reason == "unresolvable_error":
            if producer.workflow_id not in (
                workflow_contract.terminal_blocker_reporting_roles
            ):
                reasons.append("producer role may not report terminal blockers")
            for reference in signal.evidence_refs:
                reasons.extend(
                    self._validate_asserted_artifact_reference(
                        session_id=state.active_session_id,
                        reference=reference,
                        label="evidence",
                    )
                )
        return reasons

    def _validate_asserted_artifact_reference(
        self, *, session_id: str, reference: str, label: str
    ) -> list[str]:
        """Validate one asserted logical reference without interpreting content."""

        try:
            path = resolve_logical_reference(
                reference=reference, repo_root=self.repo_root, session_id=session_id
            )
        except LogicalReferenceError as exc:
            return [f"{label} reference is invalid: {exc}"]
        if not path.is_file():
            return [f"{label} reference does not resolve to a file: {reference}"]
        return []

    def _validate_accepted_eval_reference(
        self,
        *,
        state: LoopState,
        workflow_contract: WorkflowSetContract,
        reference: str,
    ) -> list[str]:
        """Validate a cited receipt against its engine-owned acceptance seal."""

        reasons: list[str] = []
        validation_errors: list[str] = []
        receipt = self._load_eval_receipt(
            state_session_id=state.active_session_id,
            reference=reference,
            validation_errors=validation_errors,
        )
        if receipt is None:
            return validation_errors or ["eval receipt is missing or invalid"]
        seal = state.accepted_eval_receipt_seals.get(reference)
        if seal is None:
            return ["eval receipt was not accepted by the engine"]
        try:
            receipt_path = resolve_logical_reference(
                reference=reference,
                repo_root=self.repo_root,
                session_id=state.active_session_id,
            )
        except LogicalReferenceError as exc:  # pragma: no cover - loaded above
            return [f"eval receipt reference is invalid: {exc}"]
        if file_sha256(path=receipt_path) != seal.receipt_sha256:
            reasons.append("eval receipt bytes no longer match their acceptance seal")
        if receipt.subject != seal.subject or receipt.producer != seal.producer:
            reasons.append("eval receipt identity contradicts its acceptance seal")
        if receipt.subject.session_id != state.active_session_id:
            reasons.append("eval receipt session does not match")
        if receipt.subject.root_session_id != (
            state.root_session_id or state.active_session_id
        ):
            reasons.append("eval receipt root session does not match")
        if receipt.subject.goal_hash != state.goal_hash:
            reasons.append("eval receipt goal hash does not match")
        if receipt.producer.workflow_id not in workflow_contract.check_runner_roles:
            reasons.append("eval receipt producer is not a declared check runner")
        evaluated_git = {
            "git_commit": receipt.subject.git_commit,
            "dirty_tree_digest": receipt.subject.dirty_tree_digest,
        }
        if evaluated_git != seal.evaluated_git:
            reasons.append("eval receipt git identity contradicts its acceptance seal")
        return reasons

    def _validate_control_handoff_reference(
        self, *, state: LoopState, reference: str
    ) -> list[str]:
        """Validate the canonical handoff identity cited by v3 control."""

        if reference != "session:/project_state/handoff.json":
            return ["handoff reference must name the canonical layer handoff"]
        observation = state.latest_handoff_observation
        snapshot = state.accepted_handoff_snapshot
        if observation is None or observation.status == "missing":
            return ["cited handoff has not been published by the layer orchestrator"]
        if observation.status != "valid":
            return [f"cited handoff observation is {observation.status}"]
        if (
            snapshot is None
            or observation.sha256 != snapshot.sha256
            or observation.revision != snapshot.handoff.revision
        ):
            return ["cited handoff lacks a matching engine acceptance snapshot"]
        return []

    def _validate_completion_handoff_currency(
        self, *, state: LoopState, producer: SignalProducer, cited: str | None
    ) -> list[str]:
        """Require completion to rest on a handoff the completing attempt made current.

        D13 (A1): the accepted handoff snapshot must have been (re)stamped by the
        exact attempt that is declaring ``goal_met``. Evaluated independent of any
        ``handoff_ref`` citation (the citation is optional and therefore bypassable
        on its own); a currency contract additionally requires the citation so a
        completion always names the handoff it rests on. A stale snapshot routes to
        the existing control-repair path via a non-empty reason.
        """

        reasons: list[str] = []
        if cited is None:
            reasons.append(
                "goal_met must cite the current handoff when it is a currency output"
            )
        snapshot = state.accepted_handoff_snapshot
        if snapshot is None or snapshot.handoff.producer is None:
            reasons.append(
                "no accepted handoff snapshot was (re)stamped by the completing attempt"
            )
            return reasons
        stamped = snapshot.handoff.producer
        if (
            stamped.workflow_id != producer.workflow_id
            or stamped.attempt_id != producer.attempt_id
        ):
            reasons.append(
                "the accepted handoff was not re-stamped by the completing attempt "
                "(stale continuity); re-stamp the handoff before declaring completion"
            )
        return reasons

    def _accept_current_eval_receipts(
        self,
        *,
        state: LoopState,
        active: CurrentTask,
        workflow_contract: WorkflowSetContract,
    ) -> None:
        """Seal provenance-valid receipts from an authorized current attempt.

        Evaluation remains optional and advisory. Missing or malformed output
        is reported as a diagnostic event and never changes harness success.
        """

        if active.workflow_id not in workflow_contract.check_runner_roles:
            return
        receipts_dir = (
            session_dir_path(
                repo_root=self.repo_root, session_id=state.active_session_id
            )
            / "eval_receipts"
        )
        accepted_dir = receipts_dir / "accepted"
        accepted_count = 0
        diagnostics: list[str] = []
        for path in sorted(receipts_dir.glob("*.json")):
            try:
                receipt = EvalReceipt.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, ValueError):
                continue
            if (
                receipt.producer.workflow_id != active.workflow_id
                or receipt.producer.attempt_id != active.attempt_id
                or receipt.producer.iteration != active.iteration
            ):
                continue
            reference = f"session:/eval_receipts/{path.name}"
            receipt_errors: list[str] = []
            if receipt.subject.session_id != state.active_session_id:
                receipt_errors.append("subject session does not match")
            if receipt.subject.root_session_id != (
                state.root_session_id or state.active_session_id
            ):
                receipt_errors.append("subject root session does not match")
            if receipt.subject.goal_hash != state.goal_hash:
                receipt_errors.append("subject goal does not match")
            receipt_errors.extend(
                self._validate_eval_receipt_artifacts(
                    session_id=state.active_session_id, receipt=receipt
                )
            )
            if receipt_errors:
                diagnostics.extend(
                    f"{path.name}: {reason}" for reason in receipt_errors[:8]
                )
                continue
            seal = AcceptedEvalReceiptSeal(
                receipt_ref=reference,
                receipt_sha256=file_sha256(path=path),
                subject=receipt.subject,
                producer=receipt.producer,
                evaluated_git={
                    "git_commit": receipt.subject.git_commit,
                    "dirty_tree_digest": receipt.subject.dirty_tree_digest,
                },
                accepted_at=utc_now(),
            )
            state.accepted_eval_receipt_seals[reference] = seal
            write_json_atomic(
                path=accepted_dir / f"{receipt.eval_id}.json",
                payload=seal.model_dump(mode="json"),
            )
            accepted_count += 1
        self._emit(
            session_id=state.active_session_id,
            event_type="eval_observation",
            payload={
                "workflow_id": active.workflow_id,
                "attempt_id": active.attempt_id,
                "accepted_receipts": accepted_count,
                "diagnostics": diagnostics[:8],
            },
        )

    def _observe_layer_handoff(
        self,
        *,
        state: LoopState,
        active: CurrentTask,
        workflow_contract: WorkflowSetContract,
    ) -> None:
        """Record structural handoff continuity without gating loop progress."""

        path = handoff_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        try:
            raw_json = path.read_text(encoding="utf-8")
            digest = file_sha256(path=path)
        except OSError as exc:
            observation = OutcomeHandoff(status="missing")
            state.latest_handoff_observation = observation
            self._emit(
                session_id=state.active_session_id,
                event_type="handoff_observed",
                payload={"status": "missing", "reason": str(exc)},
            )
            return
        try:
            handoff = LayerHandoff.model_validate_json(raw_json)
        except (ValidationError, ValueError) as exc:
            observation = OutcomeHandoff(
                status="invalid",
                ref="session:/project_state/handoff.json",
                sha256=digest,
            )
            state.latest_handoff_observation = observation
            self._emit(
                session_id=state.active_session_id,
                event_type="handoff_observed",
                payload={"status": "invalid", "sha256": digest, "reason": str(exc)},
            )
            return
        status: Literal["valid", "missing", "invalid", "non_monotonic"] = "valid"
        reason: str | None = None
        if handoff.session_id != state.active_session_id:
            status = "invalid"
            reason = "session identity does not match"
        elif handoff.goal_sha256 != state.goal_hash:
            status = "invalid"
            reason = "goal identity does not match"
        elif handoff.producer is None:
            status = "missing"
            reason = "no orchestrator handoff has been published"
        elif (
            workflow_contract.orchestration is not None
            and handoff.producer.workflow_id
            != workflow_contract.orchestration.handoff_owner
        ):
            status = "invalid"
            reason = "producer is not the declared handoff owner"
        elif not self._handoff_producer_is_known(
            state=state, active=active, handoff=handoff
        ):
            status = "invalid"
            reason = "producer attempt is not current or present in session history"
        if status == "valid" and handoff.revision < state.handoff_revision:
            status = "non_monotonic"
            reason = "revision moved backward"
        elif (
            status == "valid"
            and handoff.revision == state.handoff_revision
            and state.handoff_sha256 is not None
            and digest != state.handoff_sha256
            and not self._is_provenance_only_restamp(
                state=state, active=active, handoff=handoff
            )
        ):
            # D13: a same-revision content change is tampering UNLESS it is a
            # provenance-only re-stamp by the current owner attempt (producer and
            # timestamp advance, every other field byte-equal to the accepted
            # snapshot). That exception keeps "re-stamp without a material change"
            # expressible without opening a fixed-revision rewrite hole.
            status = "non_monotonic"
            reason = "revision unchanged but content changed without a valid re-stamp"
        if status == "valid" and handoff.revision >= state.handoff_revision:
            state.handoff_revision = handoff.revision
            state.handoff_sha256 = digest
            state.accepted_handoff_snapshot = AcceptedHandoffSnapshot(
                handoff=handoff, raw_json=raw_json, sha256=digest, accepted_at=utc_now()
            )
        observation = OutcomeHandoff(
            status=status,
            ref=(
                "session:/project_state/handoff.json" if status != "missing" else None
            ),
            sha256=(digest if status != "missing" else None),
            revision=(handoff.revision if status != "missing" else None),
        )
        state.latest_handoff_observation = observation
        self._emit(
            session_id=state.active_session_id,
            event_type="handoff_observed",
            payload={
                "status": status,
                "revision": handoff.revision,
                "sha256": digest,
                "reason": reason,
            },
        )

    def _diagnose_currency_outputs(
        self,
        *,
        state: LoopState,
        active: CurrentTask,
        workflow_contract: WorkflowSetContract,
    ) -> None:
        """Emit non-gating currency diagnostics for the finishing role (D13/D14).

        These never change harness success, respawn, or touch a cap. For the
        handoff (`kind: handoff`) a successful owner finish that did not re-stamp
        the handoff raises ``handoff_stale`` and leaves a repair note the standing
        owner reads on its next turn (correctness is enforced separately at the
        completion boundary, A1). For an advisory output (`kind: advisory`, e.g.
        the eval result) an absent declared path raises ``eval_missing``; the
        engine never reads the file — freshness is the reading agent's judgment.
        """

        session_dir = session_dir_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if active.workflow_id == workflow_contract.currency_handoff_owner:
            snapshot = state.accepted_handoff_snapshot
            stamped = snapshot.handoff.producer if snapshot is not None else None
            if (
                stamped is None
                or stamped.workflow_id != active.workflow_id
                or stamped.attempt_id != active.attempt_id
            ):
                failure_id = f"handoff-currency-{uuid.uuid4().hex[:12]}"
                write_json_atomic(
                    path=protocol_failures_dir_path(
                        repo_root=self.repo_root, session_id=state.active_session_id
                    )
                    / f"{failure_id}.json",
                    payload={
                        "schema_version": 1,
                        "failure_id": failure_id,
                        "kind": "stale_handoff",
                        "reasons": [
                            "the handoff owner finished without re-stamping "
                            "project_state/handoff.json with this attempt's "
                            "producer identity; re-stamp it on your next turn so a "
                            "successor never reads stale continuity"
                        ],
                        "finishing_attempt": {
                            "workflow_id": active.workflow_id,
                            "attempt_id": active.attempt_id,
                        },
                        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                    },
                )
                self._emit(
                    session_id=state.active_session_id,
                    event_type="handoff_stale",
                    payload={
                        "workflow_id": active.workflow_id,
                        "attempt_id": active.attempt_id,
                        "protocol_failure_ref": (
                            f"session:/protocol_failures/{failure_id}.json"
                        ),
                    },
                )
        for entry in workflow_contract.advisory_currency_outputs():
            if active.workflow_id != entry.owner_role:
                continue
            declared_path = session_dir / entry.path
            if not declared_path.is_file():
                self._emit(
                    session_id=state.active_session_id,
                    event_type="eval_missing",
                    payload={
                        "workflow_id": active.workflow_id,
                        "attempt_id": active.attempt_id,
                        "path": entry.path,
                    },
                )

    @staticmethod
    def _is_provenance_only_restamp(
        *, state: LoopState, active: CurrentTask, handoff: LayerHandoff
    ) -> bool:
        """Return whether a same-revision handoff is a legitimate re-stamp (D13).

        A re-stamp is legitimate only when the current owner attempt advances the
        provenance and timestamp of an otherwise-identical handoff: the producer
        is exactly this finishing attempt, ``updated_at`` does not move backward,
        and every field other than ``producer``/``updated_at`` equals the accepted
        snapshot. Anything else at the same revision is tampering.
        """

        snapshot = state.accepted_handoff_snapshot
        producer = handoff.producer
        if snapshot is None or producer is None:
            return False
        if (
            active.session_id != state.active_session_id
            or producer.workflow_id != active.workflow_id
            or producer.attempt_id != active.attempt_id
        ):
            return False
        if handoff.updated_at < snapshot.handoff.updated_at:
            return False
        ignore = {"producer", "updated_at"}
        return handoff.model_dump(exclude=ignore) == snapshot.handoff.model_dump(
            exclude=ignore
        )

    @staticmethod
    def _handoff_producer_is_known(
        *, state: LoopState, active: CurrentTask, handoff: LayerHandoff
    ) -> bool:
        """Return whether handoff provenance names a real session attempt."""

        producer = handoff.producer
        if producer is None:
            return False
        if (
            active.session_id == state.active_session_id
            and producer.workflow_id == active.workflow_id
            and producer.attempt_id == active.attempt_id
        ):
            return True
        return any(
            entry.session_id == state.active_session_id
            and entry.workflow_id == producer.workflow_id
            and entry.attempt_id == producer.attempt_id
            for entry in state.history
        )

    def _validate_v2_control(
        self,
        *,
        state: LoopState,
        signal: ControlSignal,
        workflow_contract: WorkflowSetContract,
    ) -> list[str]:
        """Return every identity or evidence contradiction in a v2 control."""

        reasons: list[str] = []
        producer = signal.producer
        if producer is None:
            return ["producer is required"]
        if producer.session_id != state.active_session_id:
            reasons.append("producer session does not match control subject")
        current_task = state.current_task
        owns_current_attempt = (
            current_task is not None
            and current_task.session_id == producer.session_id
            and current_task.attempt_id == producer.attempt_id
            and current_task.workflow_id == producer.workflow_id
        )
        if not owns_current_attempt:
            reasons.append(
                "producer attempt is not owned by this session's exact current task"
            )
        matching_iteration = (
            current_task.iteration
            if current_task is not None and owns_current_attempt
            else None
        )
        if signal.stop_reason == "goal_met":
            if workflow_contract.eval.goal_control_role != producer.workflow_id:
                reasons.append("producer is not the declared goal-control role")
            projection: GoalCheckSignal | None = None
            if matching_iteration is not None:
                projection = _read_signal(
                    path=goal_check_path(
                        repo_root=self.repo_root,
                        session_id=state.active_session_id,
                        iteration=matching_iteration,
                        workflow_id=producer.workflow_id,
                    ),
                    model=GoalCheckSignal,
                )
            if projection is None or projection.schema_version != 2:
                reasons.append(
                    "producer iteration has no valid goal_check v2 projection"
                )
            else:
                if not projection.goal_met:
                    reasons.append("producer goal_check projection is not passing")
                if projection.eval_receipt_ref != signal.eval_receipt_ref:
                    reasons.append(
                        "control eval receipt does not match the producer "
                        "goal_check projection"
                    )
            receipt_validation_errors: list[str] = []
            receipt = self._load_eval_receipt(
                state_session_id=state.active_session_id,
                reference=signal.eval_receipt_ref,
                validation_errors=receipt_validation_errors,
            )
            if receipt is None:
                reasons.extend(
                    receipt_validation_errors or ["eval receipt is missing or invalid"]
                )
            else:
                if receipt.subject.session_id != state.active_session_id:
                    reasons.append("eval receipt session does not match")
                if receipt.subject.root_session_id != (
                    state.root_session_id or state.active_session_id
                ):
                    reasons.append("eval receipt root session does not match")
                if receipt.subject.goal_hash != state.goal_hash:
                    reasons.append("eval receipt goal hash does not match")
                if receipt.producer.attempt_id != producer.attempt_id:
                    reasons.append("eval receipt producer attempt does not match")
                if receipt.producer.workflow_id != producer.workflow_id:
                    reasons.append("eval receipt producer role does not match")
                if receipt.producer.iteration != matching_iteration:
                    reasons.append("eval receipt producer iteration does not match")
                if not receipt.verdict.goal_met:
                    reasons.append("eval receipt verdict does not report goal_met")
                if (
                    projection is not None
                    and projection.schema_version == 2
                    and projection.reason != receipt.verdict.reason
                ):
                    reasons.append(
                        "goal_check projection reason does not match eval receipt "
                        "verdict reason"
                    )
                reasons.extend(
                    self._validate_eval_receipt_artifacts(
                        session_id=state.active_session_id, receipt=receipt
                    )
                )
        elif signal.stop_reason == "unresolvable_error":
            if producer.workflow_id not in (
                workflow_contract.terminal_blocker_reporting_roles
            ):
                reasons.append("producer role may not report terminal blockers")
            if not signal.attempted_routes:
                reasons.append("terminal blocker lacks attempted autonomous routes")
        return reasons

    def _read_workflow_contract(self, *, session_id: str) -> WorkflowSetContract:
        """Load a session's persisted workflow role contract."""

        path = workflow_contract_path(repo_root=self.repo_root, session_id=session_id)
        try:
            return WorkflowSetContract.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise ConfigError(f"invalid workflow contract at {path}: {exc}") from exc

    def _workflow_contract_for_state(self, *, state: LoopState) -> WorkflowSetContract:
        """Return engine-owned v2 trust or derive a legacy-v1 contract.

        Pre-v2 sessions did not persist ``workflow_contract.json``. Their
        historical v1 control/goal-check/child-request behavior must remain
        usable after resume, so a missing file is projected from the current
        workflow-set definition with protocol v1 semantics. A v2 session uses
        the contract stored in engine state; its agent-visible files are only
        synchronized projections and cannot downgrade later attempts.
        """

        if state.schema_version >= 2:
            self._validate_session_contract(state=state)
            if state.workflow_contract is None:  # pragma: no cover - validated above
                raise ConfigError(
                    "v2 session state has no engine-owned workflow contract trust root"
                )
            return state.workflow_contract
        path = workflow_contract_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if path.is_file():
            return self._read_workflow_contract(session_id=state.active_session_id)
        return self._preflight_for(
            workflow_set=state.workflow_set
        ).workflow_contract.model_copy(update={"session_protocol_version": 1})

    def _load_eval_receipt(
        self,
        *,
        state_session_id: str,
        reference: str | None,
        validation_errors: list[str] | None = None,
    ) -> EvalReceipt | None:
        """Resolve and validate a current-session eval receipt reference."""

        def reject(reason: str) -> None:
            """Append one receipt validation reason when a sink was supplied."""

            if validation_errors is not None:
                validation_errors.append(reason)

        if reference is None:
            reject(reason="eval receipt reference is missing")
            return None
        try:
            path = resolve_logical_reference(
                reference=reference,
                repo_root=self.repo_root,
                session_id=state_session_id,
            )
        except LogicalReferenceError as exc:
            reject(reason=f"eval receipt reference is invalid: {exc}")
            return None
        expected_parent = (
            session_dir_path(repo_root=self.repo_root, session_id=state_session_id)
            / "eval_receipts"
        ).resolve()
        if path.parent != expected_parent or path.suffix != ".json":
            reject(
                reason="eval receipt must be a current-session eval_receipts JSON file"
            )
            return None
        try:
            return EvalReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            reject(reason=f"eval receipt cannot be read: {exc}")
            return None
        except ValidationError as exc:
            messages = []
            for item in exc.errors(include_input=False, include_url=False):
                location = ".".join(str(part) for part in item.get("loc", ()))
                message = str(item.get("msg", "invalid value"))
                messages.append(f"{location}: {message}" if location else message)
            reject(reason="eval receipt schema is invalid: " + "; ".join(messages[:8]))
            return None
        except ValueError as exc:
            reject(reason=f"eval receipt JSON is invalid: {exc}")
            return None

    def _validate_eval_receipt_artifacts(
        self, *, session_id: str, receipt: EvalReceipt
    ) -> list[str]:
        """Return transport and provenance defects in a claimed eval receipt."""

        reasons: list[str] = []
        raw_paths: list[Path] = []
        checks_dir = (
            session_dir_path(repo_root=self.repo_root, session_id=session_id)
            / "eval_checks"
        )
        definitions: dict[str, list[Path]] = {}
        definition_types: dict[Path, object] = {}
        for path in sorted([*checks_dir.rglob("*.yaml"), *checks_dir.rglob("*.yml")]):
            if path.is_symlink() or not path.is_file():
                reasons.append(
                    f"authored eval check {path.name!r} is not a regular file"
                )
                continue
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                reasons.append(f"authored eval check {path.name!r} is invalid: {exc}")
                continue
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                definitions.setdefault(str(payload["id"]), []).append(path)
                definition_types[path] = payload.get("type")
            else:
                reasons.append(
                    f"authored eval check {path.name!r} has no string check id"
                )
        receipt_check_ids = {check.check_id for check in receipt.checks}
        missing_check_ids = sorted(set(definitions) - receipt_check_ids)
        if missing_check_ids:
            reasons.append(
                "eval receipt omitted authored checks: " + ", ".join(missing_check_ids)
            )
        for check in receipt.checks:
            matches = definitions.get(check.check_id, [])
            if len(matches) != 1:
                reasons.append(
                    f"eval check {check.check_id!r} does not resolve uniquely"
                )
            elif definition_types[matches[0]] != check.kind:
                reasons.append(
                    f"eval check {check.check_id!r} kind does not match its "
                    "definition type"
                )
            else:
                try:
                    definition_sha256 = compute_check_definition_sha256(
                        source_path=matches[0]
                    )
                except (OSError, ValueError) as exc:
                    reasons.append(
                        f"authored eval check {matches[0].name!r} cannot be "
                        f"canonically hashed by eval-banana: {exc}"
                    )
                else:
                    if definition_sha256 != check.definition_sha256:
                        reasons.append(
                            f"eval check {check.check_id!r} definition hash does "
                            "not match"
                        )
        eval_receipts_dir = (
            session_dir_path(repo_root=self.repo_root, session_id=session_id)
            / "eval_receipts"
        ).resolve()
        try:
            canonical = resolve_logical_reference(
                reference=receipt.canonical_report_ref,
                repo_root=self.repo_root,
                session_id=session_id,
            )
            if canonical.parent != eval_receipts_dir:
                reasons.append(
                    "canonical eval report is not a current-session "
                    "eval_receipts sibling"
                )
            elif not canonical.is_file():
                reasons.append("canonical eval report does not exist")
            elif file_sha256(path=canonical) != receipt.canonical_report_sha256:
                reasons.append("canonical eval report hash does not match")
        except LogicalReferenceError:
            reasons.append("canonical eval report reference is invalid")
        layout = session_layout(repo_root=self.repo_root, session_id=session_id)
        if layout == SESSION_LAYOUT_FOLDED:
            # Folded: the raw report lives at the producer iteration's raw dir
            # and is bound to the exact producer by that deterministic path.
            # There is no per-attempt manifest, so identity is proven by the
            # path itself rather than by a manifest identity block.
            expected_raw_report = (
                raw_attempt_dir_path(
                    repo_root=self.repo_root,
                    session_id=receipt.subject.session_id,
                    iteration=receipt.producer.iteration,
                    workflow_id=receipt.producer.workflow_id,
                )
                / "eval"
                / "report.json"
            ).resolve()
            session_root = session_dir_path(
                repo_root=self.repo_root, session_id=receipt.subject.session_id
            ).resolve()
            expected_raw_ref = (
                "session:/" + expected_raw_report.relative_to(session_root).as_posix()
            )
        else:
            expected_raw_report = (
                attempt_trace_dir_path(
                    repo_root=self.repo_root,
                    root_session_id=receipt.subject.root_session_id,
                    session_id=receipt.subject.session_id,
                    attempt_id=receipt.producer.attempt_id,
                ).resolve()
                / "eval"
                / "report.json"
            )
            expected_raw_ref = (
                f"trace:trace-{receipt.producer.attempt_id}:/eval/report.json"
            )
        if receipt.raw_report_refs != [expected_raw_ref]:
            reasons.append(
                "eval receipt must reference only the canonical attempt "
                "eval/report.json"
            )
        for reference in receipt.raw_report_refs:
            try:
                raw_report = resolve_logical_reference(
                    reference=reference, repo_root=self.repo_root, session_id=session_id
                )
                if not raw_report.is_file():
                    reasons.append("raw eval report does not exist")
                elif raw_report != expected_raw_report:
                    reasons.append(
                        "raw eval report is not the producer attempt's canonical "
                        "eval/report.json"
                    )
                elif file_sha256(path=raw_report) != receipt.raw_report_sha256s.get(
                    reference
                ):
                    reasons.append("raw eval report hash does not match")
                elif layout == SESSION_LAYOUT_FOLDED:
                    raw_paths.append(raw_report)
                else:
                    try:
                        manifest = json.loads(
                            expected_raw_report.parent.parent.joinpath(
                                "trace_manifest.json"
                            ).read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        manifest = None
                    identity = (
                        manifest.get("identity") if isinstance(manifest, dict) else None
                    )
                    expected_identity = {
                        "root_session_id": receipt.subject.root_session_id,
                        "session_id": receipt.subject.session_id,
                        "workflow_id": receipt.producer.workflow_id,
                        "iteration": receipt.producer.iteration,
                        "attempt_id": receipt.producer.attempt_id,
                    }
                    if not isinstance(identity, dict) or any(
                        identity.get(key) != value
                        for key, value in expected_identity.items()
                    ):
                        reasons.append(
                            "raw eval report trace identity does not match the "
                            "receipt producer"
                        )
                    elif identity.get("harness_run_id") != (
                        receipt.producer.harness_run_id
                    ):
                        reasons.append(
                            "raw eval report harness run does not match the "
                            "receipt producer"
                        )
                    else:
                        raw_paths.append(raw_report)
            except LogicalReferenceError:
                reasons.append("raw eval report reference is invalid")
        git_binding_required = receipt.verdict.goal_met or any(
            value is not None
            for value in (receipt.subject.git_commit, receipt.subject.dirty_tree_digest)
        )
        if git_binding_required:
            if receipt.verdict.goal_met and receipt.subject.git_commit is None:
                reasons.append("passing eval receipt must record its git commit")
            if receipt.verdict.goal_met and receipt.subject.dirty_tree_digest is None:
                reasons.append("passing eval receipt must record its dirty tree digest")
            git_after_path = git_receipt_path(
                repo_root=self.repo_root,
                session_id=session_id,
                iteration=receipt.producer.iteration,
                workflow_id=receipt.producer.workflow_id,
                attempt_id=receipt.producer.attempt_id,
                phase="after",
            )
            try:
                git_after = json.loads(git_after_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                git_after = None
            if not isinstance(git_after, dict):
                reasons.append("producer git-after receipt is missing or invalid")
            else:
                if (
                    git_after.get("phase") != "after"
                    or git_after.get("attempt_id") != receipt.producer.attempt_id
                ):
                    reasons.append(
                        "git-after receipt identity does not match the eval producer"
                    )
                if git_after.get("head") != receipt.subject.git_commit:
                    reasons.append("eval subject git commit does not match git-after")
                if (
                    git_after.get("dirty_tree_digest")
                    != receipt.subject.dirty_tree_digest
                ):
                    reasons.append(
                        "eval subject dirty tree digest does not match git-after"
                    )
            # The compact git-after receipt proves what the worker observed;
            # this second read proves those bytes are still the acceptance
            # subject when the coordinator applies the terminal decision.
            try:
                live_git = capture_git_evidence(
                    repo_root=self.repo_root,
                    phase="after",
                    attempt_id=receipt.producer.attempt_id,
                )
            except GitEvidenceError as exc:
                reasons.append(f"cannot inspect live eval subject git state: {exc}")
            else:
                if live_git.head != receipt.subject.git_commit:
                    reasons.append(
                        "eval subject git commit does not match live repository"
                    )
                if live_git.dirty_tree_digest != receipt.subject.dirty_tree_digest:
                    reasons.append(
                        "eval subject dirty tree digest does not match live repository"
                    )
        if receipt.verdict.goal_met:
            reports = [path for path in raw_paths if path == expected_raw_report]
            if len(reports) != 1:
                reasons.append(
                    "passing eval receipt must reference exactly one eval-banana "
                    "report.json"
                )
            else:
                reasons.extend(
                    _validate_passing_eval_banana_report(
                        path=reports[0],
                        receipt=receipt,
                        repo_root=self.repo_root,
                        expected_output_dir=expected_raw_report.parent,
                    )
                )
        return reasons

    def _reject_v2_control(
        self,
        *,
        state: LoopState,
        path: Path,
        raw: object,
        reasons: list[str],
        schema_version: int = 2,
    ) -> None:
        """Archive invalid identity-bound control and publish repair context."""

        raw_control_id = raw.get("control_id") if isinstance(raw, dict) else None
        control_id = (
            raw_control_id
            if isinstance(raw_control_id, str)
            and SAFE_DURABLE_ID_PATTERN.fullmatch(raw_control_id)
            else f"control-{uuid.uuid4().hex[:12]}"
        )
        rejected_dir = control_rejected_dir_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        rejected_dir.mkdir(parents=True, exist_ok=True)
        rejected_path = rejected_dir / f"{control_id}.json"
        if rejected_path.exists():
            rejected_path = rejected_dir / f"{control_id}-{uuid.uuid4().hex[:8]}.json"
        original_hash = file_sha256(path=path)
        path.rename(rejected_path)
        state.control_protocol_consecutive_failures += 1
        failure_id = f"protocol-failure-{uuid.uuid4().hex[:12]}"
        producer = raw.get("producer") if isinstance(raw, dict) else None
        rejected_attempt_id = (
            state.current_task.attempt_id if state.current_task is not None else None
        )
        failure_ref = f"session:/protocol_failures/{failure_id}.json"
        write_json_atomic(
            path=protocol_failures_dir_path(
                repo_root=self.repo_root, session_id=state.active_session_id
            )
            / f"{failure_id}.json",
            payload={
                "schema_version": 1,
                "failure_id": failure_id,
                "kind": "invalid_control",
                "producer": producer,
                "rejected_control_ref": (
                    f"session:/control_rejected/{rejected_path.name}"
                ),
                "original_sha256": original_hash,
                "reasons": reasons,
                "consecutive_failure_count": (
                    state.control_protocol_consecutive_failures
                ),
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            },
        )
        write_json_atomic(
            path=path,
            payload={
                "schema_version": schema_version,
                "state": "running",
                "reason": "invalid terminal request archived for autonomous repair",
                "stop_reason": None,
                "engine_repair": {
                    "kind": _CONTROL_REPAIR_PLACEHOLDER_KIND,
                    "rejected_attempt_id": rejected_attempt_id,
                    "protocol_failure_ref": failure_ref,
                },
            },
        )
        if (
            state.control_protocol_consecutive_failures
            >= state.config_snapshot.goal_check_consecutive_failures_cap
        ):
            state.status = "failed"
            state.stop_reason = "control_protocol_broken"


def _control_repair_placeholder_identity(*, raw: object) -> tuple[bool, str | None]:
    """Recognize the coordinator placeholder left after rejecting v2 control."""

    if not isinstance(raw, dict):
        return False, None
    marker = raw.get("engine_repair")
    if not isinstance(marker, dict) or marker.get("kind") != (
        _CONTROL_REPAIR_PLACEHOLDER_KIND
    ):
        return False, None
    attempt_id = marker.get("rejected_attempt_id")
    return True, attempt_id if isinstance(attempt_id, str) and attempt_id else None


def _new_attempt_id() -> str:
    return uuid.uuid4().hex[:12]


def _layout_for_protocol(*, protocol_version: int) -> str:
    """Select the on-disk layout for a freshly created session.

    Protocol-v3 sessions fold their raw artifacts and receipts into the session
    tree; the historical v1/v2 contracts keep the mirror trace tree and
    per-family receipt dirs so their frozen behavior is untouched.
    """

    return SESSION_LAYOUT_FOLDED if protocol_version >= 3 else SESSION_LAYOUT_MIRROR


def _build_run_response(
    *, current_task: CurrentTask, config_snapshot: RootConfigSnapshot, repo_root: Path
) -> TaskResponse:
    """Build the worker-facing response for one frozen current task."""

    descriptor = current_task.workflow_snapshot
    protocol_version = (
        current_task.completion_contract_version if descriptor is not None else 1
    )
    required_capabilities = (
        REQUIRED_V3_WORKER_CAPABILITIES
        if protocol_version >= 3
        else REQUIRED_V2_WORKER_CAPABILITIES
        if protocol_version >= 2
        else frozenset()
    )
    return TaskResponse(
        action="run",
        workflow_set=current_task.workflow_set,
        workflow_id=current_task.workflow_id,
        session_id=current_task.session_id,
        iteration=current_task.iteration,
        attempt_id=current_task.attempt_id,
        config_snapshot=config_snapshot,
        coordinator_protocol_version=protocol_version,
        required_capabilities=sorted(required_capabilities),
        repo_root=str(repo_root.resolve()),
        repository_id=current_task.repository_id,
        assignment_path=(
            str(
                assignment_path(
                    repo_root=repo_root,
                    session_id=current_task.session_id,
                    iteration=current_task.iteration,
                    workflow_id=current_task.workflow_id,
                    attempt_id=current_task.attempt_id or "legacy",
                ).resolve()
            )
            if descriptor is not None
            else None
        ),
        assignment_sha256=current_task.assignment_sha256,
        workflow_snapshot=descriptor,
    )


def _require_state(*, state: LoopState | None) -> LoopState:
    if state is None:
        raise RuntimeError("Coordinator state is not initialized")
    return state


def _reject_request(*, request_path: Path, reason: str) -> None:
    """Terminally reject a child request, keeping an inspectable record.

    Collision-safe: a second rejection with the same original name never
    overwrites the first record.
    """
    requests_root = (
        request_path.parent.parent
        if request_path.parent.name == "pending"
        else request_path.parent
    )
    if request_path.parent.name == "pending":
        rejected_dir = requests_root / "rejected"
        rejected_dir.mkdir(parents=True, exist_ok=True)
        rejected = rejected_dir / request_path.name
        if rejected.exists():
            rejected = rejected_dir / (
                f"{request_path.stem}.{uuid.uuid4().hex[:8]}{request_path.suffix}"
            )
    else:
        # Preserve the v1 filename contract while the dual reader is active.
        rejected = request_path.with_suffix(request_path.suffix + ".rejected")
        if rejected.exists():
            rejected = request_path.with_suffix(
                request_path.suffix + f".{uuid.uuid4().hex[:8]}.rejected"
            )
    original_hash = file_sha256(path=request_path)
    request_path.rename(rejected)
    receipt_dir = requests_root / "rejected"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path=receipt_dir / f"{rejected.name}.receipt.json",
        payload={
            "schema_version": 1,
            "kind": "child_request_rejected",
            "original_name": request_path.name,
            "archived_path": str(rejected),
            "original_sha256": original_hash,
            "reason": reason,
            "rejected_at": utc_now().isoformat().replace("+00:00", "Z"),
        },
    )
    logger.warning(
        "rejected child request %s (%s); kept as %s",
        request_path.name,
        reason,
        rejected.name,
    )


def _same_task(*, a: CurrentTask, b: CurrentTask) -> bool:
    """Compare durable task coordinates with legacy attempt-ID tolerance."""

    return (
        a.session_id == b.session_id
        and a.workflow_id == b.workflow_id
        and a.iteration == b.iteration
        and (
            a.attempt_id is None or b.attempt_id is None or a.attempt_id == b.attempt_id
        )
    )


def _validate_passing_eval_banana_report(
    *, path: Path, receipt: EvalReceipt, repo_root: Path, expected_output_dir: Path
) -> list[str]:
    """Verify transport facts for a passing LLM-as-judge report.

    This does not reinterpret the judge's semantic reasons. It proves that the
    hashed raw report actually records the agent/model and all-pass mechanics
    claimed by the compact receipt.
    """

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["passing eval-banana report is unreadable"]
    if not isinstance(report, dict):
        return ["passing eval-banana report is not an object"]
    reasons: list[str] = []
    raw_project_root = report.get("project_root")
    if (
        not isinstance(raw_project_root, str)
        or not Path(raw_project_root).is_absolute()
    ):
        reasons.append("eval-banana report project root is not absolute")
    elif Path(raw_project_root).resolve() != repo_root.resolve():
        reasons.append("eval-banana report project root does not match repository")
    raw_output_dir = report.get("output_dir")
    if not isinstance(raw_output_dir, str) or not Path(raw_output_dir).is_absolute():
        reasons.append("eval-banana report output directory is not absolute")
    elif Path(raw_output_dir).resolve() != expected_output_dir.resolve():
        reasons.append(
            "eval-banana report output directory is not the canonical attempt eval path"
        )
    if report.get("run_passed") is not True:
        reasons.append("eval-banana report does not record a passing run")
    if report.get("pass_threshold") != 1.0:
        reasons.append("eval-banana report pass threshold is not 1.0")
    checks = report.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(item, dict) for item in checks
    ):
        return [*reasons, "eval-banana report check inventory is invalid"]
    by_id = {
        str(item.get("check_id")): item
        for item in checks
        if isinstance(item.get("check_id"), str)
    }
    expected_ids = {item.check_id for item in receipt.checks}
    if set(by_id) != expected_ids or len(by_id) != len(checks):
        reasons.append("eval-banana report checks do not match the receipt")
        return reasons
    for check_id in sorted(expected_ids):
        item = by_id[check_id]
        expected_definition = next(
            check.definition_sha256
            for check in receipt.checks
            if check.check_id == check_id
        )
        if item.get("check_definition_sha256") != expected_definition:
            reasons.append(
                f"eval-banana check {check_id!r} definition hash does not match"
            )
        if item.get("status") != "passed":
            reasons.append(f"eval-banana check {check_id!r} is not passed")
        if item.get("exit_code") != 0:
            reasons.append(
                f"eval-banana check {check_id!r} judge did not exit successfully"
            )
        details = item.get("details")
        if not isinstance(details, dict):
            reasons.append(f"eval-banana check {check_id!r} lacks judge details")
            continue
        if details.get("agent_type") != receipt.judge.provider:
            reasons.append(f"eval-banana check {check_id!r} judge agent does not match")
        if details.get("model") != receipt.judge.model:
            reasons.append(f"eval-banana check {check_id!r} judge model does not match")
        if details.get("reasoning_effort") != receipt.judge.reasoning_effort:
            reasons.append(
                f"eval-banana check {check_id!r} judge reasoning effort does not match"
            )
    return reasons


def _matches_current_task(
    *, request: FinishedRequest, current_task: CurrentTask
) -> bool:
    return (
        request.session_id == current_task.session_id
        and request.workflow_id == current_task.workflow_id
        and request.iteration == current_task.iteration
        and (
            current_task.attempt_id is None
            or request.attempt_id == current_task.attempt_id
        )
    )


class ChildLedgerError(RuntimeError):
    pass


def _read_children_payload(*, path: Path) -> dict[str, Any]:
    """Read and structurally validate a v1 or v2 child-session ledger."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChildLedgerError(
            f"children ledger is unreadable at {path}; preserved for repair: {exc}"
        ) from exc
    if isinstance(raw, dict) and isinstance(raw.get("children"), list):
        version = raw.get("schema_version", 1)
        if version == 2:
            if not isinstance(raw.get("parent_session_id"), str):
                raise ChildLedgerError(
                    f"v2 children ledger lacks parent_session_id at {path}"
                )
            revision = raw.get("revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise ChildLedgerError(
                    f"v2 children ledger has invalid revision at {path}"
                )
            request_ids: set[str] = set()
            child_ids: set[str] = set()
            live = 0
            for record in raw["children"]:
                if not isinstance(record, dict):
                    raise ChildLedgerError(
                        f"children ledger has a non-object record at {path}"
                    )
                child_id = record.get("session_id")
                request_id = record.get("request_id")
                if not isinstance(child_id, str) or not isinstance(request_id, str):
                    raise ChildLedgerError(
                        f"v2 children ledger record lacks identity at {path}"
                    )
                if child_id in child_ids or request_id in request_ids:
                    raise ChildLedgerError(
                        f"v2 children ledger has duplicate identity at {path}"
                    )
                child_ids.add(child_id)
                request_ids.add(request_id)
                status = record.get("status")
                if status not in _VALID_CHILD_STATUSES:
                    raise ChildLedgerError(
                        f"v2 children ledger has invalid status at {path}"
                    )
                if status in _LIVE_CHILD_STATUSES:
                    live += 1
            if live > 1:
                raise ChildLedgerError(
                    f"v2 children ledger records multiple live children at {path}"
                )
            return raw
        return {"schema_version": 1, "children": raw["children"]}
    if isinstance(raw, list):
        return {"schema_version": 1, "children": raw}
    raise ChildLedgerError(f"children ledger has an invalid schema at {path}")


def _bump_children_revision(*, payload: dict[str, Any]) -> None:
    """Increment a v2 child ledger's optimistic revision counter."""

    if payload.get("schema_version") == 2:
        payload["revision"] = int(payload.get("revision", 0)) + 1


def _child_request_id(*, request: ChildSessionRequest, path: Path) -> str:
    """Return an explicit request ID or a stable legacy content-derived ID."""

    if request.request_id:
        return request.request_id
    digest = file_sha256(path=path).split(":", 1)[1][:20]
    return f"legacy-{digest}"


def _latest_artifact_ref(
    *, repo_root: Path, session_id: str, directory: str
) -> str | None:
    """Return a logical reference to the latest regular file in a session dir."""

    root = session_dir_path(repo_root=repo_root, session_id=session_id) / directory
    if not root.exists():
        return None
    candidates = sorted(path for path in root.iterdir() if path.is_file())
    if not candidates:
        return None
    return f"session:{session_id}:/{directory}/{candidates[-1].name}"


def _artifact_ref_if_present(
    *, repo_root: Path, session_id: str, relative_path: str
) -> str | None:
    """Return a session reference when the requested artifact exists."""

    path = session_dir_path(repo_root=repo_root, session_id=session_id) / relative_path
    if not path.is_file():
        return None
    return f"session:{session_id}:/{relative_path}"


def _trace_outcome_projection(
    *, value: str | None, repo_root: Path
) -> tuple[str | None, bool]:
    """Project a trace-manifest path into a logical ref and integrity flag."""

    if not value:
        return None, False
    path = Path(value)
    if not path.is_file():
        return None, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    manifest_id = payload.get("manifest_id") if isinstance(payload, dict) else None
    if not isinstance(manifest_id, str) or not manifest_id:
        return None, False
    finalized = payload.get("lifecycle") in {"sealed", "incomplete"}
    if not finalized:
        return f"trace:{manifest_id}:/", False
    integrity = verify_trace_integrity(trace_root=path.parent, repo_root=repo_root)
    return f"trace:{manifest_id}:/", integrity.get("status") == "verified"


SignalModel = TypeVar("SignalModel", bound=BaseModel)


def _read_signal(*, path: Path, model: type[SignalModel]) -> SignalModel | None:
    """Read an optional typed protocol signal without treating absence as error."""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable signal file at %s", path)
        return None
    try:
        signal = model.model_validate(payload)
    except Exception:
        logger.warning("Ignoring invalid signal schema at %s", path)
        return None
    return signal
