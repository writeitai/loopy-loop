from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from fastapi.testclient import TestClient
import pytest
import yaml

from loopy_loop.assignments import build_attempt_assignment
from loopy_loop.assignments import repository_id
from loopy_loop.coordinator_app import ChildLedgerError
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.git_evidence import capture_git_evidence
from loopy_loop.models import ControlSignal
from loopy_loop.models import CurrentTask
from loopy_loop.models import EvalReceipt
from loopy_loop.models import FinishedRequest
from loopy_loop.models import IterationResult
from loopy_loop.models import LoopState
from loopy_loop.models import TaskResponse
from loopy_loop.models import utc_now
from loopy_loop.models import WorkerIdentity
from loopy_loop.models import WorkflowSnapshotDescriptor
from loopy_loop.recovery import RecoveryOutcome
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import attempt_trace_dir_path
from loopy_loop.sessions import child_outcomes_dir_path
from loopy_loop.sessions import child_requests_pending_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import control_rejected_dir_path
from loopy_loop.sessions import eval_receipts_dir_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import git_receipts_dir_path
from loopy_loop.sessions import goal_check_path
from loopy_loop.sessions import goal_contract_path
from loopy_loop.sessions import iteration_dir_path
from loopy_loop.sessions import pending_finished_request_path
from loopy_loop.sessions import protocol_failures_dir_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import state_path
from loopy_loop.sessions import trace_finalization_outbox_dir_path
from loopy_loop.sessions import trace_seal_receipt_path
from loopy_loop.sessions import workflow_contract_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.state_store import StateStore
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import read_trace_manifest
from loopy_loop.tracing import seal_attempt_trace
from loopy_loop.tracing import update_trace_manifest
from loopy_loop.tracing import verify_trace_integrity
from loopy_loop.worker import _run_task
from loopy_loop.worker import FatalAssignmentError

_WORKER = {"hostname": "contract-test-host", "pid": 919191, "starttime": None}
_V2_CAPABILITIES = [
    "assignment_v1",
    "caller_run_record_v1",
    "nested_caller_context_v1",
    "coordinator_input_v1",
    "frozen_workflow_v1",
    "nested_caller_context_v1",
    "spawn_assignment_v1",
    "trace_manifest_v1",
]
_REGISTER_V2 = {
    "worker": _WORKER,
    "worker_protocol_version": 2,
    "capabilities": _V2_CAPABILITIES,
}


def _register_v2(repo_root: Path) -> dict[str, object]:
    return {
        **_REGISTER_V2,
        "repo_root": str(repo_root.resolve()),
        "repository_id": repository_id(repo_root=repo_root),
    }


def _write_workflow_set(
    *,
    repo_root: Path,
    workflow_set: str,
    workflow_id: str,
    emits_goal_check: bool = False,
    contract: dict[str, object] | None = None,
) -> None:
    root = repo_root / ".loopy_loop" / "workflow_sets" / workflow_set
    workflow_dir = root / "workflows" / workflow_id
    workflow_dir.mkdir(parents=True, exist_ok=True)
    workflow_dir.joinpath("prompt.txt").write_text(
        f"Execute the {workflow_id} role.", encoding="utf-8"
    )
    workflow_dir.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "run_every": 1,
                "must_follow": None,
                "not_before_iteration": 0,
                "emits_goal_check": emits_goal_check,
                "description": f"Execute {workflow_id}.",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if contract is not None:
        root.joinpath("contract.yaml").write_text(
            yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
        )


def _write_v2_child_request(
    *,
    repo_root: Path,
    parent_task: dict[str, Any],
    request_id: str,
    workflow_set: str,
    goal: str,
    completion_criteria: list[str] | None = None,
    stop_criteria: list[str] | None = None,
    inputs: list[dict[str, str]] | None = None,
) -> Path:
    path = (
        child_requests_pending_dir_path(
            repo_root=repo_root, session_id=str(parent_task["session_id"])
        )
        / f"{request_id}.json"
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "request_id": request_id,
                "workflow_set": workflow_set,
                "origin": {
                    "parent_attempt_id": parent_task["attempt_id"],
                    "parent_work_item_id": f"work-{request_id}",
                    "supersedes_request_id": None,
                },
                "assignment": {
                    "goal": goal,
                    "completion_criteria": completion_criteria or [],
                    "stop_criteria": stop_criteria or [],
                    "constraints": [f"constraint-{request_id}"],
                    "deliverables": [f"deliverable-{request_id}"],
                    "required_evidence": [f"evidence-{request_id}"],
                },
                "inputs": inputs or [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _finish(
    client: TestClient, task: dict[str, Any], *, success: bool = True
) -> dict[str, Any]:
    assignment_file = Path(str(task["assignment_path"]))
    if not assignment_file.exists():
        assignment_file.write_text("{}\n", encoding="utf-8")
    response = client.post(
        "/finished",
        json={
            "worker": _WORKER,
            "workflow_id": task["workflow_id"],
            "session_id": task["session_id"],
            "iteration": task["iteration"],
            "attempt_id": task["attempt_id"],
            "repository_id": task.get("repository_id"),
            "assignment_sha256": file_sha256(assignment_file),
            "success": success,
            "text": "completed" if success else None,
            "error": None if success else "failed",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _read_state(repo_root: Path, session_id: str):
    state = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=session_id),
    ).read_state()
    assert state is not None
    return state


def _set_stop_requested(*, repo_root: Path, session_id: str) -> None:
    store = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=session_id),
    )

    def mutate(state: LoopState | None):
        assert state is not None
        state.stop_requested = True
        return state, None

    store.mutate(mutate)


