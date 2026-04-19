from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from loopy_loop.cli import main
from loopy_loop.state_store import StateStore


def test_init_scaffolds_expected_files(repo_root: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    result = runner.invoke(main, ["init"])

    assert result.exit_code == 0
    assert repo_root.joinpath("loopy_loop_config.yaml").exists()
    assert repo_root.joinpath(".loopy_loop/workflows/goal_check/prompt.txt").exists()


def test_init_is_idempotent(repo_root: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    first = runner.invoke(main, ["init"])
    second = runner.invoke(main, ["init"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already initialized" in second.output


def test_status_and_stop_commands(
    repo_builder: Any, monkeypatch: Any, state_factory: Any
) -> None:
    repo_root = repo_builder()
    monkeypatch.chdir(repo_root)
    store = StateStore(repo_root=repo_root)
    store.write_state(state=state_factory())
    runner = CliRunner()

    status_result = runner.invoke(main, ["status"])
    stop_result = runner.invoke(main, ["stop"])
    updated = store.read_state()

    assert status_result.exit_code == 0
    assert "iteration_count: 0" in status_result.output
    assert stop_result.exit_code == 0
    assert updated is not None
    assert updated.stop_requested is True


def test_coordinator_requires_resume_for_running_state(
    repo_builder: Any, monkeypatch: Any, state_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    monkeypatch.chdir(repo_root)
    StateStore(repo_root=repo_root).write_state(state=state_factory())
    runner = CliRunner()

    result = runner.invoke(main, ["coordinator"])

    assert result.exit_code != 0
    assert "--resume" in result.output
