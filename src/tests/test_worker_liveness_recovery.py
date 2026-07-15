"""Tests for worker liveness verification and orphan recovery (D7 / P0.1 / P2.5).

The worker sends its process identity with /register and /finished; the
coordinator stamps it onto the dispatched CurrentTask, refuses (409) to reclaim
a task whose worker is verifiably alive, and — when the prior worker is not
verifiably alive and nothing is recoverable — applies the recovery policy to
agent processes via team-harness before recording abandonment and resuming
normal scheduling.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any

from fastapi.testclient import TestClient
import pytest

from loopy_loop import recovery as recovery_module
from loopy_loop import worker_identity as worker_identity_module
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.models import RegisterRequest
from loopy_loop.models import WorkerIdentity
from loopy_loop.recovery import recover_interrupted_iteration
from loopy_loop.recovery import RecoveryOutcome
from loopy_loop.recovery import RecoveryRefusedError
from loopy_loop.recovery import SALVAGE_FILENAME
from loopy_loop.state_store import StateStore
from loopy_loop.worker import run_worker_loop
from loopy_loop.worker_identity import current_worker_identity
from loopy_loop.worker_identity import is_worker_alive

LOCAL_IDENTITY = {
    "hostname": socket.gethostname(),
    "pid": 4242,
    "starttime": "lstart:Sun Jul 12 00:00:00 2026",
}

REGISTER_BODY = {"worker": {"hostname": "test-host", "pid": 999983, "starttime": None}}


# ---------------------------------------------------------------------------
# worker_identity unit behavior
# ---------------------------------------------------------------------------


def test_current_worker_identity_carries_pid_and_hostname() -> None:
    identity = current_worker_identity()
    assert identity.pid > 0
    assert identity.hostname == socket.gethostname()
    # starttime may be None when team-harness predates process identity.


def test_is_worker_alive_unknown_cases() -> None:
    assert is_worker_alive(None) is None
    no_token = WorkerIdentity(hostname=socket.gethostname(), pid=1, starttime=None)
    assert is_worker_alive(no_token) is None
    remote = WorkerIdentity(hostname="another-host", pid=1, starttime="lstart:x")
    assert is_worker_alive(remote) is None


def test_is_worker_alive_verified_true_and_false(monkeypatch: Any) -> None:
    identity = WorkerIdentity(**LOCAL_IDENTITY)
    monkeypatch.setattr(worker_identity_module, "_identity_module", lambda: object())
    monkeypatch.setattr(worker_identity_module, "_is_zombie", lambda pid: False)
    monkeypatch.setattr(
        worker_identity_module,
        "capture_starttime",
        lambda pid: LOCAL_IDENTITY["starttime"],
    )
    assert is_worker_alive(identity) is True
    monkeypatch.setattr(worker_identity_module, "capture_starttime", lambda pid: None)
    assert is_worker_alive(identity) is False  # pid gone
    monkeypatch.setattr(
        worker_identity_module, "capture_starttime", lambda pid: "lstart:other"
    )
    assert is_worker_alive(identity) is False  # pid recycled


def test_is_worker_alive_unknown_when_capture_unavailable(monkeypatch: Any) -> None:
    # Version skew: a coordinator whose team-harness cannot capture start
    # times must NOT interpret its own None-capture as "the pid is gone" —
    # that would declare a live worker verifiably dead (duplicate work).
    identity = WorkerIdentity(**LOCAL_IDENTITY)
    monkeypatch.setattr(worker_identity_module, "_identity_module", lambda: None)
    assert is_worker_alive(identity) is None


def test_is_worker_alive_zombie_counts_as_dead(monkeypatch: Any) -> None:
    # An exited-but-unreaped worker still matches its start time, but it will
    # never call /finished: it must not hold the task hostage via 409.
    identity = WorkerIdentity(**LOCAL_IDENTITY)
    monkeypatch.setattr(worker_identity_module, "_identity_module", lambda: object())
    monkeypatch.setattr(
        worker_identity_module,
        "capture_starttime",
        lambda pid: LOCAL_IDENTITY["starttime"],
    )
    monkeypatch.setattr(worker_identity_module, "_is_zombie", lambda pid: True)
    assert is_worker_alive(identity) is False


# ---------------------------------------------------------------------------
# Coordinator: verified-alive worker -> 409, no state mutation
# ---------------------------------------------------------------------------


def _register_with_orphan(
    repo_builder: Any,
    current_task_factory: Any,
    monkeypatch: Any,
    *,
    orphan_worker: dict[str, Any] | None,
    liveness: bool | None,
    recovery_outcome: RecoveryOutcome | None = None,
) -> tuple[Any, StateStore, Any]:
    """Build a repo with an orphaned current_task and post /register."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Plan work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Plan",
                },
            },
            "implement": {
                "prompt": "Implement.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement",
                },
            },
        }
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    overrides: dict[str, Any] = {
        "workflow_id": "implement",
        "session_id": state.active_session_id,
        "iteration": 1,
    }
    if orphan_worker is not None:
        overrides["worker"] = orphan_worker
    state.current_task = current_task_factory(**overrides)
    store.write_state(state=state)
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.is_worker_alive", lambda identity: liveness
    )
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.recover_interrupted_iteration",
        lambda **kwargs: recovery_outcome or RecoveryOutcome(),
    )
    response = client.post("/register", json=REGISTER_BODY)
    return response, store, state


