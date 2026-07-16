from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from pydantic import ValidationError
import yaml

from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.config import PreflightResult
from loopy_loop.config import ROOT_CONFIG_FILENAME
from loopy_loop.config import WorkflowDefinition
from loopy_loop.models import AttemptAssignment
from loopy_loop.models import CurrentTask
from loopy_loop.models import GoalContract
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import SessionManifest
from loopy_loop.models import WorkflowSetContract
from loopy_loop.models import WorkflowSnapshotDescriptor
from loopy_loop.references import LogicalReferenceError
from loopy_loop.references import resolve_logical_reference
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import child_outcomes_dir_path
from loopy_loop.sessions import child_requests_pending_dir_path
from loopy_loop.sessions import child_sessions_dir_path
from loopy_loop.sessions import children_path
from loopy_loop.sessions import control_path
from loopy_loop.sessions import control_rejected_dir_path
from loopy_loop.sessions import delivery_receipts_dir_path
from loopy_loop.sessions import eval_checks_dir_path
from loopy_loop.sessions import eval_readiness_dir_path
from loopy_loop.sessions import eval_receipts_dir_path
from loopy_loop.sessions import file_sha256
from loopy_loop.sessions import finished_path
from loopy_loop.sessions import git_receipts_dir_path
from loopy_loop.sessions import goal_contract_path
from loopy_loop.sessions import parent_acceptance_dir_path
from loopy_loop.sessions import project_state_dir_path
from loopy_loop.sessions import protocol_failures_dir_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import session_goal_path
from loopy_loop.sessions import state_path
from loopy_loop.sessions import user_updates_journal_path
from loopy_loop.sessions import workflow_contract_path
from loopy_loop.sessions import workflow_snapshot_dir_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.sessions import write_text_atomic

REPOSITORY_IDENTITY_FILENAME = "repository.json"
SNAPSHOT_MANIFEST_FILENAME = "manifest.json"


class AssignmentContractError(RuntimeError):
    pass


def repository_identity_path(*, repo_root: Path) -> Path:
    """Return the canonical path for the checkout identity document."""

    return repo_root.resolve() / LOOPY_DIRNAME / REPOSITORY_IDENTITY_FILENAME


def ensure_repository_identity(*, repo_root: Path) -> dict[str, object]:
    """Return a stable checkout identity, creating it once when absent."""
    root = repo_root.resolve()
    path = repository_identity_path(repo_root=root)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AssignmentContractError(
                f"repository identity is unreadable at {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("repository_id"), str
        ):
            raise AssignmentContractError(
                f"repository identity has an invalid schema at {path}"
            )
        return payload
    remote = _remote_fingerprint(repo_root=root)
    config_path = root / ROOT_CONFIG_FILENAME
    payload = {
        "schema_version": 1,
        "repository_id": f"repo-{uuid.uuid4().hex}",
        "config_sha256": (
            file_sha256(path=config_path) if config_path.exists() else None
        ),
        "remote_fingerprint": remote,
    }
    write_json_atomic(path=path, payload=payload)
    return payload


def repository_id(*, repo_root: Path) -> str:
    """Return the stable repository identifier for this checkout."""

    value = ensure_repository_identity(repo_root=repo_root).get("repository_id")
    assert isinstance(value, str)
    return value


