from __future__ import annotations

import json
from typing import Any

from team_harness import HarnessError
from team_harness import HarnessResult

from loopy_loop.harness_runner import run_harness_iteration
from loopy_loop.harness_runner import write_iteration_artifacts


def test_harness_runner_normalizes_success(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> HarnessResult:
            assert task == "rendered prompt"
            return HarnessResult(text="done", agents=[], run_id="run-123")

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


def test_harness_runner_normalizes_harness_error(
    repo_root: Any, snapshot_factory: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class FakeHarness:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, task: str) -> HarnessResult:
            raise HarnessError("boom")

    result = run_harness_iteration(
        repo_root=repo_root,
        config_snapshot=snapshot_factory(),
        rendered_prompt="rendered prompt",
        harness_factory=FakeHarness,
    )

    assert result.success is False
    assert result.error == "boom"
