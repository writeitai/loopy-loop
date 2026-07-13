from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import socket
from typing import Any
from typing import TypeVar
import uuid

from fastapi import FastAPI
from fastapi import HTTPException
from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel

from loopy_loop.config import ConfigError
from loopy_loop.config import derive_goal_hash
from loopy_loop.config import PreflightResult
from loopy_loop.config import run_preflight
from loopy_loop.config import WorkflowDefinition
from loopy_loop.models import ChildSessionRecord
from loopy_loop.models import ChildSessionRequest
from loopy_loop.models import ControlSignal
from loopy_loop.models import CurrentTask
from loopy_loop.models import FinishedRequest
from loopy_loop.models import GoalCheckSignal
from loopy_loop.models import HistoryEntry
from loopy_loop.models import IterationResult
from loopy_loop.models import LoopState
from loopy_loop.models import RegisterRequest
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import STOP_ACTION
from loopy_loop.models import TaskResponse
from loopy_loop.models import utc_now
from loopy_loop.models import WorkerIdentity
from loopy_loop.recovery import recover_interrupted_iteration
from loopy_loop.recovery import RecoveryIncompleteError
from loopy_loop.recovery import RecoveryOutcome
from loopy_loop.recovery import RecoveryRefusedError
from loopy_loop.scheduler import choose_next_workflow
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import goal_check_path
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.sessions import result_path
from loopy_loop.sessions import state_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.state_store import StateStore
from loopy_loop.worker_identity import is_worker_alive

logger = logging.getLogger(__name__)

# Coordinator-operational settings that must never enter the wire config
# snapshot: released workers validate the snapshot with extra="forbid", so any
# new response field would crash them at parse time (protocol compatibility).
_COORDINATOR_ONLY_FIELDS = {"recovery_policy", "recovery_drain_timeout_s"}


class WorkerBusyError(RuntimeError):
    """The current task's worker is verifiably still alive — do not reclaim."""