def _write_terminal_blocker_control(*, repo_root: Path, task: dict[str, Any]) -> None:
    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": f"control-{task['attempt_id']}",
                "state": "stopped",
                "reason": "all autonomous routes were exhausted",
                "stop_reason": "unresolvable_error",
                "producer": {
                    "session_id": task["session_id"],
                    "workflow_id": task["workflow_id"],
                    "attempt_id": task["attempt_id"],
                },
                "attempted_routes": ["retry", "re-scope", "alternate workflow"],
                "evidence_refs": [],
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _dispatch_three_levels(
    *, repo_root: Path, client: TestClient
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root_task = client.post("/register", json=_register_v2(repo_root)).json()
    assert root_task["action"] == "run"
    _write_v2_child_request(
        repo_root=repo_root,
        parent_task=root_task,
        request_id="child-one",
        workflow_set="child_set",
        goal="Complete the middle-loop assignment",
        completion_criteria=["Middle result is integrated"],
    )
    child_task = _finish(client, root_task)
    assert child_task["action"] == "run"
    assert child_task["workflow_set"] == "child_set"

    _write_v2_child_request(
        repo_root=repo_root,
        parent_task=child_task,
        request_id="grandchild-one",
        workflow_set="grandchild_set",
        goal="Complete the inner-loop assignment",
        completion_criteria=["Inner result has evidence"],
    )
    grandchild_task = _finish(client, child_task)
    assert grandchild_task["action"] == "run"
    assert grandchild_task["workflow_set"] == "grandchild_set"
    return root_task, child_task, grandchild_task


def test_fresh_v2_manifest_has_full_goal_contract_hash_and_child_isolates_criteria(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _write_workflow_set(
        repo_root=repo_root, workflow_set="child_set", workflow_id="child_work"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    root_state = StateStore(repo_root=repo_root).read_state()
    assert root_state is not None
    assert root_state.schema_version == 2
    root_id = root_state.active_session_id

    root_contract_path = goal_contract_path(repo_root=repo_root, session_id=root_id)
    root_contract = json.loads(root_contract_path.read_text(encoding="utf-8"))
    root_manifest = json.loads(
        session_dir_path(repo_root=repo_root, session_id=root_id)
        .joinpath("session.json")
        .read_text(encoding="utf-8")
    )
    expected_full_hash = (
        "sha256:"
        + hashlib.sha256(root_state.config_snapshot.goal.encode("utf-8")).hexdigest()
    )
    assert root_state.goal_hash == expected_full_hash
    assert root_contract["goal_hash"] == expected_full_hash
    assert root_contract["completion_criteria"] == [
        "Homepage renders without errors",
        "Primary CTA is wired",
    ]
    assert root_contract["stop_criteria"] == [
        "A workflow updates session control.json to stopped"
    ]
    assert root_manifest["schema_version"] == 2
    assert root_manifest["goal_hash"] == expected_full_hash
    assert root_manifest["goal_contract_hash"] == file_sha256(root_contract_path)

    root_task = client.post("/register", json=_register_v2(repo_root)).json()
    parent_input = (
        session_dir_path(repo_root=repo_root, session_id=root_id)
        / "project_state"
        / "selected-work.json"
    )
    parent_input.write_text('{"work_item":"component"}\n', encoding="utf-8")
    _write_v2_child_request(
        repo_root=repo_root,
        parent_task=root_task,
        request_id="criteria-isolation",
        workflow_set="child_set",
        goal="Validate one isolated component",
        completion_criteria=["The isolated component passes review"],
        stop_criteria=["The isolated component is terminally blocked"],
        inputs=[
            {
                "ref": "parent:/project_state/selected-work.json",
                "sha256": file_sha256(parent_input),
            }
        ],
    )
    child_task = _finish(client, root_task)
    child_id = child_task["session_id"]
    child_contract_path = goal_contract_path(repo_root=repo_root, session_id=child_id)
    child_contract = json.loads(child_contract_path.read_text(encoding="utf-8"))
    child_manifest = json.loads(
        session_dir_path(repo_root=repo_root, session_id=child_id)
        .joinpath("session.json")
        .read_text(encoding="utf-8")
    )

    assert child_contract["completion_criteria"] == [
        "The isolated component passes review"
    ]
    assert child_contract["stop_criteria"] == [
        "The isolated component is terminally blocked"
    ]
    assert child_contract["completion_criteria"] != root_contract["completion_criteria"]
    assert child_manifest["root_session_id"] == root_id
    assert child_manifest["parent_session_id"] == root_id
    assert child_manifest["depth"] == 1
    assert child_manifest["goal_contract_hash"] == file_sha256(child_contract_path)
    source_input_hash = file_sha256(parent_input)
    expected_source_ref = f"session:{root_id}:/project_state/selected-work.json"
    frozen_input = child_contract["inputs"][0]
    assert frozen_input["ref"].startswith("session:/inputs/artifacts/input-0001-")
    assert frozen_input["sha256"] == source_input_hash
    assert child_manifest["origin"]["inputs"] == [
        {"ref": expected_source_ref, "sha256": source_input_hash}
    ]
    assert child_manifest["origin"]["frozen_inputs"] == child_contract["inputs"]
    assert child_contract["accepted_request_ref"] == (
        "session:/inputs/accepted_request.json"
    )

    # The parent can continue evolving under D8. Child attempts consume the
    # exact child-local bytes frozen at dispatch, so that evolution cannot
    # wedge every later child assignment.
    parent_input.write_text('{"work_item":"different"}\n', encoding="utf-8")

    descriptor = WorkflowSnapshotDescriptor.model_validate(
        child_task["workflow_snapshot"]
    )
    assignment = build_attempt_assignment(
        repo_root=repo_root,
        task=CurrentTask(
            workflow_set=child_task["workflow_set"],
            workflow_id=child_task["workflow_id"],
            session_id=child_id,
            iteration=child_task["iteration"],
            attempt_id=child_task["attempt_id"],
            started_at=utc_now(),
            workflow_snapshot=descriptor,
        ),
        descriptor=descriptor,
        trace_root=repo_root / ".loopy_loop" / "traces" / "assignment-test",
        git_before_ref="session:/git_receipts/git-before-test.json",
    )
    assert len(assignment.objective["input_artifacts"]) == 1
    assigned_input = assignment.objective["input_artifacts"][0]
    assert assigned_input["ref"] == frozen_input["ref"]
    assert assigned_input["sha256"] == source_input_hash
    frozen_path = Path(assigned_input["absolute_path"])
    assert frozen_path.read_text(encoding="utf-8") == '{"work_item":"component"}\n'
    assert file_sha256(frozen_path) == source_input_hash
    assert assignment.absolute_paths["accepted_request"].endswith(
        "/inputs/accepted_request.json"
    )
    assert json.loads(root_contract_path.read_text(encoding="utf-8")) == root_contract


def test_v1_worker_is_rejected_with_426_without_state_revision_mutation(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    before = StateStore(repo_root=repo_root).read_state()
    assert before is not None

    response = client.post(
        "/register",
        json={"worker": _WORKER, "worker_protocol_version": 1, "capabilities": []},
    )

    assert response.status_code == 426
    assert "protocol v2" in response.json()["detail"]
    after = StateStore(repo_root=repo_root).read_state()
    assert after is not None
    assert after.state_revision == before.state_revision
    assert after.model_dump() == before.model_dump()


def test_corrupt_v2_child_ledger_reconstructs_only_from_immutable_evidence(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _write_workflow_set(
        repo_root=repo_root, workflow_set="child_set", workflow_id="child_work"
    )
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    root_task = client.post("/register", json=_register_v2(repo_root)).json()
    _write_v2_child_request(
        repo_root=repo_root,
        parent_task=root_task,
        request_id="repairable-child",
        workflow_set="child_set",
        goal="Repair from immutable evidence",
    )
    child_task = _finish(client, root_task)
    ledger = children_path(repo_root=repo_root, session_id=root_task["session_id"])
    ledger.write_text("{corrupt", encoding="utf-8")

    # Repair is reachable while the coordinator already owns the parent's
    # state lock (for example during a tree-wide budget projection).  Exercise
    # that path so reconstruction cannot regress to recursively acquiring a
    # second lock for the same state file.
    parent_store = StateStore(
        repo_root=repo_root,
        state_path=ledger.parent / "state.json",
        lock_timeout_seconds=0.1,
    )

    def repair_while_parent_locked(state: LoopState | None) -> tuple[LoopState, Any]:
        assert state is not None
        repaired_payload = app.state.service._read_or_repair_children_payload(
            path=ledger
        )
        return state, repaired_payload

    repaired = parent_store.mutate(repair_while_parent_locked)

    assert [item["session_id"] for item in repaired["children"]] == [
        child_task["session_id"]
    ]
    assert repaired["children"][0]["request_id"] == "repairable-child"
    failures = list(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=root_task["session_id"]
        ).glob("children-ledger-*.json")
    )
    assert len(failures) == 1
    assert json.loads(failures[0].read_text(encoding="utf-8"))["kind"] == (
        "children_ledger_reconstructed"
    )


def test_corrupt_v2_empty_child_ledger_fails_visible_instead_of_inventing_empty(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    state = StateStore(repo_root=repo_root).read_state()
    assert state is not None
    ledger = children_path(repo_root=repo_root, session_id=state.active_session_id)
    ledger.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(ChildLedgerError, match="refusing to reconstruct an empty"):
        app.state.service._read_or_repair_children_payload(path=ledger)
    with pytest.raises(ChildLedgerError, match="refusing to reconstruct an empty"):
        app.state.service._read_or_repair_children_payload(path=ledger)

    assert ledger.read_text(encoding="utf-8") == "{corrupt"
    failure_receipts = list(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=state.active_session_id
        ).glob("children-ledger-*.json")
    )
    assert len(failure_receipts) == 1
    assert json.loads(failure_receipts[0].read_text(encoding="utf-8"))["kind"] == (
        "children_ledger_reconstruction_refused"
    )


def _init_git_repo(repo_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)


def test_assignment_and_exact_prompt_are_durable_before_harness_call(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Implement only from the frozen assignment.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement the assignment.",
                },
            }
        }
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    observed = {"called": False}

    def fake_run_harness_iteration(**kwargs: Any):
        from loopy_loop.models import IterationResult

        observed["called"] = True
        assert task.assignment_path is not None
        assignment_file = Path(task.assignment_path)
        assert assignment_file.is_absolute()
        assert assignment_file.is_file()
        assignment = json.loads(assignment_file.read_text(encoding="utf-8"))
        assert assignment["identity"]["attempt_id"] == task.attempt_id
        assert assignment["identity"]["session_id"] == task.session_id
        assert task.iteration is not None
        assert all(
            Path(value).is_absolute() for value in assignment["absolute_paths"].values()
        )
        prompt_file = (
            iteration_dir_path(
                repo_root=repo_root,
                session_id=str(task.session_id),
                iteration=int(task.iteration),
                workflow_id=str(task.workflow_id),
            )
            / "prompt.txt"
        )
        assert prompt_file.read_text(encoding="utf-8") == kwargs["rendered_prompt"]
        assert str(assignment_file) in kwargs["rendered_prompt"]
        caller = kwargs["caller_context"]
        assert caller["parent_assignment_path"] == str(assignment_file)
        assert Path(str(caller["trace_root"])).is_absolute()
        return IterationResult(
            success=True, text="implemented", harness_run_id="run-contract"
        )

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )

    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )

    assert observed["called"] is True
    assert finished.request.success is True
    assert task.assignment_path is not None
    assert finished.request.assignment_sha256 == file_sha256(Path(task.assignment_path))


def test_retry_same_coordinates_keeps_attempt_artifacts_and_completion_fence_isolated(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Implement this exact frozen assignment.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement the assignment.",
                },
            }
        }
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    owner = WorkerIdentity.model_validate(_WORKER)
    old_task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )

    harness_calls = 0

    def fake_run_harness_iteration(**_: Any) -> IterationResult:
        nonlocal harness_calls
        harness_calls += 1
        return IterationResult(
            success=True,
            text=f"attempt result {harness_calls}",
            harness_run_id=f"run-attempt-{harness_calls}",
        )

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", fake_run_harness_iteration
    )
    old_finished = _run_task(repo_root=repo_root, task=old_task, identity=owner)
    assert old_task.assignment_path is not None
    assert old_task.attempt_id is not None
    old_assignment = Path(old_task.assignment_path)
    old_assignment_bytes = old_assignment.read_bytes()

    # Model a coordinator recovery decision that abandoned the first dispatch
    # before its late network completion arrived. The scheduler must be free to
    # retry the same logical coordinates without reusing mutable artifacts.
    store = StateStore(repo_root=repo_root)

    def abandon_without_advancing(state: LoopState | None) -> tuple[LoopState, None]:
        assert state is not None
        assert state.current_task is not None
        assert state.current_task.attempt_id == old_task.attempt_id
        state.current_task = None
        return state, None

    store.mutate(abandon_without_advancing)
    new_task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )

    assert new_task.session_id == old_task.session_id
    assert new_task.iteration == old_task.iteration
    assert new_task.workflow_id == old_task.workflow_id
    assert new_task.attempt_id != old_task.attempt_id
    assert new_task.attempt_id is not None
    assert new_task.assignment_path is not None
    expected_new_assignment = assignment_path(
        repo_root=repo_root,
        session_id=str(new_task.session_id),
        iteration=int(new_task.iteration or 0),
        workflow_id=str(new_task.workflow_id),
        attempt_id=new_task.attempt_id,
    ).resolve()
    assert Path(new_task.assignment_path) == expected_new_assignment
    assert Path(new_task.assignment_path).is_absolute()
    assert old_task.workflow_snapshot is not None
    assert new_task.workflow_snapshot is not None
    assert Path(old_task.workflow_snapshot.snapshot_root) != Path(
        new_task.workflow_snapshot.snapshot_root
    )
    assert old_assignment != expected_new_assignment

    new_finished = _run_task(repo_root=repo_root, task=new_task, identity=owner)
    assert old_assignment.read_bytes() == old_assignment_bytes
    assert expected_new_assignment.is_file()
    assert file_sha256(old_assignment) != file_sha256(expected_new_assignment)

    # The real late completion only replays the live retry and records no
    # history. Even if its attempt id is rewritten to the live id, its old
    # assignment digest cannot satisfy the new attempt's provenance fence.
    stale_response = client.post(
        "/finished", json=old_finished.request.model_dump(mode="json")
    )
    assert stale_response.status_code == 200, stale_response.text
    assert stale_response.json()["attempt_id"] == new_task.attempt_id
    assert stale_response.json()["assignment_path"] == str(expected_new_assignment)
    after_stale = store.read_state()
    assert after_stale is not None
    assert after_stale.history == []
    assert after_stale.current_task is not None
    assert after_stale.current_task.attempt_id == new_task.attempt_id

    forged = old_finished.request.model_dump(mode="json")
    forged["attempt_id"] = new_task.attempt_id
    rejected = client.post("/finished", json=forged)
    assert rejected.status_code == 409
    after_rejected = store.read_state()
    assert after_rejected is not None
    assert after_rejected.history == []
    assert after_rejected.current_task is not None
    assert after_rejected.current_task.attempt_id == new_task.attempt_id

    accepted = client.post(
        "/finished", json=new_finished.request.model_dump(mode="json")
    )
    assert accepted.status_code == 200, accepted.text
    completed = store.read_state()
    assert completed is not None
    assert len(completed.history) == 1
    assert completed.history[0].attempt_id == new_task.attempt_id


