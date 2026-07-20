from __future__ import annotations

from loopy_loop.contract_descriptors import build_contracts_descriptor
from loopy_loop.models import ControlSignal
from loopy_loop.models import LayerHandoff
from loopy_loop.models import utc_now
from loopy_loop.models import WorkflowRoster
from loopy_loop.models import WorkflowRosterRole
from loopy_loop.models import WorkflowSetContract
from loopy_loop.references import LOGICAL_REFERENCE_IMPLICIT_SCOPES
from loopy_loop.references import LOGICAL_REFERENCE_NAMED_SCOPES


def _workflow_contract() -> WorkflowSetContract:
    return WorkflowSetContract.model_validate(
        {
            "schema_version": 1,
            "session_protocol_version": 3,
            "roles": {
                "outer": {"responsibility": "Orchestrate."},
                "eval_runner": {"responsibility": "Run checks."},
            },
            "orchestration": {
                "completion_role": "outer",
                "plan_owner": "outer",
                "handoff_owner": "outer",
                "task_acceptance_owner": "outer",
            },
            "evaluation": {
                "advisory": True,
                "check_author_roles": [],
                "check_runner_roles": ["eval_runner"],
            },
            "terminal_blocker_reporting_roles": ["outer", "eval_runner"],
        }
    )


def _workflow_roster(*, expected_outputs: list[str]) -> WorkflowRoster:
    return WorkflowRoster(
        session_id="session-contract-test",
        workflow_contract_sha256="sha256:" + "a" * 64,
        created_at=utc_now(),
        completion_role="outer",
        roles=[
            WorkflowRosterRole(
                workflow_id="outer",
                responsibility="Orchestrate.",
                cadence={},
                expected_outputs=["project_state/handoff.json"],
            ),
            WorkflowRosterRole(
                workflow_id="eval_runner",
                responsibility="Run checks.",
                cadence={},
                expected_outputs=expected_outputs,
                authorities=["eval_check_runner"],
            ),
        ],
    )


def test_descriptor_uses_engine_schema_and_reference_scope_constants() -> None:
    descriptor = build_contracts_descriptor(
        workflow_contract=_workflow_contract(),
        workflow_roster=_workflow_roster(
            expected_outputs=["project_state/eval_state.md"]
        ),
    )

    assert descriptor["layer_handoff"]["json_schema"] == (
        LayerHandoff.model_json_schema()
    )
    assert descriptor["terminal_control"]["json_schema"] == (
        ControlSignal.model_json_schema()
    )
    references = descriptor["logical_references"]
    assert references["implicit"] == {
        "shape": "<scope>:/<path>",
        "scopes": sorted(LOGICAL_REFERENCE_IMPLICIT_SCOPES),
    }
    assert references["named"] == {
        "shape": "<scope>:<id>:/<path>",
        "scopes": sorted(LOGICAL_REFERENCE_NAMED_SCOPES),
    }


def test_eval_receipt_refs_applicability_follows_frozen_runner_outputs() -> None:
    contract = _workflow_contract()
    retired = build_contracts_descriptor(
        workflow_contract=contract,
        workflow_roster=_workflow_roster(
            expected_outputs=["project_state/eval_state.md"]
        ),
    )
    receipt_producing = build_contracts_descriptor(
        workflow_contract=contract,
        workflow_roster=_workflow_roster(expected_outputs=["eval_receipts/"]),
    )

    retired_control = retired["terminal_control"]
    assert retired_control["eval_receipt_refs"] == {
        "applicable": False,
        "check_runner_roles": ["eval_runner"],
        "receipt_producing_check_runner_roles": [],
    }
    assert "eval_receipt_refs" not in retired_control["accepted_fields"]

    receipt_control = receipt_producing["terminal_control"]
    assert receipt_control["eval_receipt_refs"] == {
        "applicable": True,
        "check_runner_roles": ["eval_runner"],
        "receipt_producing_check_runner_roles": ["eval_runner"],
    }
    assert "eval_receipt_refs" in receipt_control["accepted_fields"]
    assert "eval_receipt_ref" not in receipt_control["accepted_fields"]
    assert set(receipt_control["accepted_fields"]) <= set(ControlSignal.model_fields)