def create_coordinator_app(
    *,
    repo_root: Path,
    resume: bool,
    workflow_set: str | None = None,
    goal_file: Path | None = None,
) -> FastAPI:
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
        try:
            return service.finish_assignment(request=request)
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
        self.repo_root = repo_root
        self.preflight = preflight
        self.preflights: dict[str, PreflightResult] = {
            preflight.workflow_set: preflight
        }
        self.state_store = state_store
        self._prepare_state(resume=resume)

    def register_worker(
        self, *, request: RegisterRequest | None = None
    ) -> TaskResponse:
        caller = request.worker if request is not None else None
        # Two-phase recovery: the potentially long drain of a dead worker's
        # orphaned agents (up to recovery_drain_timeout_s) runs in phase A,
        # OUTSIDE the state lock, so `loopy status`/`stop` and /finished stay
        # responsive. Phase B re-validates under the lock and retries from
        # phase A when the state moved in between.
        for _ in range(3):
            recovery = self._plan_orphan_recovery()
            response = self._register_attempt(caller=caller, recovery=recovery)
            if response is not None:
                return response
        raise WorkerBusyError(
            "crash recovery is contended (state changed repeatedly while "
            "recovering); retry shortly"
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
    ) -> TaskResponse | None:
        """Phase B: commit under the state lock; None means retry from phase A."""
        recovered_pending_paths: list[Path] = []

        def mutator(state: LoopState | None) -> tuple[LoopState, TaskResponse | None]:
            current = _require_state(state=state)
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
                    recovered_request, pending_path = recovered
                    if pending_path is not None:
                        recovered_pending_paths.append(pending_path)
                    self._record_finished_task(
                        state=current,
                        active=orphaned,
                        request=recovered_request,
                        now=now,
                    )
                elif recovery is not None and _same_task(orphaned, recovery[0]):
                    # The dead worker's orphans were handled in phase A
                    # (outside the lock); commit the abandonment.
                    outcome = recovery[1]
                    error = "abandoned"
                    if outcome.salvaged:
                        error = f"abandoned_after_{outcome.policy or 'drain'}"
                    current.history.append(
                        HistoryEntry(
                            iteration=orphaned.iteration,
                            workflow_set=orphaned.workflow_set,
                            workflow_id=orphaned.workflow_id,
                            session_id=orphaned.session_id,
                            success=False,
                            error=error,
                            started_at=orphaned.started_at,
                            finished_at=now,
                        )
                    )
                    current.iteration_count += 1
                    current.current_task = None
                else:
                    # The state moved between phase A and phase B (a stale
                    # /finished landed, or another register recovered first):
                    # do not act on a stale plan — replan.
                    return current, None

            # Step 4+: stop conditions, child dispatch, next workflow.
            return current, self._advance(state=current, caller=caller, now=now)

        response = self.state_store.mutate(mutator)
        for path in recovered_pending_paths:
            path.unlink(missing_ok=True)
        return response

    def _advance(
        self, *, state: LoopState, caller: WorkerIdentity | None, now: datetime
    ) -> TaskResponse:
        """The single scheduling step shared by every dispatch path.

        Order: stop conditions -> pending child dispatch -> next workflow ->
        stamp a fresh CurrentTask (new attempt id, caller as owner). Extracted
        so the three former copies (register, finished no-task, finished
        matched) cannot drift apart.
        """
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
        )
        if workflow is None:
            state.stop_reason = "no_eligible_workflow"
            state.status = "failed"
            return TaskResponse(action=STOP_ACTION, stop_reason="no_eligible_workflow")
        state.current_task = CurrentTask(
            workflow_set=state.workflow_set,
            workflow_id=workflow.id,
            session_id=state.active_session_id,
            iteration=state.iteration_count + 1,
            started_at=now,
            worker=caller,
            attempt_id=_new_attempt_id(),
        )
        return _build_run_response(
            current_task=state.current_task, config_snapshot=state.config_snapshot
        )

    def _raise_if_worker_alive(self, *, current_task: CurrentTask) -> None:
        if is_worker_alive(current_task.worker) is not True:
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
        caller = request.worker

        def mutator(state: LoopState | None) -> tuple[LoopState, TaskResponse]:
            current = _require_state(state=state)
            now = utc_now()

            # Step 3: No active task — stale call. Dispatch as if /register was called.
            # This handles the post-crash stale retry scenario safely.
            if current.current_task is None:
                return current, self._advance(state=current, caller=caller, now=now)

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
                    # A late /finished from a SUPERSEDED attempt of the very
                    # same coordinates is stale too: the work was already
                    # recovered/abandoned and redispatched under a new attempt.
                    request.attempt_id is not None
                    and active.attempt_id is not None
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
                return current, _build_run_response(
                    current_task=active, config_snapshot=current.config_snapshot
                )

            # Step 5: Match confirmed — process result.
            self._record_finished_task(
                state=current, active=active, request=request, now=now
            )

            # Step 6: Special cases that stop immediately.
            if current.stop_reason == "goal_check_broken":
                return current, TaskResponse(
                    action=STOP_ACTION, stop_reason="goal_check_broken"
                )

            # Step 7+: stop conditions, child dispatch, next workflow.
            return current, self._advance(state=current, caller=caller, now=now)

        response = self.state_store.mutate(mutator)
        if response.action == STOP_ACTION:
            parent_response = self._resume_parent_if_active_child_completed(
                caller=caller
            )
            if parent_response is not None:
                return parent_response
        return response

    def _record_finished_task(
        self,
        *,
        state: LoopState,
        active: CurrentTask,
        request: FinishedRequest,
        now: datetime,
    ) -> None:
        success = request.success
        error = request.error

        if self._workflow_expects_goal_check_signal(
            workflow_set=active.workflow_set, workflow_id=active.workflow_id
        ):
            goal_signal = self._read_goal_check_signal(current_task=active)
            if goal_signal is None:
                success = False
                error = "invalid_goal_check_output"
                state.goal_check_consecutive_failures += 1
                if (
                    state.goal_check_consecutive_failures
                    >= state.config_snapshot.goal_check_consecutive_failures_cap
                ):
                    state.stop_reason = "goal_check_broken"
                    state.status = "failed"
            else:
                state.goal_check_consecutive_failures = 0

        if state.stop_reason != "goal_check_broken":
            self._apply_session_control(state=state)
            if state.stop_reason == "invalid_control_output":
                success = False
                error = "invalid_control_output"

        state.history.append(
            HistoryEntry(
                iteration=active.iteration,
                workflow_set=active.workflow_set,
                workflow_id=active.workflow_id,
                session_id=active.session_id,
                success=success,
                error=error,
                started_at=active.started_at,
                finished_at=now,
            )
        )
        state.iteration_count += 1
        state.current_task = None

    def _recover_orphaned_agents(self, *, current_task: CurrentTask) -> RecoveryOutcome:
        """Apply the configured recovery policy to a dead worker's orphans.

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
        )

    def _read_recoverable_finished_request(
        self, *, current_task: CurrentTask
    ) -> tuple[FinishedRequest, Path | None] | None:
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
            return request, pending

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
        return (
            FinishedRequest(
                session_id=current_task.session_id,
                workflow_id=current_task.workflow_id,
                iteration=current_task.iteration,
                success=result.success,
                text=result.text,
                error=result.error,
            ),
            None,
        )

    def _prepare_state(self, *, resume: bool) -> None:
        existing_state = self.state_store.read_state()
        if existing_state is None:
            self._write_fresh_state()
            return
        if self.state_store.is_terminal_state(state=existing_state):
            self.state_store.archive_state()
            self._write_fresh_state()
            return
        if not resume:
            raise ConfigError(
                "Found running loopy-loop state. Restart with --resume to continue "
                "the in-progress session."
            )
        active_state = self._reconstruct_session_stack(top_state=existing_state)
        create_session_dir(
            repo_root=self.repo_root,
            session_id=active_state.active_session_id,
            goal_hash=active_state.goal_hash,
            goal=active_state.config_snapshot.goal,
            workflow_set=active_state.workflow_set,
            parent_session_id=active_state.parent_session_id,
        )

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
        state = top_state
        while True:
            child_id = state.active_child_session_id or self._adoptable_child_id(
                parent_state=state
            )
            if child_id is None:
                return state
            child_store = StateStore(
                repo_root=self.repo_root,
                state_path=state_path(repo_root=self.repo_root, session_id=child_id),
            )
            child_state = child_store.read_state()
            parent_store = self._store_for(session_id=state.active_session_id)
            if child_state is None:
                logger.warning(
                    "session %s points at child %s whose state is missing; "
                    "clearing the pointer (interrupted dispatch)",
                    state.active_session_id,
                    child_id,
                )
                state = self._set_child_pointer(
                    store=parent_store, child_session_id=None
                )
                return state
            if child_store.is_terminal_state(state=child_state):
                self._mark_child_record_complete(child_state=child_state)
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
        """The crash window where the child was fully created (state written,
        children.json recorded) but the parent's pointer commit never landed:
        adopt the running child instead of redispatching a duplicate."""
        payload = _read_children_payload(
            path=children_path(
                repo_root=self.repo_root, session_id=parent_state.active_session_id
            )
        )
        for record in reversed(payload["children"]):
            if record.get("status") != "running":
                continue
            child_id = record.get("session_id")
            if not child_id:
                continue
            child_state = StateStore(
                repo_root=self.repo_root,
                state_path=state_path(repo_root=self.repo_root, session_id=child_id),
            ).read_state()
            if child_state is not None and child_state.status == "running":
                return child_id
        return None

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

    def _write_fresh_state(self) -> None:
        session_id = create_session_id(goal_hash=self.preflight.root_config.goal_hash)
        create_session_dir(
            repo_root=self.repo_root,
            session_id=session_id,
            goal_hash=self.preflight.root_config.goal_hash,
            goal=self.preflight.root_config.goal,
            workflow_set=self.preflight.workflow_set,
        )
        self.state_store = StateStore(
            repo_root=self.repo_root,
            state_path=state_path(repo_root=self.repo_root, session_id=session_id),
        )
        snapshot = RootConfigSnapshot.model_validate(
            self.preflight.root_config.model_dump(exclude=_COORDINATOR_ONLY_FIELDS)
        )
        state = LoopState(
            status="running",
            goal_hash=self.preflight.root_config.goal_hash,
            workflow_set=self.preflight.workflow_set,
            max_turns=self.preflight.root_config.max_turns,
            active_session_id=session_id,
            config_snapshot=snapshot,
        )
        self.state_store.write_state(state=state)

    def _preflight_for(
        self, *, workflow_set: str, goal: str | None = None
    ) -> PreflightResult:
        preflight = self.preflights.get(workflow_set)
        if preflight is None:
            preflight = run_preflight(
                repo_root=self.repo_root, workflow_set=workflow_set
            )
            self.preflights[workflow_set] = preflight
        if goal is None:
            return preflight
        root_config = preflight.root_config.model_copy(
            update={"goal": goal, "workflow_set": workflow_set}
        )
        return PreflightResult(
            root_config=root_config,
            workflow_set=workflow_set,
            workflows=preflight.workflows,
        )

    def _workflows_for(self, *, workflow_set: str) -> list[WorkflowDefinition]:
        return self._preflight_for(workflow_set=workflow_set).workflows

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
        if state.parent_session_id is not None:
            return None
        requests_dir = child_requests_dir_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not requests_dir.exists():
            return None
        dispatched_request_files = self._dispatched_request_files(
            parent_session_id=state.active_session_id
        )
        for request_path in sorted(requests_dir.glob("*.json")):
            if request_path.name in dispatched_request_files:
                # Already produced a child (the crash window between recording
                # the child and unlinking the request): never dispatch twice.
                request_path.unlink(missing_ok=True)
                continue
            request = _read_signal(path=request_path, model=ChildSessionRequest)
            if request is None:
                # Terminal rejection: leave an inspectable record instead of
                # silently re-reading a broken file on every dispatch scan.
                rejected = request_path.with_suffix(request_path.suffix + ".rejected")
                request_path.rename(rejected)
                logger.warning(
                    "rejected invalid child request %s (kept as %s)",
                    request_path.name,
                    rejected.name,
                )
                continue
            preflight = self._preflight_for(
                workflow_set=request.workflow_set, goal=request.goal
            )
            workflows = preflight.workflows
            workflow = choose_next_workflow(
                workflows=workflows, history=[], iteration_count=0
            )
            if workflow is None:
                continue
            goal_hash = derive_goal_hash(goal=request.goal)
            child_session_id = create_session_id(goal_hash=goal_hash)
            create_session_dir(
                repo_root=self.repo_root,
                session_id=child_session_id,
                goal_hash=goal_hash,
                goal=request.goal,
                workflow_set=request.workflow_set,
                parent_session_id=state.active_session_id,
            )
            snapshot = RootConfigSnapshot.model_validate(
                preflight.root_config.model_dump(exclude=_COORDINATOR_ONLY_FIELDS)
            )
            now = utc_now()
            child_state = LoopState(
                status="running",
                goal_hash=goal_hash,
                workflow_set=request.workflow_set,
                parent_session_id=state.active_session_id,
                max_turns=preflight.root_config.max_turns,
                active_session_id=child_session_id,
                config_snapshot=snapshot,
                current_task=CurrentTask(
                    workflow_set=request.workflow_set,
                    workflow_id=workflow.id,
                    session_id=child_session_id,
                    iteration=1,
                    started_at=now,
                    worker=caller,
                ),
            )
            child_store = StateStore(
                repo_root=self.repo_root,
                state_path=state_path(
                    repo_root=self.repo_root, session_id=child_session_id
                ),
            )
            child_store.write_state(state=child_state)
            child_task = child_state.current_task
            if child_task is None:
                raise RuntimeError("Child session was created without a task")
            self._append_child_record(
                parent_session_id=state.active_session_id,
                record=ChildSessionRecord(
                    session_id=child_session_id,
                    workflow_set=request.workflow_set,
                    goal_hash=goal_hash,
                    status="running",
                    created_at=now,
                    request_file=request_path.name,
                ),
            )
            # The durable session-stack pointer: committed with the parent
            # state when this mutator returns, so a restarted coordinator can
            # walk parent -> child instead of resuming the parent and
            # orphaning the running child.
            state.active_child_session_id = child_session_id
            request_path.unlink(missing_ok=True)
            self.state_store = child_store
            return _build_run_response(
                current_task=child_task, config_snapshot=child_state.config_snapshot
            )
        return None

    def _dispatch_child_session_after_success(
        self, *, state: LoopState, caller: WorkerIdentity | None = None
    ) -> TaskResponse | None:
        if not state.history or not state.history[-1].success:
            return None
        return self._dispatch_child_session_if_requested(state=state, caller=caller)

    def _resume_parent_if_active_child_completed(
        self, *, caller: WorkerIdentity | None = None
    ) -> TaskResponse | None:
        child_state = self.state_store.read_state()
        if child_state is None or child_state.parent_session_id is None:
            return None
        if not self.state_store.is_terminal_state(state=child_state):
            return None
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
        return self.register_worker(request=RegisterRequest(worker=caller))

    def _clear_child_pointer(
        self, *, parent_store: StateStore, parent_session_id: str, child_session_id: str
    ) -> None:
        """The child reached a terminal state: the parent's stack pointer no
        longer points at live work. Also removes the originating request file
        if a crash window left it behind (its children.json record already
        prevents redispatch; this is just hygiene)."""

        def mutator(state: LoopState | None) -> tuple[LoopState, None]:
            parent = _require_state(state=state)
            if parent.active_child_session_id == child_session_id:
                parent.active_child_session_id = None
            return parent, None

        parent_store.mutate(mutator)
        payload = _read_children_payload(
            path=children_path(repo_root=self.repo_root, session_id=parent_session_id)
        )
        for record in payload["children"]:
            if record.get("session_id") != child_session_id:
                continue
            request_file = record.get("request_file")
            if request_file:
                leftover = (
                    child_requests_dir_path(
                        repo_root=self.repo_root, session_id=parent_session_id
                    )
                    / request_file
                )
                leftover.unlink(missing_ok=True)
            break

    def _dispatched_request_files(self, *, parent_session_id: str) -> set[str]:
        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = _read_children_payload(path=path)
        return {
            record["request_file"]
            for record in payload["children"]
            if record.get("request_file")
        }

    def _append_child_record(
        self, *, parent_session_id: str, record: ChildSessionRecord
    ) -> None:
        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = _read_children_payload(path=path)
        payload["children"].append(json.loads(record.model_dump_json()))
        write_json_atomic(path=path, payload=payload)

    def _mark_child_record_complete(self, *, child_state: LoopState) -> None:
        assert child_state.parent_session_id is not None
        path = children_path(
            repo_root=self.repo_root, session_id=child_state.parent_session_id
        )
        payload = _read_children_payload(path=path)
        for record in payload["children"]:
            if record.get("session_id") == child_state.active_session_id:
                record["status"] = child_state.status
                record["completed_at"] = utc_now().isoformat().replace("+00:00", "Z")
                record["stop_reason"] = child_state.stop_reason
                break
        write_json_atomic(path=path, payload=payload)

    def _apply_stop_precedence(self, *, state: LoopState) -> str | None:
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
        if state.iteration_count >= state.max_turns:
            state.status = "max_turns"
            state.stop_reason = "max_turns"
            return "max_turns"
        if state.status in {"stopped", "goal_met", "failed", "max_turns"}:
            return state.stop_reason or state.status
        return None

    def _stop_response_if_needed(self, *, state: LoopState) -> TaskResponse | None:
        self._apply_session_control(state=state)
        stop_reason = self._apply_stop_precedence(state=state)
        if stop_reason is not None:
            return TaskResponse(action=STOP_ACTION, stop_reason=stop_reason)
        return None

    def _read_goal_check_signal(
        self, *, current_task: CurrentTask
    ) -> GoalCheckSignal | None:
        path = goal_check_path(
            repo_root=self.repo_root,
            session_id=current_task.session_id,
            iteration=current_task.iteration,
            workflow_id=current_task.workflow_id,
        )
        return _read_signal(path=path, model=GoalCheckSignal)

    def _workflow_expects_goal_check_signal(
        self, *, workflow_set: str, workflow_id: str
    ) -> bool:
        workflow = self._workflows_by_id_for(workflow_set=workflow_set).get(workflow_id)
        return workflow_id == "goal_check" or (
            workflow is not None and workflow.emits_goal_check
        )

    def _apply_session_control(self, *, state: LoopState) -> None:
        path = control_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not path.exists():
            return
        signal = _read_signal(path=path, model=ControlSignal)
        if signal is None:
            state.status = "failed"
            state.stop_reason = "invalid_control_output"
            return
        if signal.state == "running":
            return
        if signal.stop_reason == "goal_met":
            state.goal_met = True
            return
        if signal.stop_reason == "unresolvable_error":
            state.unresolvable_error = True
            return
        raise RuntimeError("unreachable")


def _new_attempt_id() -> str:
    return uuid.uuid4().hex[:12]


def _build_run_response(
    *, current_task: CurrentTask, config_snapshot: RootConfigSnapshot
) -> TaskResponse:
    return TaskResponse(
        action="run",
        workflow_set=current_task.workflow_set,
        workflow_id=current_task.workflow_id,
        session_id=current_task.session_id,
        iteration=current_task.iteration,
        attempt_id=current_task.attempt_id,
        config_snapshot=config_snapshot,
    )


def _require_state(*, state: LoopState | None) -> LoopState:
    if state is None:
        raise RuntimeError("Coordinator state is not initialized")
    return state


def _same_task(a: CurrentTask, b: CurrentTask) -> bool:
    return (
        a.session_id == b.session_id
        and a.workflow_id == b.workflow_id
        and a.iteration == b.iteration
    )


def _matches_current_task(
    *, request: FinishedRequest, current_task: CurrentTask
) -> bool:
    return (
        request.session_id == current_task.session_id
        and request.workflow_id == current_task.workflow_id
        and request.iteration == current_task.iteration
        and (
            request.attempt_id is None
            or current_task.attempt_id is None
            or request.attempt_id == current_task.attempt_id
        )
    )


def _read_children_payload(*, path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw, dict) and isinstance(raw.get("children"), list):
        return {"schema_version": 1, "children": raw["children"]}
    if isinstance(raw, list):
        return {"schema_version": 1, "children": raw}
    return {"schema_version": 1, "children": []}


SignalModel = TypeVar("SignalModel", bound=BaseModel)


def _read_signal(*, path: Path, model: type[SignalModel]) -> SignalModel | None:
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
    schema_version = getattr(signal, "schema_version", None)
    if schema_version is not None and schema_version != 1:
        logger.warning("Ignoring unsupported signal schema_version at %s", path)
        return None
    return signal
