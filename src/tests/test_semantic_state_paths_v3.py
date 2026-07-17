from __future__ import annotations

import hashlib
import json
from pathlib import Path

from loopy_loop.assignments import build_attempt_assignment
from loopy_loop.models import AttemptAssignment
from loopy_loop.models import CurrentTask
from loopy_loop.models import utc_now
from loopy_loop.models import WorkflowSnapshotDescriptor
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import current_state_path
from loopy_loop.sessions import decisions_dir_path
from loopy_loop.sessions import eval_readiness_dir_path
from loopy_loop.sessions import eval_state_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import handoff_path
from loopy_loop.sessions import harness_capability_roster_path
from loopy_loop.sessions import layer_plan_path
from loopy_loop.sessions import scheduler_view_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import session_outcome_path
from loopy_loop.sessions import tasks_dir_path
from loopy_loop.sessions import workflow_contract_path
from loopy_loop.sessions import workflow_roster_path
from loopy_loop.sessions import write_json_atomic

_GOAL_HASH = "sha256:" + "a" * 64


def _v3_contract() -> dict[str, object]:
    """Return the smallest valid protocol-v3 orchestration contract."""

    return {
        "schema_version": 1,
        "session_protocol_version": 3,
        "layer_kind": "delivery",
        "roles": {
            "outer": {"responsibility": "Own this layer's plan and result"},
            "inner": {"responsibility": "Execute one selected leaf"},
        },
        "state": [],
        "eval": {},
        "orchestration": {
            "completion_role": "outer",
            "plan_owner": "outer",
            "handoff_owner": "outer",
            "task_acceptance_owner": "outer",
        },
        "evaluation": {
            "advisory": True,
            "check_author_roles": ["outer"],
            "check_runner_roles": ["outer"],
        },
        "terminal_blocker_reporting_roles": ["outer", "inner"],
        "child_interface": "none",
    }


def _workflow_roster(*, session_id: str) -> dict[str, object]:
    """Return compact scheduled-role context for one test session."""

    return {
        "schema_version": 1,
        "session_id": session_id,
        "workflow_contract_sha256": "sha256:" + "b" * 64,
        "created_at": "2026-07-17T10:00:00Z",
        "completion_role": "outer",
        "roles": [
            {
                "workflow_id": "outer",
                "responsibility": "Own this layer's plan and result",
                "cadence": {"run_every": 1},
                "expected_outputs": ["project_state/plan.md"],
                "authorities": ["goal_met"],
            }
        ],
    }


def _capability_roster(*, root_session_id: str) -> dict[str, object]:
    """Return compact tree-wide harness context for assignment tests."""

    return {
        "schema_version": 1,
        "root_session_id": root_session_id,
        "root_execution_config_sha256": "sha256:" + "c" * 64,
        "created_at": "2026-07-17T10:00:00Z",
        "coordinator": {"provider": "test", "model": "coordinator"},
        "tiers": {
            "frontier": "maximum capability",
            "strong": "complex work",
            "standard": "balanced default",
            "economy": "bounded work",
        },
        "harnesses": {
            "codex": {
                "standard": {
                    "available": True,
                    "model": "test-model",
                    "source": "configured_tier",
                }
            }
        },
        "default_tier": "standard",
    }


def _create_v3_session(
    *,
    repo_root: Path,
    session_id: str,
    parent_session_id: str | None = None,
    root_session_id: str | None = None,
    accepted_request_ref: str | None = None,
    accepted_request_sha256: str | None = None,
    frozen_input_files: dict[str, bytes] | None = None,
) -> Path:
    """Create one protocol-v3 session with its two frozen rosters."""

    return create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash=_GOAL_HASH,
        goal="Deliver one coherent outcome",
        workflow_set="inner_outer_eval",
        parent_session_id=parent_session_id,
        root_session_id=root_session_id,
        accepted_request_ref=accepted_request_ref,
        accepted_request_sha256=accepted_request_sha256,
        frozen_input_files=frozen_input_files,
        workflow_contract=_v3_contract(),
        workflow_roster_payload=_workflow_roster(session_id=session_id),
        harness_capability_roster_payload=_capability_roster(
            root_session_id=root_session_id or session_id
        ),
        session_protocol_version=3,
        schema_version=2,
    )


