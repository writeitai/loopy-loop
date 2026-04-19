from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from loopy_loop.models import IterationResult
from loopy_loop.models import NextActionResponse
from loopy_loop.worker import _run_assignment
from loopy_loop.worker import run_worker_loop


def test_worker_reads_prompt_from_disk_and_retries_finished(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Disk prompt body",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            }
        }
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured: dict[str, str] = {}

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        captured["prompt"] = kwargs["rendered_prompt"]
        captured["model"] = kwargs["config_snapshot"].model
        return IterationResult(
            success=True, text="completed", error=None, harness_run_id="run-123"
        )

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    monkeypatch.setattr("loopy_loop.worker.time.sleep", lambda _: None)

    run_payload = {
        "action": "run",
        "assignment_id": "assignment-1",
        "workflow_id": "planner",
        "session_id": "goal_20260419_143022_ab12cd34",
        "iteration": 1,
        "config_snapshot": snapshot_factory(model="gpt-test").model_dump(),
    }
    finished_payload = {"action": "stop", "stop_reason": "goal_met"}
    responses: list[object] = [
        {"worker_id": "worker_1"},
        run_payload,
        httpx.ConnectError("transient"),
        finished_payload,
    ]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> FakeResponse:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, dict)
            return FakeResponse(item)

    monkeypatch.setattr("loopy_loop.worker.httpx.Client", FakeClient)

    run_worker_loop(repo_root=repo_root, coordinator_url="http://coordinator")
    result_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / "goal_20260419_143022_ab12cd34"
        / "iterations"
        / "0001_planner"
        / "result.json"
    )
    result_json = json.loads(result_path.read_text(encoding="utf-8"))

    assert "Disk prompt body" in captured["prompt"]
    assert captured["model"] == "gpt-test"
    assert result_json["harness_run_id"] == "run-123"


def test_worker_uses_config_snapshot_not_disk(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    repo_root = repo_builder(
        root_config={"model": "disk-model"},
        workflows={
            "planner": {
                "prompt": "Disk prompt body",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            }
        },
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured_models: list[str] = []

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        captured_models.append(kwargs["config_snapshot"].model)
        return IterationResult(
            success=True, text="completed", error=None, harness_run_id="run-123"
        )

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    next_action = NextActionResponse.model_validate(
        {
            "action": "run",
            "assignment_id": "assignment-1",
            "workflow_id": "planner",
            "session_id": "goal_20260419_143022_ab12cd34",
            "iteration": 1,
            "config_snapshot": snapshot_factory(model="snapshot-model").model_dump(),
        }
    )

    _run_assignment(repo_root=repo_root, next_action=next_action)
    repo_root.joinpath("loopy_loop_config.yaml").write_text(
        'goal_slug: "broken"\n', encoding="utf-8"
    )
    second_action = next_action.model_copy(
        update={"assignment_id": "assignment-2", "iteration": 2}
    )
    _run_assignment(repo_root=repo_root, next_action=second_action)

    assert captured_models == ["snapshot-model", "snapshot-model"]


def test_worker_exits_when_api_key_missing(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any, capsys: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Disk prompt body",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            }
        }
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    posted_payloads: list[dict[str, object]] = []
    responses: list[object] = [
        {"worker_id": "worker_1"},
        {
            "action": "run",
            "assignment_id": "assignment-1",
            "workflow_id": "planner",
            "session_id": "goal_20260419_143022_ab12cd34",
            "iteration": 1,
            "config_snapshot": snapshot_factory().model_dump(),
        },
        {"action": "stop", "stop_reason": "goal_met"},
    ]

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> FakeResponse:
            if url.endswith("/finished"):
                posted_payloads.append(json)
            item = responses.pop(0)
            assert isinstance(item, dict)
            return FakeResponse(item)

    monkeypatch.setattr("loopy_loop.worker.httpx.Client", FakeClient)

    with pytest.raises(SystemExit) as exc_info:
        run_worker_loop(repo_root=repo_root, coordinator_url="http://coordinator")

    stderr = capsys.readouterr().err

    assert exc_info.value.code == 2
    assert posted_payloads == [
        {
            "assignment_id": "assignment-1",
            "session_id": "goal_20260419_143022_ab12cd34",
            "workflow_id": "planner",
            "success": False,
            "text": None,
            "error": "Missing required environment variable: OPENROUTER_API_KEY",
        }
    ]
    assert "Missing required environment variable: OPENROUTER_API_KEY" in stderr
