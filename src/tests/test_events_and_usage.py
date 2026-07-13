"""Tests for P1.1: events.jsonl, the usage/cost ledger, and max_cost_usd."""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest

from loopy_loop.cli import main
from loopy_loop.config import ConfigError
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.coordinator_app import session_tree_usage_totals
from loopy_loop.events import events_path
from loopy_loop.events import read_events
from loopy_loop.models import IterationResult
from loopy_loop.models import IterationUsage
from loopy_loop.models import TaskResponse
from loopy_loop.sessions import child_requests_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import state_path
from loopy_loop.state_store import StateStore
from loopy_loop.worker import _read_harness_usage
from loopy_loop.worker import _run_task

REGISTER_BODY = {"worker": {"hostname": "test-host", "pid": 999983, "starttime": None}}

PLANNER_ONLY = {
    "planner": {
        "prompt": "Plan the next repo change.",
        "config": {
            "enabled": True,
            "run_every": 1,
            "must_follow": None,
            "not_before_iteration": 0,
            "description": "Plan work.",
        },
    }
}

USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "turns": 2}

CHILD_WORKFLOW_CONFIG = "\n".join(
    [
        "enabled: true",
        "run_every: 1",
        "must_follow: null",
        "not_before_iteration: 0",
        "description: Child work",
    ]
)


