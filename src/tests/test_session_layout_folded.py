from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from click.testing import CliRunner

from loopy_loop.cli import main
from loopy_loop.sessions import child_sessions_dir_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import git_receipt_filename
from loopy_loop.sessions import git_receipt_path
from loopy_loop.sessions import git_receipt_ref
from loopy_loop.sessions import raw_attempt_dir_path
from loopy_loop.sessions import raw_dir_path
from loopy_loop.sessions import receipts_dir_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import session_gitignore_path
from loopy_loop.sessions import session_layout
from loopy_loop.sessions import SESSION_LAYOUT_FOLDED
from loopy_loop.sessions import SESSION_LAYOUT_MIRROR
from loopy_loop.sessions import trace_seals_dir_path
from loopy_loop.sessions import traces_root_path
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import TRACE_MANIFEST_FILENAME


def _folded_root(*, repo_root: Path, goal: str = "Ship a landing page") -> str:
    session_id = create_session_id(
        repo_root=repo_root, goal=goal, parent_session_id=None, request_id=None
    )
    create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="71393ee22450",
        goal=goal,
        workflow_set="inner_outer_eval",
        root_session_id=session_id,
        depth=0,
        workflow_contract={"schema_version": 1, "session_protocol_version": 3},
        session_protocol_version=3,
        schema_version=2,
        layout=SESSION_LAYOUT_FOLDED,
    )
    return session_id


def test_child_id_derives_from_request_id(repo_root: Any) -> None:
    parent = _folded_root(repo_root=repo_root)
    child_id = create_session_id(
        repo_root=repo_root,
        goal="Some child goal text",
        parent_session_id=parent,
        request_id="phase-0-foundations",
    )

    assert child_id == "01_phase-0-foundations"


def test_child_ordinal_increments_within_parent(repo_root: Any) -> None:
    parent = _folded_root(repo_root=repo_root)
    children_root = child_sessions_dir_path(repo_root=repo_root, session_id=parent)
    children_root.mkdir(parents=True, exist_ok=True)
    (children_root / "01_first").mkdir()

    child_id = create_session_id(
        repo_root=repo_root,
        goal="child goal",
        parent_session_id=parent,
        request_id="second-milestone",
    )

    assert child_id == "02_second-milestone"


def test_folded_session_tree_shape(repo_root: Any) -> None:
    session_id = _folded_root(repo_root=repo_root)
    session_dir = session_dir_path(repo_root=repo_root, session_id=session_id)

    assert session_layout(repo_root=repo_root, session_id=session_id) == (
        SESSION_LAYOUT_FOLDED
    )
    entries = set(os.listdir(session_dir))
    assert "raw" in entries
    assert "receipts" in entries
    assert ".gitignore" in entries
    # Retired mirror-only machinery is absent.
    assert "trace_seals" not in entries
    assert "git_receipts" not in entries
    assert "delivery_receipts" not in entries
    assert "harness_outputs" not in entries
    # The parallel top-level traces mirror is not created for folded sessions.
    assert not traces_root_path(repo_root=repo_root).exists()
    assert (
        session_gitignore_path(repo_root=repo_root, session_id=session_id)
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()[-1]
        == "raw/"
    )


