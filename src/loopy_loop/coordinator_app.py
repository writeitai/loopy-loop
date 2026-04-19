from __future__ import annotations

from datetime import datetime
from datetime import timedelta
import json
import logging
from pathlib import Path
from typing import TypeVar
import uuid

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel

from loopy_loop.config import ConfigError
from loopy_loop.config import PreflightResult
from loopy_loop.config import run_preflight
from loopy_loop.models import ActiveAssignment
from loopy_loop.models import ControlSignal
from loopy_loop.models import FinishedRequest
from loopy_loop.models import GoalCheckSignal
from loopy_loop.models import HistoryEntry
from loopy_loop.models import LoopState
from loopy_loop.models import NextActionResponse
from loopy_loop.models import RegisterWorkerResponse
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import STOP_ACTION
from loopy_loop.models import utc_now
from loopy_loop.models import WAIT_ACTION
from loopy_loop.models import WorkerState
from loopy_loop.scheduler import choose_next_workflow
from loopy_loop.sessions import control_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import goal_check_path
from loopy_loop.state_store import StateStore

logger = logging.getLogger(__name__)


def create_coordinator_app(*, repo_root: Path, resume: bool) -> FastAPI:
    preflight = run_preflight(repo_root=repo_root)
    store = StateStore(repo_root=repo_root)
    service = CoordinatorService(
        repo_root=repo_root, preflight=preflight, state_store=store, resume=resume
    )
    app = FastAPI()
    app.state.service = service

    @app.post("/workers/register", response_model=RegisterWorkerResponse)
    def register_worker() -> RegisterWorkerResponse:
        return service.register_worker()

    @app.post("/workers/{worker_id}/next", response_model=NextActionResponse)
    def next_action(worker_id: str) -> NextActionResponse:
        try:
            return service.next_action(worker_id=worker_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/workers/{worker_id}/finished", response_model=NextActionResponse)
    def finish_assignment(
        worker_id: str, request: FinishedRequest
    ) -> NextActionResponse:
        try:
            return service.finish_assignment(worker_id=worker_id, request=request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
        self.workflows = preflight.workflows
        self.state_store = state_store
        self._prepare_state(resume=resume)

    def register_worker(self) -> RegisterWorkerResponse:
        def mutator(
            state: LoopState | None,
        ) -> tuple[LoopState, RegisterWorkerResponse]:
            current = _require_state(state=state)
            now = utc_now()
            worker_id = f"worker_{uuid.uuid4().hex[:8]}"
            current.workers[worker_id] = WorkerState(
                status="idle", registered_at=now, last_seen_at=now
            )
            return current, RegisterWorkerResponse(worker_id=worker_id)

        return self.state_store.mutate(mutator)

    def next_action(self, *, worker_id: str) -> NextActionResponse:
        def mutator(state: LoopState | None) -> tuple[LoopState, NextActionResponse]:
            current = _require_state(state=state)
            response = self._dispatch_next_action(state=current, worker_id=worker_id)
            return current, response

        return self.state_store.mutate(mutator)

    def finish_assignment(
        self, *, worker_id: str, request: FinishedRequest
    ) -> NextActionResponse:
        def mutator(state: LoopState | None) -> tuple[LoopState, NextActionResponse]:
            current = _require_state(state=state)
            worker = self._require_worker(state=current, worker_id=worker_id)
            now = utc_now()
            worker.last_seen_at = now
            if current.active_assignment is None:
                return current, self._dispatch_next_action(
                    state=current, worker_id=worker_id
                )
            active_assignment = current.active_assignment
            if request.assignment_id != active_assignment.assignment_id:
                return current, self._dispatch_next_action(
                    state=current, worker_id=worker_id
                )

            success = request.success
            error = request.error
            if self._has_invalid_control_output(active_assignment=active_assignment):
                success = False
                error = "invalid_control_output"

            if active_assignment.workflow_id == "goal_check":
                goal_signal = self._read_goal_check_signal(
                    active_assignment=active_assignment
                )
                if goal_signal is None:
                    success = False
                    error = "invalid_goal_check_output"
                    current.goal_check_consecutive_failures += 1
                    if (
                        current.goal_check_consecutive_failures
                        >= current.config_snapshot.goal_check_consecutive_failures_cap
                    ):
                        current.stop_reason = "goal_check_broken"
                        current.status = "failed"
                else:
                    current.goal_check_consecutive_failures = 0
                    if goal_signal.goal_met:
                        current.goal_met = True
                        current.stop_reason = "goal_met"
                        current.status = "goal_met"

            if self._has_unresolvable_error_signal(active_assignment=active_assignment):
                current.unresolvable_error = True
                current.stop_reason = "unresolvable_error"
                current.status = "failed"

            current.history.append(
                HistoryEntry(
                    assignment_id=active_assignment.assignment_id,
                    iteration=active_assignment.iteration,
                    workflow_id=active_assignment.workflow_id,
                    worker_id=active_assignment.worker_id,
                    session_id=active_assignment.session_id,
                    success=success,
                    error=error,
                    started_at=active_assignment.assigned_at,
                    finished_at=now,
                )
            )
            current.iteration_count += 1
            current.active_assignment = None
            worker.status = "idle"

            if current.stop_reason == "goal_check_broken":
                return current, NextActionResponse(
                    action=STOP_ACTION, stop_reason="goal_check_broken"
                )

            response = self._dispatch_next_action(state=current, worker_id=worker_id)
            return current, response

        return self.state_store.mutate(mutator)

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
        create_session_dir(
            repo_root=self.repo_root,
            session_id=existing_state.active_session_id,
            goal_slug=existing_state.goal_slug,
        )

    def _write_fresh_state(self) -> None:
        session_id = create_session_id(goal_slug=self.preflight.root_config.goal_slug)
        create_session_dir(
            repo_root=self.repo_root,
            session_id=session_id,
            goal_slug=self.preflight.root_config.goal_slug,
        )
        snapshot = RootConfigSnapshot.model_validate(
            self.preflight.root_config.model_dump()
        )
        state = LoopState(
            status="running",
            goal_slug=self.preflight.root_config.goal_slug,
            max_turns=self.preflight.root_config.max_turns,
            active_session_id=session_id,
            config_snapshot=snapshot,
        )
        self.state_store.write_state(state=state)

    def _dispatch_next_action(
        self, *, state: LoopState, worker_id: str
    ) -> NextActionResponse:
        worker = self._require_worker(state=state, worker_id=worker_id)
        now = utc_now()
        worker.last_seen_at = now
        self._reclaim_expired_assignment(state=state, now=now)
        stop_response = self._stop_response_if_needed(state=state)
        if stop_response is not None:
            worker.status = "idle"
            return stop_response

        if state.active_assignment is not None:
            if state.active_assignment.worker_id == worker_id:
                worker.status = "busy"
                return self._run_response(
                    assignment=state.active_assignment, snapshot=state.config_snapshot
                )
            worker.status = "idle"
            return NextActionResponse(action=WAIT_ACTION)

        workflow = choose_next_workflow(
            workflows=self.workflows,
            history=state.history,
            iteration_count=state.iteration_count,
        )
        if workflow is None:
            state.stop_reason = "no_eligible_workflow"
            state.status = "failed"
            worker.status = "idle"
            return NextActionResponse(
                action=STOP_ACTION, stop_reason="no_eligible_workflow"
            )

        assignment = ActiveAssignment(
            assignment_id=str(uuid.uuid4()),
            worker_id=worker_id,
            session_id=state.active_session_id,
            iteration=state.iteration_count + 1,
            workflow_id=workflow.id,
            assigned_at=now,
        )
        state.active_assignment = assignment
        worker.status = "busy"
        return self._run_response(assignment=assignment, snapshot=state.config_snapshot)

    def _reclaim_expired_assignment(self, *, state: LoopState, now: datetime) -> None:
        active_assignment = state.active_assignment
        if active_assignment is None:
            return
        worker = state.workers.get(active_assignment.worker_id)
        lease_deadline = active_assignment.assigned_at + timedelta(
            seconds=active_assignment.lease_seconds
        )
        worker_deadline = None
        if worker is not None:
            worker_deadline = worker.last_seen_at + timedelta(
                seconds=active_assignment.lease_seconds
            )
        if now <= lease_deadline and (
            worker_deadline is None or now <= worker_deadline
        ):
            return
        state.history.append(
            HistoryEntry(
                assignment_id=active_assignment.assignment_id,
                iteration=active_assignment.iteration,
                workflow_id=active_assignment.workflow_id,
                worker_id=active_assignment.worker_id,
                session_id=active_assignment.session_id,
                success=False,
                error="lease_expired",
                started_at=active_assignment.assigned_at,
                finished_at=now,
            )
        )
        state.iteration_count += 1
        if worker is not None:
            worker.status = "idle"
        state.active_assignment = None

    def _stop_response_if_needed(
        self, *, state: LoopState
    ) -> NextActionResponse | None:
        if state.goal_met:
            state.status = "goal_met"
            state.stop_reason = "goal_met"
            return NextActionResponse(action=STOP_ACTION, stop_reason="goal_met")
        if state.stop_requested:
            state.status = "stopped"
            state.stop_reason = "stop_requested"
            return NextActionResponse(action=STOP_ACTION, stop_reason="stop_requested")
        if state.unresolvable_error:
            state.status = "failed"
            state.stop_reason = "unresolvable_error"
            return NextActionResponse(
                action=STOP_ACTION, stop_reason="unresolvable_error"
            )
        if state.iteration_count >= state.max_turns:
            state.status = "max_turns"
            state.stop_reason = "max_turns"
            return NextActionResponse(action=STOP_ACTION, stop_reason="max_turns")
        if state.status in {"stopped", "goal_met", "failed", "max_turns"}:
            return NextActionResponse(
                action=STOP_ACTION, stop_reason=state.stop_reason or state.status
            )
        return None

    def _run_response(
        self, *, assignment: ActiveAssignment, snapshot: RootConfigSnapshot
    ) -> NextActionResponse:
        return NextActionResponse(
            action="run",
            assignment_id=assignment.assignment_id,
            workflow_id=assignment.workflow_id,
            session_id=assignment.session_id,
            iteration=assignment.iteration,
            config_snapshot=snapshot,
        )

    def _require_worker(self, *, state: LoopState, worker_id: str) -> WorkerState:
        worker = state.workers.get(worker_id)
        if worker is None:
            raise KeyError(f"Unknown worker: {worker_id}")
        return worker

    def _read_goal_check_signal(
        self, *, active_assignment: ActiveAssignment
    ) -> GoalCheckSignal | None:
        path = goal_check_path(
            repo_root=self.repo_root,
            session_id=active_assignment.session_id,
            iteration=active_assignment.iteration,
        )
        return _read_signal(path=path, model=GoalCheckSignal)

    def _has_unresolvable_error_signal(
        self, *, active_assignment: ActiveAssignment
    ) -> bool:
        path = control_path(
            repo_root=self.repo_root,
            session_id=active_assignment.session_id,
            iteration=active_assignment.iteration,
            workflow_id=active_assignment.workflow_id,
        )
        signal = _read_signal(path=path, model=ControlSignal)
        return signal is not None and signal.unresolvable_error

    def _has_invalid_control_output(
        self, *, active_assignment: ActiveAssignment
    ) -> bool:
        path = control_path(
            repo_root=self.repo_root,
            session_id=active_assignment.session_id,
            iteration=active_assignment.iteration,
            workflow_id=active_assignment.workflow_id,
        )
        if not path.exists():
            return False
        return _read_signal(path=path, model=ControlSignal) is None


def _require_state(*, state: LoopState | None) -> LoopState:
    if state is None:
        raise RuntimeError("Coordinator state is not initialized")
    return state


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
    if getattr(signal, "schema_version", None) != 1:
        logger.warning("Ignoring unsupported signal schema_version at %s", path)
        return None
    return signal
