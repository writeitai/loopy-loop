from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from fastapi.testclient import TestClient
import pytest
import yaml

from loopy_loop.assignments import repository_id
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.events import events_path
from loopy_loop.events import read_events
from loopy_loop.models import LoopState
from loopy_loop.models import REQUIRED_V3_WORKER_CAPABILITIES
from loopy_loop.models import utc_now
from loopy_loop.models import WorkflowSetContract
from loopy_loop.sessions import child_outcomes_dir_path
from loopy_loop.sessions import child_requests_pending_dir_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import eval_receipts_dir_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import handoff_path
from loopy_loop.sessions import session_outcome_path
from loopy_loop.sessions import state_path
from loopy_loop.state_store import StateStore
from tests.test_recursive_loop_contract_v2 import _init_git_repo
from tests.test_recursive_loop_contract_v2 import _write_valid_eval_bundle

_WORKER = {"hostname": "v3-contract-host", "pid": 303003, "starttime": None}
_FULL_SHA256 = "sha256:" + "a" * 64


def _workflow(
    *, priority: int = 0, run_every: int = 1, emits_goal_check: bool = False
) -> dict[str, object]:
    """Return one small workflow fixture with deterministic scheduling priority."""

    return {
        "prompt": "Perform the assigned durable role.",
        "config": {
            "enabled": True,
            "priority": priority,
            "run_every": run_every,
            "must_follow": None,
            "not_before_iteration": 0,
            "emits_goal_check": emits_goal_check,
            "description": "Perform the assigned durable role.",
        },
    }


def _v3_contract(
    *,
    workflow_ids: list[str],
    completion_role: str,
    check_runner_roles: list[str] | None = None,
    child_interface: str = "none",
    currency_outputs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Return a minimal v3 contract with one durable orchestrator."""

    roles = dict.fromkeys([*workflow_ids, completion_role])
    contract: dict[str, object] = {
        "schema_version": 1,
        "session_protocol_version": 3,
        "layer_kind": "delivery",
        "roles": {
            role: {"responsibility": f"Own the {role} responsibility."}
            for role in roles
        },
        "state": [],
        "eval": {},
        "orchestration": {
            "completion_role": completion_role,
            "plan_owner": completion_role,
            "handoff_owner": completion_role,
            "task_acceptance_owner": completion_role,
            "child_acceptance_owner": (
                completion_role if child_interface == "recursive" else None
            ),
        },
        "evaluation": {
            "advisory": True,
            "check_author_roles": [],
            "check_runner_roles": check_runner_roles or [],
        },
        "terminal_blocker_reporting_roles": list(roles),
        "child_interface": child_interface,
    }
    if currency_outputs is not None:
        contract["currency_outputs"] = currency_outputs
    return contract


def _write_contract(
    *, repo_root: Path, workflow_set: str, contract: dict[str, object]
) -> None:
    """Write one explicit workflow-set contract beside its workflows."""

    root = repo_root / ".loopy_loop" / "workflow_sets" / workflow_set
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("contract.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
    )


def _write_workflow_set(
    *,
    repo_root: Path,
    workflow_set: str,
    workflows: dict[str, dict[str, object]],
    contract: dict[str, object],
) -> None:
    """Materialize an additional workflow set used by a child session."""

    workflows_root = (
        repo_root / ".loopy_loop" / "workflow_sets" / workflow_set / "workflows"
    )
    for workflow_id, definition in workflows.items():
        workflow_root = workflows_root / workflow_id
        workflow_root.mkdir(parents=True, exist_ok=True)
        workflow_root.joinpath("prompt.txt").write_text(
            str(definition["prompt"]), encoding="utf-8"
        )
        workflow_root.joinpath("config.yaml").write_text(
            yaml.safe_dump(definition["config"], sort_keys=False), encoding="utf-8"
        )
    _write_contract(repo_root=repo_root, workflow_set=workflow_set, contract=contract)


def _build_v3_repo(
    *,
    repo_builder: Any,
    workflows: dict[str, dict[str, object]],
    completion_role: str,
    check_runner_roles: list[str] | None = None,
    child_interface: str = "none",
    root_config: dict[str, object] | None = None,
    currency_outputs: list[dict[str, object]] | None = None,
) -> Path:
    """Create a root repository whose selected workflow set uses protocol v3."""

    repo_root = repo_builder(workflows=workflows, root_config=root_config)
    _write_contract(
        repo_root=repo_root,
        workflow_set="main",
        contract=_v3_contract(
            workflow_ids=list(workflows),
            completion_role=completion_role,
            check_runner_roles=check_runner_roles,
            child_interface=child_interface,
            currency_outputs=currency_outputs,
        ),
    )
    return repo_root


def _register_body(
    *, repo_root: Path, protocol_version: int = 3, capabilities: list[str] | None = None
) -> dict[str, object]:
    """Return the repository-bound protocol-v3 worker registration body."""

    return {
        "worker": _WORKER,
        "worker_protocol_version": protocol_version,
        "capabilities": (
            sorted(REQUIRED_V3_WORKER_CAPABILITIES)
            if capabilities is None
            else capabilities
        ),
        "repo_root": str(repo_root.resolve()),
        "repository_id": repository_id(repo_root=repo_root),
    }


def _finish_body(*, task: dict[str, Any], success: bool = True) -> dict[str, object]:
    """Bind a completion request to the exact v3 assignment and worker."""

    assignment_path = Path(str(task["assignment_path"]))
    assert assignment_path.is_file()
    return {
        "worker": _WORKER,
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": task["iteration"],
        "attempt_id": task["attempt_id"],
        "repository_id": task["repository_id"],
        "assignment_sha256": file_sha256(path=assignment_path),
        "success": success,
        "text": "completed" if success else None,
        "error": None if success else "harness failed",
    }


def _read_state(*, repo_root: Path, session_id: str) -> LoopState:
    """Read one session's durable state and require it to exist."""

    state = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=session_id),
    ).read_state()
    assert state is not None
    return state


