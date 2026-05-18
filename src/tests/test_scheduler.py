from __future__ import annotations

from typing import Any

from loopy_loop.config import load_workflow_definitions
from loopy_loop.scheduler import choose_next_workflow


def test_iteration_one_never_picks_goal_check(repo_builder: Any) -> None:
    repo_root = repo_builder()
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")

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
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    history = [history_entry_factory(iteration=1, workflow_id="planner")]

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=1
    )

    assert chosen is None


def test_failed_outer_retries_when_inner_is_blocked(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "outer": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "priority": 10,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "inner": {
                "prompt": "Implement",
                "config": {
                    "enabled": True,
                    "priority": 20,
                    "run_every": 1,
                    "must_follow": "outer",
                    "not_before_iteration": 1,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    history = [
        history_entry_factory(iteration=37, workflow_id="inner", success=True),
        history_entry_factory(iteration=38, workflow_id="outer", success=False),
    ]

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=38
    )

    assert chosen is not None
    assert chosen.id == "outer"


def test_failed_retry_does_not_preempt_normally_eligible_workflow(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "outer": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "priority": 10,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "reviewer": {
                "prompt": "Review",
                "config": {
                    "enabled": True,
                    "priority": 20,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    history = [history_entry_factory(iteration=2, workflow_id="outer", success=False)]

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=2
    )

    assert chosen is not None
    assert chosen.id == "reviewer"


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
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")

    chosen = choose_next_workflow(
        workflows=workflows,
        history=[history_entry_factory(workflow_id="goal_check")],
        iteration_count=0,
    )

    assert chosen is None


def test_scheduler_skips_disabled_and_requires_most_recent_successful_predecessor(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "run_every": 10,
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
            "reviewer": {
                "prompt": "Review",
                "config": {
                    "enabled": True,
                    "run_every": 10,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "disabled": {
                "prompt": "Skip me",
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
    history = [
        history_entry_factory(iteration=1, workflow_id="planner", success=True),
        history_entry_factory(
            assignment_id="assignment-2",
            iteration=2,
            workflow_id="reviewer",
            success=True,
        ),
    ]

    locked = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=2
    )
    unlocked = choose_next_workflow(
        workflows=workflows,
        history=history
        + [
            history_entry_factory(
                assignment_id="assignment-3",
                iteration=3,
                workflow_id="planner",
                success=True,
            )
        ],
        iteration_count=3,
    )

    assert locked is None
    assert unlocked is not None
    assert unlocked.id == "implement"


def test_run_on_start_priority_wins_first_iteration(repo_builder: Any) -> None:
    repo_root = repo_builder(
        workflows={
            "eval_reviewer": {
                "prompt": "Review evals",
                "config": {
                    "enabled": True,
                    "priority": 100,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "run_on_start": True,
                    "run_after_successes": {"workflow_id": "inner", "every": 10},
                    "description": "",
                },
            },
            "outer": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "priority": 10,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "inner": {
                "prompt": "Implement",
                "config": {
                    "enabled": True,
                    "priority": 20,
                    "run_every": 1,
                    "must_follow": "outer",
                    "not_before_iteration": 1,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")

    chosen = choose_next_workflow(workflows=workflows, history=[], iteration_count=0)

    assert chosen is not None
    assert chosen.id == "eval_reviewer"


def test_run_after_successes_waits_for_target_success_count(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "eval_reviewer": {
                "prompt": "Review evals",
                "config": {
                    "enabled": True,
                    "priority": 100,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "run_after_successes": {"workflow_id": "inner", "every": 10},
                    "description": "",
                },
            },
            "outer": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "priority": 10,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
            "inner": {
                "prompt": "Implement",
                "config": {
                    "enabled": True,
                    "priority": 20,
                    "run_every": 1,
                    "must_follow": "outer",
                    "not_before_iteration": 1,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    history = [history_entry_factory(iteration=1, workflow_id="eval_reviewer")]
    for index in range(9):
        history.extend(
            [
                history_entry_factory(iteration=2 + index * 2, workflow_id="outer"),
                history_entry_factory(iteration=3 + index * 2, workflow_id="inner"),
            ]
        )

    before_tenth = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=19
    )
    history.extend(
        [
            history_entry_factory(iteration=20, workflow_id="outer"),
            history_entry_factory(iteration=21, workflow_id="inner"),
        ]
    )
    after_tenth = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=21
    )

    assert before_tenth is not None
    assert before_tenth.id == "outer"
    assert after_tenth is not None
    assert after_tenth.id == "eval_reviewer"


def test_run_after_successes_does_not_repeat_same_bucket(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "eval_reviewer": {
                "prompt": "Review evals",
                "config": {
                    "enabled": True,
                    "priority": 100,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "run_after_successes": {"workflow_id": "inner", "every": 10},
                    "description": "",
                },
            },
            "outer": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "priority": 10,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    history = [
        history_entry_factory(iteration=iteration, workflow_id="inner")
        for iteration in range(1, 11)
    ]
    history.append(history_entry_factory(iteration=11, workflow_id="eval_reviewer"))

    chosen = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=11
    )

    assert chosen is not None
    assert chosen.id == "outer"


def test_eval_runner_waits_for_eval_reviewer_predecessor(
    repo_builder: Any, history_entry_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "eval_reviewer": {
                "prompt": "Review evals",
                "config": {
                    "enabled": True,
                    "priority": 100,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "run_after_successes": {"workflow_id": "inner", "every": 10},
                    "description": "",
                },
            },
            "eval_runner": {
                "prompt": "Run evals",
                "config": {
                    "enabled": True,
                    "priority": 90,
                    "run_every": 1,
                    "must_follow": "eval_reviewer",
                    "not_before_iteration": 0,
                    "run_after_successes": {"workflow_id": "inner", "every": 10},
                    "emits_goal_check": True,
                    "description": "",
                },
            },
            "outer": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "priority": 10,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    history = [
        history_entry_factory(iteration=iteration, workflow_id="inner")
        for iteration in range(1, 11)
    ]

    before_reviewer = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=10
    )
    history.append(history_entry_factory(iteration=11, workflow_id="eval_reviewer"))
    after_reviewer = choose_next_workflow(
        workflows=workflows, history=history, iteration_count=11
    )

    assert before_reviewer is not None
    assert before_reviewer.id == "eval_reviewer"
    assert after_reviewer is not None
    assert after_reviewer.id == "eval_runner"