def _descriptor(*, repo_root: Path, session_id: str) -> WorkflowSnapshotDescriptor:
    """Build the immutable descriptor fields consumed by assignment creation."""

    snapshot_root = scheduler_view_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id="outer",
        attempt_id="attempt-one",
    ).parent
    return WorkflowSnapshotDescriptor(
        session_id=session_id,
        workflow_set="inner_outer_eval",
        workflow_id="outer",
        iteration=1,
        attempt_id="attempt-one",
        snapshot_root=str(snapshot_root.resolve()),
        workflow_config_path=str((snapshot_root / "config.yaml").resolve()),
        workflow_prompt_path=str((snapshot_root / "prompt.txt").resolve()),
        workflow_contract_path=str(
            (snapshot_root / "workflow_contract.yaml").resolve()
        ),
        root_config_snapshot_path=str(
            (snapshot_root / "root_config_snapshot.json").resolve()
        ),
        workflow_config_sha256="sha256:" + "d" * 64,
        workflow_prompt_sha256="sha256:" + "e" * 64,
        workflow_contract_sha256=file_sha256(
            path=workflow_contract_path(repo_root=repo_root, session_id=session_id)
        ),
        root_config_snapshot_sha256="sha256:" + "f" * 64,
    )


def _write_scheduler_view(*, repo_root: Path, session_id: str) -> Path:
    """Materialize the compact attempt-frozen scheduler context."""

    path = scheduler_view_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id="outer",
        attempt_id="attempt-one",
    )
    write_json_atomic(
        path=path,
        payload={
            "schema_version": 1,
            "session_id": session_id,
            "state_revision": 1,
            "attempt_id": "attempt-one",
            "workflow_roster_sha256": file_sha256(
                path=workflow_roster_path(repo_root=repo_root, session_id=session_id)
            ),
            "history_watermark": 0,
            "captured_at": "2026-07-17T10:00:00Z",
            "conditional_forecast": {
                "next_workflow_id": "inner",
                "reasons": ["normal cadence"],
                "assumptions": ["current attempt returns normally"],
            },
        },
    )
    return path


def _build_assignment(*, repo_root: Path, session_id: str) -> AttemptAssignment:
    """Build one outer assignment from the v3 filesystem artifacts."""

    descriptor = _descriptor(repo_root=repo_root, session_id=session_id)
    return build_attempt_assignment(
        repo_root=repo_root,
        task=CurrentTask(
            workflow_set="inner_outer_eval",
            workflow_id="outer",
            session_id=session_id,
            iteration=1,
            attempt_id="attempt-one",
            started_at=utc_now(),
            workflow_snapshot=descriptor,
        ),
        descriptor=descriptor,
        trace_root=repo_root / ".loopy_loop" / "traces" / "attempt-one",
        git_before_ref="session:/git_receipts/git-before.json",
    )


def test_v3_session_creation_materializes_semantic_spine(repo_root: Path) -> None:
    """Fresh v3 sessions expose compact state and retire eval-readiness."""

    session_id = "root-session"
    session_dir = _create_v3_session(repo_root=repo_root, session_id=session_id)

    assert layer_plan_path(repo_root=repo_root, session_id=session_id).is_file()
    assert tasks_dir_path(repo_root=repo_root, session_id=session_id).is_dir()
    assert current_state_path(repo_root=repo_root, session_id=session_id).is_file()
    assert decisions_dir_path(repo_root=repo_root, session_id=session_id).is_dir()
    assert eval_state_path(repo_root=repo_root, session_id=session_id).is_file()
    assert handoff_path(repo_root=repo_root, session_id=session_id).is_file()
    assert workflow_roster_path(repo_root=repo_root, session_id=session_id).is_file()
    assert harness_capability_roster_path(
        repo_root=repo_root, session_id=session_id
    ).is_file()
    assert not eval_readiness_dir_path(
        repo_root=repo_root, session_id=session_id
    ).exists()
    assert not session_outcome_path(repo_root=repo_root, session_id=session_id).exists()
    handoff = json.loads(
        handoff_path(repo_root=repo_root, session_id=session_id).read_text(
            encoding="utf-8"
        )
    )
    assert handoff["session_id"] == session_id
    assert handoff["revision"] == 0
    assert handoff["producer"] is None

    plan_path = layer_plan_path(repo_root=repo_root, session_id=session_id)
    plan_path.write_text("# Layer Plan\n\nAgent-owned revision\n", encoding="utf-8")
    _create_v3_session(repo_root=repo_root, session_id=session_id)
    assert plan_path.read_text(encoding="utf-8").endswith("Agent-owned revision\n")
    assert session_dir == session_dir_path(repo_root=repo_root, session_id=session_id)