def _write_goal_met_control(
    *,
    repo_root: Path,
    task: dict[str, Any],
    producer_workflow_id: str | None = None,
    producer_attempt_id: str | None = None,
    eval_receipt_refs: list[str] | None = None,
    handoff_ref: str | None = None,
) -> None:
    """Write a v3 goal-met request with caller-selected producer identity."""

    payload = {
        "schema_version": 3,
        "control_id": f"control-{task['attempt_id']}",
        "state": "stopped",
        "reason": "The durable orchestrator judges this layer complete.",
        "stop_reason": "goal_met",
        "producer": {
            "session_id": task["session_id"],
            "workflow_id": producer_workflow_id or task["workflow_id"],
            "attempt_id": producer_attempt_id or task["attempt_id"],
        },
        "eval_receipt_refs": eval_receipt_refs or [],
        "evidence_refs": [],
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
    }
    if handoff_ref is not None:
        payload["handoff_ref"] = handoff_ref
    control_path(repo_root=repo_root, session_id=str(task["session_id"])).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _write_handoff(
    *,
    repo_root: Path,
    task: dict[str, Any],
    state: LoopState,
    producer_attempt_id: str | None = None,
    revision: int = 1,
    summary: str = "The assigned outcome is ready.",
    updated_at: str | None = None,
) -> Path:
    """Write one structurally valid handoff with selectable attempt provenance."""

    path = handoff_path(repo_root=repo_root, session_id=str(task["session_id"]))
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": task["session_id"],
                "goal_sha256": state.goal_hash,
                "revision": revision,
                "producer": {
                    "workflow_id": task["workflow_id"],
                    "attempt_id": producer_attempt_id or task["attempt_id"],
                },
                "summary": summary,
                "accepted_outcomes": ["assigned-outcome"],
                "open_work": [],
                "risks": [],
                "decision_refs": [],
                "evidence_refs": [],
                "delivery_refs": [],
                "eval_refs": [],
                "updated_at": (
                    updated_at
                    if updated_at is not None
                    else utc_now().isoformat().replace("+00:00", "Z")
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _write_nonpassing_receipt(*, repo_root: Path, task: dict[str, Any]) -> None:
    """Write a schema-valid failed observation with deliberately absent artifacts."""

    receipt_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=str(task["session_id"]))
        / "nonpassing.json"
    )
    raw_ref = "session:/eval_receipts/nonpassing.raw.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "eval_id": "nonpassing",
                "subject": {
                    "root_session_id": task["session_id"],
                    "session_id": task["session_id"],
                    "goal_hash": _read_state(
                        repo_root=repo_root, session_id=str(task["session_id"])
                    ).goal_hash,
                    "git_commit": None,
                    "dirty_tree_digest": None,
                },
                "producer": {
                    "workflow_id": task["workflow_id"],
                    "iteration": task["iteration"],
                    "attempt_id": task["attempt_id"],
                    "harness_run_id": "run-nonpassing",
                },
                "checks": [
                    {
                        "check_id": "judge-result",
                        "definition_sha256": _FULL_SHA256,
                        "kind": "harness_judge",
                    }
                ],
                "judge": {
                    "provider": "test",
                    "model": "test-model",
                    "reasoning_effort": "high",
                },
                "check_results": [
                    {
                        "check_id": "judge-result",
                        "passed": False,
                        "reason": "More work remains.",
                    }
                ],
                "verdict": {"goal_met": False, "reason": "More work remains."},
                "canonical_report_ref": ("session:/eval_receipts/nonpassing.report.md"),
                "canonical_report_sha256": _FULL_SHA256,
                "raw_report_refs": [raw_ref],
                "raw_report_sha256s": {raw_ref: _FULL_SHA256},
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _start_v3_child_session(
    *, repo_builder: Any, root_config: dict[str, object] | None = None
) -> tuple[Path, TestClient, dict[str, Any], dict[str, Any], str]:
    """Dispatch one protocol-v3 child beneath a protocol-v3 parent."""

    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"planner": _workflow()},
        completion_role="planner",
        child_interface="recursive",
        root_config=root_config,
    )
    child_workflows = {"outer": _workflow()}
    _write_workflow_set(
        repo_root=repo_root,
        workflow_set="child_set",
        workflows=child_workflows,
        contract=_v3_contract(
            workflow_ids=list(child_workflows), completion_role="outer"
        ),
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post(
        "/register", json=_register_body(repo_root=repo_root)
    ).json()
    request_id = "phase-foundation"
    child_requests_pending_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    ).joinpath(f"{request_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "request_id": request_id,
                "workflow_set": "child_set",
                "origin": {
                    "parent_attempt_id": parent_task["attempt_id"],
                    "parent_work_item_id": "phase-0",
                    "supersedes_request_id": None,
                },
                "assignment": {
                    "goal": "Make the development foundations ready.",
                    "completion_criteria": ["Foundation outcome is evidenced."],
                    "stop_criteria": [],
                    "constraints": [],
                    "deliverables": [],
                    "required_evidence": [],
                },
                "inputs": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    child_response = client.post("/finished", json=_finish_body(task=parent_task))
    assert child_response.status_code == 200
    child_task = child_response.json()
    assert child_task["workflow_set"] == "child_set"
    return repo_root, client, parent_task, child_task, request_id


def test_v3_registration_requires_version_and_capability_before_dispatch(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v3 session refuses old or incompletely capable workers before work."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow()},
        completion_role="outer",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))

    old_worker = client.post(
        "/register", json=_register_body(repo_root=repo_root, protocol_version=2)
    )
    assert old_worker.status_code == 426
    assert "protocol v3" in old_worker.json()["detail"]

    missing_capability = next(iter(REQUIRED_V3_WORKER_CAPABILITIES))
    incomplete_worker = client.post(
        "/register",
        json=_register_body(
            repo_root=repo_root,
            capabilities=sorted(REQUIRED_V3_WORKER_CAPABILITIES - {missing_capability}),
        ),
    )
    assert incomplete_worker.status_code == 426
    assert missing_capability in incomplete_worker.json()["detail"]

    accepted = client.post("/register", json=_register_body(repo_root=repo_root))
    assert accepted.status_code == 200
    task = accepted.json()
    assert task["action"] == "run"
    assert task["coordinator_protocol_version"] == 3
    assert set(task["required_capabilities"]) == REQUIRED_V3_WORKER_CAPABILITIES


