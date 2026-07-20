from __future__ import annotations

from pathlib import Path

import pytest

from loopy_loop.config import run_preflight
from loopy_loop.contract_descriptors import build_contracts_descriptor
from loopy_loop.coordinator_app import _build_workflow_roster
from loopy_loop.models import ControlSignal
from loopy_loop.models import LayerHandoff
from loopy_loop.models import utc_now
from loopy_loop.models import WorkflowRoster
from loopy_loop.models import WorkflowRosterRole
from loopy_loop.models import WorkflowSetContract
from loopy_loop.references import LOGICAL_REFERENCE_ABSOLUTE_PATH_PREFIX
from loopy_loop.references import LOGICAL_REFERENCE_FORBIDDEN_CHARACTERS
from loopy_loop.references import LOGICAL_REFERENCE_IMPLICIT_SCOPES
from loopy_loop.references import LOGICAL_REFERENCE_INVALID_PATH_SEGMENTS
from loopy_loop.references import LOGICAL_REFERENCE_NAMED_SCOPES
from loopy_loop.references import LOGICAL_REFERENCE_PATH_MARKER

TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "loopy_loop" / "templates"


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


def _stock_inner_outer_contract_and_roster() -> tuple[
    WorkflowSetContract, WorkflowRoster
]:
    preflight = run_preflight(repo_root=TEMPLATES_ROOT / "inner_outer_eval")
    roster = _build_workflow_roster(
        session_id="session-stock-inner-outer",
        preflight=preflight,
        created_at=utc_now(),
    )
    return preflight.workflow_contract, roster


def test_descriptor_uses_engine_schema_and_reference_scope_constants() -> None:
    contract, roster = _stock_inner_outer_contract_and_roster()
    descriptor = build_contracts_descriptor(
        workflow_contract=contract, workflow_roster=roster
    )

    assert descriptor["layer_handoff"]["json_schema"] == (
        LayerHandoff.model_json_schema()
    )
    assert "json_schema" not in descriptor["terminal_control"]
    references = descriptor["logical_references"]
    assert references["implicit"] == {
        "shape": "<scope>:/<path>",
        "scopes": sorted(LOGICAL_REFERENCE_IMPLICIT_SCOPES),
    }
    assert references["named"] == {
        "shape": "<scope>:<id>:/<path>",
        "scopes": sorted(LOGICAL_REFERENCE_NAMED_SCOPES),
    }
    assert references["path_rules"] == {
        "path_must_be_relative_after_marker": True,
        "forbidden_absolute_path_prefix": LOGICAL_REFERENCE_ABSOLUTE_PATH_PREFIX,
        "forbidden_embedded_path_marker": LOGICAL_REFERENCE_PATH_MARKER,
        "forbidden_characters": sorted(LOGICAL_REFERENCE_FORBIDDEN_CHARACTERS),
        "forbidden_segments": sorted(LOGICAL_REFERENCE_INVALID_PATH_SEGMENTS),
    }
    assert descriptor["terminal_control"]["evidence_refs"] == {
        "value_kind": "logical_reference",
        "must_resolve_to": "file",
        "path_must_be_nonempty": True,
        "not_valid_reference_values": ["URL", "git SHA", "absolute filesystem path"],
        "grammar_and_path_rules": "#/logical_references",
    }


def test_stock_advisory_roster_omits_retired_eval_receipt_refs() -> None:
    contract, roster = _stock_inner_outer_contract_and_roster()
    assert contract.evaluation.advisory is True
    assert contract.check_runner_roles == ["outer"]
    outer = next(role for role in roster.roles if role.workflow_id == "outer")
    assert "project_state/eval_state.md" in outer.expected_outputs
    assert "eval_receipts/" not in outer.expected_outputs

    descriptor = build_contracts_descriptor(
        workflow_contract=contract, workflow_roster=roster
    )
    terminal_control = descriptor["terminal_control"]
    assert terminal_control["eval_receipt_refs"] == {
        "applicable": False,
        "check_runner_roles": ["outer"],
        "receipt_producing_check_runner_roles": [],
    }
    assert "eval_receipt_refs" not in terminal_control["accepted_fields"]
    assert "eval_receipt_ref" not in terminal_control["accepted_fields"]
    assert terminal_control["accepted_fields_are_authoritative"] is True
    assert set(terminal_control["accepted_field_schemas"]) == set(
        terminal_control["accepted_fields"]
    )


def test_eval_receipt_refs_remain_applicable_to_frozen_receipt_roster() -> None:
    contract = _workflow_contract()
    receipt_producing = build_contracts_descriptor(
        workflow_contract=contract,
        workflow_roster=_workflow_roster(expected_outputs=["eval_receipts/"]),
    )

    receipt_control = receipt_producing["terminal_control"]
    assert receipt_control["eval_receipt_refs"] == {
        "applicable": True,
        "check_runner_roles": ["eval_runner"],
        "receipt_producing_check_runner_roles": ["eval_runner"],
    }
    assert "eval_receipt_refs" in receipt_control["accepted_fields"]
    assert "eval_receipt_ref" not in receipt_control["accepted_fields"]
    assert set(receipt_control["accepted_fields"]) <= set(ControlSignal.model_fields)


def test_goal_met_example_round_trips_through_real_control_model() -> None:
    contract, roster = _stock_inner_outer_contract_and_roster()
    terminal_control = build_contracts_descriptor(
        workflow_contract=contract, workflow_roster=roster
    )["terminal_control"]

    accepted_fields = set(terminal_control["accepted_fields"])
    required_fields = terminal_control["required_fields"]
    assert required_fields == ControlSignal.required_field_names(
        schema_version=contract.session_protocol_version,
        state="stopped",
        stop_reason="goal_met",
    )
    assert {"control_id", "producer", "created_at"} <= set(required_fields)
    assert set(required_fields) <= accepted_fields

    example = terminal_control["goal_met_example"]
    assert example is not None
    payload = {field_name: example[field_name] for field_name in required_fields}
    assert set(payload) == set(required_fields)
    assert set(payload) <= accepted_fields
    validated = ControlSignal.model_validate(payload)
    assert validated.state == "stopped"
    assert validated.stop_reason == "goal_met"

    incomplete = dict(payload)
    incomplete.pop("control_id")
    with pytest.raises(ValueError):
        ControlSignal.model_validate(incomplete)