def test_finished_response_is_captured_in_exact_attempt_trace_before_seal(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Complete the traced assignment.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Complete traced work.",
                },
            }
        }
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="trace complete", harness_run_id="run-trace-complete"
        ),
    )
    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )
    assert finished.request.trace_manifest_path is not None
    manifest_path = Path(finished.request.trace_manifest_path)
    trace_root = manifest_path.parent
    before = read_trace_manifest(manifest_path=manifest_path)
    assert before["lifecycle"] == "active"
    assert before["channels"]["service"] == (
        "finished_request_captured_response_pending"
    )

    response = client.post("/finished", json=finished.request.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    captured = json.loads(
        trace_root.joinpath("protocol/finished_response.json").read_text(
            encoding="utf-8"
        )
    )
    assert captured == response.json()
    sealed = read_trace_manifest(manifest_path=manifest_path)
    # The fake harness intentionally produced no canonical run record, so the
    # trace is finalized but honestly incomplete; the service exchange itself
    # is still captured and anchored.
    assert sealed["lifecycle"] == "incomplete"
    assert sealed["channels"]["service"] == "complete"
    assert "protocol/finished_response.json" in {
        item["path"] for item in sealed["inventory"]
    }
    integrity = verify_trace_integrity(trace_root=trace_root)
    assert integrity["status"] == "verified"
    assert integrity["added"] == []
    assert integrity["removed"] == []
    assert integrity["modified"] == []

    # Re-hashing a changed artifact into the mutable manifest cannot re-bless
    # it because the compact session-plane seal receipt anchors that manifest.
    finished_response = trace_root / "protocol" / "finished_response.json"
    finished_response.write_text('{"forged": true}', encoding="utf-8")
    forged_bytes = finished_response.read_bytes()
    for item in sealed["inventory"]:
        if item["path"] == "protocol/finished_response.json":
            item["size"] = len(forged_bytes)
            item["sha256"] = "sha256:" + hashlib.sha256(forged_bytes).hexdigest()
            break
    manifest_path.write_text(json.dumps(sealed, indent=2), encoding="utf-8")
    reblessed = verify_trace_integrity(trace_root=trace_root)
    assert reblessed["status"] == "failed"
    assert any("seal receipt" in reason for reason in reblessed["manifest_errors"])


def test_trace_finalization_failure_is_journaled_and_retried_on_restart(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Complete traced work.",
                "config": {"enabled": True, "run_every": 1},
            }
        }
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="done", harness_run_id="run-finalize-retry"
        ),
    )
    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )
    assert finished.request.trace_manifest_path is not None
    manifest_path = Path(finished.request.trace_manifest_path)

    from loopy_loop import coordinator_app as coordinator_module

    real_seal = coordinator_module.seal_attempt_trace

    def fail_seal(**_: Any) -> dict[str, Any]:
        raise RuntimeError("forced seal failure")

    monkeypatch.setattr(coordinator_module, "seal_attempt_trace", fail_seal)
    response = client.post("/finished", json=finished.request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    outbox = trace_finalization_outbox_dir_path(repo_root=repo_root)
    queued_path = next(outbox.glob("*.json"))
    assert read_trace_manifest(manifest_path=manifest_path)["lifecycle"] == "active"

    monkeypatch.setattr(coordinator_module, "seal_attempt_trace", real_seal)
    original_intent = json.loads(queued_path.read_text(encoding="utf-8"))
    forged_intent = json.loads(json.dumps(original_intent))
    forged_intent["request"]["text"] = "forged completion text"
    forged_intent["response"]["stop_reason"] = "forged response"
    write_json_atomic(path=queued_path, payload=forged_intent)

    # Agent-visible runtime files are not write-fenced (D8), but a modified
    # outbox exchange cannot become engine-sealed evidence because history
    # binds the exact request and response hashes.
    create_coordinator_app(repo_root=repo_root, resume=True)
    assert queued_path.is_file()
    assert read_trace_manifest(manifest_path=manifest_path)["lifecycle"] == "active"

    write_json_atomic(path=queued_path, payload=original_intent)
    create_coordinator_app(repo_root=repo_root, resume=True)

    assert list(outbox.glob("*.json")) == []
    finalized = read_trace_manifest(manifest_path=manifest_path)
    assert finalized["lifecycle"] == "incomplete"
    assert verify_trace_integrity(trace_root=manifest_path.parent)["status"] == (
        "verified"
    )


def test_trace_finalization_refuses_mutated_session_topology_without_redirect(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="done", harness_run_id="run-frozen-root"
        ),
    )
    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )
    assert finished.request.trace_manifest_path is not None
    manifest_path = Path(finished.request.trace_manifest_path)
    owner = WorkerIdentity.model_validate(_WORKER)
    with app.state.service._transition_lock:
        response, accepted = app.state.service._finish_assignment_locked(
            request=finished.request, caller=owner
        )
    assert accepted is True
    session_manifest_path = (
        session_dir_path(repo_root=repo_root, session_id=finished.request.session_id)
        / "session.json"
    )
    session_manifest = json.loads(session_manifest_path.read_text(encoding="utf-8"))
    session_manifest["root_session_id"] = "forged-root-session"
    write_json_atomic(path=session_manifest_path, payload=session_manifest)

    app.state.service._queue_trace_finalization(
        request=finished.request, response=response, error=None
    )
    finalized = app.state.service._finalize_completion_trace(
        request=finished.request, response=response
    )

    assert finalized is False
    assert read_trace_manifest(manifest_path=manifest_path)["lifecycle"] == "active"
    assert (
        len(
            list(trace_finalization_outbox_dir_path(repo_root=repo_root).glob("*.json"))
        )
        == 1
    )


def test_restart_finalizes_committed_completion_with_interrupted_response(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    owner = WorkerIdentity.model_validate(_WORKER)
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="done", harness_run_id="run-interrupted-response"
        ),
    )
    finished = _run_task(repo_root=repo_root, task=task, identity=owner)
    assert finished.request.trace_manifest_path is not None
    manifest_path = Path(finished.request.trace_manifest_path)

    # Simulate process death after the state mutation commits but before the
    # HTTP response is durably associated with the finalization intent.
    with app.state.service._transition_lock:
        app.state.service._finish_assignment_locked(
            request=finished.request, caller=owner
        )
    outbox = trace_finalization_outbox_dir_path(repo_root=repo_root)
    queued = json.loads(next(outbox.glob("*.json")).read_text(encoding="utf-8"))
    assert queued["response"] is None
    assert read_trace_manifest(manifest_path=manifest_path)["lifecycle"] == "active"

    create_coordinator_app(repo_root=repo_root, resume=True)

    assert list(outbox.glob("*.json")) == []
    manifest = read_trace_manifest(manifest_path=manifest_path)
    assert manifest["lifecycle"] == "incomplete"
    exchange = json.loads(
        manifest_path.parent.joinpath("service/finished_exchange.json").read_text(
            encoding="utf-8"
        )
    )
    assert exchange["response"] is None
    assert exchange["response_status"] == "unavailable"
    assert not manifest_path.parent.joinpath("protocol/finished_response.json").exists()
    assert (
        verify_trace_integrity(trace_root=manifest_path.parent, repo_root=repo_root)[
            "status"
        ]
        == "verified"
    )


def test_register_recovered_v2_completion_binds_returned_response_hash(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    owner = WorkerIdentity.model_validate(_WORKER)
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="done", harness_run_id="run-recovered-binding"
        ),
    )
    finished = _run_task(repo_root=repo_root, task=task, identity=owner)
    assert finished.request.trace_manifest_path is not None
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.is_worker_alive", lambda *args, **kwargs: False
    )

    recovered = client.post("/register", json=_register_v2(repo_root))

    assert recovered.status_code == 200, recovered.text
    state = StateStore(
        repo_root=repo_root,
        state_path=state_path(
            repo_root=repo_root, session_id=finished.request.session_id
        ),
    ).read_state()
    assert state is not None
    assert state.history[0].finished_request_sha256 is not None
    assert state.history[0].finished_response_sha256 is not None
    assert (
        list(trace_finalization_outbox_dir_path(repo_root=repo_root).glob("*.json"))
        == []
    )
    manifest_path = Path(finished.request.trace_manifest_path)
    assert read_trace_manifest(manifest_path=manifest_path)["lifecycle"] == "incomplete"


