from __future__ import annotations

import json
import re
from typing import Any

from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import ensure_iteration_dir
from loopy_loop.sessions import finished_path
from loopy_loop.sessions import harness_outputs_dir_path
from loopy_loop.sessions import iteration_harness_output_root
from loopy_loop.sessions import updates_from_user_path


def test_session_id_format() -> None:
    session_id = create_session_id(goal_hash="71393ee22450")

    assert re.fullmatch(r"71393ee22450_\d{8}_\d{6}_[a-f0-9]{8}", session_id)


def test_create_session_and_iteration_dirs(repo_root: Any) -> None:
    session_dir = create_session_dir(
        repo_root=repo_root,
        session_id="71393ee22450_20260419_143022_ab12cd34",
        goal_hash="71393ee22450",
    )
    iteration_dir = ensure_iteration_dir(
        repo_root=repo_root,
        session_id="71393ee22450_20260419_143022_ab12cd34",
        iteration=1,
        workflow_id="planner",
    )

    metadata = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))

    assert metadata["session_id"] == "71393ee22450_20260419_143022_ab12cd34"
    assert metadata["goal_hash"] == "71393ee22450"
    assert (session_dir / "events.jsonl").exists()
    assert updates_from_user_path(
        repo_root=repo_root, session_id="71393ee22450_20260419_143022_ab12cd34"
    ).exists()
    assert (
        finished_path(
            repo_root=repo_root, session_id="71393ee22450_20260419_143022_ab12cd34"
        ).read_text(encoding="utf-8")
        == "# Finished Work\n"
    )
    assert harness_outputs_dir_path(
        repo_root=repo_root, session_id="71393ee22450_20260419_143022_ab12cd34"
    ).is_dir()
    assert iteration_dir.name == "0001_planner"


def test_create_session_preserves_user_updates_and_finished_ledger(
    repo_root: Any,
) -> None:
    session_id = "71393ee22450_20260419_143022_ab12cd34"
    create_session_dir(
        repo_root=repo_root, session_id=session_id, goal_hash="71393ee22450"
    )
    updates_path = updates_from_user_path(repo_root=repo_root, session_id=session_id)
    ledger_path = finished_path(repo_root=repo_root, session_id=session_id)
    updates_path.write_text("Please prioritize evals.\n", encoding="utf-8")
    ledger_path.write_text("# Finished Work\n\nExisting entry\n", encoding="utf-8")

    create_session_dir(
        repo_root=repo_root, session_id=session_id, goal_hash="71393ee22450"
    )

    assert updates_path.read_text(encoding="utf-8") == "Please prioritize evals.\n"
    assert (
        ledger_path.read_text(encoding="utf-8") == "# Finished Work\n\nExisting entry\n"
    )


def test_iteration_harness_output_root_uses_iteration_name(repo_root: Any) -> None:
    output_root = iteration_harness_output_root(
        repo_root=repo_root,
        session_id="71393ee22450_20260419_143022_ab12cd34",
        iteration=12,
        workflow_id="outer",
    )

    assert output_root.name == "0012_outer"
    assert output_root.parent.name == "harness_outputs"
