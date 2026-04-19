from __future__ import annotations

from typing import Any

from loopy_loop.config import load_workflow_definitions
from loopy_loop.scheduler import choose_next_workflow


def test_iteration_one_never_picks_goal_check(repo_builder: Any) -> None:
    repo_root = repo_builder()
    workflows = load_workflow_definitions(repo_root=repo_root)

    chosen = choose_next_workflow(workflows=workflows, history=[], iteration_count=0)

    assert chosen is not None
    assert chosen.id == "planner"


def test_run_every_and_not_before_iteration_are_enforced(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "run_every": 2,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "implement": {
                "prompt": "Implement",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 2,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root)
    history = [history_entry_factory(iteration=1, workflow_id="planner")]

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=1
    )

    assert chosen is None


def test_no_eligible_workflow_returns_none(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "goal_check": {
                "prompt": "Check",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 1,
                    "description": "",
                },
            }
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root)

    chosen = choose_next_workflow(
        workflows=workflows,
        history=[history_entry_factory(workflow_id="goal_check")],
        iteration_count=0,
    )

    assert chosen is None
