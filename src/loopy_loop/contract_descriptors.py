from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from loopy_loop.models import ControlSignal
from loopy_loop.models import LayerHandoff
from loopy_loop.models import WorkflowRoster
from loopy_loop.models import WorkflowSetContract
from loopy_loop.references import LOGICAL_REFERENCE_IMPLICIT_SCOPES
from loopy_loop.references import LOGICAL_REFERENCE_NAMED_SCOPES
from loopy_loop.references import LOGICAL_REFERENCE_PATH_MARKER
from loopy_loop.sessions import EVAL_RECEIPTS_DIRNAME


def build_contracts_descriptor(
    *,
    workflow_contract: WorkflowSetContract | None,
    workflow_roster: WorkflowRoster | Mapping[str, object] | None,
) -> dict[str, Any]:
    """Describe agent-authored artifacts from their engine-owned definitions."""

    roster = _validated_roster(workflow_roster=workflow_roster)
    check_runner_roles = (
        workflow_contract.check_runner_roles if workflow_contract is not None else []
    )
    receipt_producing_roles = _receipt_producing_check_runner_roles(
        check_runner_roles=check_runner_roles, workflow_roster=roster
    )
    eval_receipt_refs_applicable = bool(receipt_producing_roles)
    protocol_version = (
        workflow_contract.session_protocol_version
        if workflow_contract is not None
        else None
    )

    return {
        "schema_version": 1,
        "layer_handoff": {
            "source": "loopy_loop.models.LayerHandoff",
            "json_schema": LayerHandoff.model_json_schema(),
        },
        "logical_references": {
            "source": "loopy_loop.references",
            "implicit": {
                "shape": f"<scope>{LOGICAL_REFERENCE_PATH_MARKER}<path>",
                "scopes": sorted(LOGICAL_REFERENCE_IMPLICIT_SCOPES),
            },
            "named": {
                "shape": f"<scope>:<id>{LOGICAL_REFERENCE_PATH_MARKER}<path>",
                "scopes": sorted(LOGICAL_REFERENCE_NAMED_SCOPES),
            },
        },
        "terminal_control": {
            "source": "loopy_loop.models.ControlSignal",
            "json_schema": ControlSignal.model_json_schema(),
            "active_protocol_version": protocol_version,
            "accepted_fields": ControlSignal.accepted_field_names(
                schema_version=protocol_version,
                eval_receipt_refs_applicable=eval_receipt_refs_applicable,
            ),
            "completion_role": (
                workflow_contract.completion_role
                if workflow_contract is not None
                else None
            ),
            "terminal_blocker_reporting_roles": (
                workflow_contract.terminal_blocker_reporting_roles
                if workflow_contract is not None
                else []
            ),
            "eval_receipt_refs": {
                "applicable": eval_receipt_refs_applicable,
                "check_runner_roles": check_runner_roles,
                "receipt_producing_check_runner_roles": receipt_producing_roles,
            },
        },
    }


def _validated_roster(
    *, workflow_roster: WorkflowRoster | Mapping[str, object] | None
) -> WorkflowRoster | None:
    """Use a valid frozen roster without making descriptor emission a new gate."""

    if workflow_roster is None:
        return None
    if isinstance(workflow_roster, WorkflowRoster):
        return workflow_roster
    try:
        return WorkflowRoster.model_validate(workflow_roster)
    except (ValidationError, ValueError):
        return None


def _receipt_producing_check_runner_roles(
    *, check_runner_roles: list[str], workflow_roster: WorkflowRoster | None
) -> list[str]:
    """Return declared runners whose frozen roster advertises receipt output."""

    if workflow_roster is None:
        return []
    declared = set(check_runner_roles)
    return sorted(
        role.workflow_id
        for role in workflow_roster.roles
        if role.workflow_id in declared
        and any(_is_eval_receipt_output(path=path) for path in role.expected_outputs)
    )


def _is_eval_receipt_output(*, path: str) -> bool:
    normalized = path.strip().rstrip("/")
    return normalized == EVAL_RECEIPTS_DIRNAME or normalized.startswith(
        f"{EVAL_RECEIPTS_DIRNAME}/"
    )