@pytest.mark.parametrize("supplied_path", [None, "relative/wrong-manifest.json"])
def test_completion_derives_canonical_trace_and_marks_bad_supplied_path_incomplete(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, supplied_path: str | None
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="done", harness_run_id="run-bad-trace-path"
        ),
    )
    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )
    assert finished.request.trace_manifest_path is not None
    canonical_manifest = Path(finished.request.trace_manifest_path)
    request = finished.request.model_copy(update={"trace_manifest_path": supplied_path})

    response = client.post("/finished", json=request.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    manifest = read_trace_manifest(manifest_path=canonical_manifest)
    assert manifest["lifecycle"] == "incomplete"
    assert manifest["failure"]["trace_error"]
    assert (
        verify_trace_integrity(
            trace_root=canonical_manifest.parent, repo_root=repo_root
        )["status"]
        == "verified"
    )


def test_worker_presealed_manifest_is_reopened_and_anchored_incomplete(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration",
        lambda **_: IterationResult(
            success=True, text="done", harness_run_id="run-premature-seal"
        ),
    )
    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )
    assert finished.request.trace_manifest_path is not None
    manifest_path = Path(finished.request.trace_manifest_path)
    seal_attempt_trace(
        trace_root=manifest_path.parent, usage=None, incomplete=False, repo_root=None
    )

    response = client.post("/finished", json=finished.request.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    manifest = read_trace_manifest(manifest_path=manifest_path)
    assert manifest["lifecycle"] == "incomplete"
    assert "claimed finalization" in manifest["failure"]["trace_error"]
    assert (
        verify_trace_integrity(trace_root=manifest_path.parent, repo_root=repo_root)[
            "status"
        ]
        == "verified"
    )


def test_terminal_child_outcome_refreshes_after_incomplete_trace_is_anchored(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _write_workflow_set(
        repo_root=repo_root, workflow_set="child_set", workflow_id="child_work"
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    root_task = client.post("/register", json=_register_v2(repo_root)).json()
    _write_v2_child_request(
        repo_root=repo_root,
        parent_task=root_task,
        request_id="trace-refresh-child",
        workflow_set="child_set",
        goal="Finish and project the sealed trace",
    )
    child_task = _finish(client, root_task)
    _write_terminal_blocker_control(repo_root=repo_root, task=child_task)

    _finish(client, child_task)

    outcome_path = (
        child_outcomes_dir_path(repo_root=repo_root, session_id=root_task["session_id"])
        / "trace-refresh-child.json"
    )
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    assert outcome["completeness"]["trace_sealed"] is True
    assert outcome["trace_ref"] == f"trace:trace-{child_task['attempt_id']}:/"
    child_manifest = read_trace_manifest(
        manifest_path=(
            attempt_trace_dir_path(
                repo_root=repo_root,
                root_session_id=root_task["session_id"],
                session_id=child_task["session_id"],
                attempt_id=child_task["attempt_id"],
            )
            / "trace_manifest.json"
        )
    )
    assert child_manifest["lifecycle"] == "incomplete"


@pytest.mark.parametrize("premature_seal", [False, True])
def test_crash_abandoned_attempt_trace_is_finalized_incomplete(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, premature_seal: bool
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    task = client.post("/register", json=_register_v2(repo_root)).json()
    assignment = json.loads(Path(task["assignment_path"]).read_text(encoding="utf-8"))
    trace_root = Path(assignment["absolute_paths"]["trace_root"])
    app.state.service._queue_trace_finalization(
        request=FinishedRequest(
            worker=WorkerIdentity.model_validate(_WORKER),
            workflow_id=str(task["workflow_id"]),
            session_id=str(task["session_id"]),
            iteration=int(task["iteration"]),
            attempt_id=str(task["attempt_id"]),
            repository_id=str(task["repository_id"]),
            assignment_sha256=str(task["assignment_sha256"]),
            success=True,
            text="completion not yet committed",
            trace_manifest_path=str(trace_root / "trace_manifest.json"),
        ),
        response=None,
        error=None,
    )
    if premature_seal:
        manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
        manifest["lifecycle"] = "sealed"
        manifest["sealed_at"] = "2026-07-16T00:00:00Z"
        manifest["inventory"] = []
        manifest["incompleteness_reasons"] = []
        write_json_atomic(path=trace_root / "trace_manifest.json", payload=manifest)
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.is_worker_alive", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.recover_interrupted_iteration",
        lambda **kwargs: RecoveryOutcome(policy="drain"),
    )

    response = client.post("/register", json=_register_v2(repo_root))

    assert response.status_code == 200, response.text
    manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
    assert manifest["lifecycle"] == "incomplete"
    recovery = json.loads(
        (trace_root / "service" / "recovery.json").read_text(encoding="utf-8")
    )
    assert recovery["kind"] == "iteration_abandoned"
    assert recovery["attempt_id"] == task["attempt_id"]
    assert recovery["error"] == "abandoned"
    assert recovery["trace_protocol_errors"] == (
        ["trace claimed finalization before coordinator abandonment"]
        if premature_seal
        else []
    )
    assert manifest["failure"] == {"error": "abandoned", "failure_kind": "crash"}
    assert trace_seal_receipt_path(
        repo_root=repo_root,
        session_id=str(task["session_id"]),
        attempt_id=str(task["attempt_id"]),
    ).is_file()
    assert (
        list(trace_finalization_outbox_dir_path(repo_root=repo_root).glob("*.json"))
        == []
    )
    assert (
        verify_trace_integrity(trace_root=trace_root, repo_root=repo_root)["status"]
        == "verified"
    )


def test_frozen_workflow_tampering_fails_before_harness_call(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "This exact frozen prompt must run.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement the assignment.",
                },
            }
        }
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    assert task.workflow_snapshot is not None
    Path(task.workflow_snapshot.workflow_prompt_path).write_text(
        "tampered after dispatch", encoding="utf-8"
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", lambda **kwargs: calls.append(kwargs)
    )

    with pytest.raises(FatalAssignmentError, match="snapshot artifact hash mismatch"):
        _run_task(repo_root=repo_root, task=task)

    assert calls == []
    assert task.assignment_path is not None
    # The coordinator freezes the assignment before dispatch. Worker-side
    # snapshot verification fails without deleting or rewriting that evidence.
    assert Path(task.assignment_path).is_file()
    assert task.assignment_sha256 == file_sha256(Path(task.assignment_path))


def test_wire_config_must_match_frozen_snapshot_before_harness_call(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    assert task.config_snapshot is not None
    forged = task.model_copy(
        update={
            "config_snapshot": task.config_snapshot.model_copy(
                update={"goal": "wire-only substituted goal"}
            )
        }
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", lambda **kwargs: calls.append(kwargs)
    )

    with pytest.raises(FatalAssignmentError, match="does not match the frozen"):
        _run_task(repo_root=repo_root, task=forged)

    assert calls == []


def test_assignment_mutation_during_harness_is_restored_and_cannot_complete(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Use the immutable assignment.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement the assignment.",
                },
            }
        }
    )
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    assert task.assignment_path is not None
    assert task.assignment_sha256 is not None
    assignment_file = Path(task.assignment_path)

    def mutate_assignment(**_: Any) -> IterationResult:
        assignment_file.write_text('{"forged": true}', encoding="utf-8")
        return IterationResult(
            success=True, text="claimed success", harness_run_id="run-mutation"
        )

    monkeypatch.setattr("loopy_loop.worker.run_harness_iteration", mutate_assignment)
    finished = _run_task(
        repo_root=repo_root, task=task, identity=WorkerIdentity.model_validate(_WORKER)
    )

    assert finished.request.success is False
    assert finished.request.failure_kind == "deterministic"
    assert "immutable assignment changed" in str(finished.request.error)
    assert file_sha256(assignment_file) == task.assignment_sha256
    response = client.post("/finished", json=finished.request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    state = _read_state(repo_root, str(task.session_id))
    assert state.history[-1].success is False


def test_assignment_directory_conflict_is_archived_and_restored(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    assert task.assignment_path is not None
    assert task.assignment_sha256 is not None
    assignment_file = Path(task.assignment_path)

    def replace_assignment_with_directory(**_: Any) -> IterationResult:
        assignment_file.unlink()
        assignment_file.mkdir()
        assignment_file.joinpath("agent-note.txt").write_text(
            "preserve this conflicting object", encoding="utf-8"
        )
        return IterationResult(
            success=True, text="claimed success", harness_run_id="run-dir-conflict"
        )

    monkeypatch.setattr(
        "loopy_loop.worker.run_harness_iteration", replace_assignment_with_directory
    )
    finished = _run_task(repo_root=repo_root, task=task)

    assert finished.request.success is False
    assert finished.request.failure_kind == "deterministic"
    assert assignment_file.is_file()
    assert file_sha256(assignment_file) == task.assignment_sha256
    conflicts = list(assignment_file.parent.glob("assignment.json.protocol-conflict-*"))
    assert len(conflicts) == 1
    assert conflicts[0].joinpath("agent-note.txt").read_text(encoding="utf-8") == (
        "preserve this conflicting object"
    )


def test_snapshot_tamper_after_harness_is_rejected_then_reissued_on_restart(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _init_git_repo(repo_root)
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    owner = WorkerIdentity.model_validate(_WORKER)
    task = TaskResponse.model_validate(
        client.post("/register", json=_register_v2(repo_root)).json()
    )
    assert task.workflow_snapshot is not None
    prompt_path = Path(task.workflow_snapshot.workflow_prompt_path)

    def tamper_after_load(**_: Any) -> IterationResult:
        prompt_path.write_text("changed after the worker loaded it", encoding="utf-8")
        return IterationResult(
            success=True, text="claimed success", harness_run_id="run-snapshot-tamper"
        )

    monkeypatch.setattr("loopy_loop.worker.run_harness_iteration", tamper_after_load)
    finished = _run_task(repo_root=repo_root, task=task, identity=owner)
    rejected = client.post("/finished", json=finished.request.model_dump(mode="json"))

    assert rejected.status_code == 409
    still_live = _read_state(repo_root, str(task.session_id))
    assert still_live.history == []
    assert still_live.current_task is not None
    assert still_live.current_task.attempt_id == task.attempt_id

    monkeypatch.setattr(
        "loopy_loop.coordinator_app.is_worker_alive", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        "loopy_loop.coordinator_app.recover_interrupted_iteration",
        lambda **kwargs: RecoveryOutcome(policy="drain"),
    )
    resumed = TestClient(create_coordinator_app(repo_root=repo_root, resume=True)).post(
        "/register", json=_register_v2(repo_root)
    )

    assert resumed.status_code == 200, resumed.text
    retry = TaskResponse.model_validate(resumed.json())
    assert retry.action == "run"
    assert retry.attempt_id != task.attempt_id
    recovered = _read_state(repo_root, str(task.session_id))
    assert recovered.history[-1].attempt_id == task.attempt_id
    assert recovered.history[-1].failure_kind == "crash"
    assert recovered.current_task is not None
    assert recovered.current_task.attempt_id == retry.attempt_id


def test_three_depth_dispatch_unwinds_two_terminal_descendants(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _write_workflow_set(
        repo_root=repo_root, workflow_set="child_set", workflow_id="child_work"
    )
    _write_workflow_set(
        repo_root=repo_root,
        workflow_set="grandchild_set",
        workflow_id="grandchild_work",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    root_task, child_task, grandchild_task = _dispatch_three_levels(
        repo_root=repo_root, client=client
    )
    root_id = root_task["session_id"]
    child_id = child_task["session_id"]
    grandchild_id = grandchild_task["session_id"]
    assert _read_state(repo_root, root_id).depth == 0
    assert _read_state(repo_root, child_id).depth == 1
    assert _read_state(repo_root, grandchild_id).depth == 2

    _set_stop_requested(repo_root=repo_root, session_id=child_id)
    _write_terminal_blocker_control(repo_root=repo_root, task=grandchild_task)
    resumed = _finish(client, grandchild_task)

    assert resumed["action"] == "run"
    assert resumed["session_id"] == root_id
    assert resumed["iteration"] == 2
    root_state = _read_state(repo_root, root_id)
    child_state = _read_state(repo_root, child_id)
    grandchild_state = _read_state(repo_root, grandchild_id)
    assert root_state.active_child_session_id is None
    assert root_state.current_task is not None
    assert root_state.current_task.session_id == root_id
    assert child_state.status == "stopped"
    assert child_state.current_task is None
    assert grandchild_state.status == "failed"
    assert grandchild_state.stop_reason == "unresolvable_error"
    root_children = json.loads(
        children_path(repo_root=repo_root, session_id=root_id).read_text(
            encoding="utf-8"
        )
    )
    child_children = json.loads(
        children_path(repo_root=repo_root, session_id=child_id).read_text(
            encoding="utf-8"
        )
    )
    assert root_children["children"][0]["status"] == "stopped"
    assert child_children["children"][0]["status"] == "failed"


def test_terminal_child_repairs_ledger_while_ignoring_non_session_directory(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _write_workflow_set(
        repo_root=repo_root, workflow_set="child_set", workflow_id="child_work"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    root_task = client.post("/register", json=_register_v2(repo_root)).json()
    _write_v2_child_request(
        repo_root=repo_root,
        parent_task=root_task,
        request_id="repair-with-stray",
        workflow_set="child_set",
        goal="Reach a valid terminal blocker",
    )
    child_task = _finish(client, root_task)
    parent_root = session_dir_path(
        repo_root=repo_root, session_id=root_task["session_id"]
    )
    parent_root.joinpath("children", "agent-scratch").mkdir()
    child_root = session_dir_path(
        repo_root=repo_root, session_id=child_task["session_id"]
    )
    child_root.joinpath("children", "agent-scratch").mkdir(parents=True)
    children_path(repo_root=repo_root, session_id=root_task["session_id"]).write_text(
        "{", encoding="utf-8"
    )
    _write_terminal_blocker_control(repo_root=repo_root, task=child_task)

    resumed = _finish(client, child_task)

    assert resumed["action"] == "run"
    assert resumed["session_id"] == root_task["session_id"]
    repaired = json.loads(
        children_path(
            repo_root=repo_root, session_id=root_task["session_id"]
        ).read_text(encoding="utf-8")
    )
    assert [record["request_id"] for record in repaired["children"]] == [
        "repair-with-stray"
    ]
    assert repaired["children"][0]["stop_reason"] == "unresolvable_error"


def test_root_stop_is_projected_to_depth_two_and_dispatches_no_next_task(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    _write_workflow_set(
        repo_root=repo_root, workflow_set="child_set", workflow_id="child_work"
    )
    _write_workflow_set(
        repo_root=repo_root,
        workflow_set="grandchild_set",
        workflow_id="grandchild_work",
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    root_task, child_task, grandchild_task = _dispatch_three_levels(
        repo_root=repo_root, client=client
    )
    root_id = root_task["session_id"]
    child_id = child_task["session_id"]
    grandchild_id = grandchild_task["session_id"]

    _set_stop_requested(repo_root=repo_root, session_id=root_id)
    stopped = _finish(client, grandchild_task)

    assert stopped == {
        "action": "stop",
        "workflow_set": None,
        "workflow_id": None,
        "session_id": None,
        "iteration": None,
        "attempt_id": None,
        "config_snapshot": None,
        "stop_reason": "stop_requested",
        "coordinator_protocol_version": None,
        "required_capabilities": [],
        "repo_root": None,
        "repository_id": None,
        "assignment_path": None,
        "assignment_sha256": None,
        "workflow_snapshot": None,
    }
    for session_id in (root_id, child_id, grandchild_id):
        state = _read_state(repo_root, session_id)
        assert state.status == "stopped"
        assert state.stop_reason == "stop_requested"
        assert state.current_task is None
    assert len(_read_state(repo_root, root_id).history) == 1
    assert len(_read_state(repo_root, child_id).history) == 1
    assert len(_read_state(repo_root, grandchild_id).history) == 1


def _eval_contract(workflow_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "session_protocol_version": 2,
        "layer_kind": "evaluation",
        "roles": {
            workflow_id: {
                "responsibility": "Judge this session's exact immutable goal."
            }
        },
        "state": [],
        "eval": {
            "author_role": workflow_id,
            "runner_role": workflow_id,
            "goal_control_role": workflow_id,
        },
        "task_acceptance_role": None,
        "terminal_blocker_reporting_roles": [workflow_id],
        "child_interface": "recursive",
    }


def _write_eval_receipt(
    *,
    repo_root: Path,
    task: dict[str, Any],
    state: LoopState,
    subject_session_id: str | None = None,
    producer_attempt_id: str | None = None,
) -> str:
    name = "eval-contract.json"
    path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / name
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "eval_id": "eval-contract",
                "subject": {
                    "root_session_id": state.root_session_id,
                    "session_id": subject_session_id or task["session_id"],
                    "goal_hash": state.goal_hash,
                    "git_commit": None,
                    "dirty_tree_digest": None,
                },
                "producer": {
                    "workflow_id": task["workflow_id"],
                    "iteration": task["iteration"],
                    "attempt_id": producer_attempt_id or task["attempt_id"],
                    "harness_run_id": "run-eval",
                },
                "checks": [
                    {
                        "check_id": "judge-goal",
                        "definition_sha256": "sha256:" + "1" * 64,
                        "kind": "harness_judge",
                    }
                ],
                "judge": {
                    "provider": "test",
                    "model": "test-judge",
                    "reasoning_effort": "high",
                },
                "check_results": [
                    {"check_id": "judge-goal", "passed": True, "reason": "done"}
                ],
                "verdict": {"goal_met": True, "reason": "done"},
                "canonical_report_ref": "trace:eval:/eval/report.json",
                "canonical_report_sha256": "sha256:" + "2" * 64,
                "raw_report_refs": ["trace:eval:/eval/raw.json"],
                "raw_report_sha256s": {
                    "trace:eval:/eval/raw.json": "sha256:" + "3" * 64
                },
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return f"session:/eval_receipts/{name}"


def _minimal_eval_receipt_payload() -> dict[str, Any]:
    raw_ref = "trace:trace-test:/eval/report.json"
    return {
        "schema_version": 1,
        "eval_id": "eval-test",
        "subject": {
            "root_session_id": "session-root",
            "session_id": "session-current",
            "goal_hash": "sha256:" + "0" * 64,
            "git_commit": "a" * 40,
            "dirty_tree_digest": "sha256:" + "4" * 64,
        },
        "producer": {
            "workflow_id": "goal_check",
            "iteration": 1,
            "attempt_id": "attempt-test",
            "harness_run_id": "run-test",
        },
        "checks": [
            {
                "check_id": "judge-goal",
                "definition_sha256": "sha256:" + "1" * 64,
                "kind": "harness_judge",
            }
        ],
        "judge": {"provider": "codex", "model": "gpt-5.5", "reasoning_effort": "high"},
        "check_results": [{"check_id": "judge-goal", "passed": True, "reason": "done"}],
        "verdict": {"goal_met": True, "reason": "done"},
        "canonical_report_ref": "session:/eval_receipts/eval-test.report.md",
        "canonical_report_sha256": "sha256:" + "2" * 64,
        "raw_report_refs": [raw_ref],
        "raw_report_sha256s": {raw_ref: "sha256:" + "3" * 64},
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
    }


def _setup_eval_task(
    *, repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any, TestClient, dict[str, Any], LoopState]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "eval_runner": {
                "prompt": "Judge the goal and write a receipt.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "emits_goal_check": True,
                    "description": "Judge the goal.",
                },
            }
        }
    )
    workflow_root = repo_root / ".loopy_loop" / "workflow_sets" / "main"
    workflow_root.joinpath("contract.yaml").write_text(
        yaml.safe_dump(_eval_contract("eval_runner"), sort_keys=False), encoding="utf-8"
    )
    _init_git_repo(repo_root)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo_root, check=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "baseline"], cwd=repo_root, check=True
    )
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    task = client.post("/register", json=_register_v2(repo_root)).json()
    return (
        repo_root,
        app.state.service,
        client,
        task,
        _read_state(repo_root, task["session_id"]),
    )


def _write_valid_eval_bundle(
    *, repo_root: Path, task: dict[str, Any], state: LoopState
) -> tuple[dict[str, Any], str, Path]:
    check_path = (
        session_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval_checks"
        / "judge-goal.yaml"
    )
    check_path.write_text(
        "schema_version: 1\nid: judge-goal\ntype: harness_judge\n"
        "description: Judge the goal.\ninstructions: Inspect the result.\n",
        encoding="utf-8",
    )
    canonical_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval-valid.report.md"
    )
    canonical_path.write_text("# Passing evaluation\n", encoding="utf-8")
    trace_root, trace_manifest = create_attempt_trace(
        repo_root=repo_root,
        root_session_id=state.root_session_id or state.active_session_id,
        session_id=state.active_session_id,
        request_id=None,
        work_item_id=None,
        workflow_set=task["workflow_set"],
        workflow_id=task["workflow_id"],
        iteration=task["iteration"],
        attempt_id=task["attempt_id"],
    )
    raw_path = trace_root / "eval" / "report.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(
            {
                "project_root": str(repo_root.resolve()),
                "output_dir": str((trace_root / "eval").resolve()),
                "run_passed": True,
                "pass_threshold": 1.0,
                "checks": [
                    {
                        "check_id": "judge-goal",
                        "check_definition_sha256": file_sha256(check_path),
                        "status": "passed",
                        "exit_code": 0,
                        "details": {
                            "agent_type": "codex",
                            "model": "gpt-5.5",
                            "reasoning_effort": "high",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    update_trace_manifest(
        trace_root=trace_root, updates={"identity": {"harness_run_id": "run-valid"}}
    )
    live_git = capture_git_evidence(
        repo_root=repo_root, phase="after", attempt_id=task["attempt_id"]
    )
    git_commit = live_git.head
    dirty_tree_digest = live_git.dirty_tree_digest
    assert git_commit is not None
    git_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"]).joinpath(
        f"git-after-{task['attempt_id']}.json"
    ).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "after",
                "attempt_id": task["attempt_id"],
                "head": git_commit,
                "dirty_tree_digest": dirty_tree_digest,
            }
        ),
        encoding="utf-8",
    )
    raw_ref = f"trace:{trace_manifest['manifest_id']}:/eval/report.json"
    payload = {
        "schema_version": 1,
        "eval_id": "eval-valid",
        "subject": {
            "root_session_id": state.root_session_id,
            "session_id": task["session_id"],
            "goal_hash": state.goal_hash,
            "git_commit": git_commit,
            "dirty_tree_digest": dirty_tree_digest,
        },
        "producer": {
            "workflow_id": task["workflow_id"],
            "iteration": task["iteration"],
            "attempt_id": task["attempt_id"],
            "harness_run_id": "run-valid",
        },
        "checks": [
            {
                "check_id": "judge-goal",
                "definition_sha256": file_sha256(check_path),
                "kind": "harness_judge",
            }
        ],
        "judge": {"provider": "codex", "model": "gpt-5.5", "reasoning_effort": "high"},
        "check_results": [{"check_id": "judge-goal", "passed": True, "reason": "done"}],
        "verdict": {"goal_met": True, "reason": "done"},
        "canonical_report_ref": "session:/eval_receipts/eval-valid.report.md",
        "canonical_report_sha256": file_sha256(canonical_path),
        "raw_report_refs": [raw_ref],
        "raw_report_sha256s": {raw_ref: file_sha256(raw_path)},
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
    }
    receipt_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval-valid.json"
    )
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, "session:/eval_receipts/eval-valid.json", trace_root


def test_valid_evidence_bound_eval_and_current_attempt_control_close_session(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "eval_runner": {
                "prompt": "Judge the goal and write a receipt.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "emits_goal_check": True,
                    "description": "Judge the goal.",
                },
            }
        }
    )
    workflow_root = repo_root / ".loopy_loop" / "workflow_sets" / "main"
    workflow_root.joinpath("contract.yaml").write_text(
        yaml.safe_dump(_eval_contract("eval_runner"), sort_keys=False), encoding="utf-8"
    )
    _init_git_repo(repo_root)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo_root, check=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "baseline"], cwd=repo_root, check=True
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_v2(repo_root)).json()
    state = _read_state(repo_root, task["session_id"])

    check_path = (
        session_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval_checks"
        / "judge-goal.yaml"
    )
    check_path.write_text(
        "schema_version: 1\nid: judge-goal\ntype: harness_judge\n"
        "description: Judge the goal.\ninstructions: Inspect the result.\n",
        encoding="utf-8",
    )
    canonical_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval-valid.report.md"
    )
    canonical_path.write_text("# Passing evaluation\n", encoding="utf-8")
    trace_root, trace_manifest = create_attempt_trace(
        repo_root=repo_root,
        root_session_id=state.root_session_id or state.active_session_id,
        session_id=state.active_session_id,
        request_id=None,
        work_item_id=None,
        workflow_set=task["workflow_set"],
        workflow_id=task["workflow_id"],
        iteration=task["iteration"],
        attempt_id=task["attempt_id"],
    )
    raw_path = trace_root / "eval" / "report.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(
            {
                "project_root": str(repo_root.resolve()),
                "output_dir": str((trace_root / "eval").resolve()),
                "run_passed": True,
                "pass_threshold": 1.0,
                "checks": [
                    {
                        "check_id": "judge-goal",
                        "check_definition_sha256": file_sha256(check_path),
                        "status": "passed",
                        "exit_code": 0,
                        "details": {
                            "agent_type": "codex",
                            "model": "gpt-5.5",
                            "reasoning_effort": "high",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    update_trace_manifest(
        trace_root=trace_root, updates={"identity": {"harness_run_id": "run-valid"}}
    )
    live_git = capture_git_evidence(
        repo_root=repo_root, phase="after", attempt_id=task["attempt_id"]
    )
    git_commit = live_git.head
    dirty_tree_digest = live_git.dirty_tree_digest
    assert git_commit is not None
    git_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"]).joinpath(
        f"git-after-{task['attempt_id']}.json"
    ).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "after",
                "attempt_id": task["attempt_id"],
                "head": git_commit,
                "dirty_tree_digest": dirty_tree_digest,
            }
        ),
        encoding="utf-8",
    )
    raw_ref = f"trace:{trace_manifest['manifest_id']}:/eval/report.json"
    receipt_path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval-valid.json"
    )
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "eval_id": "eval-valid",
                "subject": {
                    "root_session_id": state.root_session_id,
                    "session_id": task["session_id"],
                    "goal_hash": state.goal_hash,
                    "git_commit": git_commit,
                    "dirty_tree_digest": dirty_tree_digest,
                },
                "producer": {
                    "workflow_id": task["workflow_id"],
                    "iteration": task["iteration"],
                    "attempt_id": task["attempt_id"],
                    "harness_run_id": "run-valid",
                },
                "checks": [
                    {
                        "check_id": "judge-goal",
                        "definition_sha256": file_sha256(check_path),
                        "kind": "harness_judge",
                    }
                ],
                "judge": {
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "reasoning_effort": "high",
                },
                "check_results": [
                    {"check_id": "judge-goal", "passed": True, "reason": "done"}
                ],
                "verdict": {"goal_met": True, "reason": "done"},
                "canonical_report_ref": "session:/eval_receipts/eval-valid.report.md",
                "canonical_report_sha256": file_sha256(canonical_path),
                "raw_report_refs": [raw_ref],
                "raw_report_sha256s": {raw_ref: file_sha256(raw_path)},
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    receipt_ref = "session:/eval_receipts/eval-valid.json"
    goal_check_path(
        repo_root=repo_root,
        session_id=task["session_id"],
        iteration=task["iteration"],
        workflow_id=task["workflow_id"],
    ).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "goal_met": True,
                "reason": "done",
                "eval_receipt_ref": receipt_ref,
            }
        ),
        encoding="utf-8",
    )
    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": "control-valid",
                "state": "stopped",
                "reason": "same-session eval passed",
                "stop_reason": "goal_met",
                "producer": {
                    "session_id": task["session_id"],
                    "workflow_id": task["workflow_id"],
                    "attempt_id": task["attempt_id"],
                },
                "eval_receipt_ref": receipt_ref,
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    response = _finish(client, task)

    assert response["action"] == "stop"
    assert response["stop_reason"] == "goal_met"
    final_state = _read_state(repo_root, task["session_id"])
    assert final_state.status == "goal_met"
    assert final_state.history[-1].success is True


