from __future__ import annotations

from loopy_loop.config import WorkflowDefinition
from loopy_loop.models import HistoryEntry


def choose_next_workflow(
    *,
    workflows: list[WorkflowDefinition],
    history: list[HistoryEntry],
    iteration_count: int,
) -> WorkflowDefinition | None:
    eligible: list[tuple[int, int, WorkflowDefinition]] = []
    last_successful_workflow_id = _last_successful_workflow_id(history=history)
    has_successful_history = any(entry.success for entry in history)
    has_successful_non_goal_check = any(
        entry.success and entry.workflow_id != "goal_check" for entry in history
    )

    for workflow in workflows:
        if not workflow.enabled:
            continue
        if iteration_count < workflow.not_before_iteration:
            continue
        if workflow.id == "goal_check" and not has_successful_non_goal_check:
            continue
        if (
            workflow.must_follow is not None
            and workflow.must_follow != last_successful_workflow_id
        ):
            continue
        is_run_on_start = workflow.run_on_start and not has_successful_history
        if (
            workflow.run_after_successes is not None
            and not is_run_on_start
            and not _run_after_successes_satisfied(workflow=workflow, history=history)
        ):
            continue
        if not _run_every_satisfied(
            workflow_id=workflow.id,
            history=history,
            iteration_count=iteration_count,
            run_every=workflow.run_every,
        ):
            continue
        eligible.append(
            (
                workflow.priority,
                _workflow_score(
                    workflow_id=workflow.id,
                    history=history,
                    iteration_count=iteration_count,
                    run_every=workflow.run_every,
                ),
                workflow,
            )
        )

    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1], item[2].id))[2]


def _last_successful_workflow_id(*, history: list[HistoryEntry]) -> str | None:
    for entry in reversed(history):
        if entry.success:
            return entry.workflow_id
    return None


def _workflow_score(
    *,
    workflow_id: str,
    history: list[HistoryEntry],
    iteration_count: int,
    run_every: int,
) -> int:
    last_entry = _last_entry_for_workflow(workflow_id=workflow_id, history=history)
    if last_entry is None:
        return 10**9
    return (
        _steps_since_last_run(
            workflow_id=workflow_id, history=history, iteration_count=iteration_count
        )
        - run_every
    )


def _run_every_satisfied(
    *,
    workflow_id: str,
    history: list[HistoryEntry],
    iteration_count: int,
    run_every: int,
) -> bool:
    last_entry = _last_entry_for_workflow(workflow_id=workflow_id, history=history)
    if last_entry is None:
        return True
    return (
        _steps_since_last_run(
            workflow_id=workflow_id, history=history, iteration_count=iteration_count
        )
        >= run_every
    )


def _run_after_successes_satisfied(
    *, workflow: WorkflowDefinition, history: list[HistoryEntry]
) -> bool:
    rule = workflow.run_after_successes
    if rule is None:
        return True
    target_success_count = _successful_workflow_count(
        workflow_id=rule.workflow_id, history=history
    )
    previous_target_success_count = _successful_target_count_at_last_candidate_success(
        candidate_workflow_id=workflow.id,
        target_workflow_id=rule.workflow_id,
        history=history,
    )
    return target_success_count - previous_target_success_count >= rule.every


def _successful_workflow_count(*, workflow_id: str, history: list[HistoryEntry]) -> int:
    return sum(
        1 for entry in history if entry.success and entry.workflow_id == workflow_id
    )


def _successful_target_count_at_last_candidate_success(
    *, candidate_workflow_id: str, target_workflow_id: str, history: list[HistoryEntry]
) -> int:
    last_candidate_index: int | None = None
    for index, entry in enumerate(history):
        if entry.success and entry.workflow_id == candidate_workflow_id:
            last_candidate_index = index
    if last_candidate_index is None:
        return 0
    return sum(
        1
        for entry in history[: last_candidate_index + 1]
        if entry.success and entry.workflow_id == target_workflow_id
    )


def _steps_since_last_run(
    *, workflow_id: str, history: list[HistoryEntry], iteration_count: int
) -> int:
    last_entry = _last_entry_for_workflow(workflow_id=workflow_id, history=history)
    if last_entry is None:
        return iteration_count
    return iteration_count - last_entry.iteration


def _last_entry_for_workflow(
    *, workflow_id: str, history: list[HistoryEntry]
) -> HistoryEntry | None:
    for entry in reversed(history):
        if entry.workflow_id == workflow_id:
            return entry
    return None
