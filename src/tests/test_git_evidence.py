from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest

from loopy_loop.cli import main
from loopy_loop.git_evidence import capture_git_evidence
from loopy_loop.git_evidence import dirty_tree_digest
from loopy_loop.git_evidence import GitEvidenceError
from loopy_loop.git_evidence import sanitized_remote_fingerprints


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "core.filemode", "true")
    (tmp_path / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_receipt_tracks_content_mode_and_deletion(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    clean = capture_git_evidence(repo_root=repo, phase="before", attempt_id="attempt-1")
    assert clean.phase == "before"
    assert clean.attempt_id == "attempt-1"
    assert clean.head == _git(repo, "rev-parse", "HEAD")
    assert clean.branch is not None
    assert clean.detached is False
    assert clean.dirty is False
    assert Path(clean.repository_root) == repo.resolve()

    untracked = repo / "new.txt"
    untracked.write_text("one\n", encoding="utf-8")
    first = dirty_tree_digest(repo_root=repo)
    untracked.write_text("two\n", encoding="utf-8")
    second = dirty_tree_digest(repo_root=repo)
    untracked.chmod(0o755)
    third = dirty_tree_digest(repo_root=repo)
    assert first.dirty is second.dirty is third.dirty is True
    assert len({first.digest, second.digest, third.digest}) == 3

    untracked.unlink()
    (repo / "tracked.txt").unlink()
    deleted = capture_git_evidence(repo_root=repo, phase="after")
    assert deleted.phase == "after"
    assert deleted.dirty is True
    assert deleted.dirty_tree_digest != clean.dirty_tree_digest
    assert deleted.changed_path_count == 1


def test_ignored_and_engine_runtime_files_are_excluded(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    clean = dirty_tree_digest(repo_root=repo)
    (repo / "ignored.log").write_text("ignored content", encoding="utf-8")
    runtime = repo / ".loopy_loop"
    runtime.mkdir()
    (runtime / "state.json").write_text('{"runtime": true}', encoding="utf-8")
    after = dirty_tree_digest(repo_root=repo)

    assert after == clean
    assert after.dirty is False


def test_workflow_definitions_under_loopy_directory_are_not_runtime_excluded(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    workflow = repo / ".loopy_loop/workflow_sets/main/contract.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("schema_version: 1\n", encoding="utf-8")

    digest = dirty_tree_digest(repo_root=repo)

    assert digest.dirty is True
    assert digest.changed_path_count == 1


def test_dirty_digest_binds_partial_staging_index_blob(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.write_text("staged version one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    tracked.write_text("same working tree\n", encoding="utf-8")
    first = dirty_tree_digest(repo_root=repo)

    tracked.write_text("staged version two\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    tracked.write_text("same working tree\n", encoding="utf-8")
    second = dirty_tree_digest(repo_root=repo)

    assert _git(repo, "status", "--porcelain=v1") == "MM tracked.txt"
    assert first.dirty is second.dirty is True
    assert first.digest != second.digest


def test_rename_hashes_destination_and_source_tombstone(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "mv", "tracked.txt", "renamed.txt")

    renamed = dirty_tree_digest(repo_root=repo)

    assert renamed.dirty is True
    assert renamed.status_entry_count == 1
    assert renamed.changed_path_count == 2


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_symlink_target_is_part_of_dirty_digest(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    link = repo / "artifact-link"
    link.symlink_to("first-target")
    first = dirty_tree_digest(repo_root=repo)
    link.unlink()
    link.symlink_to("second-target")
    second = dirty_tree_digest(repo_root=repo)

    assert first.digest != second.digest


def test_untracked_embedded_repository_directory_is_content_bound(
    tmp_path: Path,
) -> None:
    """Git prints embedded repositories as ``?? name/`` even with -uall."""

    repo = _repository(tmp_path)
    embedded = repo / "reference-impl"
    embedded.mkdir()
    _repository(embedded)

    first = dirty_tree_digest(repo_root=repo)
    (embedded / "tracked.txt").write_text("changed once\n", encoding="utf-8")
    second = dirty_tree_digest(repo_root=repo)
    (embedded / "new.txt").write_text("new nested input\n", encoding="utf-8")
    third = dirty_tree_digest(repo_root=repo)

    assert first.dirty is second.dirty is third.dirty is True
    assert first.status_entry_count == 1
    assert first.changed_path_count == 1
    assert len({first.digest, second.digest, third.digest}) == 3

    runtime = embedded / ".loopy_loop"
    runtime.mkdir()
    (runtime / "trace.json").write_text('{"runtime": true}', encoding="utf-8")
    assert dirty_tree_digest(repo_root=repo) == third


def test_dirty_submodule_digest_changes_with_content_inside_same_boundary(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    outer_root = tmp_path / "outer"
    source_root.mkdir()
    outer_root.mkdir()
    source = _repository(source_root)
    outer = _repository(outer_root)
    _git(
        outer,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(source),
        "vendor",
    )
    _git(outer, "commit", "-qam", "add submodule")
    vendor = outer / "vendor"
    (vendor / "tracked.txt").write_text("dirty one\n", encoding="utf-8")

    first = dirty_tree_digest(repo_root=outer)
    (vendor / "tracked.txt").write_text("dirty two\n", encoding="utf-8")
    second = dirty_tree_digest(repo_root=outer)
    (vendor / "added.txt").write_text("untracked nested file\n", encoding="utf-8")
    third = dirty_tree_digest(repo_root=outer)

    assert first.dirty is second.dirty is third.dirty is True
    assert first.changed_path_count == 1
    assert len({first.digest, second.digest, third.digest}) == 3


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_nested_directory_digest_does_not_follow_symlink_outside_repo(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _repository(repo_root)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside one\n", encoding="utf-8")
    embedded = repo / "embedded"
    embedded.mkdir()
    (embedded / "link").symlink_to(outside)

    first = dirty_tree_digest(repo_root=repo)
    outside.write_text("outside two\n", encoding="utf-8")
    second = dirty_tree_digest(repo_root=repo)

    assert first == second


def test_remote_fingerprints_retain_only_transport_host_and_path_digest(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    first_url = "https://example.com/private/repository.git"
    _git(repo, "remote", "add", "origin", first_url)
    first = sanitized_remote_fingerprints(repo_root=repo)
    serialized = json.dumps([item.__dict__ for item in first])

    assert len(first) == 1
    assert first[0].host == "example.com"
    assert first[0].transport == "https"
    assert "private" not in serialized
    assert "repository" not in serialized
    repeated = sanitized_remote_fingerprints(repo_root=repo)
    assert repeated[0].fingerprint == first[0].fingerprint

    _git(repo, "remote", "set-url", "origin", "https://example.com/private/other.git")
    repository_change = sanitized_remote_fingerprints(repo_root=repo)
    assert repository_change[0].fingerprint != first[0].fingerprint


def test_verbose_outputs_are_optional_and_exclude_engine_runtime_tree(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("product change\n", encoding="utf-8")
    runtime = repo / ".loopy_loop"
    runtime.mkdir()
    private_log = runtime / "traces/private.log"
    private_log.parent.mkdir()
    private_log.write_text("runtime detail", encoding="utf-8")
    status_path = runtime / "traces/attempt/git/status.jsonl"
    diff_path = runtime / "traces/attempt/git/diff.patch"

    receipt = capture_git_evidence(
        repo_root=repo,
        phase="after",
        verbose_status_path=status_path,
        verbose_diff_path=diff_path,
    )

    assert receipt.verbose_status_path == str(status_path.resolve())
    assert receipt.verbose_diff_path == str(diff_path.resolve())
    status = status_path.read_text(encoding="utf-8")
    diff = diff_path.read_text(encoding="utf-8")
    assert "tracked.txt" in status
    assert "product change" in diff
    assert ".loopy_loop/traces" not in status
    assert "private.log" not in diff


def test_unborn_branch_is_not_detached(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    receipt = capture_git_evidence(repo_root=tmp_path, phase="before")

    assert receipt.branch is not None
    assert receipt.head is None
    assert receipt.detached is False


def test_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(GitEvidenceError, match="failed"):
        dirty_tree_digest(repo_root=tmp_path)


def test_capture_git_receipt_cli_writes_eval_boundary(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    output = repo / ".loopy_loop/sessions/session/git_receipts/git-after-a1.json"

    result = CliRunner().invoke(
        main,
        [
            "capture-git-receipt",
            "--repo-root",
            str(repo),
            "--attempt-id",
            "a1",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["phase"] == "after"
    assert receipt["attempt_id"] == "a1"
    assert receipt["head"] == _git(repo, "rev-parse", "HEAD")
    assert receipt["dirty_tree_digest"]