def test_v3_contract_rejects_non_advisory_evaluation() -> None:
    """Protocol v3 cannot turn optional observations into an eval gate."""

    contract = _v3_contract(workflow_ids=["outer"], completion_role="outer")
    evaluation = contract["evaluation"]
    assert isinstance(evaluation, dict)
    evaluation["advisory"] = False

    with pytest.raises(ValueError, match="evaluation must remain advisory"):
        WorkflowSetContract.model_validate(contract)


def test_v3_stale_finished_accepts_valid_v3_worker_handshake(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable retry may redispatch after a protocol-v3 worker handshake."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow()},
        completion_role="outer",
    )
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    stale_task = client.post(
        "/register", json=_register_body(repo_root=repo_root)
    ).json()
    store = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=str(stale_task["session_id"])
        ),
    )
    state = store.read_state()
    assert state is not None
    state.current_task = None
    store.write_state(state=state)

    response = client.post("/finished", json=_finish_body(task=stale_task))

    assert response.status_code == 200
    assert response.json()["action"] == "run"
    assert response.json()["coordinator_protocol_version"] == 3


def test_v3_api_assignment_exposes_rosters_scheduler_and_layer_state(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatched v3 assignment names the inspectable semantic context."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow()},
        completion_role="outer",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    assignment = json.loads(Path(task["assignment_path"]).read_text(encoding="utf-8"))

    required = {
        "layer_plan",
        "layer_tasks",
        "layer_current_state",
        "layer_decisions",
        "layer_eval_state",
        "layer_handoff",
        "session_outcome",
        "workflow_roster",
        "scheduler_view",
        "harness_capability_roster",
    }
    assert required <= set(assignment["absolute_paths"])
    assert all(
        Path(assignment["absolute_paths"][name]).is_absolute() for name in required
    )
    assert assignment["context"]["workflow_roster"]["completion_role"] == "outer"
    assert assignment["context"]["scheduler_view"]["attempt_id"] == task["attempt_id"]


def test_v3_orchestrator_completes_without_eval_and_gets_root_outcome(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared orchestrator may complete with no eval receipt at all."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow(emits_goal_check=True)},
        completion_role="outer",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    _write_goal_met_control(repo_root=repo_root, task=task)

    response = client.post("/finished", json=_finish_body(task=task))

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "goal_met"
    state = _read_state(repo_root=repo_root, session_id=task["session_id"])
    assert state.history[-1].success is True
    assert state.goal_check_consecutive_failures == 0
    outcome = json.loads(
        session_outcome_path(
            repo_root=repo_root, session_id=task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert outcome["stop_reason"] == "goal_met"
    assert outcome["eval_refs"] == []
    assert outcome["handoff"]["status"] == "missing"
    assert outcome["fallback_summary"]["source"] == "control_reason"


@pytest.mark.parametrize(
    argnames="observation", argvalues=["missing", "malformed", "nonpassing"]
)
def test_v3_eval_observations_never_rewrite_harness_success(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, observation: str
) -> None:
    """Missing, malformed, and failed advisory eval output remain observations."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    workflows = {
        "outer": _workflow(priority=10),
        "eval_runner": _workflow(priority=100, emits_goal_check=True),
    }
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=workflows,
        completion_role="outer",
        check_runner_roles=["eval_runner"],
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    assert task["workflow_id"] == "eval_runner"
    receipts = eval_receipts_dir_path(
        repo_root=repo_root, session_id=task["session_id"]
    )
    if observation == "malformed":
        receipts.joinpath("malformed.json").write_text("{", encoding="utf-8")
    elif observation == "nonpassing":
        _write_nonpassing_receipt(repo_root=repo_root, task=task)

    response = client.post("/finished", json=_finish_body(task=task))

    assert response.status_code == 200
    state = _read_state(repo_root=repo_root, session_id=task["session_id"])
    assert state.history[-1].success is True
    assert state.history[-1].error is None
    assert state.goal_check_consecutive_failures == 0
    assert state.stop_reason is None


@pytest.mark.parametrize(
    argnames=("first_role", "producer_role", "producer_attempt"),
    argvalues=[
        ("eval_runner", "eval_runner", None),
        ("outer", "outer", "stale-attempt"),
    ],
    ids=["wrong-completion-role", "stale-attempt"],
)
def test_v3_rejects_false_completion_authority(
    repo_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
    first_role: str,
    producer_role: str,
    producer_attempt: str | None,
) -> None:
    """Only the exact current attempt of the declared orchestrator may close."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    workflows = {
        "outer": _workflow(priority=10),
        "eval_runner": _workflow(priority=100 if first_role == "eval_runner" else 0),
    }
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=workflows,
        completion_role="outer",
        check_runner_roles=["eval_runner"],
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    assert task["workflow_id"] == first_role
    _write_goal_met_control(
        repo_root=repo_root,
        task=task,
        producer_workflow_id=producer_role,
        producer_attempt_id=producer_attempt,
    )

    response = client.post("/finished", json=_finish_body(task=task))

    assert response.status_code == 200
    state = _read_state(repo_root=repo_root, session_id=task["session_id"])
    assert state.goal_met is False
    assert state.history[-1].success is False
    assert state.history[-1].error == "invalid_control_output"
    assert state.control_protocol_consecutive_failures == 1


def test_v3_rejects_cited_eval_that_engine_did_not_accept(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional eval evidence becomes citable only after engine acceptance."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow()},
        completion_role="outer",
        check_runner_roles=["outer"],
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    _write_nonpassing_receipt(repo_root=repo_root, task=task)
    receipt_ref = "session:/eval_receipts/nonpassing.json"
    _write_goal_met_control(
        repo_root=repo_root, task=task, eval_receipt_refs=[receipt_ref]
    )

    response = client.post("/finished", json=_finish_body(task=task))

    assert response.status_code == 200
    state = _read_state(repo_root=repo_root, session_id=task["session_id"])
    assert state.goal_met is False
    assert receipt_ref not in state.accepted_eval_receipt_seals
    assert state.history[-1].error == "invalid_control_output"


def test_v3_rejects_cited_handoff_with_false_attempt_provenance(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plausible handoff cannot be cited unless a real attempt produced it."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow()},
        completion_role="outer",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=str(task["session_id"]))
    _write_handoff(
        repo_root=repo_root,
        task=task,
        state=state,
        producer_attempt_id="invented-attempt",
    )
    _write_goal_met_control(
        repo_root=repo_root,
        task=task,
        handoff_ref="session:/project_state/handoff.json",
    )

    response = client.post("/finished", json=_finish_body(task=task))

    assert response.status_code == 200
    rejected = _read_state(repo_root=repo_root, session_id=str(task["session_id"]))
    assert rejected.goal_met is False
    assert rejected.latest_handoff_observation is not None
    assert rejected.latest_handoff_observation.status == "invalid"
    assert rejected.history[-1].error == "invalid_control_output"


def test_v3_outer_may_cite_accepted_nonpassing_eval_from_prior_attempt(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sealed advisory observation remains citable across attempts and need not pass."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={
            "outer": _workflow(priority=10),
            "eval_runner": _workflow(
                priority=100, run_every=100, emits_goal_check=True
            ),
        },
        completion_role="outer",
        check_runner_roles=["eval_runner"],
    )
    _init_git_repo(repo_root)
    subprocess.run(
        args=["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        args=["git", "config", "user.name", "Test User"], cwd=repo_root, check=True
    )
    subprocess.run(
        args=["git", "commit", "--allow-empty", "-qm", "baseline"],
        cwd=repo_root,
        check=True,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    eval_task = client.post(
        "/register", json=_register_body(repo_root=repo_root)
    ).json()
    assert eval_task["workflow_id"] == "eval_runner"
    eval_state = _read_state(repo_root=repo_root, session_id=eval_task["session_id"])
    receipt, receipt_ref, trace_root = _write_valid_eval_bundle(
        repo_root=repo_root, task=eval_task, state=eval_state
    )

    receipt["check_results"] = [
        {
            "check_id": "judge-goal",
            "passed": False,
            "reason": "The eval recommends more work.",
        }
    ]
    receipt["verdict"] = {"goal_met": False, "reason": "The eval recommends more work."}
    canonical_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=eval_task["session_id"])
        / "eval-valid.report.md"
    )
    canonical_path.write_text("# Non-passing evaluation\n", encoding="utf-8")
    receipt["canonical_report_sha256"] = file_sha256(path=canonical_path)
    raw_path = trace_root / "eval" / "report.json"
    raw_report = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_report["run_passed"] = False
    raw_report["checks"][0]["status"] = "failed"
    raw_report["checks"][0]["exit_code"] = 1
    raw_path.write_text(json.dumps(raw_report), encoding="utf-8")
    raw_ref = receipt["raw_report_refs"][0]
    receipt["raw_report_sha256s"][raw_ref] = file_sha256(path=raw_path)
    receipt_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=eval_task["session_id"])
        / "eval-valid.json"
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    outer_response = client.post("/finished", json=_finish_body(task=eval_task))

    assert outer_response.status_code == 200
    outer_task = outer_response.json()
    assert outer_task["workflow_id"] == "outer"
    accepted_state = _read_state(
        repo_root=repo_root, session_id=eval_task["session_id"]
    )
    assert accepted_state.history[-1].success is True
    assert receipt_ref in accepted_state.accepted_eval_receipt_seals
    assert accepted_state.accepted_eval_receipt_seals[
        receipt_ref
    ].receipt_sha256 == file_sha256(path=receipt_path)

    raw_path.unlink()
    _write_goal_met_control(
        repo_root=repo_root, task=outer_task, eval_receipt_refs=[receipt_ref]
    )
    completion = client.post("/finished", json=_finish_body(task=outer_task))

    assert completion.status_code == 200
    assert completion.json()["stop_reason"] == "goal_met"
    final_state = _read_state(repo_root=repo_root, session_id=eval_task["session_id"])
    assert final_state.goal_met is True
    assert final_state.history[-1].success is True


def test_v3_child_outcome_links_the_same_valid_handoff(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal child projects its topology-neutral outcome to its parent."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"planner": _workflow()},
        completion_role="planner",
        child_interface="recursive",
    )
    child_workflows = {"outer": _workflow()}
    _write_workflow_set(
        repo_root=repo_root,
        workflow_set="child_set",
        workflows=child_workflows,
        contract=_v3_contract(
            workflow_ids=list(child_workflows), completion_role="outer"
        ),
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    parent_task = client.post(
        "/register", json=_register_body(repo_root=repo_root)
    ).json()
    request_id = "phase-foundation"
    child_requests_pending_dir_path(
        repo_root=repo_root, session_id=parent_task["session_id"]
    ).joinpath(f"{request_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "request_id": request_id,
                "workflow_set": "child_set",
                "origin": {
                    "parent_attempt_id": parent_task["attempt_id"],
                    "parent_work_item_id": "phase-0",
                    "supersedes_request_id": None,
                },
                "assignment": {
                    "goal": "Make the development foundations ready.",
                    "completion_criteria": ["Foundation outcome is evidenced."],
                    "stop_criteria": [],
                    "constraints": [],
                    "deliverables": [],
                    "required_evidence": [],
                },
                "inputs": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    child_response = client.post("/finished", json=_finish_body(task=parent_task))
    assert child_response.status_code == 200
    child_task = child_response.json()
    assert child_task["workflow_set"] == "child_set"
    child_state = _read_state(repo_root=repo_root, session_id=child_task["session_id"])
    child_assignment = json.loads(
        Path(child_task["assignment_path"]).read_text(encoding="utf-8")
    )
    assert child_assignment["context"]["scheduler_view"]["state_revision"] == (
        child_state.state_revision
    )
    _write_handoff(repo_root=repo_root, task=child_task, state=child_state)
    _write_goal_met_control(
        repo_root=repo_root,
        task=child_task,
        handoff_ref="session:/project_state/handoff.json",
    )

    resumed = client.post("/finished", json=_finish_body(task=child_task))

    assert resumed.status_code == 200
    child_outcome_file = session_outcome_path(
        repo_root=repo_root, session_id=child_task["session_id"]
    )
    child_outcome = json.loads(child_outcome_file.read_text(encoding="utf-8"))
    assert child_outcome["handoff"]["status"] == "valid"
    parent_projection = json.loads(
        child_outcomes_dir_path(
            repo_root=repo_root, session_id=parent_task["session_id"]
        )
        .joinpath(f"{request_id}.json")
        .read_text(encoding="utf-8")
    )
    assert parent_projection["session_outcome_ref"] == (
        f"session:{child_task['session_id']}:/session_outcome.json"
    )
    assert parent_projection["session_outcome_sha256"] == file_sha256(
        path=child_outcome_file
    )


def test_v3_terminal_outcome_reuses_accepted_control_and_handoff_bytes(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later file edits cannot rewrite an accepted terminal outcome basis."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={"outer": _workflow()},
        completion_role="outer",
    )
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=str(task["session_id"]))
    layer_handoff_path = _write_handoff(repo_root=repo_root, task=task, state=state)
    _write_goal_met_control(
        repo_root=repo_root,
        task=task,
        handoff_ref="session:/project_state/handoff.json",
    )
    completed = client.post("/finished", json=_finish_body(task=task))
    assert completed.status_code == 200
    assert completed.json()["stop_reason"] == "goal_met"

    terminal_state = _read_state(
        repo_root=repo_root, session_id=str(task["session_id"])
    )
    assert terminal_state.accepted_terminal_control is not None
    assert terminal_state.accepted_handoff_snapshot is not None
    outcome_path = session_outcome_path(
        repo_root=repo_root, session_id=str(task["session_id"])
    )
    original_outcome = outcome_path.read_text(encoding="utf-8")
    terminal_control_path = control_path(
        repo_root=repo_root, session_id=str(task["session_id"])
    )
    terminal_control_path.write_text("{}", encoding="utf-8")
    layer_handoff_path.write_text("{}", encoding="utf-8")

    refreshed = app.state.service._ensure_session_outcome(state=terminal_state)

    assert refreshed is not None
    assert file_sha256(path=terminal_control_path) == (
        terminal_state.accepted_terminal_control.sha256
    )
    assert file_sha256(path=layer_handoff_path) == (
        terminal_state.accepted_handoff_snapshot.sha256
    )
    assert outcome_path.read_text(encoding="utf-8") == original_outcome


def test_v3_non_control_child_stop_writes_outcome_and_resumes_parent(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every factual child terminal reason unwinds through one outcome shape."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root, client, parent_task, child_task, request_id = _start_v3_child_session(
        repo_builder=repo_builder, root_config={"workflow_consecutive_failures_cap": 1}
    )

    resumed = client.post(
        "/finished", json=_finish_body(task=child_task, success=False)
    )

    assert resumed.status_code == 200
    assert resumed.json()["action"] == "stop"
    assert resumed.json()["stop_reason"] == "no_eligible_workflow"
    child_outcome_file = session_outcome_path(
        repo_root=repo_root, session_id=str(child_task["session_id"])
    )
    outcome = json.loads(child_outcome_file.read_text(encoding="utf-8"))
    assert outcome["terminal_status"] == "failed"
    assert outcome["stop_reason"] == "workflow_failure_cap"
    assert outcome["control"] is None
    assert outcome["fallback_summary"] == {
        "source": "engine_stop_reason",
        "text": "workflow_failure_cap",
    }
    parent_projection = json.loads(
        child_outcomes_dir_path(
            repo_root=repo_root, session_id=str(parent_task["session_id"])
        )
        .joinpath(f"{request_id}.json")
        .read_text(encoding="utf-8")
    )
    assert parent_projection["session_outcome_sha256"] == file_sha256(
        path=child_outcome_file
    )
    parent_state = _read_state(
        repo_root=repo_root, session_id=str(parent_task["session_id"])
    )
    assert parent_state.active_child_session_id is None
    assert parent_state.stop_reason == "no_eligible_workflow"


# ---------------------------------------------------------------------------
# D13/D14/D15: handoff currency, advisory eval provenance, raw-trace exposure.
# ---------------------------------------------------------------------------

_HANDOFF_CURRENCY: list[dict[str, object]] = [
    {"path": "project_state/handoff.json", "owner_role": "outer", "kind": "handoff"}
]


def _outer_inner_workflows() -> dict[str, dict[str, object]]:
    """Return the alternating outer/inner roles used by the currency tests."""

    return {"outer": _workflow(priority=10), "inner": _workflow(priority=0)}


def _advance_to_outer(*, client: TestClient, task: dict[str, Any]) -> dict[str, Any]:
    """Finish `task` and return the next 'outer' assignment (running interleaved roles).

    The stock cadence alternates outer/inner, so the standing orchestrator's next
    turn is reached by finishing any interleaved inner attempt.
    """

    response = client.post("/finished", json=_finish_body(task=task)).json()
    while response.get("action") == "run" and response["workflow_id"] != "outer":
        response = client.post("/finished", json=_finish_body(task=response)).json()
    assert response.get("action") == "run"
    assert response["workflow_id"] == "outer"
    return response


def _events_of(*, repo_root: Path, session_id: str, event_type: str) -> list[Any]:
    """Return every emitted event of one type for a session."""

    events = read_events(path=events_path(repo_root=repo_root, session_id=session_id))
    return [event for event in events if event["type"] == event_type]


@pytest.mark.parametrize("cite", [True, False], ids=["cited", "uncited"])
def test_currency_completion_rejected_when_handoff_not_restamped(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, cite: bool
) -> None:
    """A1: goal_met resting on a stale (past-attempt) handoff is rejected.

    The rejection holds whether or not the completion cites handoff_ref, closing
    the uncited-goal_met bypass (handoff_ref is optional on control).
    """

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    assert task0["workflow_id"] == "outer"
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state)

    outer2 = _advance_to_outer(client=client, task=task0)
    assert outer2["attempt_id"] != task0["attempt_id"]
    # outer2 declares completion WITHOUT re-stamping the handoff (still task0's).
    _write_goal_met_control(
        repo_root=repo_root,
        task=outer2,
        handoff_ref="session:/project_state/handoff.json" if cite else None,
    )
    response = client.post("/finished", json=_finish_body(task=outer2))

    assert response.status_code == 200
    final = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    assert final.goal_met is False
    assert final.history[-1].success is False
    assert final.history[-1].error == "invalid_control_output"
    assert final.control_protocol_consecutive_failures == 1
    # A1 rides the existing control-reject path, which also ticks the general
    # per-workflow failure cap (documented dual-counter, pre-existing behavior).
    assert final.workflow_consecutive_failures.get("outer", 0) == 1


def test_currency_completion_accepted_when_handoff_restamped(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: goal_met resting on a handoff the completing attempt re-stamped closes."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state)

    outer2 = _advance_to_outer(client=client, task=task0)
    # outer2 re-stamps the handoff with its own identity (provenance-only re-stamp
    # at the same revision) and cites it.
    _write_handoff(repo_root=repo_root, task=outer2, state=state)
    _write_goal_met_control(
        repo_root=repo_root,
        task=outer2,
        handoff_ref="session:/project_state/handoff.json",
    )
    response = client.post("/finished", json=_finish_body(task=outer2))

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "goal_met"
    final = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    assert final.goal_met is True
    assert final.history[-1].success is True


def test_currency_completion_unaffected_without_declaration(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in: a contract with no currency_outputs keeps prior completion behavior."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state)

    outer2 = _advance_to_outer(client=client, task=task0)
    # Stale handoff (task0's) + goal_met, but no currency declaration → accepted.
    _write_goal_met_control(repo_root=repo_root, task=outer2)
    response = client.post("/finished", json=_finish_body(task=outer2))

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "goal_met"
    final = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    assert final.goal_met is True


def test_currency_handoff_stale_diagnostic_is_pure(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2: an un-re-stamped owner finish emits handoff_stale with no failure."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state)
    handoff_file = handoff_path(repo_root=repo_root, session_id=task0["session_id"])

    outer2 = _advance_to_outer(client=client, task=task0)
    bytes_before = handoff_file.read_bytes()
    # outer2 finishes successfully but never re-stamps the handoff, no control.
    client.post("/finished", json=_finish_body(task=outer2))

    stale_events = _events_of(
        repo_root=repo_root, session_id=task0["session_id"], event_type="handoff_stale"
    )
    assert len(stale_events) == 1
    final = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    stale_finish = next(
        entry for entry in final.history if entry.attempt_id == outer2["attempt_id"]
    )
    assert stale_finish.success is True
    assert final.control_protocol_consecutive_failures == 0
    # Pure diagnostic: it touches neither the control cap nor the general cap.
    assert final.workflow_consecutive_failures.get("outer", 0) == 0
    assert handoff_file.read_bytes() == bytes_before


def test_currency_no_stale_diagnostic_on_crashed_attempt(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Currency diagnostics run only after a mechanically successful attempt."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state)
    outer2 = _advance_to_outer(client=client, task=task0)

    # A crashed attempt (harness failure) must not raise a currency diagnostic.
    client.post("/finished", json=_finish_body(task=outer2, success=False))

    assert not _events_of(
        repo_root=repo_root, session_id=task0["session_id"], event_type="handoff_stale"
    )


def test_provenance_only_restamp_vs_tamper_same_revision(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13: same-revision re-stamp is valid; a same-revision content change is not."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state, revision=1)

    # (a) same revision, provenance-only change by the new owner attempt → valid.
    outer2 = _advance_to_outer(client=client, task=task0)
    _write_handoff(repo_root=repo_root, task=outer2, state=state, revision=1)
    outer4 = _advance_to_outer(client=client, task=outer2)
    accepted = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    assert accepted.latest_handoff_observation is not None
    assert accepted.latest_handoff_observation.status == "valid"
    assert accepted.accepted_handoff_snapshot is not None
    assert accepted.accepted_handoff_snapshot.handoff.producer is not None
    assert (
        accepted.accepted_handoff_snapshot.handoff.producer.attempt_id
        == outer2["attempt_id"]
    )

    # (b) same revision, a real content change → non_monotonic (tamper detection).
    _write_handoff(
        repo_root=repo_root,
        task=outer4,
        state=state,
        revision=1,
        summary="A materially different summary at the same revision.",
    )
    client.post("/finished", json=_finish_body(task=outer4))
    tampered = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    assert tampered.latest_handoff_observation is not None
    assert tampered.latest_handoff_observation.status == "non_monotonic"


@pytest.mark.parametrize("write_result", [False, True], ids=["absent", "present"])
def test_advisory_currency_missing_emits_diagnostic_only(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, write_result: bool
) -> None:
    """D14: an absent advisory output emits eval_missing; never fails the run."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={
            "outer": _workflow(priority=0),
            "eval_runner": _workflow(priority=100),
        },
        completion_role="outer",
        currency_outputs=[
            *_HANDOFF_CURRENCY,
            {
                "path": "project_state/eval_results.md",
                "owner_role": "eval_runner",
                "kind": "advisory",
            },
        ],
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    assert task["workflow_id"] == "eval_runner"
    if write_result:
        result_path = (
            handoff_path(repo_root=repo_root, session_id=task["session_id"]).parent
            / "eval_results.md"
        )
        result_path.write_text("# eval result\n", encoding="utf-8")

    client.post("/finished", json=_finish_body(task=task))

    missing = _events_of(
        repo_root=repo_root, session_id=task["session_id"], event_type="eval_missing"
    )
    state = _read_state(repo_root=repo_root, session_id=task["session_id"])
    assert state.history[-1].success is True
    if write_result:
        assert not missing
    else:
        assert len(missing) == 1
        assert missing[0]["payload"]["path"] == "project_state/eval_results.md"


def test_orchestration_role_assignment_exposes_raw_root(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D15: only the orchestration role receives the read-only raw-trace root."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows={
            "outer": _workflow(priority=0),
            "eval_runner": _workflow(priority=100),
        },
        completion_role="outer",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    eval_task = client.post(
        "/register", json=_register_body(repo_root=repo_root)
    ).json()
    assert eval_task["workflow_id"] == "eval_runner"
    eval_assignment = json.loads(
        Path(eval_task["assignment_path"]).read_text(encoding="utf-8")
    )
    assert "raw_root" not in eval_assignment["absolute_paths"]

    outer_task = client.post("/finished", json=_finish_body(task=eval_task)).json()
    assert outer_task["workflow_id"] == "outer"
    outer_assignment = json.loads(
        Path(outer_task["assignment_path"]).read_text(encoding="utf-8")
    )
    assert "raw_root" in outer_assignment["absolute_paths"]
    assert outer_assignment["absolute_paths"]["raw_root"].endswith("/raw")


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "project_state/other.json", "owner_role": "outer", "kind": "handoff"},
        {
            "path": "project_state/handoff.json",
            "owner_role": "inner",
            "kind": "handoff",
        },
        {"path": "/etc/passwd", "owner_role": "outer", "kind": "advisory"},
        {"path": "../escape.md", "owner_role": "outer", "kind": "advisory"},
        {"path": "x.md", "owner_role": "ghost", "kind": "advisory"},
    ],
    ids=["handoff-path", "handoff-owner", "absolute", "traversal", "unknown-owner"],
)
def test_currency_outputs_contract_validation_rejects_bad_entries(
    entry: dict[str, object],
) -> None:
    """Contract load validates currency_outputs (path confinement + identity)."""

    contract = _v3_contract(
        workflow_ids=["outer", "inner"],
        completion_role="outer",
        currency_outputs=[entry],
    )
    with pytest.raises(ValueError):
        WorkflowSetContract.model_validate(contract)


def test_currency_outputs_contract_validation_rejects_duplicate_paths() -> None:
    """Duplicate currency_outputs paths are rejected at contract load."""

    contract = _v3_contract(
        workflow_ids=["outer", "eval_runner"],
        completion_role="outer",
        currency_outputs=[
            {"path": "project_state/x.md", "owner_role": "outer", "kind": "advisory"},
            {
                "path": "project_state/x.md",
                "owner_role": "eval_runner",
                "kind": "advisory",
            },
        ],
    )
    with pytest.raises(ValueError):
        WorkflowSetContract.model_validate(contract)


def test_currency_completion_rejected_when_no_handoff_snapshot(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: goal_met with no accepted handoff snapshot at all is rejected."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    # No handoff is ever written, so there is no accepted snapshot to rest on.
    _write_goal_met_control(
        repo_root=repo_root,
        task=task,
        handoff_ref="session:/project_state/handoff.json",
    )
    response = client.post("/finished", json=_finish_body(task=task))

    assert response.status_code == 200
    final = _read_state(repo_root=repo_root, session_id=task["session_id"])
    assert final.goal_met is False
    assert final.history[-1].success is False
    assert final.history[-1].error == "invalid_control_output"


def test_currency_non_owner_finish_emits_no_stale_diagnostic(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2: only the handoff owner is diagnosed; a non-owner finish is never stale."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(repo_root=repo_root, task=task0, state=state)

    # outer0 re-stamped and finishes; the interleaved inner attempt (a non-owner)
    # then finishes without touching the handoff.
    inner1 = client.post("/finished", json=_finish_body(task=task0)).json()
    assert inner1["workflow_id"] == "inner"
    client.post("/finished", json=_finish_body(task=inner1))

    assert not _events_of(
        repo_root=repo_root, session_id=task0["session_id"], event_type="handoff_stale"
    )


def test_provenance_restamp_rejected_when_timestamp_moves_backward(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13: a same-revision re-stamp whose updated_at regresses is non_monotonic."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = _build_v3_repo(
        repo_builder=repo_builder,
        workflows=_outer_inner_workflows(),
        completion_role="outer",
        currency_outputs=_HANDOFF_CURRENCY,
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task0 = client.post("/register", json=_register_body(repo_root=repo_root)).json()
    state = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    _write_handoff(
        repo_root=repo_root,
        task=task0,
        state=state,
        revision=1,
        updated_at="2026-07-22T12:00:00Z",
    )

    outer2 = _advance_to_outer(client=client, task=task0)
    # Same revision, current owner, but the timestamp regresses → not a valid
    # provenance-only re-stamp.
    _write_handoff(
        repo_root=repo_root,
        task=outer2,
        state=state,
        revision=1,
        updated_at="2026-07-22T11:00:00Z",
    )
    client.post("/finished", json=_finish_body(task=outer2))

    final = _read_state(repo_root=repo_root, session_id=task0["session_id"])
    assert final.latest_handoff_observation is not None
    assert final.latest_handoff_observation.status == "non_monotonic"
