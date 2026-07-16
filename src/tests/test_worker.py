from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from loopy_loop.models import IterationResult
from loopy_loop.models import TaskResponse
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import eval_readiness_dir_path
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.worker import _bundled_cli_scripts_dir
from loopy_loop.worker import _eval_trace_channel_status
from loopy_loop.worker import _render_prompt
from loopy_loop.worker import _run_task
from loopy_loop.worker import _semantic_prompt_context
from loopy_loop.worker import ensure_interpreter_scripts_on_path
from loopy_loop.worker import run_worker_loop


@pytest.fixture(autouse=True)
def _restore_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_worker_loop mutates os.environ["PATH"]; re-setting it through
    # monkeypatch makes pytest restore the original value after each test.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


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
        "workflow_set": "main",
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
        "workflow_set": None,
        "workflow_id": None,
        "session_id": None,
        "iteration": None,
        "config_snapshot": None,
    }


class FakeResponse:
    def __init__(
        self, payload: dict[str, object], status_code: int = 200, text: str = ""
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

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

    def post(
        self, url: str, json: dict[str, object], timeout: object = None
    ) -> FakeResponse:
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
    pending_path = pending_finished_request_path(
        repo_root=repo_root,
        session_id="goal_20260419_143022_ab12cd34",
        iteration=1,
        workflow_id="planner",
    )
    assert not pending_path.exists()


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
    assert "Workflow set: main" in captured["prompt"]
    assert "Session directory:" in captured["prompt"]
    assert "Session goal path:" in captured["prompt"]
    assert "Session project_state directory:" in captured["prompt"]
    assert "Session eval_checks directory:" in captured["prompt"]
    assert "Session updates_from_user path:" in captured["prompt"]
    assert "Session child_requests directory:" in captured["prompt"]
    assert "Session control path:" in captured["prompt"]
    assert "Session finished ledger path:" in captured["prompt"]
    assert "Session harness outputs directory:" in captured["prompt"]
    # Assignment ID should NOT appear.
    assert "Assignment ID" not in captured["prompt"]


def test_worker_includes_goal_check_path_for_emitting_workflow(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any
) -> None:
    repo_root = repo_builder(
        workflows={
            "eval_runner": {
                "prompt": "Run evals",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "emits_goal_check": True,
                    "description": "",
                },
            }
        }
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    captured: dict[str, str] = {}

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        captured["prompt"] = kwargs["rendered_prompt"]
        return IterationResult(success=True, text="ok", error=None, harness_run_id="r1")

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )

    task = TaskResponse.model_validate(
        _make_run_response(
            snapshot_factory=snapshot_factory,
            workflow_id="eval_runner",
            session_id="goal_20260419_143022_ab12cd34",
            iteration=3,
        )
    )
    _run_task(repo_root=repo_root, task=task)

    assert "goal_check.json output path:" in captured["prompt"]
    assert "0003_eval_runner/goal_check.json" in captured["prompt"]
    assert "Iteration harness output root:" in captured["prompt"]
    assert "harness_outputs/0003_eval_runner" in captured["prompt"]


def test_render_prompt_includes_parent_session_for_child(
    repo_root: Any, snapshot_factory: Any
) -> None:
    parent_session_id = "20260419_143022_71393ee22450_ab12cd34"
    child_session_id = "20260419_143123_91aa0ab84591_cd34ef56"
    create_session_dir(
        repo_root=repo_root,
        session_id=parent_session_id,
        goal_hash="71393ee22450",
        workflow_set="main",
    )
    child_dir = create_session_dir(
        repo_root=repo_root,
        session_id=child_session_id,
        goal_hash="91aa0ab84591",
        workflow_set="inner_outer_eval",
        parent_session_id=parent_session_id,
    )

    prompt = _render_prompt(
        config_snapshot=snapshot_factory(),
        session_id=child_session_id,
        workflow_set="inner_outer_eval",
        iteration=1,
        workflow_id="inner",
        iteration_dir=child_dir / "iterations" / "0001_inner",
        harness_output_root=child_dir / "harness_outputs" / "0001_inner",
        workflow_prompt="Do child work.",
        repo_root=repo_root,
    )

    assert "Parent session directory:" in prompt
    assert parent_session_id in prompt


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
    # NO /finished: posting would make the coordinator dispatch a next task to
    # this exiting worker, which the next /register would then record as a
    # phantom crash failure (P2.3 review M2). The durable pending file stays
    # for the next /register to recover the failure exactly once.
    finished_calls = [p for p in posted if "/finished" in p["url"]]
    assert finished_calls == []
    pending = pending_finished_request_path(
        repo_root=repo_root,
        session_id="goal_20260419_143022_ab12cd34",
        iteration=1,
        workflow_id="planner",
    )
    assert pending.exists()


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
    assert finished_calls[0]["json"]["iteration"] == 1


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
    pending_path = pending_finished_request_path(
        repo_root=repo_root,
        session_id="goal_20260419_143022_ab12cd34",
        iteration=1,
        workflow_id="planner",
    )
    assert pending_path.exists()


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
    assert finished_calls[0]["json"]["iteration"] == 1


