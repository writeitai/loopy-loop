from __future__ import annotations

import json
from typing import Any

from team_harness import TeamHarnessError
from team_harness import TeamHarnessResult

from loopy_loop.harness_runner import run_harness_iteration
from loopy_loop.harness_runner import write_iteration_artifacts
from loopy_loop.models import IterationResult


def test_harness_runner_normalizes_success(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> TeamHarnessResult:
            assert task == "rendered prompt"
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )
    write_iteration_artifacts(
        iteration_dir=tmp_path,
        rendered_prompt="rendered prompt",
        iteration_result=result,
    )
    result_json = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))

    assert result.success is True
    assert result.harness_run_id == "run-123"
    assert result_json["text"] == "done"


def test_harness_runner_passes_normalized_constructor_kwargs(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured: dict[str, Any] = {}

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self, task: str) -> TeamHarnessResult:
            assert task == "rendered prompt"
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(
            team_harness_provider="openai_compat",
            team_harness_model="snapshot-model",
            team_harness_agents=["codex", "reviewer"],
            team_harness_api_base="https://openrouter.ai/api",
            team_harness_system_prompt_extension="extra instructions",
        ),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert captured == {
        "provider": "openai_compat",
        "model": "snapshot-model",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "secret",
        "agents": ["codex", "reviewer"],
        "system_prompt": "extra instructions",
        "cwd": str(repo_root),
        "console_mode": "silent",
    }


def test_harness_runner_passes_none_api_key_for_codex_provider(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(team_harness_provider="codex"),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert captured["provider"] == "codex"
    assert captured["api_key"] is None


def test_harness_runner_normalizes_harness_error(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> TeamHarnessResult:
            raise TeamHarnessError("boom")

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert result.success is False
    assert result.error == "boom"


def test_harness_runner_writes_failure_artifacts(tmp_path: Any) -> None:
    write_iteration_artifacts(
        iteration_dir=tmp_path,
        rendered_prompt="rendered prompt",
        iteration_result=IterationResult(
            success=False, text=None, error="boom", harness_run_id=""
        ),
    )
    result_json = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))

    assert (tmp_path / "prompt.txt").read_text(encoding="utf-8") == "rendered prompt"
    assert (tmp_path / "result_text.txt").read_text(encoding="utf-8") == ""
    assert (tmp_path / "harness_run_id.txt").read_text(encoding="utf-8") == ""
    assert result_json["success"] is False
    assert result_json["error"] == "boom"
