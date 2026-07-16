from __future__ import annotations

import json
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
    (tmp_path / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_receipt_tracks_branch_head_and_dirty_status_diff(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    clean = capture_git_evidence(repo_root=repo, phase="before", attempt_id="a1")

    assert clean.phase == "before"
    assert clean.attempt_id == "a1"
    assert clean.head == _git(repo, "rev-parse", "HEAD")
    assert clean.branch is not None
    assert clean.detached is False
    assert clean.dirty is False
    assert Path(clean.repository_root) == repo.resolve()

    (repo / "tracked.txt").write_text("working tree change\n", encoding="utf-8")
    unstaged = dirty_tree_digest(repo_root=repo)
    _git(repo, "add", "tracked.txt")
    staged = dirty_tree_digest(repo_root=repo)

    assert unstaged.dirty is staged.dirty is True
    assert unstaged.changed_path_count == staged.changed_path_count == 1
    assert unstaged.digest != clean.dirty_tree_digest
    assert staged.digest != unstaged.digest


def test_porcelain_records_untracked_and_renamed_paths(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")

    untracked = dirty_tree_digest(repo_root=repo)

    assert untracked.status_entry_count == 1
    assert untracked.changed_path_count == 1

    (repo / "new.txt").unlink()
    _git(repo, "mv", "tracked.txt", "renamed.txt")
    renamed = dirty_tree_digest(repo_root=repo)

    assert renamed.status_entry_count == 1
    assert renamed.changed_path_count == 2


def test_untracked_nested_repository_is_visible_without_recursive_hashing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "outer"
    nested_root = repo_root / "reference"
    repo_root.mkdir()
    repo = _repository(repo_root)
    nested_root.mkdir()
    _repository(nested_root)

    digest = dirty_tree_digest(repo_root=repo)

    assert digest.dirty is True
    assert digest.status_entry_count == 1
    assert digest.changed_path_count == 1


def test_ignored_runtime_files_are_excluded_but_workflow_sources_are_not(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    clean = dirty_tree_digest(repo_root=repo)
    (repo / "ignored.log").write_text("ignored content", encoding="utf-8")
    runtime = repo / ".loopy_loop"
    runtime.mkdir()
    (runtime / "state.json").write_text('{"runtime": true}', encoding="utf-8")

    assert dirty_tree_digest(repo_root=repo) == clean

    workflow = runtime / "workflow_sets/main/contract.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("schema_version: 1\n", encoding="utf-8")
    configured = dirty_tree_digest(repo_root=repo)

    assert configured.dirty is True
    assert configured.changed_path_count == 1


def test_remote_fingerprints_do_not_retain_embedded_credentials_or_path(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://user:super-secret@example.com/private/repository.git?token=value",
    )

    first = sanitized_remote_fingerprints(repo_root=repo)
    serialized = json.dumps([item.__dict__ for item in first])

    assert len(first) == 1
    assert first[0].host == "example.com"
    assert first[0].transport == "https"
    assert "user" not in serialized
    assert "super-secret" not in serialized
    assert "private" not in serialized
    assert "repository" not in serialized
    assert "token" not in serialized

    _git(repo, "remote", "set-url", "origin", "https://example.com/other.git")
    changed = sanitized_remote_fingerprints(repo_root=repo)
    assert changed[0].fingerprint != first[0].fingerprint


def test_verbose_outputs_include_product_changes_and_exclude_runtime(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("product change\n", encoding="utf-8")
    runtime = repo / ".loopy_loop/traces"
    runtime.mkdir(parents=True)
    (runtime / "private.log").write_text("runtime detail", encoding="utf-8")
    status_path = runtime / "attempt/git/status.jsonl"
    diff_path = runtime / "attempt/git/diff.patch"

    receipt = capture_git_evidence(
        repo_root=repo,
        phase="after",
        verbose_status_path=status_path,
        verbose_diff_path=diff_path,
    )

    assert receipt.verbose_status_path == str(status_path.resolve())
    assert receipt.verbose_diff_path == str(diff_path.resolve())
    assert "tracked.txt" in status_path.read_text(encoding="utf-8")
    diff = diff_path.read_text(encoding="utf-8")
    assert "product change" in diff
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
