from __future__ import annotations

from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "loopy-loop" / "SKILL.md"


def test_agent_skill_describes_the_current_protocol_v3_contract() -> None:
    """The installable skill must teach stock v3 ownership and state clearly."""

    skill = SKILL_PATH.read_text(encoding="utf-8")

    required_current_contract = (
        "assignment.json",
        "agent_assignment.json",
        "layer_plan",
        "layer_tasks",
        "layer_current_state",
        "layer_handoff",
        "workflow_roster.json",
        "scheduler_view.json",
        "harness_capability_roster.json",
        "frontier",
        "strong",
        "standard",
        "economy",
        "inputs/user_updates.jsonl",
        "The PM layer has only two scheduled roles",
        "child_outcomes/",
        "session_outcome.json",
        ".loopy_loop/traces/",
        '"schema_version": 3',
        '"control_id"',
        '"producer"',
        '"eval_receipt_refs"',
        "Evaluation roles provide optional",
        "different enabled harness families",
        "tree-wide stop",
        "The same edge can recurse to any depth",
    )
    retired_false_claims = (
        "Every fresh packaged workflow uses session protocol v2",
        '"schema_version": 2',
        "eval_runner (run checks, write `goal_check.json`)",
        "eval_runner` runs the complete current-layer inventory, writes the canonical",
        "and alone may request",
        "Missing, malformed, or mismatched output fails the iteration",
        "goal_check_broken",
        "eval_readiness/",
        "parent-layer `eval_reviewer`/`eval_runner` roles",
        "one level only in v1",
        "grandchildren waits forever",
        "Outer workflows should read `updates_from_user.md` every run",
        "`stop` still operates on the latest",
    )

    for marker in required_current_contract:
        assert marker in skill
    for marker in retired_false_claims:
        assert marker not in skill
