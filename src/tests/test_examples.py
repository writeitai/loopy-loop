from __future__ import annotations

from pathlib import Path

from loopy_loop.config import run_preflight


def test_inner_outer_eval_template_preflight() -> None:
    repo_root = (
        Path(__file__).resolve().parents[1]
        / "loopy_loop"
        / "templates"
        / "inner_outer_eval"
    )

    preflight = run_preflight(repo_root=repo_root)

    assert [workflow.id for workflow in preflight.workflows] == [
        "eval_reviewer",
        "eval_runner",
        "inner",
        "outer",
    ]
    assert preflight.root_config.team_harness_provider == "codex"


def test_pm_planner_dispatcher_template_preflight() -> None:
    repo_root = (
        Path(__file__).resolve().parents[1]
        / "loopy_loop"
        / "templates"
        / "pm_planner_dispatcher"
    )

    preflight = run_preflight(repo_root=repo_root)

    assert [workflow.id for workflow in preflight.workflows] == [
        "dispatcher",
        "planner",
    ]
    assert preflight.workflow_set == "pm_planner_dispatcher"
    assert preflight.root_config.team_harness_provider == "codex"
