from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from loopy_loop.references import LogicalReferenceError
from loopy_loop.references import LogicalReferenceResolver
from loopy_loop.references import resolve_logical_reference


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _session(
    repo: Path,
    session_id: str,
    *,
    parent: Path | None = None,
    parent_id: str | None = None,
    root_id: str | None = None,
    depth: int = 0,
) -> Path:
    directory = (
        repo / ".loopy_loop" / "sessions" / session_id
        if parent is None
        else parent / "children" / session_id
    )
    _write_json(
        directory / "session.json",
        {
            "schema_version": 2,
            "session_id": session_id,
            "root_session_id": root_id or session_id,
            "parent_session_id": parent_id,
            "depth": depth,
        },
    )
    if parent is not None:
        _write_json(
            directory / "parent.json",
            {
                "schema_version": 1,
                "parent_session_id": parent_id,
                "parent_relative_path": "../..",
            },
        )
    return directory


def _tree(repo: Path) -> tuple[Path, Path, Path, Path]:
    root = _session(repo, "root")
    child = _session(
        repo, "child", parent=root, parent_id="root", root_id="root", depth=1
    )
    grandchild = _session(
        repo, "grandchild", parent=child, parent_id="child", root_id="root", depth=2
    )
    sibling = _session(
        repo, "sibling", parent=root, parent_id="root", root_id="root", depth=1
    )
    return root, child, grandchild, sibling


def test_resolves_all_session_scopes_to_absolute_paths(tmp_path: Path) -> None:
    root, child, grandchild, sibling = _tree(tmp_path)
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="grandchild"
    )

    assert resolver.resolve("repo:/src/new.py") == (tmp_path / "src/new.py").resolve()
    assert (
        resolver.resolve("session:/state.json") == (grandchild / "state.json").resolve()
    )
    assert resolver.resolve("root:/goal.md") == (root / "goal.md").resolve()
    assert resolver.resolve("parent:/goal.md") == (child / "goal.md").resolve()
    assert (
        resolver.resolve("session:sibling:/goal.md") == (sibling / "goal.md").resolve()
    )
    assert resolver.resolve("session:/").is_absolute()

    assert (
        resolve_logical_reference(
            reference="session:root:/children.json",
            repo_root=tmp_path,
            session_id="grandchild",
        )
        == (root / "children.json").resolve()
    )


def test_named_session_is_limited_to_current_validated_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    _session(tmp_path, "other-root")
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="child"
    )

    with pytest.raises(LogicalReferenceError, match="unknown session ID"):
        resolver.resolve("session:other-root:/goal.md")


def test_corrupt_unrelated_session_tree_does_not_break_healthy_references(
    tmp_path: Path,
) -> None:
    root, child, _, _ = _tree(tmp_path)
    corrupt = tmp_path / ".loopy_loop" / "sessions" / "abandoned"
    corrupt.mkdir(parents=True)
    (corrupt / "session.json").write_text("{", encoding="utf-8")

    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="child"
    )

    assert resolver.resolve("root:/goal.md") == (root / "goal.md").resolve()
    assert resolver.resolve("session:/state.json") == (child / "state.json").resolve()


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "repo",
        "Repo:/file",
        "repo:relative",
        "repo://absolute",
        "repo:/../outside",
        "repo:/a/./b",
        "repo:/a//b",
        "repo:/a\\b",
        "unknown:/file",
        "trace::/file",
        "trace:bad$id:/file",
        "session:child:/../root",
        "session:child:/x:/y",
    ],
)
def test_rejects_malformed_or_traversing_references(
    tmp_path: Path, reference: str
) -> None:
    _tree(tmp_path)
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="child"
    )

    with pytest.raises(LogicalReferenceError):
        resolver.resolve(reference)


def test_root_session_has_no_parent_scope(tmp_path: Path) -> None:
    _session(tmp_path, "root")
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="root"
    )

    with pytest.raises(LogicalReferenceError, match="undefined"):
        resolver.resolve("parent:/goal.md")


def test_rejects_symlink_escape_but_allows_internal_symlink(tmp_path: Path) -> None:
    root = _session(tmp_path, "root")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "external").symlink_to(outside, target_is_directory=True)
    safe = root / "safe"
    safe.mkdir()
    (root / "internal").symlink_to(safe, target_is_directory=True)
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="root"
    )

    with pytest.raises(LogicalReferenceError, match="escapes"):
        resolver.resolve("session:/external/secret.txt")
    assert (
        resolver.resolve("session:/internal/result.txt")
        == (safe / "result.txt").resolve()
    )


def test_rejects_symlink_loop(tmp_path: Path) -> None:
    root = _session(tmp_path, "root")
    (root / "loop").symlink_to("loop")
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="root"
    )

    with pytest.raises(LogicalReferenceError, match="cannot resolve"):
        resolver.resolve("session:/loop/result.txt")