def _finished_body(
    task: dict[str, Any], *, success: bool, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": task["iteration"],
        "attempt_id": task["attempt_id"],
        "success": success,
        "text": "done" if success else None,
        "error": None if success else "boom",
        "worker": REGISTER_BODY["worker"],
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# events.jsonl
# ---------------------------------------------------------------------------


def test_event_stream_envelope_and_lifecycle(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    client.post(
        "/finished",
        json=_finished_body(task, success=False, usage=USAGE, duration_s=1.5),
    )

    events = read_events(
        path=events_path(repo_root=repo_root, session_id=task["session_id"])
    )
    types = [event["type"] for event in events]
    assert types[0] == "session_started"
    assert "task_dispatched" in types
    assert "task_finished" in types

    ids = [event["event_id"] for event in events]
    assert len(set(ids)) == len(ids)
    for event in events:
        assert event["schema_version"] == 1
        assert event["ts"].endswith("Z")
        assert event["session_id"] == task["session_id"]

    finished = next(e for e in events if e["type"] == "task_finished")
    assert finished["payload"]["prompt_tokens"] == 1000
    assert finished["payload"]["completion_tokens"] == 500
    assert finished["payload"]["duration_s"] == 1.5
    assert finished["payload"]["success"] is False

    dispatched = next(e for e in events if e["type"] == "task_dispatched")
    assert dispatched["payload"]["attempt_id"] == task["attempt_id"]
    assert dispatched["payload"]["worker"]["pid"] == 999983


def test_session_stopped_event_emitted_once(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    # A single-workflow set stops with no_eligible_workflow after a success.
    stop = client.post("/finished", json=_finished_body(task, success=True)).json()
    assert stop["action"] == "stop"
    # A repeated register against the terminal state must not re-emit.
    client.post("/register", json=REGISTER_BODY)

    events = read_events(
        path=events_path(repo_root=repo_root, session_id=task["session_id"])
    )
    stopped = [event for event in events if event["type"] == "session_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["payload"]["stop_reason"] == "no_eligible_workflow"
    assert stopped[0]["payload"]["status"] == "failed"


def test_read_events_tolerates_torn_tail(repo_builder: Any, tmp_path: Any) -> None:
    path = tmp_path / "events.jsonl"
    complete = json.dumps({"event_id": "a" * 12, "type": "session_started"})
    path.write_text(complete + "\n" + '{"event_id": "torn', encoding="utf-8")

    events = read_events(path=path)

    assert len(events) == 1
    assert events[0]["type"] == "session_started"


# ---------------------------------------------------------------------------
# usage ledger + budget
# ---------------------------------------------------------------------------


def test_usage_ledger_accumulates_and_counts_unknown(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    task = client.post(
        "/finished",
        json=_finished_body(task, success=False, usage=USAGE, duration_s=2.0),
    ).json()
    client.post("/finished", json=_finished_body(task, success=False))

    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    totals = state.usage_totals
    assert totals.prompt_tokens == 1000
    assert totals.completion_tokens == 500
    assert totals.iterations_with_usage == 1
    assert totals.iterations_without_usage == 1
    assert totals.duration_s == 2.0


def test_max_cost_usd_stops_loop(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={
            "model_prices": {"prompt_usd_per_1m": 10.0, "completion_usd_per_1m": 30.0},
            # 1000 prompt + 500 completion tokens => $0.01 + $0.015 = $0.025
            "max_cost_usd": 0.02,
        },
        workflows=PLANNER_ONLY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()
    response = client.post(
        "/finished", json=_finished_body(task, success=False, usage=USAGE)
    ).json()

    assert response["action"] == "stop"
    assert response["stop_reason"] == "max_cost_usd"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.status == "stopped"
    assert state.stop_reason == "max_cost_usd"
    events = read_events(
        path=events_path(repo_root=repo_root, session_id=task["session_id"])
    )
    stopped = [event for event in events if event["type"] == "session_stopped"]
    assert [event["payload"]["stop_reason"] for event in stopped] == ["max_cost_usd"]


def test_max_cost_usd_requires_model_prices(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"max_cost_usd": 5.0}, workflows=PLANNER_ONLY)
    with pytest.raises(ConfigError):
        create_coordinator_app(repo_root=repo_root, resume=False)


def test_budget_fields_not_in_wire_snapshot(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={
            "model_prices": {"prompt_usd_per_1m": 1.0, "completion_usd_per_1m": 1.0},
            "max_cost_usd": 100.0,
        },
        workflows=PLANNER_ONLY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    task = client.post("/register", json=REGISTER_BODY).json()

    assert "max_cost_usd" not in task["config_snapshot"]
    assert "model_prices" not in task["config_snapshot"]


# ---------------------------------------------------------------------------
# child roll-up
# ---------------------------------------------------------------------------


def test_child_usage_rolls_up_and_child_events_emitted(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    child_workflow_dir = (
        repo_root
        / ".loopy_loop"
        / "workflow_sets"
        / "child_set"
        / "workflows"
        / "child_work"
    )
    child_workflow_dir.mkdir(parents=True)
    child_workflow_dir.joinpath("prompt.txt").write_text(
        "Do the child work.", encoding="utf-8"
    )
    child_workflow_dir.joinpath("config.yaml").write_text(
        CHILD_WORKFLOW_CONFIG + "\n", encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    parent_task = client.post("/register", json=REGISTER_BODY).json()
    request_dir = child_requests_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    request_dir.joinpath("child.json").write_text(
        json.dumps(
            {
                "workflow_set": "child_set",
                "goal": "Handle a focused child task.",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    child_task = client.post(
        "/finished", json=_finished_body(parent_task, success=True)
    ).json()
    assert child_task["workflow_set"] == "child_set"

    control_path(repo_root=repo_root, session_id=child_task["session_id"]).write_text(
        json.dumps(
            {
                "state": "stopped",
                "reason": "child done",
                "stop_reason": "goal_met",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    resumed = client.post(
        "/finished", json=_finished_body(child_task, success=True, usage=USAGE)
    ).json()
    assert resumed["session_id"] == parent_task["session_id"]

    payload = json.loads(
        children_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ).read_text()
    )
    record = payload["children"][0]
    assert record["usage"]["prompt_tokens"] == 1000
    assert record["usage"]["completion_tokens"] == 500

    parent_state = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        ),
    ).read_state()
    assert parent_state is not None
    tree = session_tree_usage_totals(repo_root=repo_root, state=parent_state)
    assert tree.prompt_tokens == 1000
    assert tree.completion_tokens == 500

    parent_events = read_events(
        path=events_path(repo_root=repo_root, session_id=parent_task["session_id"])
    )
    parent_types = [event["type"] for event in parent_events]
    assert "child_started" in parent_types
    assert "child_finished" in parent_types
    child_events = read_events(
        path=events_path(repo_root=repo_root, session_id=child_task["session_id"])
    )
    child_types = [event["type"] for event in child_events]
    assert "session_started" in child_types
    assert "task_dispatched" in child_types
    assert "session_stopped" in child_types


# ---------------------------------------------------------------------------
# worker usage extraction
# ---------------------------------------------------------------------------


def test_read_harness_usage_sums_turns(tmp_path: Any) -> None:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    run_dir.joinpath("run.json").write_text(
        json.dumps(
            {
                "turns": [
                    {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
                    {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                    {"usage": {}},
                    {"no_usage": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    usage = _read_harness_usage(harness_output_dir=str(run_dir))

    assert usage == IterationUsage(prompt_tokens=17, completion_tokens=8, turns=2)


def test_read_harness_usage_unknown_cases(tmp_path: Any) -> None:
    assert _read_harness_usage(harness_output_dir="") is None
    assert _read_harness_usage(harness_output_dir=str(tmp_path / "missing")) is None
    run_dir = tmp_path / "run-2"
    run_dir.mkdir()
    run_dir.joinpath("run.json").write_text(
        json.dumps({"turns": [{"usage": {}}]}), encoding="utf-8"
    )
    assert _read_harness_usage(harness_output_dir=str(run_dir)) is None


def test_worker_attaches_usage_and_duration(
    repo_builder: Any, monkeypatch: Any, snapshot_factory: Any, tmp_path: Any
) -> None:
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    run_dir = tmp_path / "harness" / "run-9"
    run_dir.mkdir(parents=True)
    run_dir.joinpath("run.json").write_text(
        json.dumps(
            {"turns": [{"usage": {"prompt_tokens": 42, "completion_tokens": 7}}]}
        ),
        encoding="utf-8",
    )

    def fake_run_harness_iteration(**kwargs: Any) -> IterationResult:
        return IterationResult(
            success=True,
            text="ok",
            error=None,
            harness_run_id="run-9",
            harness_output_dir=str(run_dir),
        )

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    task = TaskResponse.model_validate(
        {
            "action": "run",
            "workflow_set": "main",
            "workflow_id": "planner",
            "session_id": "20260419_143022_ab12cd34",
            "iteration": 1,
            "attempt_id": "abc123def456",
            "config_snapshot": snapshot_factory().model_dump(),
            "stop_reason": None,
        }
    )

    assignment = _run_task(repo_root=repo_root, task=task)

    assert assignment.request.usage == IterationUsage(
        prompt_tokens=42, completion_tokens=7, turns=1
    )
    assert assignment.request.duration_s is not None
    assert assignment.request.duration_s >= 0


# ---------------------------------------------------------------------------
# CLI surface for status and events
# ---------------------------------------------------------------------------


def test_cli_status_shows_usage_and_cost(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={
            "model_prices": {"prompt_usd_per_1m": 10.0, "completion_usd_per_1m": 30.0}
        },
        workflows=PLANNER_ONLY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=REGISTER_BODY).json()
    client.post("/finished", json=_finished_body(task, success=False, usage=USAGE))

    monkeypatch.chdir(repo_root)
    result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "prompt_tokens=1000" in result.output
    # 1000 * 10 / 1e6 + 500 * 30 / 1e6 = 0.025
    assert "estimated_cost_usd: 0.0250" in result.output


def test_cli_events_prints_stream(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(workflows=PLANNER_ONLY)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=REGISTER_BODY).json()
    client.post("/finished", json=_finished_body(task, success=False))

    monkeypatch.chdir(repo_root)
    pretty = CliRunner().invoke(main, ["events"])
    assert pretty.exit_code == 0, pretty.output
    assert "task_finished" in pretty.output

    raw = CliRunner().invoke(main, ["events", "--json"])
    assert raw.exit_code == 0
    parsed = [json.loads(line) for line in raw.output.strip().splitlines()]
    assert any(event["type"] == "session_started" for event in parsed)
