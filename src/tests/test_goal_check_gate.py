from __future__ import annotations

from typing import Any

from loopy_loop.config import load_workflow_definitions
from loopy_loop.scheduler import choose_next_workflow


def test_goal_check_requires_successful_non_goal_check_history(
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
            },
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": False,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")

    no_unlock = choose_next_workflow(
        workflows=workflows,
        history=[history_entry_factory(workflow_id="goal_check", success=True)],
        iteration_count=1,
    )
    unlocked = choose_next_workflow(
        workflows=workflows,
        history=[history_entry_factory(workflow_id="planner", success=True)],
        iteration_count=1,
    )

    assert no_unlock is None
    assert unlocked is not None
    assert unlocked.id == "goal_check"