def test_folded_session_records_layout_field(repo_root: Any) -> None:
    session_id = _folded_root(repo_root=repo_root)
    manifest = json.loads(
        (
            session_dir_path(repo_root=repo_root, session_id=session_id)
            / "session.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["layout"] == SESSION_LAYOUT_FOLDED
    assert re.fullmatch(r"[0-9a-f]{32}", manifest["session_uuid"])


def test_receipt_dirs_route_by_layout(repo_root: Any) -> None:
    folded = _folded_root(repo_root=repo_root)
    from loopy_loop.sessions import delivery_receipts_dir_path
    from loopy_loop.sessions import git_receipts_dir_path

    assert git_receipts_dir_path(
        repo_root=repo_root, session_id=folded
    ) == receipts_dir_path(repo_root=repo_root, session_id=folded)
    assert delivery_receipts_dir_path(
        repo_root=repo_root, session_id=folded
    ) == receipts_dir_path(repo_root=repo_root, session_id=folded)


def test_git_receipt_names_are_self_describing_when_folded() -> None:
    folded_name = git_receipt_filename(
        iteration=26,
        workflow_id="outer",
        attempt_id="97521a5ed6b7",
        phase="after",
        layout=SESSION_LAYOUT_FOLDED,
    )
    mirror_name = git_receipt_filename(
        iteration=26,
        workflow_id="outer",
        attempt_id="97521a5ed6b7",
        phase="after",
        layout=SESSION_LAYOUT_MIRROR,
    )

    assert folded_name == "0026_outer_git_after.json"
    assert mirror_name == "git-after-97521a5ed6b7.json"
    assert (
        git_receipt_ref(
            session_id=None,
            iteration=26,
            workflow_id="outer",
            attempt_id="97521a5ed6b7",
            phase="after",
            layout=SESSION_LAYOUT_FOLDED,
        )
        == "session:/receipts/0026_outer_git_after.json"
    )


def test_git_receipt_path_lands_in_receipts_when_folded(repo_root: Any) -> None:
    session_id = _folded_root(repo_root=repo_root)
    path = git_receipt_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=26,
        workflow_id="outer",
        attempt_id="97521a5ed6b7",
        phase="after",
    )

    assert path.parent == receipts_dir_path(repo_root=repo_root, session_id=session_id)
    assert path.name == "0026_outer_git_after.json"


def test_create_attempt_trace_folds_into_raw_without_manifest(repo_root: Any) -> None:
    session_id = _folded_root(repo_root=repo_root)
    trace_root, manifest = create_attempt_trace(
        repo_root=repo_root,
        root_session_id=session_id,
        session_id=session_id,
        request_id=None,
        work_item_id=None,
        workflow_set="inner_outer_eval",
        workflow_id="outer",
        iteration=26,
        attempt_id="97521a5ed6b7",
        layout=SESSION_LAYOUT_FOLDED,
    )

    assert trace_root == raw_attempt_dir_path(
        repo_root=repo_root, session_id=session_id, iteration=26, workflow_id="outer"
    )
    assert manifest == {}
    assert not (trace_root / TRACE_MANIFEST_FILENAME).exists()
    for subarea in ("protocol", "harness", "eval", "git", "service"):
        assert (trace_root / subarea).is_dir()
    # The iteration's trace ref is a plain session-relative path into raw/.
    ref = json.loads(
        (
            session_dir_path(repo_root=repo_root, session_id=session_id)
            / "iterations"
            / "0026_outer"
            / "trace_ref.json"
        ).read_text(encoding="utf-8")
    )
    assert ref["raw_dir"] == "raw/0026_outer"
    assert ref["raw_dir_ref"] == "session:/raw/0026_outer"


def test_legacy_timestamp_session_reads_as_mirror(repo_root: Any) -> None:
    """A pre-folded timestamp-style session keeps loading as mirror."""

    legacy_id = "20260419_143022_71393ee22450_ab12cd34"
    create_session_dir(
        repo_root=repo_root,
        session_id=legacy_id,
        goal_hash="71393ee22450",
        goal="Legacy goal",
        workflow_set="inner_outer_eval",
        root_session_id=legacy_id,
        depth=0,
        workflow_contract={"schema_version": 1, "session_protocol_version": 2},
        session_protocol_version=2,
        schema_version=2,
    )

    assert session_layout(repo_root=repo_root, session_id=legacy_id) == (
        SESSION_LAYOUT_MIRROR
    )
    entries = set(
        os.listdir(session_dir_path(repo_root=repo_root, session_id=legacy_id))
    )
    assert "git_receipts" in entries
    assert "trace_seals" in entries
    assert "raw" not in entries


def test_prune_raw_removes_raw_but_keeps_durable(repo_root: Any) -> None:
    session_id = _folded_root(repo_root=repo_root)
    raw_attempt = raw_attempt_dir_path(
        repo_root=repo_root, session_id=session_id, iteration=26, workflow_id="outer"
    )
    (raw_attempt / "harness").mkdir(parents=True)
    (raw_attempt / "harness" / "run.json").write_text("{}", encoding="utf-8")
    receipt = (
        receipts_dir_path(repo_root=repo_root, session_id=session_id)
        / "0026_outer_git_after.json"
    )
    receipt.write_text("{}", encoding="utf-8")

    runner = CliRunner()
    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        result = runner.invoke(main, ["prune-raw"], catch_exceptions=False)
    finally:
        os.chdir(cwd)

    assert result.exit_code == 0
    assert not raw_attempt.exists()
    assert raw_dir_path(repo_root=repo_root, session_id=session_id).is_dir()
    assert receipt.is_file()


def test_prune_raw_never_touches_durable_or_mirror_by_default(repo_root: Any) -> None:
    """Without --legacy-traces, a legacy traces tree is left intact."""

    _folded_root(repo_root=repo_root)
    legacy_trace = (
        traces_root_path(repo_root=repo_root)
        / "root"
        / "sessions"
        / "root"
        / "attempts"
        / "abc"
    )
    legacy_trace.mkdir(parents=True)
    (legacy_trace / "run.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        default = runner.invoke(main, ["prune-raw"], catch_exceptions=False)
        assert legacy_trace.is_file() or (legacy_trace / "run.json").is_file()
        legacy = runner.invoke(
            main, ["prune-raw", "--legacy-traces"], catch_exceptions=False
        )
    finally:
        os.chdir(cwd)

    assert default.exit_code == 0
    assert legacy.exit_code == 0
    assert not legacy_trace.exists()


def test_trace_seals_dir_helper_still_addressable_for_mirror(repo_root: Any) -> None:
    """Mirror path helpers remain available for legacy sessions."""

    legacy_id = "20260419_143022_71393ee22450_ab12cd34"
    create_session_dir(
        repo_root=repo_root,
        session_id=legacy_id,
        goal_hash="71393ee22450",
        goal="Legacy goal",
        workflow_set="main",
        root_session_id=legacy_id,
        depth=0,
        workflow_contract={"schema_version": 1, "session_protocol_version": 2},
        session_protocol_version=2,
        schema_version=2,
    )

    assert trace_seals_dir_path(repo_root=repo_root, session_id=legacy_id).is_dir()
