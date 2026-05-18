from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any
from typing import TypeVar

from fastapi import FastAPI
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
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import STOP_ACTION
from loopy_loop.models import TaskResponse
from loopy_loop.models import utc_now
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
from loopy_loop.state_store import StateStore

logger = logging.getLogger(__name__)


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
        self.preflights: dict[str, PreflightResult] = {
            preflight.workflow_set: preflight
        }
        self.state_store = state_store
        self._prepare_state(resume=resume)

    def register_worker(self) -> TaskResponse:
        recovered_pending_paths: list[Path] = []

        def mutator(state: LoopState | None) -> tuple[LoopState, TaskResponse]:
            current = _require_state(state=state)
            now = utc_now()

            # Step 3: If current_task is set (crash recovery from previous worker crash),
            # recover a locally completed result before calling it abandoned.
            # Important: this increment can itself trigger max_turns — that is correct
            # behaviour. The stop check (step 4) will catch it immediately after.
            if current.current_task is not None:
                orphaned = current.current_task
                recovered = self._read_recoverable_finished_request(
                    current_task=orphaned
                )
                if recovered is None:
                    current.history.append(
                        HistoryEntry(
                            iteration=orphaned.iteration,
                            workflow_set=orphaned.workflow_set,
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
                else:
                    recovered_request, pending_path = recovered
                    if pending_path is not None:
                        recovered_pending_paths.append(pending_path)
                    self._record_finished_task(
                        state=current,
                        active=orphaned,
                        request=recovered_request,
                        now=now,
                    )

            # Step 4: Check stop conditions after abandoned-task cleanup.
            stop_response = self._stop_response_if_needed(state=current)
            if stop_response is not None:
                return current, stop_response

            child_response = self._dispatch_child_session_after_success(state=current)
            if child_response is not None:
                return current, child_response

            # Step 5: Choose next workflow.
            workflows = self._workflows_for(workflow_set=current.workflow_set)
            workflow = choose_next_workflow(
                workflows=workflows,
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
                workflow_set=current.workflow_set,
                workflow_id=workflow.id,
                session_id=current.active_session_id,
                iteration=current.iteration_count + 1,
                started_at=now,
            )
            return current, _build_run_response(
                current_task=current.current_task,
                config_snapshot=current.config_snapshot,
            )

        response = self.state_store.mutate(mutator)
        for path in recovered_pending_paths:
            path.unlink(missing_ok=True)
        return response

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
                child_response = self._dispatch_child_session_after_success(
                    state=current
                )
                if child_response is not None:
                    return current, child_response
                workflows = self._workflows_for(workflow_set=current.workflow_set)
                workflow = choose_next_workflow(
                    workflows=workflows,
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
                    workflow_set=current.workflow_set,
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
                or request.iteration != active.iteration
            ):
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

            # Step 7: Check stop conditions.
            stop_response = self._stop_response_if_needed(state=current)
            if stop_response is not None:
                return current, stop_response

            # Step 8: Dispatch next workflow.
            child_response = self._dispatch_child_session_after_success(state=current)
            if child_response is not None:
                return current, child_response

            workflows = self._workflows_for(workflow_set=current.workflow_set)
            workflow = choose_next_workflow(
                workflows=workflows,
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
                workflow_set=current.workflow_set,
                workflow_id=workflow.id,
                session_id=current.active_session_id,
                iteration=current.iteration_count + 1,
                started_at=now,
            )
            return current, _build_run_response(
                current_task=current.current_task,
                config_snapshot=current.config_snapshot,
            )

        response = self.state_store.mutate(mutator)
        if response.action == STOP_ACTION:
            parent_response = self._resume_parent_if_active_child_completed()
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
        create_session_dir(
            repo_root=self.repo_root,
            session_id=existing_state.active_session_id,
            goal_hash=existing_state.goal_hash,
            goal=existing_state.config_snapshot.goal,
            workflow_set=existing_state.workflow_set,
            parent_session_id=existing_state.parent_session_id,
        )

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
            self.preflight.root_config.model_dump()
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
        self, *, state: LoopState
    ) -> TaskResponse | None:
        if state.parent_session_id is not None:
            return None
        requests_dir = child_requests_dir_path(
            repo_root=self.repo_root, session_id=state.active_session_id
        )
        if not requests_dir.exists():
            return None
        for request_path in sorted(requests_dir.glob("*.json")):
            request = _read_signal(path=request_path, model=ChildSessionRequest)
            if request is None:
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
                preflight.root_config.model_dump()
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
                ),
            )
            request_path.unlink(missing_ok=True)
            self.state_store = child_store
            return _build_run_response(
                current_task=child_task, config_snapshot=child_state.config_snapshot
            )
        return None

    def _dispatch_child_session_after_success(
        self, *, state: LoopState
    ) -> TaskResponse | None:
        if not state.history or not state.history[-1].success:
            return None
        return self._dispatch_child_session_if_requested(state=state)

    def _resume_parent_if_active_child_completed(self) -> TaskResponse | None:
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
        self.state_store = parent_store
        return self.register_worker()

    def _append_child_record(
        self, *, parent_session_id: str, record: ChildSessionRecord
    ) -> None:
        path = children_path(repo_root=self.repo_root, session_id=parent_session_id)
        payload = _read_children_payload(path=path)
        payload["children"].append(json.loads(record.model_dump_json()))
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

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
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

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


def _build_run_response(
    *, current_task: CurrentTask, config_snapshot: RootConfigSnapshot
) -> TaskResponse:
    return TaskResponse(
        action="run",
        workflow_set=current_task.workflow_set,
        workflow_id=current_task.workflow_id,
        session_id=current_task.session_id,
        iteration=current_task.iteration,
        config_snapshot=config_snapshot,
    )


def _require_state(*, state: LoopState | None) -> LoopState:
    if state is None:
        raise RuntimeError("Coordinator state is not initialized")
    return state


def _matches_current_task(
    *, request: FinishedRequest, current_task: CurrentTask
) -> bool:
    return (
        request.session_id == current_task.session_id
        and request.workflow_id == current_task.workflow_id
        and request.iteration == current_task.iteration
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