@pytest.mark.parametrize(
    ("subject_session_id", "producer_attempt_id"),
    [("different-session", None), (None, "different-attempt")],
    ids=["subject-mismatch", "producer-mismatch"],
)
def test_goal_check_rejects_eval_receipt_subject_or_producer_mismatch(
    repo_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
    subject_session_id: str | None,
    producer_attempt_id: str | None,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "eval_runner": {
                "prompt": "Judge the goal and write a receipt.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "emits_goal_check": True,
                    "description": "Judge the goal.",
                },
            }
        }
    )
    workflow_root = repo_root / ".loopy_loop" / "workflow_sets" / "main"
    workflow_root.joinpath("contract.yaml").write_text(
        yaml.safe_dump(_eval_contract("eval_runner"), sort_keys=False), encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_v2(repo_root)).json()
    state = _read_state(repo_root, task["session_id"])
    receipt_ref = _write_eval_receipt(
        repo_root=repo_root,
        task=task,
        state=state,
        subject_session_id=subject_session_id,
        producer_attempt_id=producer_attempt_id,
    )
    goal_check_path(
        repo_root=repo_root,
        session_id=task["session_id"],
        iteration=task["iteration"],
        workflow_id=task["workflow_id"],
    ).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "goal_met": True,
                "reason": "done",
                "eval_receipt_ref": receipt_ref,
            }
        ),
        encoding="utf-8",
    )

    _finish(client, task)

    state = _read_state(repo_root, task["session_id"])
    assert state.history[-1].success is False
    assert state.history[-1].error == "invalid_goal_check_output"
    assert state.goal_met is False


