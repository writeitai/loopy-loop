from __future__ import annotations

from typing import Any

from loopy_loop.config import load_workflow_definitions
from loopy_loop.scheduler import choose_next_workflow


def test_must_follow_uses_last_successful_workflow(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "run_every": 1,
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
                    "must_follow": "planner",
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root)
    history = [history_entry_factory(workflow_id="planner", success=True)]

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=1
    )

    assert chosen is not None
    assert chosen.id == "implement"


def test_failed_predecessor_retries_instead_of_unlocking_dependent(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "run_every": 1,
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
                    "must_follow": "planner",
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root)
    history = [history_entry_factory(workflow_id="planner", success=False)]

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=1
    )

    assert chosen is not None
    assert chosen.id == "planner"