def materialize_workflow_snapshot(
    *,
    repo_root: Path,
    task: CurrentTask,
    workflow: WorkflowDefinition,
    preflight: PreflightResult,
    config_snapshot: RootConfigSnapshot | None = None,
) -> WorkflowSnapshotDescriptor:
    """Freeze the workflow inputs selected for one dispatched attempt."""

    root = repo_root.resolve()
    snapshot_root = workflow_snapshot_dir_path(
        repo_root=root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
        attempt_id=task.attempt_id or "legacy",
    ).resolve()
    snapshot_root.mkdir(parents=True, exist_ok=True)
    config_path = snapshot_root / "config.yaml"
    prompt_path = snapshot_root / "prompt.txt"
    contract_path = snapshot_root / "workflow_contract.yaml"
    root_config_path = snapshot_root / "root_config_snapshot.json"
    manifest_path = snapshot_root / SNAPSHOT_MANIFEST_FILENAME

    root_config_payload = (
        config_snapshot.model_dump()
        if config_snapshot is not None
        else preflight.root_config.model_dump(
            exclude={
                "recovery_policy",
                "recovery_drain_timeout_s",
                "workflow_consecutive_failures_cap",
                "max_cost_usd",
                "model_prices",
                "model_tiers",
                "default_tier",
            }
        )
    )
    root_config_snapshot_sha256 = _sha256_text(
        value=json.dumps(root_config_payload, indent=2)
    )

    persisted_contract_path = workflow_contract_path(
        repo_root=root, session_id=task.session_id
    )
    try:
        persisted_contract_text = persisted_contract_path.read_text(encoding="utf-8")
        WorkflowSetContract.model_validate(yaml.safe_load(persisted_contract_text))
    except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
        raise AssignmentContractError(
            f"invalid immutable session workflow contract: {exc}"
        ) from exc
    persisted_contract_sha256 = file_sha256(path=persisted_contract_path)

    expected = {
        "schema_version": 1,
        "session_id": task.session_id,
        "iteration": task.iteration,
        "attempt_id": task.attempt_id,
        "workflow_set": task.workflow_set,
        "workflow_id": task.workflow_id,
        "workflow_config_sha256": workflow.config_sha256,
        "workflow_prompt_sha256": workflow.prompt_sha256,
        "workflow_contract_sha256": persisted_contract_sha256,
        "root_config_snapshot_sha256": root_config_snapshot_sha256,
        "repository_id": repository_id(repo_root=root),
        "tree_system_extension_sha256": _sha256_text(
            value=(
                config_snapshot.team_harness_system_prompt_extension
                if config_snapshot is not None
                else preflight.root_config.team_harness_system_prompt_extension
            )
        ),
    }
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AssignmentContractError(
                f"workflow snapshot manifest is unreadable at {manifest_path}: {exc}"
            ) from exc
        if existing != expected:
            raise AssignmentContractError(
                f"workflow snapshot for attempt {task.attempt_id} contradicts "
                "the scheduler-selected sources"
            )
    else:
        write_text_atomic(path=config_path, content=workflow.config_text)
        write_text_atomic(path=prompt_path, content=workflow.prompt_text)
        write_text_atomic(path=contract_path, content=persisted_contract_text)
        write_json_atomic(path=root_config_path, payload=root_config_payload)
        write_json_atomic(path=manifest_path, payload=expected)

    _verify_hash(path=config_path, expected=workflow.config_sha256)
    _verify_hash(path=prompt_path, expected=workflow.prompt_sha256)
    _verify_hash(path=contract_path, expected=persisted_contract_sha256)
    _verify_hash(path=root_config_path, expected=root_config_snapshot_sha256)
    return WorkflowSnapshotDescriptor(
        session_id=task.session_id,
        workflow_set=task.workflow_set,
        workflow_id=task.workflow_id,
        iteration=task.iteration,
        attempt_id=task.attempt_id or "",
        snapshot_root=str(snapshot_root),
        workflow_config_path=str(config_path.resolve()),
        workflow_prompt_path=str(prompt_path.resolve()),
        workflow_contract_path=str(contract_path.resolve()),
        root_config_snapshot_path=str(root_config_path.resolve()),
        workflow_config_sha256=workflow.config_sha256,
        workflow_prompt_sha256=workflow.prompt_sha256,
        workflow_contract_sha256=persisted_contract_sha256,
        root_config_snapshot_sha256=root_config_snapshot_sha256,
    )


