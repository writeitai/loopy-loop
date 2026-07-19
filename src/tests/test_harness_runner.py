from __future__ import annotations

import json
from typing import Any

import pytest
from team_harness import TeamHarnessError
from team_harness import TeamHarnessResult

from loopy_loop.config import ConfigError
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
        harness_output_root=tmp_path / "outputs" / "0001_planner",
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
    assert result.harness_output_dir == str(
        tmp_path / "outputs" / "0001_planner" / "run-123"
    )
    assert result_json["text"] == "done"
    assert result_json["harness_output_dir"] == result.harness_output_dir


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
            team_harness_agent_models={"codex": "gpt-5.5"},
            team_harness_agent_reasoning_efforts={"codex": "high"},
            team_harness_api_base="https://openrouter.ai/api",
            team_harness_system_prompt_extension="extra instructions",
        ),
        rendered_prompt="rendered prompt",
        harness_output_root=repo_root
        / ".loopy_loop"
        / "sessions"
        / "s1"
        / "harness_outputs"
        / "0007_outer",
        harness_factory=FakeHarness,
    )

    assert captured == {
        "provider": "openai_compat",
        "model": "snapshot-model",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "secret",
        "agents": ["codex", "reviewer"],
        "agent_models": {"codex": "gpt-5.5"},
        "agent_reasoning_efforts": {"codex": "high"},
        "output_dir": str(
            repo_root
            / ".loopy_loop"
            / "sessions"
            / "s1"
            / "harness_outputs"
            / "0007_outer"
        ),
        "system_prompt": "extra instructions",
        "cwd": str(repo_root),
        "console_mode": "silent",
    }