def test_v3_assignment_names_layer_and_parent_paths(repo_root: Path) -> None:
    """Root and child envelopes use stable names and child-local input paths."""

    root_session_id = "root-session"
    child_session_id = "child-session"
    _create_v3_session(repo_root=repo_root, session_id=root_session_id)
    _write_scheduler_view(repo_root=repo_root, session_id=root_session_id)
    root_assignment = _build_assignment(repo_root=repo_root, session_id=root_session_id)

    required_paths = {
        "layer_goal",
        "layer_goal_contract",
        "layer_inputs",
        "layer_plan",
        "layer_tasks",
        "layer_current_state",
        "layer_decisions",
        "layer_finished_ledger",
        "layer_eval_state",
        "layer_handoff",
        "session_state",
        "session_outcome",
        "workflow_contract",
        "workflow_roster",
        "scheduler_view",
        "harness_capability_roster",
        "user_inputs",
        "child_requests",
        "children_index",
        "child_outcomes",
        "parent_acceptance",
        "git_receipts",
        "delivery_receipts",
        "session_control",
        "attempt_root",
        "trace_root",
    }
    assert required_paths <= set(root_assignment.absolute_paths)
    assert root_assignment.absolute_paths["parent_goal"] is None
    assert root_assignment.absolute_paths["parent_goal_contract"] is None
    assert root_assignment.absolute_paths["parent_handoff"] is None
    assert root_assignment.absolute_paths["accepted_child_request"] is None
    assert root_assignment.context["workflow_roster"]["completion_role"] == "outer"
    assert (
        root_assignment.context["scheduler_view"]["conditional_forecast"][
            "next_workflow_id"
        ]
        == "inner"
    )
    assert "workflow_roster_sha256" in root_assignment.provenance
    assert "scheduler_view_sha256" in root_assignment.provenance
    assert "harness_capability_roster_sha256" in root_assignment.provenance

    accepted_request = b'{"goal":"child"}\n'
    accepted_hash = "sha256:" + hashlib.sha256(accepted_request).hexdigest()
    _create_v3_session(
        repo_root=repo_root,
        session_id=child_session_id,
        parent_session_id=root_session_id,
        root_session_id=root_session_id,
        accepted_request_ref="session:/inputs/accepted_request.json",
        accepted_request_sha256=accepted_hash,
        frozen_input_files={"accepted_request.json": accepted_request},
    )
    _write_scheduler_view(repo_root=repo_root, session_id=child_session_id)
    child_assignment = _build_assignment(
        repo_root=repo_root, session_id=child_session_id
    )

    child_root = session_dir_path(repo_root=repo_root, session_id=child_session_id)
    assert child_assignment.absolute_paths["layer_inputs"] == str(
        (child_root / "inputs").resolve()
    )
    assert child_assignment.absolute_paths["parent_goal"] == str(
        (
            session_dir_path(repo_root=repo_root, session_id=root_session_id)
            / "goal.md"
        ).resolve()
    )
    assert child_assignment.absolute_paths["accepted_child_request"] == str(
        (child_root / "inputs" / "accepted_request.json").resolve()
    )
    assert child_assignment.absolute_paths["harness_capability_roster"] == str(
        harness_capability_roster_path(
            repo_root=repo_root, session_id=root_session_id
        ).resolve()
    )
    assert not (child_root / "harness_capability_roster.json").exists()
