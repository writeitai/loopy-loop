from __future__ import annotations

from typing import Any

import httpx
import pytest

from loopy_loop.models import IterationResult
from loopy_loop.models import TaskResponse
from loopy_loop.worker import _run_task
from loopy_loop.worker import run_worker_loop


def _make_run_response(
    *,
    snapshot_factory: Any,
    workflow_id: str = "planner",
    session_id: str = "goal_20260419_143022_ab12cd34",
    iteration: int = 1,
    model: str = "gpt-test",
) -> dict[str, object]:
    return {
        "action": "run",
        "workflow_id": workflow_id,
        "session_id": session_id,
        "iteration": iteration,
        "config_snapshot": snapshot_factory(team_harness_model=model).model_dump(),
        "stop_reason": None,
    }


def _make_stop_response(*, stop_reason: str = "goal_met") -> dict[str, object]:
    return {
        "action": "stop",
        "stop_reason": stop_reason,
        "workflow_id": None,
        "session_id": None,
        "iteration": None,
        "config_snapshot": None,
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class FakeClient:
    def __init__(self, responses: list[object], posted_payloads: list[Any]) -> None:
        self._responses = responses
        self._posted_payloads = posted_payloads

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, object]) -> FakeResponse:
        self._posted_payloads.append({"url": url, "json": json})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, dict)
        return FakeResponse(item)


def _make_fake_client_cls(responses: list[object], posted_payloads: list[Any]) -> type:
    class _FakeClient(FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(responses=responses, posted_payloads=posted_payloads)

    return _FakeClient


def test_worker_runs_one_task_and_stops(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    repo_root = repo_builder()
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    posted: list[Any] = []
    responses: list[object] = [
        _make_run_response(snapshot_factory=snapshot_factory),
        _make_stop_response(),
    ]

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=responses, posted_payloads=posted),
    )

    run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")

    assert posted[0]["url"] == "http://coord/register"
    assert posted[1]["url"] == "http://coord/finished"
    assert len(posted) == 2


def test_worker_reads_prompt_from_disk(
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
    posted: list[Any] = []
    responses: list[object] = [
        _make_run_response(snapshot_factory=snapshot_factory),
        _make_stop_response(),
    ]

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        captured["prompt"] = kwargs["rendered_prompt"]
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=responses, posted_payloads=posted),
    )

    run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")

    assert "loopy-loop assignment" in captured["prompt"]
    assert "Disk prompt body" in captured["prompt"]
    assert "Workflow body:" in captured["prompt"]
    assert "Goal:" in captured["prompt"]
    assert "Completion criteria:" in captured["prompt"]
    assert "Stop criteria:" in captured["prompt"]
    # Assignment ID should NOT appear.
    assert "Assignment ID" not in captured["prompt"]


def test_worker_uses_config_snapshot_not_disk(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    repo_root = repo_builder(
        root_config={"team_harness_model": "disk-model"},
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
        captured_models.append(kwargs["config_snapshot"].team_harness_model)
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )

    task = TaskResponse.model_validate(
        _make_run_response(snapshot_factory=snapshot_factory, model="snapshot-model")
    )
    _run_task(repo_root=repo_root, task=task)

    assert captured_models == ["snapshot-model"]


def test_worker_exits_on_fatal_config_error(
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
    posted: list[Any] = []
    responses: list[object] = [
        _make_run_response(snapshot_factory=snapshot_factory),
        _make_stop_response(),
    ]
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=responses, posted_payloads=posted),
    )

    with pytest.raises(SystemExit) as exc_info:
        run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")

    stderr = capsys.readouterr().err
    assert exc_info.value.code == 2
    assert "Missing required environment variable: OPENROUTER_API_KEY" in stderr
    # /finished must have been posted with success=False.
    finished_calls = [p for p in posted if "/finished" in p["url"]]
    assert len(finished_calls) == 1
    assert finished_calls[0]["json"]["success"] is False


def test_worker_retries_finished_on_transient_error(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    repo_root = repo_builder()
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    posted: list[Any] = []
    responses: list[object] = [
        _make_run_response(snapshot_factory=snapshot_factory),
        httpx.ConnectError("transient"),
        _make_stop_response(),
    ]

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    monkeypatch.setattr("loopy_loop.worker.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=responses, posted_payloads=posted),
    )

    run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")

    finished_calls = [p for p in posted if "/finished" in p["url"]]
    # Two /finished calls: one that failed (raised ConnectError) and one that succeeded.
    assert len(finished_calls) == 2


def test_worker_all_finished_retries_exhausted(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    """When all /finished retries fail, the exception propagates."""
    repo_root = repo_builder()
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    posted: list[Any] = []
    # Two ConnectErrors to exhaust all retry attempts (_FINISHED_RETRY_ATTEMPTS = 2).
    responses: list[object] = [
        _make_run_response(snapshot_factory=snapshot_factory),
        httpx.ConnectError("transient 1"),
        httpx.ConnectError("transient 2"),
    ]

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    monkeypatch.setattr("loopy_loop.worker.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=responses, posted_payloads=posted),
    )

    with pytest.raises(httpx.ConnectError):
        run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")


def test_finished_payload_has_no_assignment_id(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    """The JSON posted to /finished must not contain assignment_id."""
    repo_root = repo_builder()
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    posted: list[Any] = []
    responses: list[object] = [
        _make_run_response(snapshot_factory=snapshot_factory),
        _make_stop_response(),
    ]

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=responses, posted_payloads=posted),
    )

    run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")

    finished_calls = [p for p in posted if "/finished" in p["url"]]
    assert len(finished_calls) == 1
    assert "assignment_id" not in finished_calls[0]["json"]
