from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypeVar

from fastapi import FastAPI
from pydantic import BaseModel

from loopy_loop.config import ConfigError
from loopy_loop.config import PreflightResult
from loopy_loop.config import run_preflight
from loopy_loop.models import ControlSignal
from loopy_loop.models import CurrentTask
from loopy_loop.models import FinishedRequest
from loopy_loop.models import GoalCheckSignal
from loopy_loop.models import HistoryEntry
from loopy_loop.models import LoopState
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import STOP_ACTION
from loopy_loop.models import TaskResponse
from loopy_loop.models import utc_now
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

    @app.post("/register", response_model=TaskResponse)
    def register_worker() -> TaskResponse:
        return service.register_worker()

    @app.post("/finished", response_model=TaskResponse)
    def finish_assignment(request: FinishedRequest) -> TaskResponse:
        return service.finish_assignment(request=request)

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
        self.workflows_by_id = {workflow.id: workflow for workflow in self.workflows}
        self.state_store = state_store
        self._prepare_state(resume=resume)

    def register_worker(self) -> TaskResponse:
        def mutator(state: LoopState | None) -> tuple[LoopState, TaskResponse]:
            current = _require_state(state=state)
            now = utc_now()

            # Step 3: If current_task is set (crash recovery from previous worker crash),
            # record it as abandoned BEFORE checking stop conditions.
            # Important: this increment can itself trigger max_turns — that is correct
            # behaviour. The stop check (step 4) will catch it immediately after.
            if current.current_task is not None:
                orphaned = current.current_task
                current.history.append(
                    HistoryEntry(
                        iteration=orphaned.iteration,
                        workflow_id=orphaned.workflow_id,
                        session_id=orphaned.session_id,
                        success=False,
                        error="abandoned",
                        started_at=orphaned.started_at,
                        finished_at=now,
                    )
                )
                current.iteration_count += 1
                current.current_task = None

            # Step 4: Check stop conditions after abandoned-task cleanup.
            stop_response = self._stop_response_if_needed(state=current)
            if stop_response is not None:
                return current, stop_response

            # Step 5: Choose next workflow.
            workflow = choose_next_workflow(
                workflows=self.workflows,
                history=current.history,
                iteration_count=current.iteration_count,
            )
            if workflow is None:
                current.stop_reason = "no_eligible_workflow"
                current.status = "failed"
                return current, TaskResponse(
                    action=STOP_ACTION, stop_reason="no_eligible_workflow"
                )

            # Step 6: Set current_task and return run response.
            current.current_task = CurrentTask(
                workflow_id=workflow.id,
                session_id=current.active_session_id,
                iteration=current.iteration_count + 1,
                started_at=now,
            )
            return current, _build_run_response(
                current_task=current.current_task,
                config_snapshot=current.config_snapshot,
            )

        return self.state_store.mutate(mutator)

    def finish_assignment(self, *, request: FinishedRequest) -> TaskResponse:
        def mutator(state: LoopState | None) -> tuple[LoopState, TaskResponse]:
            current = _require_state(state=state)
            now = utc_now()

            # Step 3: No active task — stale call. Dispatch as if /register was called.
            # This handles the post-crash stale retry scenario safely.
            if current.current_task is None:
                # Check stop conditions first; if terminal, return stop.
                stop_response = self._stop_response_if_needed(state=current)
                if stop_response is not None:
                    return current, stop_response
                workflow = choose_next_workflow(
                    workflows=self.workflows,
                    history=current.history,
                    iteration_count=current.iteration_count,
                )
                if workflow is None:
                    current.stop_reason = "no_eligible_workflow"
                    current.status = "failed"
                    return current, TaskResponse(
                        action=STOP_ACTION, stop_reason="no_eligible_workflow"
                    )
                current.current_task = CurrentTask(
                    workflow_id=workflow.id,
                    session_id=current.active_session_id,
                    iteration=current.iteration_count + 1,
                    started_at=now,
                )
                return current, _build_run_response(
                    current_task=current.current_task,
                    config_snapshot=current.config_snapshot,
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
            ):
                return current, _build_run_response(
                    current_task=active, config_snapshot=current.config_snapshot
                )

            # Step 5: Match confirmed — process result.
            success = request.success
            error = request.error

            # 5b: Goal-check artifact validation.
            if self._workflow_expects_goal_check_signal(workflow_id=active.workflow_id):
                goal_signal = self._read_goal_check_signal(current_task=active)
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

            # 5c: Read session control before recording history. The actual stop
            # response is returned after the completed task is recorded below.
            if current.stop_reason != "goal_check_broken":
                self._apply_session_control(state=current)
                if current.stop_reason == "invalid_control_output":
                    success = False
                    error = "invalid_control_output"

            # 5d: Record history.
            current.history.append(
                HistoryEntry(
                    iteration=active.iteration,
                    workflow_id=active.workflow_id,
                    session_id=active.session_id,
                    success=success,
                    error=error,
                    started_at=active.started_at,
                    finished_at=now,
                )
            )
            # 5e: Increment iteration count.
            current.iteration_count += 1
            # 5f: Clear current task.
            current.current_task = None

            # Step 6: Special cases that stop immediately.
            if current.stop_reason == "goal_check_broken":
                return current, TaskResponse(
                    action=STOP_ACTION, stop_reason="goal_check_broken"
                )

            # Step 7: Check stop conditions.
            stop_response = self._stop_response_if_needed(state=current)
            if stop_response is not None:
                return current, stop_response

            # Step 8: Dispatch next workflow.
            workflow = choose_next_workflow(
                workflows=self.workflows,
                history=current.history,
                iteration_count=current.iteration_count,
            )
            if workflow is None:
                current.stop_reason = "no_eligible_workflow"
                current.status = "failed"
                return current, TaskResponse(
                    action=STOP_ACTION, stop_reason="no_eligible_workflow"
                )

            # Step 9: Set new current_task and return run response.
            current.current_task = CurrentTask(
                workflow_id=workflow.id,
                session_id=current.active_session_id,
                iteration=current.iteration_count + 1,
                started_at=now,
            )
            return current, _build_run_response(
                current_task=current.current_task,
                config_snapshot=current.config_snapshot,
            )

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
            goal_hash=existing_state.goal_hash,
        )

    def _write_fresh_state(self) -> None:
        session_id = create_session_id(goal_hash=self.preflight.root_config.goal_hash)
        create_session_dir(
            repo_root=self.repo_root,
            session_id=session_id,
            goal_hash=self.preflight.root_config.goal_hash,
        )
        snapshot = RootConfigSnapshot.model_validate(
            self.preflight.root_config.model_dump()
        )
        state = LoopState(
            status="running",
            goal_hash=self.preflight.root_config.goal_hash,
            max_turns=self.preflight.root_config.max_turns,
            active_session_id=session_id,
            config_snapshot=snapshot,
        )
        self.state_store.write_state(state=state)

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

    def _workflow_expects_goal_check_signal(self, *, workflow_id: str) -> bool:
        workflow = self.workflows_by_id.get(workflow_id)
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


def _build_run_response(
    *, current_task: CurrentTask, config_snapshot: RootConfigSnapshot
) -> TaskResponse:
    return TaskResponse(
        action="run",
        workflow_id=current_task.workflow_id,
        session_id=current_task.session_id,
        iteration=current_task.iteration,
        config_snapshot=config_snapshot,
    )


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