def test_harness_runner_passes_configured_retry_controls(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured: dict[str, Any] = {}

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(
            team_harness_max_retries=8,
            team_harness_retry_base_delay_s=2.0,
            team_harness_retry_max_delay_s=60.0,
        ),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert captured["max_retries"] == 8
    assert captured["retry_base_delay_s"] == 2.0
    assert captured["retry_max_delay_s"] == 60.0


def test_harness_runner_rejects_retry_controls_for_old_harness(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class OldHarness:
        def __init__(
            self,
            *,
            provider: str,
            model: str,
            api_base: str,
            api_key: str,
            agents: list[str],
            agent_models: dict[str, str],
            agent_reasoning_efforts: dict[str, str],
            system_prompt: str,
            cwd: str,
            console_mode: str,
        ) -> None:
            pass

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    with pytest.raises(ConfigError, match="coordinator retry controls"):
        run_harness_iteration(
            repo_root=repo_root,
            config_snapshot=snapshot_factory(team_harness_max_retries=8),
            rendered_prompt="rendered prompt",
            harness_factory=OldHarness,
        )


def test_harness_runner_passes_context_economy_knobs(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured: dict[str, Any] = {}

    class FakeHarness:
        def __init__(
            self, *, compact_above_tokens: int, prompt_cache: str, **kwargs: Any
        ) -> None:
            captured["compact_above_tokens"] = compact_above_tokens
            captured["prompt_cache"] = prompt_cache

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(
            team_harness_compact_above_tokens=80000,
            team_harness_prompt_cache="ephemeral",
        ),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert captured["compact_above_tokens"] == 80000
    assert captured["prompt_cache"] == "ephemeral"


def test_harness_runner_skips_context_economy_knobs_for_old_harness(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured: dict[str, Any] = {}

    class OldHarness:
        def __init__(
            self,
            *,
            provider: str,
            model: str,
            api_base: str,
            api_key: str,
            agents: list[str],
            agent_models: dict[str, str],
            agent_reasoning_efforts: dict[str, str],
            system_prompt: str,
            cwd: str,
            console_mode: str,
        ) -> None:
            captured["ran"] = True

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    # Unlike retry/agent overrides, unsupported context-economy knobs are
    # tolerated silently (they are optimizations, not correctness).
    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(
            team_harness_compact_above_tokens=80000,
            team_harness_prompt_cache="ephemeral",
        ),
        rendered_prompt="rendered prompt",
        harness_factory=OldHarness,
    )

    assert captured.get("ran") is True
    assert result.success is True


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


def test_harness_runner_rejects_agent_model_overrides_for_old_harness(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class OldHarness:
        def __init__(
            self,
            *,
            provider: str,
            model: str,
            api_base: str,
            api_key: str,
            agents: list[str],
            system_prompt: str,
            cwd: str,
            console_mode: str,
        ) -> None:
            pass

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    with pytest.raises(ConfigError, match="per-agent model overrides"):
        run_harness_iteration(
            repo_root=repo_root,
            config_snapshot=snapshot_factory(
                team_harness_agent_models={"codex": "gpt-5.5"}
            ),
            rendered_prompt="rendered prompt",
            harness_factory=OldHarness,
        )


def test_harness_runner_rejects_output_dir_for_old_harness(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class OldHarness:
        def __init__(
            self,
            *,
            provider: str,
            model: str,
            api_base: str,
            api_key: str,
            agents: list[str],
            agent_models: dict[str, str],
            agent_reasoning_efforts: dict[str, str],
            system_prompt: str,
            cwd: str,
            console_mode: str,
        ) -> None:
            pass

        async def run(self, task: str) -> TeamHarnessResult:
            return TeamHarnessResult(text="done", agents=[], run_id="run-123")

    with pytest.raises(ConfigError, match="SDK output_dir"):
        run_harness_iteration(
            repo_root=repo_root,
            config_snapshot=snapshot_factory(),
            rendered_prompt="rendered prompt",
            harness_output_root=repo_root / "outputs",
            harness_factory=OldHarness,
        )


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
    assert result.error_detail is None


def test_harness_runner_preserves_harness_error_detail(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    detail: dict[str, object] = {
        "outcome": "failed_before_session",
        "exit_code": 7,
        "stderr_tail": "TEST: synthetic auth failure",
    }

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> TeamHarnessResult:
            raise TeamHarnessError("boom", detail=detail)

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("boom")
    assert "outcome=failed_before_session" in result.error
    assert "TEST: synthetic auth failure" in result.error
    assert result.error_detail == detail


def test_harness_runner_preserves_failed_harness_paths_from_error_detail(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    detail: dict[str, object] = {
        "kind": "coordinator_api",
        "run_id": "run-456",
        "session_output_dir": str(tmp_path / "outputs" / "0002_inner" / "run-456"),
    }

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> TeamHarnessResult:
            raise TeamHarnessError("boom", detail=detail)

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="rendered prompt",
        harness_output_root=tmp_path / "outputs" / "0002_inner",
        harness_factory=FakeHarness,
    )

    assert result.success is False
    assert result.harness_run_id == "run-456"
    assert result.harness_output_dir == str(
        tmp_path / "outputs" / "0002_inner" / "run-456"
    )


def test_harness_runner_derives_failed_output_dir_from_run_id(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> TeamHarnessResult:
            raise TeamHarnessError("boom", detail={"run_id": "run-789"})

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="rendered prompt",
        harness_output_root=tmp_path / "outputs" / "0003_inner",
        harness_factory=FakeHarness,
    )

    assert result.harness_run_id == "run-789"
    assert result.harness_output_dir == str(
        tmp_path / "outputs" / "0003_inner" / "run-789"
    )


def test_harness_runner_writes_failure_artifacts(tmp_path: Any) -> None:
    write_iteration_artifacts(
        iteration_dir=tmp_path,
        rendered_prompt="rendered prompt",
        iteration_result=IterationResult(
            success=False,
            text=None,
            error="boom",
            error_detail={"outcome": "failed_before_session"},
            harness_run_id="",
        ),
    )
    result_json = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))

    assert (tmp_path / "prompt.txt").read_text(encoding="utf-8") == "rendered prompt"
    assert (tmp_path / "result_text.txt").read_text(encoding="utf-8") == ""
    assert (tmp_path / "harness_run_id.txt").read_text(encoding="utf-8") == ""
    assert result_json["success"] is False
    assert result_json["error"] == "boom"
    assert result_json["error_detail"] == {"outcome": "failed_before_session"}
    assert result_json["harness_output_dir"] == ""