def test_control_rejects_mismatched_producer_and_archives_protocol_failure(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Implement work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement work.",
                },
            }
        }
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_v2(repo_root)).json()
    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": "bad-producer",
                "state": "stopped",
                "reason": "claims a terminal blocker",
                "stop_reason": "unresolvable_error",
                "producer": {
                    "session_id": task["session_id"],
                    "workflow_id": task["workflow_id"],
                    "attempt_id": "not-the-active-attempt",
                },
                "attempted_routes": ["retry"],
                "evidence_refs": [],
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    next_task = _finish(client, task)

    assert next_task["action"] == "run"
    state = _read_state(repo_root, task["session_id"])
    assert state.status == "running"
    assert state.unresolvable_error is False
    assert state.history[-1].success is False
    assert state.history[-1].error == "invalid_control_output"
    repaired = json.loads(
        control_path(repo_root=repo_root, session_id=task["session_id"]).read_text(
            encoding="utf-8"
        )
    )
    assert repaired["schema_version"] == 2
    assert repaired["state"] == "running"
    assert repaired["engine_repair"]["kind"] == "invalid_control_archived"
    assert repaired["engine_repair"]["rejected_attempt_id"] == task["attempt_id"]
    assert (
        len(
            list(
                control_rejected_dir_path(
                    repo_root=repo_root, session_id=task["session_id"]
                ).glob("bad-producer*.json")
            )
        )
        == 1
    )
    failures = list(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=task["session_id"]
        ).glob("*.json")
    )
    assert len(failures) == 1
    assert "producer attempt is not owned" in failures[0].read_text(encoding="utf-8")


