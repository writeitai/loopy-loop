from __future__ import annotations

from pathlib import Path

from loopy_loop.config import run_preflight


def test_podcast_creator_example_preflight() -> None:
    repo_root = Path(__file__).resolve().parents[2] / "examples" / "podcast_creator"

    preflight = run_preflight(repo_root=repo_root)

    assert [workflow.id for workflow in preflight.workflows] == [
        "eval_reviewer",
        "eval_runner",
        "inner",
        "outer",
    ]
    assert preflight.root_config.team_harness_provider == "codex"
