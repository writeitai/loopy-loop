from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "loopy-loop" / "SKILL.md"


def test_agent_skill_describes_the_current_recursive_v2_contract() -> None:
    """The installable skill must not teach agents the retired v1 contract."""

    skill = SKILL_PATH.read_text(encoding="utf-8")

    required_current_contract = (
        "assignment.json",
        "agent_assignment.json",
        "inputs/user_updates.jsonl",
        "child_requests/pending/",
        ".loopy_loop/traces/",
        '"schema_version": 2',
        '"control_id"',
        '"producer"',
        "tree-wide stop",
        "three-or-more-layer",
    )
    retired_false_claims = (
        "one level only in v1",
        "grandchildren waits forever",
        "Outer workflows should read `updates_from_user.md` every run",
        "stop still operates on the latest **top-level** session state",
    )

    for marker in required_current_contract:
        assert marker in skill
    for marker in retired_false_claims:
        assert marker not in skill