def test_invalid_control_failures_accumulate_through_engine_repair_placeholder(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        root_config={"goal_check_consecutive_failures_cap": 3},
        workflows={
            "implement": {
                "prompt": "Implement work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement work.",
                },
            }
        },
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_v2(repo_root)).json()

    for failure_count in range(1, 4):
        control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "control_id": f"bad-control-{failure_count}",
                    "state": "stopped",
                    "reason": "claims a blocker without attempted routes",
                    "stop_reason": "unresolvable_error",
                    "producer": {
                        "session_id": task["session_id"],
                        "workflow_id": task["workflow_id"],
                        "attempt_id": task["attempt_id"],
                    },
                    "attempted_routes": [],
                    "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )

        response = _finish(client, task)
        state = _read_state(repo_root, task["session_id"])
        assert state.control_protocol_consecutive_failures == failure_count
        failure_records = sorted(
            protocol_failures_dir_path(
                repo_root=repo_root, session_id=task["session_id"]
            ).glob("*.json")
        )
        assert len(failure_records) == failure_count
        counts = sorted(
            json.loads(path.read_text(encoding="utf-8"))["consecutive_failure_count"]
            for path in failure_records
        )
        assert counts == list(range(1, failure_count + 1))

        if failure_count < 3:
            assert response["action"] == "run"
            task = response
        else:
            assert response["action"] == "stop"
            assert response["stop_reason"] == "control_protocol_broken"
            assert state.status == "failed"


def test_terminal_control_rejects_an_owned_but_historical_attempt(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Implement work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement work.",
                },
            },
            "review": {
                "prompt": "Review work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Review work.",
                },
            },
        }
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    historical_task = client.post("/register", json=_register_v2(repo_root)).json()
    current_task = _finish(client, historical_task)
    assert current_task["action"] == "run"

    control_path(
        repo_root=repo_root, session_id=historical_task["session_id"]
    ).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": "stale-owned-control",
                "state": "stopped",
                "reason": "replays a terminal decision from old work",
                "stop_reason": "unresolvable_error",
                "producer": {
                    "session_id": historical_task["session_id"],
                    "workflow_id": historical_task["workflow_id"],
                    "attempt_id": historical_task["attempt_id"],
                },
                "attempted_routes": ["retry"],
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    response = _finish(client, current_task)
    state = _read_state(repo_root, historical_task["session_id"])

    assert response["action"] == "run"
    assert state.unresolvable_error is False
    assert state.history[-1].attempt_id == current_task["attempt_id"]
    assert state.history[-1].error == "invalid_control_output"
    failure = next(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=historical_task["session_id"]
        ).glob("*.json")
    )
    assert "exact current task" in failure.read_text(encoding="utf-8")


