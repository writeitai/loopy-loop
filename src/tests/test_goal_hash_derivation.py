from __future__ import annotations

from loopy_loop.config import derive_goal_hash


def test_goal_hash_is_derived_from_goal() -> None:
    assert (
        derive_goal_hash(goal="Ship a minimal working landing page") == "71393ee22450"
    )


def test_goal_hash_changes_when_goal_changes() -> None:
    assert derive_goal_hash(goal="Goal A") != derive_goal_hash(goal="Goal B")
