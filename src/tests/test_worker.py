from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from loopy_loop.models import IterationResult
from loopy_loop.models import LayerHandoff
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

    prompt = captured["prompt"]
    # Diet header contract (single-goal-assignments.md §3).
    assert prompt.startswith("loopy-loop assignment — iteration 0001, role: planner")
    assert "Disk prompt body" in prompt
    assert "Workflow body:" in prompt
    assert "Goal:" in prompt
    assert "Completion criteria:" in prompt
    assert "Stop criteria:" in prompt
    assert "You are inside a durable looping session. Key paths:" in prompt
    assert "- session dir:" in prompt
    assert "- project_state/" in prompt
    assert "- child_requests/pending/" in prompt
    assert "- control.json" in prompt
    assert "- scratch dir (this iteration):" in prompt
    assert "- paths.json:" in prompt
    # The old inlined path enumeration and envelope dump are gone.
    assert "Session directory:" not in prompt
    assert "Absolute paths from the assignment envelope:" not in prompt
    assert "Assignment ID" not in prompt
    # The full machine path map now lives in a sibling paths.json.
    paths_file = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / "goal_20260419_143022_ab12cd34"
        / "iterations"
        / "0001_planner"
        / "paths.json"
    )
    assert paths_file.is_file()
    paths = json.loads(paths_file.read_text(encoding="utf-8"))
    assert paths["schema_version"] == 1
    assert paths["session_paths"]["project_state"].endswith("project_state")
    assert paths["previous_worker_sessions"] is None
    contracts_path = Path(paths["contracts"])
    assert contracts_path == paths_file.parent / "contracts.json"
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    assert contracts["layer_handoff"]["json_schema"] == (
        LayerHandoff.model_json_schema()
    )


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

    # goal_check output and the iteration scratch root move to paths.json.
    assert "goal_check.json output path:" not in captured["prompt"]
    paths_file = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / "goal_20260419_143022_ab12cd34"
        / "iterations"
        / "0003_eval_runner"
        / "paths.json"
    )
    paths = json.loads(paths_file.read_text(encoding="utf-8"))
    assert paths["goal_check_output"].endswith("0003_eval_runner/goal_check.json")
    assert "harness_outputs/0003_eval_runner" in paths["scratch_dir"]
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

    iteration_dir = child_dir / "iterations" / "0001_inner"
    prompt = _render_prompt(
        config_snapshot=snapshot_factory(),
        session_id=child_session_id,
        workflow_set="inner_outer_eval",
        iteration=1,
        workflow_id="inner",
        iteration_dir=iteration_dir,
        harness_output_root=child_dir / "harness_outputs" / "0001_inner",
        workflow_prompt="Do child work.",
        repo_root=repo_root,
    )

    # The parent session directory moves from the header into paths.json.
    assert "Parent session directory:" not in prompt
    paths = json.loads((iteration_dir / "paths.json").read_text(encoding="utf-8"))
    assert paths["parent_session_dir"] is not None
    assert parent_session_id in paths["parent_session_dir"]


def _render_synthetic_workflow_set(
    *,
    repo_root: Path,
    snapshot_factory: Any,
    goal: str,
    preamble: str | None,
    completion_criteria: list[str] | None = None,
    stop_criteria: list[str] | None = None,
) -> str:
    session_id = "goal_20260419_143022_ab12cd34"
    session_dir = create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="ab12cd34ef56",
        workflow_set="synthetic",
    )
    if preamble is not None:
        preamble_path = (
            repo_root / ".loopy_loop" / "workflow_sets" / "synthetic" / "preamble.txt"
        )
        preamble_path.parent.mkdir(parents=True, exist_ok=True)
        preamble_path.write_text(preamble, encoding="utf-8")
    snapshot = snapshot_factory(
        goal=goal,
        completion_criteria=completion_criteria
        if completion_criteria is not None
        else [],
        stop_criteria=stop_criteria if stop_criteria is not None else [],
    )
    return _render_prompt(
        config_snapshot=snapshot,
        session_id=session_id,
        workflow_set="synthetic",
        iteration=26,
        workflow_id="outer",
        iteration_dir=session_dir / "iterations" / "0026_outer",
        harness_output_root=session_dir / "harness_outputs" / "0026_outer",
        workflow_prompt="Do the synthetic role work.",
        repo_root=repo_root,
    )


def test_render_header_matches_diet_shape(
    repo_root: Any, snapshot_factory: Any
) -> None:
    prompt = _render_synthetic_workflow_set(
        repo_root=repo_root,
        snapshot_factory=snapshot_factory,
        goal="Deliver the thing.",
        preamble=None,
    )

    assert prompt.startswith(
        "loopy-loop assignment — iteration 0026, role: outer, "
        "session: goal_20260419_143022_ab12cd34\n\nGoal:\nDeliver the thing.\n"
    )
    # Empty criteria lists omit their sections entirely.
    assert "Completion criteria:" not in prompt
    assert "Stop criteria:" not in prompt
    assert "You are inside a durable looping session. Key paths:" in prompt
    for label in (
        "- session dir:",
        "- project_state/",
        "- child_requests/pending/",
        "- control.json",
        "- scratch dir (this iteration):",
        "- paths.json:",
    ):
        assert label in prompt
    # No shared preamble → no ground-rules section.
    assert "Shared ground rules:" not in prompt
    assert prompt.rstrip().endswith("Workflow body:\nDo the synthetic role work.")


def test_render_includes_preamble_when_present(
    repo_root: Any, snapshot_factory: Any
) -> None:
    prompt = _render_synthetic_workflow_set(
        repo_root=repo_root,
        snapshot_factory=snapshot_factory,
        goal="Deliver the thing.",
        preamble="Write atomically. Never force-push.",
        completion_criteria=["It works"],
        stop_criteria=["A blocker is recorded"],
    )

    assert "Completion criteria:\n- It works" in prompt
    assert "Stop criteria:\n- A blocker is recorded" in prompt
    assert "Shared ground rules:\nWrite atomically. Never force-push." in prompt
    # The preamble is included before the workflow body.
    assert prompt.index("Shared ground rules:") < prompt.index("Workflow body:")


def test_render_header_budget_excludes_goal_and_preamble(
    repo_root: Any, snapshot_factory: Any
) -> None:
    goal = "G" * 20_000
    preamble = "P" * 20_000
    prompt = _render_synthetic_workflow_set(
        repo_root=repo_root,
        snapshot_factory=snapshot_factory,
        goal=goal,
        preamble=preamble,
        completion_criteria=["It works"],
        stop_criteria=["A blocker is recorded"],
    )

    before_body = prompt.split("\n\nWorkflow body:", 1)[0]
    scaffold_bytes = (
        len(before_body.encode("utf-8"))
        - len(goal.encode("utf-8"))
        - len(preamble.encode("utf-8"))
    )
    assert scaffold_bytes <= 2048, scaffold_bytes


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
        "loopy_loop.worker.ensure_interpreter_scripts_on_path",
        lambda *, environ: seen.append(environ),
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