@pytest.mark.parametrize("recover_via_register", [False, True])
def test_v2_completion_uses_attempt_frozen_contract_after_session_downgrade(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, recover_via_register: bool
) -> None:
    """Agent-visible session copies cannot downgrade an assigned attempt.

    Rewriting both the session contract and its manifest hash makes the two
    mutable files internally consistent, so this specifically proves that
    terminal-control validation is bound to the exact attempt snapshot.  The
    recovery parameter exercises the same fence after a worker crash between
    writing its pending completion and posting ``/finished``.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Implement work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement work.",
                },
            }
        }
    )
    workflow_root = repo_root / ".loopy_loop" / "workflow_sets" / "main"
    workflow_root.joinpath("contract.yaml").write_text(
        yaml.safe_dump(_eval_contract("implement"), sort_keys=False), encoding="utf-8"
    )
    client = TestClient(create_coordinator_app(repo_root=repo_root, resume=False))
    task = client.post("/register", json=_register_v2(repo_root)).json()

    contract_path = workflow_contract_path(
        repo_root=repo_root, session_id=task["session_id"]
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["session_protocol_version"] == 2
    contract["session_protocol_version"] = 1
    write_json_atomic(path=contract_path, payload=contract)

    manifest_path = (
        session_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "session.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_contract_hash"] = file_sha256(contract_path)
    write_json_atomic(path=manifest_path, payload=manifest)

    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "stopped",
                "reason": "claims completion without v2 evidence",
                "stop_reason": "goal_met",
            }
        ),
        encoding="utf-8",
    )
    finished_payload = {
        "worker": _WORKER,
        "workflow_id": task["workflow_id"],
        "session_id": task["session_id"],
        "iteration": task["iteration"],
        "attempt_id": task["attempt_id"],
        "repository_id": task["repository_id"],
        "assignment_sha256": task["assignment_sha256"],
        "success": True,
        "text": "completed",
        "error": None,
    }
    if recover_via_register:
        pending_path = pending_finished_request_path(
            repo_root=repo_root,
            session_id=task["session_id"],
            iteration=task["iteration"],
            workflow_id=task["workflow_id"],
        )
        write_json_atomic(path=pending_path, payload=finished_payload)
        response = client.post("/register", json=_register_v2(repo_root))
        assert not pending_path.exists()
    else:
        response = client.post("/finished", json=finished_payload)

    assert response.status_code == 200, response.text
    next_task = response.json()
    assert next_task["action"] == "run"
    state = _read_state(repo_root, task["session_id"])
    assert state.status == "running"
    assert state.goal_met is False
    assert state.workflow_contract is not None
    assert state.workflow_contract.session_protocol_version == 2
    assert state.history[-1].attempt_id == task["attempt_id"]
    assert state.history[-1].success is False
    assert state.history[-1].error == "invalid_control_output"
    restored_contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert restored_contract["session_protocol_version"] == 2
    restored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert restored_manifest["workflow_contract_hash"] == file_sha256(
        path=contract_path
    )
    next_snapshot = next_task["workflow_snapshot"]
    assert next_snapshot is not None
    next_contract = yaml.safe_load(
        Path(next_snapshot["workflow_contract_path"]).read_text(encoding="utf-8")
    )
    assert next_contract["session_protocol_version"] == 2
    failure = next(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=task["session_id"]
        ).glob("*.json")
    )
    assert "requires terminal control v2" in failure.read_text(encoding="utf-8")

    # The next attempt remains v2 as well: the first rejection and the
    # between-attempt projection repair cannot turn later work into legacy
    # control semantics.
    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "stopped",
                "reason": "retries the same downgraded completion",
                "stop_reason": "goal_met",
            }
        ),
        encoding="utf-8",
    )
    after_second_attempt = _finish(client=client, task=next_task)
    after_second_state = _read_state(repo_root, task["session_id"])

    assert after_second_attempt["action"] == "run"
    assert after_second_state.goal_met is False
    assert after_second_state.history[-1].attempt_id == next_task["attempt_id"]
    assert after_second_state.history[-1].error == "invalid_control_output"


def test_legacy_state_validates_v2_terminal_control_with_contract_fallback(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Implement work.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Implement work.",
                },
            }
        }
    )
    app = create_coordinator_app(repo_root=repo_root, resume=False)
    client = TestClient(app)
    task = client.post("/register", json=_register_v2(repo_root)).json()
    store = StateStore(
        repo_root=repo_root,
        state_path=state_path(repo_root=repo_root, session_id=task["session_id"]),
    )
    state = store.read_state()
    assert state is not None
    state.schema_version = 1
    store.write_state(state=state)
    workflow_contract_path(repo_root=repo_root, session_id=task["session_id"]).unlink()
    _write_terminal_blocker_control(repo_root=repo_root, task=task)

    response = _finish(client, task)
    updated = _read_state(repo_root, task["session_id"])

    assert response["action"] == "stop"
    assert response["stop_reason"] == "unresolvable_error"
    assert updated.unresolvable_error is True


def test_passing_eval_receipt_requires_at_least_one_check() -> None:
    payload = _minimal_eval_receipt_payload()
    payload["checks"] = []
    payload["check_results"] = []

    with pytest.raises(ValueError, match="at least one check"):
        EvalReceipt.model_validate(payload)


@pytest.mark.parametrize("field", ["eval_id", "check_id"])
def test_eval_receipt_durable_ids_are_filesystem_safe(field: str) -> None:
    payload = _minimal_eval_receipt_payload()
    if field == "eval_id":
        payload["eval_id"] = "../../outside"
    else:
        checks = payload["checks"]
        results = payload["check_results"]
        assert isinstance(checks, list) and isinstance(results, list)
        checks[0]["check_id"] = "../../outside"
        results[0]["check_id"] = "../../outside"

    with pytest.raises(ValueError, match="filesystem-safe"):
        EvalReceipt.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "   "},
        {"attempted_routes": ["retry", "\t"]},
        {"control_id": "../../outside"},
    ],
    ids=["blank-reason", "blank-route", "unsafe-id"],
)
def test_v2_terminal_control_requires_legible_safe_fields(
    overrides: dict[str, Any],
) -> None:
    payload = {
        "schema_version": 2,
        "control_id": "control-test",
        "state": "stopped",
        "reason": "terminal credential is unavailable",
        "stop_reason": "unresolvable_error",
        "producer": {
            "session_id": "session-test",
            "workflow_id": "implement",
            "attempt_id": "attempt-test",
        },
        "attempted_routes": ["retry with alternate provider"],
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)

    with pytest.raises(ValueError):
        ControlSignal.model_validate(payload)


def test_unsafe_control_id_is_archived_inside_rejected_directory(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, _, client, task, _ = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    session_id = task["session_id"]
    control_path(repo_root=repo_root, session_id=session_id).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": "../../outside",
                "state": "stopped",
                "reason": "terminal credential is unavailable",
                "stop_reason": "unresolvable_error",
                "producer": {
                    "session_id": session_id,
                    "workflow_id": task["workflow_id"],
                    "attempt_id": task["attempt_id"],
                },
                "attempted_routes": ["retry with alternate provider"],
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    _finish(client, task)

    rejected = list(
        control_rejected_dir_path(repo_root=repo_root, session_id=session_id).glob(
            "*.json"
        )
    )
    assert len(rejected) == 1
    assert rejected[0].name.startswith("control-")
    assert (
        not session_dir_path(repo_root=repo_root, session_id=session_id)
        .joinpath("outside.json")
        .exists()
    )


def test_goal_met_control_must_cite_its_goal_check_projection_receipt(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, _, client, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, first_ref, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    second_payload = json.loads(json.dumps(payload))
    second_payload["eval_id"] = "eval-second"
    second_report = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval-second.report.md"
    )
    second_report.write_text("# Passing evaluation, second receipt\n", encoding="utf-8")
    second_payload["canonical_report_ref"] = (
        "session:/eval_receipts/eval-second.report.md"
    )
    second_payload["canonical_report_sha256"] = file_sha256(second_report)
    eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"]).joinpath(
        "eval-second.json"
    ).write_text(json.dumps(second_payload), encoding="utf-8")
    second_ref = "session:/eval_receipts/eval-second.json"
    goal_check_path(
        repo_root=repo_root,
        session_id=task["session_id"],
        iteration=task["iteration"],
        workflow_id=task["workflow_id"],
    ).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "goal_met": True,
                "reason": "done",
                "eval_receipt_ref": first_ref,
            }
        ),
        encoding="utf-8",
    )
    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": "control-wrong-receipt",
                "state": "stopped",
                "reason": "different passing receipt",
                "stop_reason": "goal_met",
                "producer": {
                    "session_id": task["session_id"],
                    "workflow_id": task["workflow_id"],
                    "attempt_id": task["attempt_id"],
                },
                "eval_receipt_ref": second_ref,
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    response = _finish(client, task)

    assert response["action"] == "run"
    updated = _read_state(repo_root, task["session_id"])
    assert updated.goal_met is False
    assert updated.history[-1].error == "invalid_control_output"
    failure = next(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=task["session_id"]
        ).glob("*.json")
    )
    assert "goal_check projection" in failure.read_text(encoding="utf-8")


def test_goal_met_control_rejects_projection_reason_that_differs_from_receipt(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, _, client, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    _, receipt_ref, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    goal_check_path(
        repo_root=repo_root,
        session_id=task["session_id"],
        iteration=task["iteration"],
        workflow_id=task["workflow_id"],
    ).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "goal_met": True,
                "reason": "projection-only explanation",
                "eval_receipt_ref": receipt_ref,
            }
        ),
        encoding="utf-8",
    )
    control_path(repo_root=repo_root, session_id=task["session_id"]).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_id": "control-reason-mismatch",
                "state": "stopped",
                "reason": "claims the mismatched projection",
                "stop_reason": "goal_met",
                "producer": {
                    "session_id": task["session_id"],
                    "workflow_id": task["workflow_id"],
                    "attempt_id": task["attempt_id"],
                },
                "eval_receipt_ref": receipt_ref,
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    response = _finish(client, task)

    assert response["action"] == "run"
    updated = _read_state(repo_root, task["session_id"])
    assert updated.goal_met is False
    assert updated.history[-1].error == "invalid_control_output"
    failure = next(
        protocol_failures_dir_path(
            repo_root=repo_root, session_id=task["session_id"]
        ).glob("*.json")
    )
    assert "projection reason" in failure.read_text(encoding="utf-8")


def test_canonical_eval_report_must_be_eval_receipt_sibling(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    outside = (
        session_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "project_state"
        / "report.md"
    )
    outside.write_text("# Passing but misplaced\n", encoding="utf-8")
    payload["canonical_report_ref"] = "session:/project_state/report.md"
    payload["canonical_report_sha256"] = file_sha256(outside)

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any("eval_receipts sibling" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("identity_field", "wrong_value"),
    [
        ("root_session_id", "session-wrong-root"),
        ("session_id", "session-wrong"),
        ("workflow_id", "wrong-workflow"),
        ("iteration", 999),
        ("attempt_id", "wrong-attempt"),
        ("harness_run_id", "wrong-run"),
    ],
)
def test_raw_eval_report_trace_identity_must_match_receipt_producer(
    repo_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
    identity_field: str,
    wrong_value: object,
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, trace_root = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    update_trace_manifest(
        trace_root=trace_root, updates={"identity": {identity_field: wrong_value}}
    )

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    if identity_field in {"root_session_id", "session_id"}:
        expected = "reference is invalid"
    elif identity_field == "harness_run_id":
        expected = "harness run"
    else:
        expected = "trace identity"
    assert any(expected in reason for reason in reasons)


@pytest.mark.parametrize(
    ("subject_field", "wrong_value", "expected"),
    [
        ("git_commit", None, "must record its git commit"),
        ("dirty_tree_digest", None, "must record its dirty tree digest"),
        ("git_commit", "b" * 40, "git commit does not match"),
        ("dirty_tree_digest", "sha256:" + "5" * 64, "dirty tree digest"),
    ],
)
def test_passing_eval_subject_must_match_attempt_git_after_receipt(
    repo_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
    subject_field: str,
    wrong_value: object,
    expected: str,
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    subject = payload["subject"]
    assert isinstance(subject, dict)
    subject[subject_field] = wrong_value

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any(expected in reason for reason in reasons)


def test_passing_eval_subject_must_still_match_live_repository_at_acceptance(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    (repo_root / "late-untracked-change.txt").write_text(
        "changed after the worker's git-after receipt\n", encoding="utf-8"
    )

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any("does not match live repository" in reason for reason in reasons)


def test_eval_check_kind_must_match_yaml_definition_type(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    check_path = (
        session_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval_checks"
        / "judge-goal.yaml"
    )
    check_path.write_text(
        "schema_version: 1\nid: judge-goal\ntype: other_judge\n"
        "description: Wrong kind.\ninstructions: Inspect the result.\n",
        encoding="utf-8",
    )
    checks = payload["checks"]
    assert isinstance(checks, list)
    checks[0]["definition_sha256"] = file_sha256(check_path)

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any("kind does not match" in reason for reason in reasons)


def test_eval_receipt_must_cover_every_authored_check(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, _ = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    checks_dir = (
        session_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval_checks"
    )
    nested_checks = checks_dir / "security"
    nested_checks.mkdir()
    nested_checks.joinpath("security-review.yaml").write_text(
        "schema_version: 1\nid: security-review\ntype: harness_judge\n"
        "description: Review security.\ninstructions: Inspect the result.\n",
        encoding="utf-8",
    )

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any(
        "omitted authored checks: security-review" in reason for reason in reasons
    )


def test_eval_receipt_schema_failure_reports_actionable_field(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, _ = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload = _minimal_eval_receipt_payload()
    del payload["judge"]["reasoning_effort"]
    path = (
        eval_receipts_dir_path(repo_root=repo_root, session_id=task["session_id"])
        / "eval-invalid-schema.json"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    errors: list[str] = []

    receipt = service._load_eval_receipt(
        state_session_id=task["session_id"],
        reference="session:/eval_receipts/eval-invalid-schema.json",
        validation_errors=errors,
    )

    assert receipt is None
    assert any("judge.reasoning_effort" in error for error in errors)


def test_raw_eval_report_must_echo_the_exact_authored_check_hash(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, trace_root = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    raw_path = trace_root / "eval" / "report.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["checks"][0]["check_definition_sha256"] = "sha256:" + "9" * 64
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    raw_ref = payload["raw_report_refs"][0]
    payload["raw_report_sha256s"][raw_ref] = file_sha256(raw_path)

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any("definition hash does not match" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("project_root", "project root does not match repository"),
        ("output_dir", "output directory is not the canonical attempt eval path"),
        ("exit_code", "judge did not exit successfully"),
        ("reasoning_effort", "judge reasoning effort does not match"),
    ],
)
def test_passing_eval_report_must_match_exact_execution_context(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch, mutation: str, expected: str
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, trace_root = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    raw_path = trace_root / "eval" / "report.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    if mutation == "project_root":
        raw["project_root"] = str((repo_root.parent / "other-repository").resolve())
    elif mutation == "output_dir":
        raw["output_dir"] = str((trace_root / "eval-other").resolve())
    elif mutation == "exit_code":
        raw["checks"][0]["exit_code"] = 1
    else:
        raw["checks"][0]["details"]["reasoning_effort"] = "low"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    raw_ref = payload["raw_report_refs"][0]
    payload["raw_report_sha256s"][raw_ref] = file_sha256(raw_path)

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any(expected in reason for reason in reasons)


def test_eval_receipt_cannot_substitute_a_noncanonical_raw_report_path(
    repo_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, service, _, task, state = _setup_eval_task(
        repo_builder=repo_builder, monkeypatch=monkeypatch
    )
    payload, _, trace_root = _write_valid_eval_bundle(
        repo_root=repo_root, task=task, state=state
    )
    nested = trace_root / "eval" / "run-forged" / "report.json"
    nested.parent.mkdir()
    nested.write_bytes((trace_root / "eval" / "report.json").read_bytes())
    wrong_ref = f"trace:trace-{task['attempt_id']}:/eval/run-forged/report.json"
    payload["raw_report_refs"] = [wrong_ref]
    payload["raw_report_sha256s"] = {wrong_ref: file_sha256(nested)}

    reasons = service._validate_eval_receipt_artifacts(
        session_id=task["session_id"], receipt=EvalReceipt.model_validate(payload)
    )

    assert any("canonical attempt eval/report.json" in reason for reason in reasons)
