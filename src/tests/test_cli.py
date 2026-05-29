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
    assert repo_root.joinpath("loopy_loop_goal.txt").exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/main/workflows/goal_check/prompt.txt"
    ).exists()
    root_config = repo_root.joinpath("loopy_loop_config.yaml").read_text(
        encoding="utf-8"
    )
    assert (
        """team_harness_provider: "codex"
team_harness_model: "gpt-5.5"
team_harness_agents:
  - "codex"
  - "claude"
  - "gemini"
team_harness_agent_models:
  codex: "gpt-5.5"
  claude: "claude-opus-4-8"
  gemini: "gemini-3.5-flash"
"""
        in root_config
    )


def test_init_is_idempotent(repo_root: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    first = runner.invoke(main, ["init"])
    second = runner.invoke(main, ["init"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already initialized" in second.output


def test_init_preserves_existing_files_and_updates_gitignore(
    repo_root: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()
    root_config = repo_root / "loopy_loop_config.yaml"
    goal_file = repo_root / "loopy_loop_goal.txt"
    workflow_dir = (
        repo_root
        / ".loopy_loop"
        / "workflow_sets"
        / "main"
        / "workflows"
        / "goal_check"
    )
    workflow_dir.mkdir(parents=True, exist_ok=True)
    goal_check_config = workflow_dir / "config.yaml"
    goal_check_prompt = workflow_dir / "prompt.txt"
    gitignore = repo_root / ".gitignore"
    root_config.write_text('goal: "keep me"\n', encoding="utf-8")
    goal_file.write_text("goal sentinel\n", encoding="utf-8")
    goal_check_config.write_text("sentinel-config\n", encoding="utf-8")
    goal_check_prompt.write_text("sentinel-prompt\n", encoding="utf-8")
    gitignore.write_text(".loopy_loop/sessions/\nexisting-entry\n", encoding="utf-8")

    result = runner.invoke(main, ["init"])
    gitignore_lines = gitignore.read_text(encoding="utf-8").splitlines()

    assert result.exit_code == 0
    assert root_config.read_text(encoding="utf-8") == 'goal: "keep me"\n'
    assert goal_file.read_text(encoding="utf-8") == "goal sentinel\n"
    assert goal_check_config.read_text(encoding="utf-8") == "sentinel-config\n"
    assert goal_check_prompt.read_text(encoding="utf-8") == "sentinel-prompt\n"
    for line in [".loopy_loop/sessions/"]:
        assert gitignore_lines.count(line) == 1


def test_init_inner_outer_eval_template_scaffolds_expected_files(
    repo_root: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--template", "inner_outer_eval"])

    assert result.exit_code == 0
    assert repo_root.joinpath("loopy_loop_config.yaml").exists()
    assert repo_root.joinpath("loopy_loop_goal.txt").exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_reviewer/prompt.txt"
    ).exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_runner/prompt.txt"
    ).exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/inner/prompt.txt"
    ).exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/outer/prompt.txt"
    ).exists()
    assert not repo_root.joinpath(
        ".loopy_loop/workflow_sets/main/workflows/goal_check/prompt.txt"
    ).exists()
    assert "You are the outer loop for this loopy-loop session." in repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/outer/prompt.txt"
    ).read_text(encoding="utf-8")
    assert 'gemini: "gemini-3.5-flash"' in repo_root.joinpath(
        "loopy_loop_config.yaml"
    ).read_text(encoding="utf-8")


def test_init_inner_outer_eval_template_is_idempotent(
    repo_root: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    first = runner.invoke(main, ["init", "--template", "inner_outer_eval"])
    second = runner.invoke(main, ["init", "--template", "inner_outer_eval"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already initialized" in second.output


def test_init_inner_outer_eval_template_preserves_existing_files(
    repo_root: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()
    root_config = repo_root / "loopy_loop_config.yaml"
    workflow_dir = (
        repo_root
        / ".loopy_loop"
        / "workflow_sets"
        / "inner_outer_eval"
        / "workflows"
        / "outer"
    )
    workflow_dir.mkdir(parents=True, exist_ok=True)
    outer_prompt = workflow_dir / "prompt.txt"
    gitignore = repo_root / ".gitignore"
    root_config.write_text('goal_file: "keep_me.txt"\n', encoding="utf-8")
    outer_prompt.write_text("sentinel-prompt\n", encoding="utf-8")
    gitignore.write_text(".loopy_loop/sessions/\nexisting-entry\n", encoding="utf-8")

    result = runner.invoke(main, ["init", "--template", "inner_outer_eval"])
    gitignore_lines = gitignore.read_text(encoding="utf-8").splitlines()

    assert result.exit_code == 0
    assert root_config.read_text(encoding="utf-8") == 'goal_file: "keep_me.txt"\n'
    assert outer_prompt.read_text(encoding="utf-8") == "sentinel-prompt\n"
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/inner/prompt.txt"
    ).exists()
    for line in [".loopy_loop/sessions/"]:
        assert gitignore_lines.count(line) == 1


def test_init_pm_planner_dispatcher_template_scaffolds_expected_files(
    repo_root: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--template", "pm_planner_dispatcher"])

    assert result.exit_code == 0
    assert repo_root.joinpath("loopy_loop_config.yaml").exists()
    assert repo_root.joinpath("loopy_loop_goal.txt").exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/planner/prompt.txt"
    ).exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/dispatcher/prompt.txt"
    ).exists()
    assert "workflow_set: pm_planner_dispatcher" in repo_root.joinpath(
        "loopy_loop_config.yaml"
    ).read_text(encoding="utf-8")
    assert "Child request schema:" in repo_root.joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/dispatcher/prompt.txt"
    ).read_text(encoding="utf-8")


def test_init_rejects_unknown_template(repo_root: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--template", "missing"])

    assert result.exit_code != 0
    assert "Invalid value for '--template'" in result.output


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