def test_ensure_interpreter_scripts_appends_preserving_entries() -> None:
    # The empty entry is a valid "current directory" component and must
    # survive the rewrite verbatim, in place.
    environ = {"PATH": os.pathsep.join(["", "/usr/bin", "/bin"])}

    ensure_interpreter_scripts_on_path(environ)

    entries = environ["PATH"].split(os.pathsep)
    assert entries == ["", "/usr/bin", "/bin", _bundled_cli_scripts_dir()]


def test_ensure_interpreter_scripts_noop_when_already_present() -> None:
    original = os.pathsep.join(["/usr/bin", _bundled_cli_scripts_dir()])
    environ = {"PATH": original}

    ensure_interpreter_scripts_on_path(environ)

    assert environ["PATH"] == original


def test_ensure_interpreter_scripts_missing_path_starts_from_defpath() -> None:
    environ: dict[str, str] = {}

    ensure_interpreter_scripts_on_path(environ)

    entries = environ["PATH"].split(os.pathsep)
    assert entries == [*os.defpath.split(os.pathsep), _bundled_cli_scripts_dir()]


def test_bundled_cli_scripts_dir_contains_eval_banana() -> None:
    scripts_dir = Path(_bundled_cli_scripts_dir())

    names = {entry.name for entry in scripts_dir.iterdir()}

    assert names & {"eval-banana", "eval-banana.exe"}


def test_run_worker_loop_prepares_path_for_agents(
    repo_builder: Any, monkeypatch: Any
) -> None:
    repo_root = repo_builder()
    seen: list[object] = []
    monkeypatch.setattr(
        "loopy_loop.worker.ensure_interpreter_scripts_on_path", seen.append
    )
    monkeypatch.setattr(
        "loopy_loop.worker.httpx.Client",
        _make_fake_client_cls(responses=[_make_stop_response()], posted_payloads=[]),
    )

    run_worker_loop(repo_root=repo_root, coordinator_url="http://coord")

    # Agents inherit os.environ, so that is the mapping that must be prepared.
    assert len(seen) == 1
    assert seen[0] is os.environ


def test_eval_trace_channel_requires_canonical_report(tmp_path: Path) -> None:
    trace_root = tmp_path / "trace"
    eval_root = trace_root / "eval"
    eval_root.mkdir(parents=True)

    assert _eval_trace_channel_status(trace_root=trace_root) == "not_produced"
    (eval_root / "nested").mkdir()
    assert _eval_trace_channel_status(trace_root=trace_root) == "incomplete"
    (eval_root / "report.json").write_text("{}", encoding="utf-8")
    assert _eval_trace_channel_status(trace_root=trace_root) == "complete"


def test_malformed_eval_readiness_is_context_not_a_worker_wedge(
    repo_builder: Any,
) -> None:
    repo_root = repo_builder()
    session_id = "readiness-session"
    create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="sha256:" + "1" * 64,
        goal="Inspect readiness",
        workflow_set="main",
    )
    readiness = eval_readiness_dir_path(repo_root=repo_root, session_id=session_id)
    readiness.mkdir(parents=True, exist_ok=True)
    valid = readiness / "ready-2.json"
    valid.write_text('{"ready": true}', encoding="utf-8")
    malformed = readiness / "ready-12.json"
    malformed.write_text("{", encoding="utf-8")

    context = _semantic_prompt_context(
        repo_root=repo_root, session_id=session_id, attempt_id="attempt-test"
    )

    assert str(valid.resolve()) in context
    assert '"ready": true' in context
    assert str(malformed.resolve()) in context
    assert "Ignored malformed eval-readiness receipts" in context
