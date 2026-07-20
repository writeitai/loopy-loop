from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from loopy_loop.models import ControlSignal
from loopy_loop.models import LayerHandoff
from loopy_loop.models import WorkflowRoster
from loopy_loop.models import WorkflowSetContract
from loopy_loop.references import LOGICAL_REFERENCE_ABSOLUTE_PATH_PREFIX
from loopy_loop.references import LOGICAL_REFERENCE_FORBIDDEN_CHARACTERS
from loopy_loop.references import LOGICAL_REFERENCE_IMPLICIT_SCOPES
from loopy_loop.references import LOGICAL_REFERENCE_INVALID_PATH_SEGMENTS
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
    accepted_fields = ControlSignal.accepted_field_names(
        schema_version=protocol_version,
        eval_receipt_refs_applicable=eval_receipt_refs_applicable,
    )
    required_fields = ControlSignal.required_field_names(
        schema_version=protocol_version, state="stopped", stop_reason="goal_met"
    )
    completion_role = (
        workflow_contract.completion_role if workflow_contract is not None else None
    )
    goal_met_example = _goal_met_control_example(
        protocol_version=protocol_version,
        completion_role=completion_role,
        session_id=roster.session_id if roster is not None else None,
        accepted_fields=accepted_fields,
        required_fields=required_fields,
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
            "path_rules": {
                "path_must_be_relative_after_marker": True,
                "forbidden_absolute_path_prefix": (
                    LOGICAL_REFERENCE_ABSOLUTE_PATH_PREFIX
                ),
                "forbidden_embedded_path_marker": LOGICAL_REFERENCE_PATH_MARKER,
                "forbidden_characters": sorted(LOGICAL_REFERENCE_FORBIDDEN_CHARACTERS),
                "forbidden_segments": sorted(LOGICAL_REFERENCE_INVALID_PATH_SEGMENTS),
            },
        },
        "terminal_control": {
            "source": "loopy_loop.models.ControlSignal",
            "active_protocol_version": protocol_version,
            "accepted_fields": accepted_fields,
            "accepted_fields_are_authoritative": True,
            "required_fields": required_fields,
            "required_fields_for": {"state": "stopped", "stop_reason": "goal_met"},
            "accepted_field_schemas": _accepted_control_field_schemas(
                accepted_fields=accepted_fields
            ),
            "goal_met_example": goal_met_example,
            "completion_role": completion_role,
            "terminal_blocker_reporting_roles": (
                workflow_contract.terminal_blocker_reporting_roles
                if workflow_contract is not None
                else []
            ),
            "evidence_refs": {
                "value_kind": "logical_reference",
                "must_resolve_to": "file",
                "path_must_be_nonempty": True,
                "not_valid_reference_values": [
                    "URL",
                    "git SHA",
                    "absolute filesystem path",
                ],
                "grammar_and_path_rules": "#/logical_references",
            },
            "eval_receipt_refs": {
                "applicable": eval_receipt_refs_applicable,
                "check_runner_roles": check_runner_roles,
                "receipt_producing_check_runner_roles": receipt_producing_roles,
            },
        },
    }


def _accepted_control_field_schemas(*, accepted_fields: list[str]) -> dict[str, object]:
    """Return model-owned field schemas without exposing other protocol versions."""

    schema = ControlSignal.model_json_schema()
    properties = schema.get("properties", {})
    definitions = schema.get("$defs", {})
    return {
        field_name: _inline_local_schema_refs(
            value=properties[field_name], definitions=definitions
        )
        for field_name in accepted_fields
        if field_name in properties
    }


def _inline_local_schema_refs(
    *, value: object, definitions: Mapping[str, object]
) -> object:
    """Inline local Pydantic definitions so each emitted field schema stands alone."""

    if isinstance(value, list):
        return [
            _inline_local_schema_refs(value=item, definitions=definitions)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        definition = definitions.get(reference.removeprefix("#/$defs/"))
        if isinstance(definition, dict):
            value = {
                **definition,
                **{key: item for key, item in value.items() if key != "$ref"},
            }
    return {
        key: _inline_local_schema_refs(value=item, definitions=definitions)
        for key, item in value.items()
    }


def _goal_met_control_example(
    *,
    protocol_version: int | None,
    completion_role: str | None,
    session_id: str | None,
    accepted_fields: list[str],
    required_fields: list[str],
) -> dict[str, object] | None:
    """Build and model-validate the minimal required goal-met authoring example."""

    if protocol_version is None:
        return None
    missing_from_contract = set(required_fields) - set(accepted_fields)
    if missing_from_contract:
        raise ValueError(
            "required terminal-control fields are not accepted: "
            f"{sorted(missing_from_contract)}"
        )
    placeholders: dict[str, object] = {
        "schema_version": protocol_version,
        "control_id": "control-goal-met-current-attempt",
        "state": "stopped",
        "stop_reason": "goal_met",
        "reason": "Explain why this layer's scoped goal is complete.",
        "producer": {
            "session_id": session_id or "current-session-id",
            "workflow_id": completion_role or "declared-completion-role",
            "attempt_id": "current-attempt-id",
        },
        "eval_receipt_ref": "session:/eval_receipts/current-eval-receipt.json",
        "created_at": "1970-01-01T00:00:00Z",
    }
    example = {field_name: placeholders[field_name] for field_name in required_fields}
    ControlSignal.model_validate(example)
    return example


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