def test_register_refuses_when_worker_verifiably_alive(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    response, store, _ = _register_with_orphan(
        repo_builder,
        current_task_factory,
        monkeypatch,
        orphan_worker=LOCAL_IDENTITY,
        liveness=True,
    )
    assert response.status_code == 409
    assert "still running" in response.json()["detail"]
    updated = store.read_state()
    assert updated is not None
    assert updated.current_task is not None  # untouched
    assert updated.history == []  # nothing abandoned


def test_register_recovers_when_worker_verifiably_dead(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    response, store, _ = _register_with_orphan(
        repo_builder,
        current_task_factory,
        monkeypatch,
        orphan_worker=LOCAL_IDENTITY,
        liveness=False,
    )
    assert response.status_code == 200
    assert response.json()["action"] == "run"
    updated = store.read_state()
    assert updated is not None
    assert updated.history[0].error == "abandoned"


def test_register_recovers_when_liveness_unknown(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    # Pre-upgrade persisted tasks / remote hosts: no verifiable identity
    # -> the pre-existing assume-abandoned recovery behavior.
    response, store, _ = _register_with_orphan(
        repo_builder,
        current_task_factory,
        monkeypatch,
        orphan_worker=None,
        liveness=None,
    )
    assert response.status_code == 200
    updated = store.read_state()
    assert updated is not None
    assert updated.history[0].error == "abandoned"


def test_register_records_abandoned_after_drain_when_salvaged(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    response, store, _ = _register_with_orphan(
        repo_builder,
        current_task_factory,
        monkeypatch,
        orphan_worker=LOCAL_IDENTITY,
        liveness=False,
        recovery_outcome=RecoveryOutcome(reaped_runs=1, settled_workers=2),
    )
    assert response.status_code == 200
    updated = store.read_state()
    assert updated is not None
    assert updated.history[0].error == "abandoned_after_drain"


def test_register_surfaces_recovery_refused_as_409(
    repo_builder: Any, monkeypatch: Any, current_task_factory: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None
    state.current_task = current_task_factory(
        workflow_id="planner", session_id=state.active_session_id, iteration=1
    )
    store.write_state(state=state)

    def refuse(**kwargs: Any) -> RecoveryOutcome:
        raise RecoveryRefusedError("run owner still alive")

    monkeypatch.setattr(
        "loopy_loop.coordinator_app.recover_interrupted_iteration", refuse
    )
    response = client.post("/register", json=REGISTER_BODY)
    assert response.status_code == 409
    assert "still alive" in response.json()["detail"]
    updated = store.read_state()
    assert updated is not None
    assert updated.current_task is not None  # state untouched


def test_register_stamps_worker_identity_on_dispatched_task(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    body = RegisterRequest(worker=WorkerIdentity(**LOCAL_IDENTITY)).model_dump()
    response = client.post("/register", json=body)
    assert response.status_code == 200
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.current_task is not None
    assert state.current_task.worker is not None
    assert state.current_task.worker.pid == LOCAL_IDENTITY["pid"]


def test_finished_stamps_callers_identity_on_next_task(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    body = RegisterRequest(worker=WorkerIdentity(**LOCAL_IDENTITY)).model_dump()
    task = client.post("/register", json=body).json()
    finished = {
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": task["iteration"],
        "success": True,
        "text": "done",
        "worker": LOCAL_IDENTITY,
    }
    next_task = client.post("/finished", json=finished).json()
    assert next_task["action"] == "run"
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    assert state.current_task is not None
    assert state.current_task.worker is not None
    assert state.current_task.worker.pid == LOCAL_IDENTITY["pid"]


def test_register_with_identity_dispatches(repo_builder: Any, monkeypatch: Any) -> None:
    # The canonical register: identity present, task dispatched and stamped.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    response = client.post("/register", json=REGISTER_BODY)
    assert response.status_code == 200
    assert response.json()["action"] == "run"


# ---------------------------------------------------------------------------
# recovery module
# ---------------------------------------------------------------------------


class _FakeWorkerOutcome:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class _FakeReport:
    def __init__(self, outcomes: list[str]) -> None:
        self.workers = [_FakeWorkerOutcome(outcome) for outcome in outcomes]

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"workers": [worker.outcome for worker in self.workers]}


class _FakeReaperModule:
    class ReapRefusedError(RuntimeError):
        pass

    def __init__(self, outcomes: list[str], *, refuse: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = outcomes
        self._refuse = refuse

    def reap_run(self, run_json: Path, **kwargs: Any) -> _FakeReport:
        self.calls.append({"run_json": run_json, **kwargs})
        if self._refuse:
            raise self.ReapRefusedError("owner alive")
        return _FakeReport(self._outcomes)


class _FakeThConfig:
    def __init__(self, runs_dir: Path) -> None:
        self.RUNS_DIR = runs_dir


def _build_interrupted_iteration(
    tmp_path: Path, *, run_ids: list[str]
) -> tuple[Path, str, Path]:
    session_id = "20260712_000000_deadbeef_ab12cd34"
    output_root = (
        tmp_path
        / ".loopy_loop"
        / "sessions"
        / session_id
        / "harness_outputs"
        / "0001_implement"
    )
    for run_id in run_ids:
        (output_root / run_id).mkdir(parents=True, exist_ok=True)
    runs_dir = tmp_path / "th_runs"
    for run_id in run_ids:
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {"run_id": run_id, "session_output_dir": str(output_root / run_id)}
            ),
            encoding="utf-8",
        )
    return tmp_path, session_id, runs_dir


def test_recover_interrupted_iteration_reaps_and_writes_salvage(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo_root, session_id, runs_dir = _build_interrupted_iteration(
        tmp_path, run_ids=["run_a", "run_b"]
    )
    reaper = _FakeReaperModule(["drained", "identity_mismatch_skipped"])
    monkeypatch.setattr(
        recovery_module, "_load_reaper", lambda: (reaper, _FakeThConfig(runs_dir))
    )
    outcome = recover_interrupted_iteration(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id="implement",
        policy="drain",
        drain_timeout_s=5,
    )
    assert outcome.reaped_runs == 2
    assert outcome.settled_workers == 2  # one drained per fake report
    assert outcome.salvaged
    assert [call["policy"] for call in reaper.calls] == ["drain", "drain"]
    # One SHARED deadline across runs: each call gets the remaining budget.
    budgets = [call["drain_timeout_s"] for call in reaper.calls]
    assert budgets[0] == pytest.approx(5, abs=1.0)
    assert budgets[1] <= budgets[0]
    salvage_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / session_id
        / "iterations"
        / "0001_implement"
        / SALVAGE_FILENAME
    )
    payload = json.loads(salvage_path.read_text())
    assert payload["schema_version"] == 1
    assert payload["reaped_runs"] == 2
    assert payload["settled_workers"] == 2
    assert len(payload["reports"]) == 2


def test_recover_interrupted_iteration_refusal_propagates(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo_root, session_id, runs_dir = _build_interrupted_iteration(
        tmp_path, run_ids=["run_a"]
    )
    reaper = _FakeReaperModule([], refuse=True)
    monkeypatch.setattr(
        recovery_module, "_load_reaper", lambda: (reaper, _FakeThConfig(runs_dir))
    )
    with pytest.raises(RecoveryRefusedError):
        recover_interrupted_iteration(
            repo_root=repo_root,
            session_id=session_id,
            iteration=1,
            workflow_id="implement",
            policy="drain",
            drain_timeout_s=5,
        )


def test_recover_interrupted_iteration_without_reaper_degrades(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo_root, session_id, _ = _build_interrupted_iteration(tmp_path, run_ids=["run_a"])
    monkeypatch.setattr(recovery_module, "_load_reaper", lambda: None)
    outcome = recover_interrupted_iteration(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id="implement",
        policy="drain",
        drain_timeout_s=5,
    )
    assert outcome.reaped_runs == 0
    assert not outcome.salvaged


def test_recover_interrupted_iteration_no_runs_no_salvage(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo_root, session_id, runs_dir = _build_interrupted_iteration(tmp_path, run_ids=[])
    reaper = _FakeReaperModule(["drained"])
    monkeypatch.setattr(
        recovery_module, "_load_reaper", lambda: (reaper, _FakeThConfig(runs_dir))
    )
    outcome = recover_interrupted_iteration(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id="implement",
        policy="reap",
        drain_timeout_s=5,
    )
    assert outcome.reaped_runs == 0
    assert reaper.calls == []
    iteration_dir = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / session_id
        / "iterations"
        / "0001_implement"
    )
    assert not (iteration_dir / SALVAGE_FILENAME).exists()


# ---------------------------------------------------------------------------
# Worker client: identity in payloads, 409 handling
# ---------------------------------------------------------------------------


class _Response:
    def __init__(
        self, payload: dict[str, Any], status_code: int = 200, text: str = ""
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, Any], timeout: Any = None) -> _Response:
        self.posted.append((url, json))
        return self._responses.pop(0)


def test_worker_register_payload_carries_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client = _Client([_Response({"action": "stop", "stop_reason": "goal_met"})])
    monkeypatch.setattr("loopy_loop.worker.httpx.Client", lambda timeout: client)
    run_worker_loop(repo_root=tmp_path, coordinator_url="http://coordinator")
    url, payload = client.posted[0]
    assert url.endswith("/register")
    assert payload["worker"]["pid"] > 0
    assert payload["worker"]["hostname"] == socket.gethostname()


def test_worker_exits_with_code_3_on_busy(tmp_path: Path, monkeypatch: Any) -> None:
    client = _Client(
        [_Response({"detail": "worker pid=1 is still running"}, status_code=409)]
    )
    monkeypatch.setattr("loopy_loop.worker.httpx.Client", lambda timeout: client)
    with pytest.raises(SystemExit) as excinfo:
        run_worker_loop(repo_root=tmp_path, coordinator_url="http://coordinator")
    assert excinfo.value.code == 3


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_recovery_config_defaults(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    from loopy_loop.config import run_preflight

    preflight = run_preflight(repo_root=repo_root, workflow_set=None, goal_file=None)
    assert preflight.root_config.recovery_policy == "drain"
    assert preflight.root_config.recovery_drain_timeout_s == 600.0


def test_recovery_config_overrides_and_validation(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"recovery_policy": "reap", "recovery_drain_timeout_s": 30}
    )
    from loopy_loop.config import run_preflight

    preflight = run_preflight(repo_root=repo_root, workflow_set=None, goal_file=None)
    assert preflight.root_config.recovery_policy == "reap"
    assert preflight.root_config.recovery_drain_timeout_s == 30.0


def test_recover_skips_stray_directory_names(tmp_path: Path, monkeypatch: Any) -> None:
    # A directory under harness_outputs whose run.json does not point back at
    # this iteration must not be reaped (it may belong to another run).
    repo_root, session_id, runs_dir = _build_interrupted_iteration(
        tmp_path, run_ids=["run_real"]
    )
    stray = runs_dir / "run_real" / "run.json"
    stray.write_text(
        json.dumps({"run_id": "run_real", "session_output_dir": "/elsewhere"}),
        encoding="utf-8",
    )
    reaper = _FakeReaperModule(["drained"])
    monkeypatch.setattr(
        recovery_module, "_load_reaper", lambda: (reaper, _FakeThConfig(runs_dir))
    )
    outcome = recover_interrupted_iteration(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id="implement",
        policy="drain",
        drain_timeout_s=5,
    )
    assert outcome.reaped_runs == 0
    assert reaper.calls == []


def test_recover_raises_when_orphans_may_still_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # An unsettled outcome (agent may still be running) must block dispatch:
    # two writers on one checkout is exactly what recovery exists to prevent.
    from loopy_loop.recovery import RecoveryIncompleteError

    repo_root, session_id, runs_dir = _build_interrupted_iteration(
        tmp_path, run_ids=["run_a"]
    )
    reaper = _FakeReaperModule(["kill_failed_still_running", "drained"])
    monkeypatch.setattr(
        recovery_module, "_load_reaper", lambda: (reaper, _FakeThConfig(runs_dir))
    )
    with pytest.raises(RecoveryIncompleteError, match="may still be running"):
        recover_interrupted_iteration(
            repo_root=repo_root,
            session_id=session_id,
            iteration=1,
            workflow_id="implement",
            policy="drain",
            drain_timeout_s=5,
        )
    # The salvage record still documents what WAS handled before the refusal.
    salvage_path = (
        repo_root
        / ".loopy_loop"
        / "sessions"
        / session_id
        / "iterations"
        / "0001_implement"
        / SALVAGE_FILENAME
    )
    payload = json.loads(salvage_path.read_text())
    assert payload["settled_workers"] == 1
    assert payload["unsettled_workers"] == 1


def test_stale_finished_from_other_identified_worker_is_refused(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M8: a stale /finished from a DIFFERENT identified worker must not be
    # handed the live task (that would start a second executor).
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    body = RegisterRequest(worker=WorkerIdentity(**LOCAL_IDENTITY)).model_dump()
    task = client.post("/register", json=body).json()
    other = {"hostname": socket.gethostname(), "pid": 777, "starttime": "lstart:x"}
    stale = {
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": 999,  # mismatch -> stale path
        "success": True,
        "worker": other,
    }
    response = client.post("/finished", json=stale)
    assert response.status_code == 409
    assert "belongs to worker" in response.json()["detail"]
    # An unidentified stale caller is refused too (identity is required at
    # /register, so every live task has a recorded owner):
    stale_anonymous = {**stale}
    del stale_anonymous["worker"]
    response = client.post("/finished", json=stale_anonymous)
    assert response.status_code == 409
    # The OWNER's stale call still gets the live-task replay:
    stale_owner = {**stale, "worker": LOCAL_IDENTITY}
    response = client.post("/finished", json=stale_owner)
    assert response.status_code == 200
    assert response.json()["iteration"] == task["iteration"]


def test_stale_finished_replays_for_pre_upgrade_ownerless_task(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # A task persisted by a pre-identity version has no recorded owner; the
    # legacy stale-replay behavior is preserved for that one resume.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    body = RegisterRequest(worker=WorkerIdentity(**LOCAL_IDENTITY)).model_dump()
    task = client.post("/register", json=body).json()
    store = StateStore(repo_root=repo_root)
    state = store.read_state()
    assert state is not None and state.current_task is not None
    state.current_task = state.current_task.model_copy(update={"worker": None})
    store.write_state(state=state)
    stale = {
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": 999,
        "success": True,
        "worker": {"hostname": "someone-else", "pid": 1, "starttime": None},
    }
    response = client.post("/finished", json=stale)
    assert response.status_code == 200
    assert response.json()["iteration"] == task["iteration"]


def test_register_requires_worker_identity(repo_builder: Any, monkeypatch: Any) -> None:
    # Breaking change (0.3): a register without identity fails fast with a
    # clear message instead of silently degrading recovery guarantees.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    response = client.post("/register", json={})
    assert response.status_code == 400
    assert "worker identity is required" in response.json()["detail"]
    response = client.post("/register")
    assert response.status_code == 400


def test_config_snapshot_on_the_wire_has_no_recovery_fields(
    repo_builder: Any, monkeypatch: Any
) -> None:
    # M7: released workers validate the snapshot with extra="forbid"; new
    # response fields would crash them. Recovery settings stay coordinator-only.
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"recovery_policy": "reap"})
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=REGISTER_BODY).json()
    assert "recovery_policy" not in task["config_snapshot"]
    assert "recovery_drain_timeout_s" not in task["config_snapshot"]
