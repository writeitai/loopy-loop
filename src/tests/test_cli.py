from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

from click.testing import CliRunner

from loopy_loop.cli import main
from loopy_loop.sessions import create_session_dir
from loopy_loop.state_store import StateStore
from tests.protocol_helpers import v2_finished_body
from tests.protocol_helpers import v2_register_body


def test_init_scaffolds_expected_files(repo_root: Any, monkeypatch: Any) -> None:
    """Default init writes a runnable, evidence-bound v2 workflow scaffold."""

    monkeypatch.chdir(repo_root)
    runner = CliRunner()

    result = runner.invoke(main, ["init"])

    assert result.exit_code == 0
    assert repo_root.joinpath("loopy_loop_config.yaml").exists()
    assert repo_root.joinpath("loopy_loop_goal.txt").exists()
    default_goal_check = repo_root.joinpath(
        ".loopy_loop/workflow_sets/main/workflows/goal_check/prompt.txt"
    )
    assert default_goal_check.exists()
    default_prompt = default_goal_check.read_text(encoding="utf-8")
    assert "eval-banana run --no-project-config" in default_prompt
    assert "trace:<trace_manifest_id>:/eval/report.json" in default_prompt
    assert "loopy capture-git-receipt" in default_prompt
    assert "Copy each definition digest from that report" in default_prompt
    assert "never manually\nhash the YAML" in default_prompt
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
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/contract.yaml"
    ).exists()
    assert not repo_root.joinpath(
        ".loopy_loop/workflow_sets/main/workflows/goal_check/prompt.txt"
    ).exists()
    assert "harness coordinator for the `outer` workflow role" in repo_root.joinpath(
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
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/eval_reviewer/prompt.txt"
    ).exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/eval_runner/prompt.txt"
    ).exists()
    assert repo_root.joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/contract.yaml"
    ).exists()
    assert "workflow_set: pm_planner_dispatcher" in repo_root.joinpath(
        "loopy_loop_config.yaml"
    ).read_text(encoding="utf-8")
    assert '"schema_version": 2' in repo_root.joinpath(
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
    state = state_factory()
    create_session_dir(
        repo_root=repo_root,
        session_id=state.active_session_id,
        goal_hash=state.goal_hash,
        workflow_set=state.workflow_set,
    )
    store.write_state(state=state)
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
    """Coordinator startup discovers a manifest-backed running root session."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    monkeypatch.chdir(repo_root)
    state = state_factory()
    create_session_dir(
        repo_root=repo_root,
        session_id=state.active_session_id,
        goal_hash=state.goal_hash,
        workflow_set=state.workflow_set,
    )
    StateStore(repo_root=repo_root).write_state(state=state)
    runner = CliRunner()

    result = runner.invoke(main, ["coordinator"])

    assert result.exit_code != 0
    assert "--resume" in result.output


def test_init_pm_template_ships_the_child_workflow_set(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The dispatcher spawns child sessions running inner_outer_eval: a clean
    # init that lacked that set could not execute a single child (P0.4).
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--template", "pm_planner_dispatcher"])
    assert result.exit_code == 0, result.output
    for workflow_id in ("outer", "inner", "eval_reviewer", "eval_runner"):
        prompt = tmp_path.joinpath(
            ".loopy_loop/workflow_sets/inner_outer_eval/workflows",
            workflow_id,
            "prompt.txt",
        )
        config = prompt.with_name("config.yaml")
        assert prompt.exists(), f"missing child workflow prompt: {workflow_id}"
        assert config.exists(), f"missing child workflow config: {workflow_id}"
    assert tmp_path.joinpath(
        ".loopy_loop/workflow_sets/inner_outer_eval/contract.yaml"
    ).exists()


def test_clean_pm_init_can_dispatch_an_inner_outer_eval_child(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # End-to-end proof for P0.4: from a clean `loopy init`, the PM parent can
    # actually dispatch its documented child workflow set.
    import json as json_module

    from fastapi.testclient import TestClient

    from loopy_loop.coordinator_app import create_coordinator_app
    from loopy_loop.sessions import child_requests_dir_path

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--template", "pm_planner_dispatcher"])
    assert result.exit_code == 0, result.output

    client = TestClient(create_coordinator_app(repo_root=tmp_path, resume=False))
    parent_task = client.post("/register", json=v2_register_body(tmp_path)).json()
    assert parent_task["action"] == "run"
    assert parent_task["workflow_set"] == "pm_planner_dispatcher"

    request_dir = child_requests_dir_path(
        repo_root=tmp_path, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("wp.json").write_text(
        json_module.dumps(
            {
                "schema_version": 2,
                "request_id": "wp",
                "workflow_set": "inner_outer_eval",
                "origin": {
                    "parent_attempt_id": parent_task["attempt_id"],
                    "parent_work_item_id": "wp",
                    "supersedes_request_id": None,
                },
                "assignment": {
                    "goal": "Implement the selected planner item.",
                    "completion_criteria": [],
                    "stop_criteria": [],
                    "constraints": [],
                    "deliverables": [],
                    "required_evidence": [],
                },
                "inputs": [],
            }
        ),
        encoding="utf-8",
    )
    child_task = client.post(
        "/finished",
        json=v2_finished_body(
            parent_task, success=True, text="planner selected an item"
        ),
    ).json()
    assert child_task["action"] == "run"
    assert child_task["workflow_set"] == "inner_outer_eval"
    assert child_task["session_id"] != parent_task["session_id"]

    # Run the dispatched child assignment through the real worker path (fake
    # harness) and verify the SEMANTICS, not just the dispatch: the child works
    # on ITS goal, and nothing tells its implementer not to implement.
    from loopy_loop.models import IterationResult
    from loopy_loop.models import TaskResponse
    from loopy_loop.worker import _run_task

    captured: dict[str, Any] = {}

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        captured.update(kwargs)
        return IterationResult(success=True, text="ok", harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    _run_task(repo_root=tmp_path, task=TaskResponse.model_validate(child_task))

    rendered = captured["rendered_prompt"]
    assert "Implement the selected planner item." in rendered  # the CHILD goal
    # The child prompts treat the SESSION goal as canonical — never the
    # repo-root goal file, which in a child session is the PARENT's goal.
    assert "loopy_loop_goal" not in rendered
    # The PM template's system-prompt extension must not leak a
    # "do not implement" instruction into the child's implementer.
    snapshot = captured["config_snapshot"]
    assert "not implement" not in snapshot.team_harness_system_prompt_extension