def verify_workflow_snapshot(
    *,
    descriptor: WorkflowSnapshotDescriptor,
    repo_root: Path,
    expected_task: CurrentTask,
) -> tuple[dict[str, object], str, WorkflowSetContract, RootConfigSnapshot]:
    """Verify and load every immutable member of an attempt snapshot."""

    root = repo_root.resolve()
    snapshot_root = Path(descriptor.snapshot_root).resolve()
    session_runtime_root = root / LOOPY_DIRNAME / "sessions"
    if not snapshot_root.is_relative_to(session_runtime_root):
        raise AssignmentContractError(
            f"workflow snapshot is outside this repository: {snapshot_root}"
        )
    expected_identity = {
        "session_id": expected_task.session_id,
        "workflow_set": expected_task.workflow_set,
        "workflow_id": expected_task.workflow_id,
        "iteration": expected_task.iteration,
        "attempt_id": expected_task.attempt_id or "",
    }
    actual_identity = {
        "session_id": descriptor.session_id,
        "workflow_set": descriptor.workflow_set,
        "workflow_id": descriptor.workflow_id,
        "iteration": descriptor.iteration,
        "attempt_id": descriptor.attempt_id,
    }
    if actual_identity != expected_identity:
        raise AssignmentContractError(
            "workflow snapshot identity does not match the dispatched attempt"
        )
    expected_root = workflow_snapshot_dir_path(
        repo_root=root,
        session_id=expected_task.session_id,
        iteration=expected_task.iteration,
        workflow_id=expected_task.workflow_id,
        attempt_id=expected_task.attempt_id or "legacy",
    ).resolve()
    if snapshot_root != expected_root:
        raise AssignmentContractError(
            "workflow snapshot path does not match the dispatched attempt"
        )
    for raw_path in (
        descriptor.workflow_config_path,
        descriptor.workflow_prompt_path,
        descriptor.workflow_contract_path,
        descriptor.root_config_snapshot_path,
    ):
        path = Path(raw_path)
        if not path.is_absolute() or not path.resolve().is_relative_to(snapshot_root):
            raise AssignmentContractError(
                f"workflow snapshot member escapes snapshot root: {path}"
            )
    config_path = Path(descriptor.workflow_config_path)
    prompt_path = Path(descriptor.workflow_prompt_path)
    contract_path = Path(descriptor.workflow_contract_path)
    root_config_path = Path(descriptor.root_config_snapshot_path)
    _verify_hash(path=config_path, expected=descriptor.workflow_config_sha256)
    _verify_hash(path=prompt_path, expected=descriptor.workflow_prompt_sha256)
    _verify_hash(path=contract_path, expected=descriptor.workflow_contract_sha256)
    _verify_hash(path=root_config_path, expected=descriptor.root_config_snapshot_sha256)
    manifest_path = snapshot_root / SNAPSHOT_MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssignmentContractError(
            f"invalid workflow snapshot manifest at {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise AssignmentContractError(
            f"workflow snapshot manifest is not an object at {manifest_path}"
        )
    for key, value in {
        **expected_identity,
        "workflow_config_sha256": descriptor.workflow_config_sha256,
        "workflow_prompt_sha256": descriptor.workflow_prompt_sha256,
        "workflow_contract_sha256": descriptor.workflow_contract_sha256,
        "root_config_snapshot_sha256": descriptor.root_config_snapshot_sha256,
        "repository_id": repository_id(repo_root=root),
    }.items():
        if manifest.get(key) != value:
            raise AssignmentContractError(
                f"workflow snapshot manifest field {key!r} contradicts the attempt"
            )
    try:
        config_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config_payload, dict):
            raise ValueError("workflow config must be a mapping")
        prompt = prompt_path.read_text(encoding="utf-8")
        contract = WorkflowSetContract.model_validate(
            yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        )
        root_config = RootConfigSnapshot.model_validate_json(
            root_config_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
        raise AssignmentContractError(
            f"invalid frozen workflow snapshot: {exc}"
        ) from exc
    return config_payload, prompt, contract, root_config


def build_attempt_assignment(
    *,
    repo_root: Path,
    task: CurrentTask,
    descriptor: WorkflowSnapshotDescriptor,
    trace_root: Path,
    git_before_ref: str,
) -> AttemptAssignment:
    """Build the identity-bound, absolute-path envelope for an attempt."""

    root = repo_root.resolve()
    session_root = session_dir_path(
        repo_root=root, session_id=task.session_id
    ).resolve()
    manifest = _load_model(
        path=session_root / "session.json",
        model=SessionManifest,
        label="session manifest",
    )
    goal_contract = _load_model(
        path=goal_contract_path(repo_root=root, session_id=task.session_id),
        model=GoalContract,
        label="goal contract",
    )
    contract = _load_model(
        path=workflow_contract_path(repo_root=root, session_id=task.session_id),
        model=WorkflowSetContract,
        label="workflow contract",
    )
    frozen_goal_path = goal_contract_path(repo_root=root, session_id=task.session_id)
    frozen_workflow_path = workflow_contract_path(
        repo_root=root, session_id=task.session_id
    )
    if file_sha256(path=frozen_goal_path) != manifest.goal_contract_hash:
        raise AssignmentContractError(
            "session goal contract no longer matches its manifest hash"
        )
    if file_sha256(path=frozen_workflow_path) != manifest.workflow_contract_hash:
        raise AssignmentContractError(
            "session workflow contract no longer matches its manifest hash"
        )
    if descriptor.workflow_contract_sha256 != manifest.workflow_contract_hash:
        raise AssignmentContractError(
            "attempt snapshot is not bound to the session workflow contract"
        )
    parent_root = (
        session_dir_path(
            repo_root=root, session_id=manifest.parent_session_id
        ).resolve()
        if manifest.parent_session_id
        else session_root
    )
    root_session_root = session_dir_path(
        repo_root=root, session_id=manifest.root_session_id
    ).resolve()
    iteration_root = assignment_path(
        repo_root=root,
        session_id=task.session_id,
        iteration=task.iteration,
        workflow_id=task.workflow_id,
        attempt_id=task.attempt_id or "legacy",
    ).parent.resolve()
    paths = {
        "repo_root": root,
        "root_session_root": root_session_root,
        "root_state": state_path(repo_root=root, session_id=manifest.root_session_id),
        "session_root": session_root,
        "parent_session_root": parent_root,
        "goal": session_goal_path(repo_root=root, session_id=task.session_id),
        "goal_contract": goal_contract_path(repo_root=root, session_id=task.session_id),
        "project_state": project_state_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "finished_ledger": finished_path(repo_root=root, session_id=task.session_id),
        "eval_checks": eval_checks_dir_path(repo_root=root, session_id=task.session_id),
        "eval_readiness": eval_readiness_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "eval_receipts": eval_receipts_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "children_index": children_path(repo_root=root, session_id=task.session_id),
        "children_root": child_sessions_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "child_requests": child_requests_pending_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "child_outcomes": child_outcomes_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "parent_acceptance": parent_acceptance_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "user_inputs": user_updates_journal_path(
            repo_root=root, session_id=task.session_id
        ),
        "control": control_path(repo_root=root, session_id=task.session_id),
        "control_rejected": control_rejected_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "protocol_failures": protocol_failures_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "git_receipts": git_receipts_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "delivery_receipts": delivery_receipts_dir_path(
            repo_root=root, session_id=task.session_id
        ),
        "attempt_root": iteration_root,
        "workflow_snapshot": Path(descriptor.snapshot_root),
        "trace_root": trace_root.resolve(),
        "raw_eval_output": trace_root.resolve() / "eval",
    }
    accepted_request_ref = goal_contract.accepted_request_ref
    if accepted_request_ref is not None:
        try:
            accepted_request_path = resolve_logical_reference(
                reference=accepted_request_ref,
                repo_root=root,
                session_id=task.session_id,
            )
        except LogicalReferenceError as exc:
            raise AssignmentContractError(
                f"accepted child request reference is invalid: {exc}"
            ) from exc
        if (
            goal_contract.accepted_request_sha256 is None
            or not accepted_request_path.is_file()
            or file_sha256(path=accepted_request_path)
            != goal_contract.accepted_request_sha256
        ):
            raise AssignmentContractError(
                "accepted child request no longer matches its frozen hash"
            )
        paths["accepted_request"] = accepted_request_path
    input_artifacts: list[dict[str, str]] = []
    for item in goal_contract.inputs:
        try:
            input_path = resolve_logical_reference(
                reference=item.ref, repo_root=root, session_id=task.session_id
            )
        except LogicalReferenceError as exc:
            raise AssignmentContractError(
                f"child input reference is invalid ({item.ref}): {exc}"
            ) from exc
        if not input_path.is_file() or file_sha256(path=input_path) != item.sha256:
            raise AssignmentContractError(
                f"child input no longer matches its frozen hash: {item.ref}"
            )
        input_artifacts.append(
            {
                "ref": item.ref,
                "sha256": item.sha256,
                "absolute_path": str(input_path.resolve()),
            }
        )
    absolute_paths = {key: str(path.resolve()) for key, path in paths.items()}
    role = contract.roles.get(task.workflow_id)
    return AttemptAssignment(
        identity={
            "root_session_id": manifest.root_session_id,
            "session_id": task.session_id,
            "parent_session_id": manifest.parent_session_id,
            "depth": manifest.depth,
            "request_id": manifest.origin.get("request_id"),
            "work_item_id": manifest.origin.get("parent_work_item_id"),
            "workflow_set": task.workflow_set,
            "workflow_id": task.workflow_id,
            "iteration": task.iteration,
            "attempt_id": task.attempt_id,
        },
        actor={
            "kind": "harness_coordinator",
            "workflow_role": task.workflow_id,
            "layer_kind": manifest.layer_kind,
            "responsibility": role.responsibility if role else "workflow assignment",
        },
        objective={
            "goal_ref": "session:/goal.md",
            "goal_contract_ref": "session:/goal_contract.json",
            "goal_hash": goal_contract.goal_hash,
            "assignment": role.responsibility if role else task.workflow_id,
            "expected_outputs": goal_contract.deliverables,
            "required_evidence": goal_contract.required_evidence,
            "accepted_request_ref": goal_contract.accepted_request_ref,
            "accepted_request_sha256": goal_contract.accepted_request_sha256,
            "input_artifacts": input_artifacts,
        },
        absolute_paths=absolute_paths,
        ownership={
            "own_session": "write according to workflow role and integrate delegated work",
            "parent_session": "read/reference; communicate through typed receipts",
            "engine_state": "read only",
        },
        provenance={
            "repository_id": repository_id(repo_root=root),
            "root_config_sha256": descriptor.root_config_snapshot_sha256,
            "workflow_config_sha256": descriptor.workflow_config_sha256,
            "workflow_prompt_sha256": descriptor.workflow_prompt_sha256,
            "workflow_contract_sha256": descriptor.workflow_contract_sha256,
            "goal_contract_sha256": file_sha256(
                path=goal_contract_path(repo_root=root, session_id=task.session_id)
            ),
            "git_before_ref": git_before_ref,
        },
    )


def write_attempt_assignment(*, path: Path, assignment: AttemptAssignment) -> None:
    """Persist an attempt envelope, preserving a conflicting non-file object."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        # D8 repair: preserve a conflicting agent-created object, then restore
        # the coordinator-derived envelope at its canonical path. Atomic file
        # replacement already handles an ordinary mutated regular file.
        conflict = path.with_name(
            f"{path.name}.protocol-conflict-{uuid.uuid4().hex[:12]}"
        )
        path.rename(conflict)
    write_json_atomic(path=path, payload=assignment.model_dump(mode="json"))


def _load_model(*, path: Path, model: type, label: str):
    """Load and validate a persisted Pydantic contract model."""

    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise AssignmentContractError(f"invalid {label} at {path}: {exc}") from exc


def _verify_hash(*, path: Path, expected: str) -> None:
    """Raise when an artifact is absent or differs from its frozen digest."""

    try:
        actual = file_sha256(path=path)
    except OSError as exc:
        raise AssignmentContractError(
            f"missing snapshot artifact {path}: {exc}"
        ) from exc
    if actual != expected:
        raise AssignmentContractError(
            f"snapshot artifact hash mismatch for {path}: expected {expected}, got {actual}"
        )


def _sha256_text(*, value: str) -> str:
    """Return the contract's prefixed SHA-256 representation of text."""

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _remote_fingerprint(*, repo_root: Path) -> str | None:
    """Return a stable fingerprint for the configured origin, when present."""

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    if not value:
        return None
    # Strip userinfo and query/fragment material that may carry credentials.
    if "://" in value:
        scheme, rest = value.split("://", 1)
        rest = rest.split("@", 1)[-1]
        value = f"{scheme}://{rest}"
    value = value.split("?", 1)[0].split("#", 1)[0]
    return _sha256_text(value=value)
