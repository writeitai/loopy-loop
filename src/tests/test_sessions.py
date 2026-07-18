from __future__ import annotations

import json
import re
from typing import Any

from loopy_loop.sessions import control_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import ensure_iteration_dir
from loopy_loop.sessions import finished_path
from loopy_loop.sessions import harness_outputs_dir_path
from loopy_loop.sessions import iteration_harness_output_root
from loopy_loop.sessions import parent_path
from loopy_loop.sessions import session_goal_path
from loopy_loop.sessions import updates_from_user_path


def test_root_session_id_is_ordinal_and_slug(repo_root: Any) -> None:
    session_id = create_session_id(
        repo_root=repo_root,
        goal="Ship the ultimate memory program to disk",
        parent_session_id=None,
        request_id=None,
    )

    assert session_id == "001_ship-ultimate-memory-program-disk"


def test_root_session_ordinal_increments_and_ignores_legacy(repo_root: Any) -> None:
    from loopy_loop.sessions import sessions_root_path

    root = sessions_root_path(repo_root=repo_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "001_first-goal").mkdir()
    (root / "20260419_143022_71393ee22450_ab12cd34").mkdir()  # legacy sibling
    session_id = create_session_id(
        repo_root=repo_root,
        goal="A second goal entirely",
        parent_session_id=None,
        request_id=None,
    )

    assert session_id == "002_second-goal-entirely"


def test_empty_goal_slug_falls_back(repo_root: Any) -> None:
    session_id = create_session_id(
        repo_root=repo_root, goal="the a of to", parent_session_id=None, request_id=None
    )

    assert re.fullmatch(r"001_[a-z0-9-]+", session_id)


def test_create_session_and_iteration_dirs(repo_root: Any) -> None:
    session_dir = create_session_dir(
        repo_root=repo_root,
        session_id="20260419_143022_71393ee22450_ab12cd34",
        goal_hash="71393ee22450",
        goal="Ship it",
        workflow_set="main",
    )
    iteration_dir = ensure_iteration_dir(
        repo_root=repo_root,
        session_id="20260419_143022_71393ee22450_ab12cd34",
        iteration=1,
        workflow_id="planner",
    )

    metadata = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))

    assert metadata["session_id"] == "20260419_143022_71393ee22450_ab12cd34"
    assert metadata["goal_hash"] == "71393ee22450"
    assert metadata["workflow_set"] == "main"
    assert json.loads((session_dir / "children.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "children": [],
    }
    assert (
        session_goal_path(
            repo_root=repo_root, session_id="20260419_143022_71393ee22450_ab12cd34"
        ).read_text(encoding="utf-8")
        == "Ship it\n"
    )
    assert (session_dir / "events.jsonl").exists()
    assert json.loads(
        control_path(
            repo_root=repo_root, session_id="20260419_143022_71393ee22450_ab12cd34"
        ).read_text(encoding="utf-8")
    ) == {
        "state": "running",
        "reason": "session active",
        "stop_reason": None,
        "schema_version": 1,
    }
    assert updates_from_user_path(
        repo_root=repo_root, session_id="20260419_143022_71393ee22450_ab12cd34"
    ).exists()
    assert (
        finished_path(
            repo_root=repo_root, session_id="20260419_143022_71393ee22450_ab12cd34"
        ).read_text(encoding="utf-8")
        == "# Finished Work\n"
    )
    assert harness_outputs_dir_path(
        repo_root=repo_root, session_id="20260419_143022_71393ee22450_ab12cd34"
    ).is_dir()
    assert iteration_dir.name == "0001_planner"


def test_create_session_preserves_user_updates_and_finished_ledger(
    repo_root: Any,
) -> None:
    session_id = "20260419_143022_71393ee22450_ab12cd34"
    create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="71393ee22450",
        workflow_set="main",
    )
    updates_path = updates_from_user_path(repo_root=repo_root, session_id=session_id)
    ledger_path = finished_path(repo_root=repo_root, session_id=session_id)
    updates_path.write_text("Please prioritize evals.\n", encoding="utf-8")
    ledger_path.write_text("# Finished Work\n\nExisting entry\n", encoding="utf-8")

    create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="71393ee22450",
        workflow_set="main",
    )

    assert updates_path.read_text(encoding="utf-8") == "Please prioritize evals.\n"
    assert (
        ledger_path.read_text(encoding="utf-8") == "# Finished Work\n\nExisting entry\n"
    )


def test_iteration_harness_output_root_uses_iteration_name(repo_root: Any) -> None:
    output_root = iteration_harness_output_root(
        repo_root=repo_root,
        session_id="20260419_143022_71393ee22450_ab12cd34",
        iteration=12,
        workflow_id="outer",
    )

    assert output_root.name == "0012_outer"
    assert output_root.parent.name == "harness_outputs"


def test_create_child_session_records_parent(repo_root: Any) -> None:
    parent_session_id = "20260419_143022_71393ee22450_ab12cd34"
    child_session_id = "20260419_143123_91aa0ab84591_cd34ef56"
    create_session_dir(
        repo_root=repo_root,
        session_id=parent_session_id,
        goal_hash="71393ee22450",
        workflow_set="main",
    )

    child_dir = create_session_dir(
        repo_root=repo_root,
        session_id=child_session_id,
        goal_hash="91aa0ab84591",
        goal="Child goal",
        workflow_set="inner_outer_eval",
        parent_session_id=parent_session_id,
    )

    assert child_dir.parent.name == "children"
    parent_payload = json.loads(
        parent_path(repo_root=repo_root, session_id=child_session_id).read_text(
            encoding="utf-8"
        )
    )
    assert parent_payload["schema_version"] == 1
    assert parent_payload["parent_session_id"] == parent_session_id