def test_rejects_duplicate_session_ids(tmp_path: Path) -> None:
    first = _session(tmp_path, "first")
    second = _session(tmp_path, "second")
    _session(
        tmp_path, "duplicate", parent=first, parent_id="first", root_id="first", depth=1
    )
    _session(
        tmp_path,
        "duplicate",
        parent=second,
        parent_id="second",
        root_id="second",
        depth=1,
    )

    with pytest.raises(LogicalReferenceError, match="duplicate session ID"):
        LogicalReferenceResolver.for_session(repo_root=tmp_path, session_id="first")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("parent_session_id", "wrong", "declares parent"),
        ("root_session_id", "wrong", "declares root"),
        ("depth", 9, "declares depth"),
    ],
)
def test_rejects_manifest_identity_contradictions(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    root = _session(tmp_path, "root")
    child = _session(
        tmp_path, "child", parent=root, parent_id="root", root_id="root", depth=1
    )
    payload = json.loads((child / "session.json").read_text(encoding="utf-8"))
    payload[field] = value
    _write_json(child / "session.json", payload)

    with pytest.raises(LogicalReferenceError, match=message):
        LogicalReferenceResolver.for_session(repo_root=tmp_path, session_id="child")


def test_rejects_symlinked_session_topology(tmp_path: Path) -> None:
    sessions = tmp_path / ".loopy_loop" / "sessions"
    sessions.mkdir(parents=True)
    external = tmp_path / "external"
    _write_json(
        external / "session.json",
        {"schema_version": 1, "session_id": "linked", "parent_session_id": None},
    )
    (sessions / "linked").symlink_to(external, target_is_directory=True)

    with pytest.raises(LogicalReferenceError, match="symlink"):
        LogicalReferenceResolver.for_session(repo_root=tmp_path, session_id="linked")


def test_resolves_only_traces_bound_to_current_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    trace = (
        tmp_path
        / ".loopy_loop"
        / "traces"
        / "root"
        / "sessions"
        / "child"
        / "attempts"
        / "attempt-1"
    )
    _write_json(
        trace / "trace_manifest.json",
        {
            "trace_manifest_id": "trace-1",
            "root_session_id": "root",
            "session_id": "child",
        },
    )
    (trace / "agents").mkdir()
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="child"
    )

    assert (
        resolver.resolve("trace:trace-1:/agents/output.json")
        == (trace / "agents/output.json").resolve()
    )
    with pytest.raises(LogicalReferenceError, match="unknown trace"):
        resolver.resolve("trace:missing:/output.json")


def test_trace_manifest_nested_identity_is_honored(tmp_path: Path) -> None:
    _tree(tmp_path)
    trace = (
        tmp_path
        / ".loopy_loop"
        / "traces"
        / "root"
        / "sessions"
        / "child"
        / "attempts"
        / "attempt-1"
    )
    _write_json(
        trace / "trace_manifest.json",
        {
            "manifest_id": "trace-attempt-1",
            "identity": {"root_session_id": "other-root", "session_id": "child"},
        },
    )
    resolver = LogicalReferenceResolver.for_session(
        repo_root=tmp_path, session_id="child"
    )

    with pytest.raises(LogicalReferenceError, match="unknown trace"):
        resolver.resolve("trace:trace-attempt-1:/output.json")


def test_rejects_duplicate_trace_manifest_ids(tmp_path: Path) -> None:
    _session(tmp_path, "root")
    traces = tmp_path / ".loopy_loop" / "traces" / "root"
    for attempt in ("a", "b"):
        _write_json(
            traces / "sessions/root/attempts" / attempt / "trace_manifest.json",
            {
                "trace_manifest_id": "same-trace",
                "root_session_id": "root",
                "session_id": "root",
            },
        )

    with pytest.raises(LogicalReferenceError, match="duplicate trace"):
        LogicalReferenceResolver.for_session(
            repo_root=tmp_path, session_id="root"
        ).resolve("trace:same-trace:/artifact.json")


def test_supplied_trace_root_must_be_in_current_root_tree(tmp_path: Path) -> None:
    _session(tmp_path, "root")
    foreign = tmp_path / ".loopy_loop" / "traces" / "foreign" / "attempt"
    foreign.mkdir(parents=True)

    with pytest.raises(LogicalReferenceError, match="does not belong"):
        LogicalReferenceResolver.for_session(
            repo_root=tmp_path, session_id="root", trace_roots={"trace": foreign}
        ).resolve("trace:trace:/artifact.json")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_parent_manifest_cannot_escape_sessions_root(tmp_path: Path) -> None:
    root = _session(tmp_path, "root")
    child = _session(
        tmp_path, "child", parent=root, parent_id="root", root_id="root", depth=1
    )
    payload = json.loads((child / "parent.json").read_text(encoding="utf-8"))
    payload["parent_relative_path"] = "../../../../../"
    _write_json(child / "parent.json", payload)

    with pytest.raises(LogicalReferenceError, match="escapes"):
        LogicalReferenceResolver.for_session(repo_root=tmp_path, session_id="child")
