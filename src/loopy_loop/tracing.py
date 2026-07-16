from __future__ import annotations

from datetime import datetime
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import attempt_trace_dir_path
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import trace_export_outbox_dir_path
from loopy_loop.sessions import trace_ref_path
from loopy_loop.sessions import trace_seal_receipt_path
from loopy_loop.sessions import traces_root_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.sessions import write_text_atomic

TRACE_MANIFEST_FILENAME = "trace_manifest.json"


class TraceError(RuntimeError):
    pass


def create_attempt_trace(
    *,
    repo_root: Path,
    root_session_id: str,
    session_id: str,
    request_id: str | None,
    work_item_id: str | None,
    workflow_set: str,
    workflow_id: str,
    iteration: int,
    attempt_id: str,
) -> tuple[Path, dict[str, Any]]:
    """Create or reopen the canonical raw local trace for one exact attempt."""

    trace_root = attempt_trace_dir_path(
        repo_root=repo_root.resolve(),
        root_session_id=root_session_id,
        session_id=session_id,
        attempt_id=attempt_id,
    ).resolve()
    trace_root.mkdir(parents=True, exist_ok=True)
    for name in ("protocol", "harness", "agents", "eval", "git", "service"):
        (trace_root / name).mkdir(parents=True, exist_ok=True)
    manifest_path = trace_root / TRACE_MANIFEST_FILENAME
    expected_identity = {
        "root_session_id": root_session_id,
        "session_id": session_id,
        "request_id": request_id,
        "work_item_id": work_item_id,
        "workflow_set": workflow_set,
        "workflow_id": workflow_id,
        "iteration": iteration,
        "attempt_id": attempt_id,
    }
    if manifest_path.exists():
        existing = _read_manifest(path=manifest_path)
        if (
            existing.get("schema_version") != 1
            or existing.get("manifest_id") != f"trace-{attempt_id}"
            or existing.get("identity")
            != {
                **expected_identity,
                "harness_run_id": existing.get("identity", {}).get("harness_run_id")
                if isinstance(existing.get("identity"), dict)
                else None,
            }
            or existing.get("lifecycle") != "active"
        ):
            raise TraceError(
                f"attempt trace already exists with contradictory identity: "
                f"{manifest_path}"
            )
        return trace_root, existing
    manifest = {
        "schema_version": 1,
        "manifest_id": f"trace-{attempt_id}",
        "lifecycle": "active",
        "identity": {**expected_identity, "harness_run_id": None},
        "channels": {
            "loopy_assignment": "pending",
            "coordinator_input": "pending",
            "coordinator_output": "pending",
            "direct_agents": "pending",
            "provider_native_nested_agents": "unavailable",
            "eval": "pending",
            "git": "pending",
            "service": "pending",
        },
        "inventory": [],
        "usage": None,
        "failure": None,
        "export": {"status": "not_requested"},
        "created_at": _utc_now(),
        "sealed_at": None,
    }
    write_json_atomic(path=manifest_path, payload=manifest)
    write_json_atomic(
        path=trace_ref_path(
            repo_root=repo_root,
            session_id=session_id,
            iteration=iteration,
            workflow_id=workflow_id,
        ),
        payload={
            "schema_version": 1,
            "manifest_id": manifest["manifest_id"],
            "manifest_ref": f"trace:{manifest['manifest_id']}:/trace_manifest.json",
            "attempt_id": attempt_id,
        },
    )
    return trace_root, manifest


def trace_write_json(*, trace_root: Path, relative_path: str, payload: Any) -> Path:
    """Persist a JSON payload unchanged under a trace-confined relative path."""

    path = _trace_member(trace_root=trace_root, relative_path=relative_path)
    write_json_atomic(path=path, payload=payload)
    return path


def trace_write_text(*, trace_root: Path, relative_path: str, content: str) -> Path:
    """Persist text unchanged under a trace-confined relative path."""

    path = _trace_member(trace_root=trace_root, relative_path=relative_path)
    write_text_atomic(path=path, content=content)
    return path


def update_trace_manifest(
    *, trace_root: Path, updates: dict[str, Any]
) -> dict[str, Any]:
    """Merge coordinator-owned lifecycle or channel facts into an active manifest."""

    path = trace_root / TRACE_MANIFEST_FILENAME
    manifest = _read_manifest(path=path)
    _deep_update(target=manifest, updates=updates)
    write_json_atomic(path=path, payload=manifest)
    return manifest


def import_harness_artifacts(
    *,
    trace_root: Path,
    run_json_path: str,
    session_output_dir: str,
    harness_run_id: str,
) -> None:
    """Validate the caller-owned harness run and project its channel completeness."""

    manifest = _read_manifest(path=trace_root / TRACE_MANIFEST_FILENAME)
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise TraceError("attempt trace has no identity for harness import")
    harness_root = trace_root / "harness"
    harness_root.mkdir(parents=True, exist_ok=True)
    run_record_present = False
    run_payload: dict[str, Any] | None = None
    coordinator_input_present = False
    expected_run_root = harness_root.resolve()
    if run_json_path and session_output_dir and harness_run_id:
        source = Path(run_json_path)
        source_root = Path(session_output_dir)
        expected_run_root = (harness_root / harness_run_id).resolve()
        if (
            not source.is_absolute()
            or source.resolve() != expected_run_root / "run.json"
            or not source_root.is_absolute()
            or source_root.resolve() != expected_run_root
        ):
            raise TraceError(
                "team-harness result paths are not the canonical attempt run"
            )
        run_payload = _read_json_object(path=source, label="harness run record")
        expected_context = _expected_harness_context(
            trace_root=trace_root, identity=identity
        )
        _validate_harness_run_identity(
            payload=run_payload,
            run_root=expected_run_root,
            expected_run_id=harness_run_id,
            expected_context=expected_context,
        )
        run_record_present = run_payload.get("end") is not None
        coordinator_input_present = _coordinator_input_is_complete(
            path=expected_run_root / "coordinator_input.json",
            run_payload=run_payload,
            expected_context=expected_context,
        )
    update_trace_manifest(
        trace_root=trace_root,
        updates={
            "identity": {"harness_run_id": harness_run_id or None},
            "channels": {
                "coordinator_input": (
                    "complete" if coordinator_input_present else "incomplete"
                ),
                "coordinator_output": (
                    "complete" if run_record_present else "incomplete"
                ),
                # run.json owns the direct-agent catalog. A finalized catalog
                # is complete only when every agent points at its canonical
                # local stdout and stderr files.
                "direct_agents": _direct_agent_channel_status(
                    run_payload=run_payload, run_root=expected_run_root
                ),
            },
        },
    )


def _direct_agent_channel_status(
    *, run_payload: dict[str, Any] | None, run_root: Path
) -> str:
    """Derive completeness from each agent's canonical stdout/stderr files."""

    if run_payload is None:
        return "incomplete"
    agents = run_payload.get("agents")
    if not isinstance(agents, list):
        return "incomplete"
    for agent in agents:
        if not isinstance(agent, dict):
            return "incomplete"
        if not _agent_streams_are_canonical(agent=agent, run_root=run_root):
            return "incomplete"
        if agent.get("agent_type") == "harness" and not _nested_harness_is_complete(
            agent=agent, parent_run=run_payload
        ):
            return "incomplete"
    return "complete"


def _agent_streams_are_canonical(*, agent: dict[str, Any], run_root: Path) -> bool:
    """Check that one agent's raw stream files are regular files in its run."""

    workers_root = (run_root / "workers").resolve()
    for field, filename in (
        ("stdout_log", "stdout.jsonl"),
        ("stderr_log", "stderr.log"),
    ):
        raw_path = agent.get(field)
        if not isinstance(raw_path, str):
            return False
        path = Path(raw_path)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or path.resolve().name != filename
            or not path.resolve().is_relative_to(workers_root)
            or not path.is_file()
        ):
            return False
    return True


def _read_json_object(*, path: Path, label: str) -> dict[str, Any]:
    """Read one required JSON object and report its contract label on failure."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TraceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TraceError(f"{label} is not an object: {path}")
    return payload


def _expected_harness_context(
    *, trace_root: Path, identity: dict[str, Any]
) -> dict[str, Any]:
    """Build the exact loopy identity expected in a caller-owned harness run."""

    repo_root = _repository_root_for_trace(trace_root=trace_root)
    if repo_root is None:
        raise TraceError("cannot locate repository for caller-owned harness run")
    required = {
        key: identity.get(key)
        for key in (
            "root_session_id",
            "session_id",
            "workflow_id",
            "iteration",
            "attempt_id",
        )
    }
    if any(value is None for value in required.values()):
        raise TraceError("attempt trace identity is incomplete for harness import")
    iteration_value = required["iteration"]
    if isinstance(iteration_value, bool) or not isinstance(iteration_value, int):
        raise TraceError("attempt trace iteration is invalid for harness import")
    session_manifest = _read_json_object(
        path=session_dir_path(
            repo_root=repo_root, session_id=str(required["session_id"])
        )
        / "session.json",
        label="session manifest",
    )
    expected_assignment = assignment_path(
        repo_root=repo_root,
        session_id=str(required["session_id"]),
        iteration=iteration_value,
        workflow_id=str(required["workflow_id"]),
        attempt_id=str(required["attempt_id"]),
    ).resolve()
    return {
        "trace_root": str((trace_root / "harness").resolve()),
        "parent_assignment_path": str(expected_assignment),
        "parent_attempt_id": required["attempt_id"],
        "root_session_id": required["root_session_id"],
        "session_id": required["session_id"],
        "session_depth": session_manifest.get("depth"),
        "workflow_role": required["workflow_id"],
        "parent_harness_run_id": None,
    }


def _validate_harness_run_identity(
    *,
    payload: dict[str, Any],
    run_root: Path,
    expected_run_id: str,
    expected_context: dict[str, Any],
) -> None:
    """Reject a harness record that is not bound to the active loopy attempt."""

    if payload.get("run_id") != expected_run_id:
        raise TraceError("team-harness run ID does not match the worker result")
    raw_output = payload.get("session_output_dir")
    raw_input = payload.get("coordinator_input_path")
    if (
        not isinstance(raw_output, str)
        or not Path(raw_output).is_absolute()
        or Path(raw_output).resolve() != run_root
    ):
        raise TraceError("team-harness run output directory is not canonical")
    if (
        not isinstance(raw_input, str)
        or not Path(raw_input).is_absolute()
        or Path(raw_input).resolve() != run_root / "coordinator_input.json"
    ):
        raise TraceError("team-harness coordinator input path is not canonical")
    _validate_caller_context(
        value=payload.get("caller_context"), expected=expected_context
    )


def _validate_caller_context(*, value: Any, expected: dict[str, Any]) -> None:
    """Validate the loopy identity fields carried by one harness caller context."""

    if not isinstance(value, dict):
        raise TraceError("team-harness run has no caller context")
    for key, expected_value in expected.items():
        observed = value.get(key)
        if key in {"trace_root", "parent_assignment_path"}:
            if (
                not isinstance(observed, str)
                or not Path(observed).is_absolute()
                or Path(observed).resolve() != Path(str(expected_value)).resolve()
            ):
                raise TraceError(f"team-harness caller context {key} does not match")
        elif observed != expected_value:
            raise TraceError(f"team-harness caller context {key} does not match")


def _coordinator_input_is_complete(
    *, path: Path, run_payload: dict[str, Any], expected_context: dict[str, Any]
) -> bool:
    """Check that the pre-call coordinator input exists and matches its run."""

    try:
        payload = _read_json_object(path=path, label="coordinator input")
        _validate_caller_context(
            value=payload.get("caller_context"), expected=expected_context
        )
    except TraceError:
        return False
    return (
        payload.get("schema_version") == 1
        and payload.get("status") == "complete"
        and payload.get("harness_run_id") == run_payload.get("run_id")
        and isinstance(payload.get("messages"), list)
    )


def _nested_harness_is_complete(
    *, agent: dict[str, Any], parent_run: dict[str, Any]
) -> bool:
    """Recursively validate a nested harness run and canonical stream files."""

    raw_assignment = agent.get("assignment_path")
    if not isinstance(raw_assignment, str) or not Path(raw_assignment).is_absolute():
        return False
    assignment = Path(raw_assignment).resolve()
    nested_root = assignment.parent / "harness_runs"
    run_paths = sorted(nested_root.glob("*/run.json")) if nested_root.is_dir() else []
    if not run_paths:
        # A failed launcher can have no nested run while its own canonical
        # stdout/stderr files still capture the attempted invocation.
        return agent.get("exit_code") not in {0, None}
    if len(run_paths) != 1:
        return False
    try:
        nested_payload = _read_json_object(
            path=run_paths[0], label="nested harness run"
        )
        parent_context = parent_run.get("caller_context")
        if not isinstance(parent_context, dict):
            return False
        nested_context = {
            **{
                key: parent_context.get(key)
                for key in (
                    "parent_attempt_id",
                    "root_session_id",
                    "session_id",
                    "session_depth",
                    "workflow_role",
                )
            },
            "trace_root": str(nested_root.resolve()),
            "parent_assignment_path": str(assignment),
            "parent_harness_run_id": parent_run.get("run_id"),
        }
        nested_run_root = run_paths[0].parent.resolve()
        _validate_harness_run_identity(
            payload=nested_payload,
            run_root=nested_run_root,
            expected_run_id=nested_run_root.name,
            expected_context=nested_context,
        )
        if nested_payload.get("end") is None or not _coordinator_input_is_complete(
            path=nested_run_root / "coordinator_input.json",
            run_payload=nested_payload,
            expected_context=nested_context,
        ):
            return False
        return (
            _direct_agent_channel_status(
                run_payload=nested_payload, run_root=nested_run_root
            )
            == "complete"
        )
    except TraceError:
        return False


def seal_attempt_trace(
    *,
    trace_root: Path,
    usage: dict[str, Any] | None,
    failure: dict[str, Any] | None = None,
    incomplete: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze raw trace bytes into a completeness-aware hashed inventory."""

    manifest_path = trace_root / TRACE_MANIFEST_FILENAME
    manifest = _read_manifest(path=manifest_path)
    if manifest.get("lifecycle") in {"sealed", "incomplete"}:
        if repo_root is not None:
            _write_or_verify_seal_receipt(
                repo_root=repo_root, trace_root=trace_root, manifest=manifest
            )
        return manifest
    inventory: list[dict[str, Any]] = []
    for path in sorted(p for p in trace_root.rglob("*") if p.is_file()):
        if path == manifest_path:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(trace_root.resolve()):
            raise TraceError(f"trace contains an unsafe link: {path}")
        relative = path.relative_to(trace_root).as_posix()
        data = path.read_bytes()
        inventory.append(
            {
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "media_type": _media_type(path=path),
            }
        )
    incompleteness_reasons = _trace_incompleteness_reasons(
        trace_root=trace_root, manifest=manifest
    )
    if incomplete:
        incompleteness_reasons.append("worker_or_coordinator_reported_incomplete")
    incompleteness_reasons = sorted(set(incompleteness_reasons))
    manifest["lifecycle"] = "incomplete" if incompleteness_reasons else "sealed"
    manifest["incompleteness_reasons"] = incompleteness_reasons
    manifest["inventory"] = inventory
    manifest["usage"] = usage
    manifest["failure"] = failure
    manifest["sealed_at"] = _utc_now()
    for channel, status in list(manifest["channels"].items()):
        if status == "pending":
            manifest["channels"][channel] = (
                "incomplete" if incomplete else "not_produced"
            )
    write_json_atomic(path=manifest_path, payload=manifest)
    if repo_root is not None:
        _write_or_verify_seal_receipt(
            repo_root=repo_root, trace_root=trace_root, manifest=manifest
        )
    return manifest


def _write_or_verify_seal_receipt(
    *, repo_root: Path, trace_root: Path, manifest: dict[str, Any]
) -> Path:
    """Anchor a finalized trace manifest in compact session-plane evidence."""

    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise TraceError("cannot anchor a trace manifest without identity")
    session_id = identity.get("session_id")
    attempt_id = identity.get("attempt_id")
    if not isinstance(session_id, str) or not isinstance(attempt_id, str):
        raise TraceError("cannot anchor a trace manifest with incomplete identity")
    manifest_path = trace_root / TRACE_MANIFEST_FILENAME
    inventory = manifest.get("inventory")
    inventory_root = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    payload = {
        "schema_version": 1,
        "manifest_id": manifest.get("manifest_id"),
        "session_id": session_id,
        "attempt_id": attempt_id,
        "manifest_sha256": "sha256:"
        + hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "inventory_root_sha256": inventory_root,
        "lifecycle": manifest.get("lifecycle"),
        "sealed_at": manifest.get("sealed_at"),
    }
    path = trace_seal_receipt_path(
        repo_root=repo_root, session_id=session_id, attempt_id=attempt_id
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TraceError(f"invalid trace seal receipt at {path}: {exc}") from exc
        if existing != payload:
            raise TraceError(f"trace seal receipt contradicts manifest at {path}")
        return path
    write_json_atomic(path=path, payload=payload)
    return path


def _trace_incompleteness_reasons(
    *, trace_root: Path, manifest: dict[str, Any]
) -> list[str]:
    """List missing protocol artifacts and channels that did not complete."""

    reasons: list[str] = []
    required_files = (
        "protocol/task_response.json",
        "protocol/assignment.json",
        "protocol/rendered_prompt.txt",
        "protocol/iteration_result.json",
        "protocol/finished_request.json",
        "protocol/finished_response.json",
    )
    for relative in required_files:
        if not (trace_root / relative).is_file():
            reasons.append(f"missing:{relative}")
    channels = manifest.get("channels")
    if not isinstance(channels, dict):
        reasons.append("invalid:channels")
        return reasons
    for name, status in channels.items():
        if status in {"pending", "incomplete"}:
            reasons.append(f"channel:{name}:{status}")
    if channels.get("service") != "complete":
        reasons.append(f"channel:service:{channels.get('service')}")
    return reasons


def verify_trace_integrity(
    *, trace_root: Path, repo_root: Path | None = None
) -> dict[str, Any]:
    """Compare a finalized trace's current files with its sealed inventory.

    This is deliberately detection, not write prevention: agents and operators
    can still change trace files after sealing, but the drift is made visible
    and export refuses to treat those changed bytes as sealed evidence.
    ``trace_manifest.json`` is excluded because it contains the inventory and
    therefore cannot hash itself.
    """

    root = trace_root.resolve()
    manifest = _read_manifest(path=root / TRACE_MANIFEST_FILENAME)
    lifecycle = manifest.get("lifecycle")
    if lifecycle not in {"sealed", "incomplete"}:
        return {
            "status": "not_finalized",
            "expected_file_count": 0,
            "actual_file_count": 0,
            "added": [],
            "removed": [],
            "modified": [],
            "manifest_errors": [],
        }

    expected, manifest_errors = _validated_inventory(manifest=manifest)
    if not manifest.get("sealed_at"):
        manifest_errors.append("finalized trace has no sealed_at timestamp")
    _verify_seal_receipt(
        trace_root=root,
        manifest=manifest,
        repo_root=repo_root or _repository_root_for_trace(trace_root=root),
        errors=manifest_errors,
    )

    actual_paths: set[str] = set()
    actual_metadata: dict[str, tuple[int, str] | str] = {}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        symlink_directories = [
            name for name in directory_names if (current / name).is_symlink()
        ]
        directory_names[:] = [
            name for name in directory_names if name not in symlink_directories
        ]
        for name in sorted([*file_names, *symlink_directories]):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if relative == TRACE_MANIFEST_FILENAME:
                continue
            actual_paths.add(relative)
            if path.is_symlink():
                actual_metadata[relative] = "path is a symbolic link"
                continue
            if not path.is_file():
                actual_metadata[relative] = "path is not a regular file"
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                actual_metadata[relative] = f"cannot read file: {exc}"
                continue
            actual_metadata[relative] = (
                len(data),
                "sha256:" + hashlib.sha256(data).hexdigest(),
            )

    expected_paths = set(expected)
    added = sorted(actual_paths - expected_paths)
    removed = sorted(expected_paths - actual_paths)
    modified: list[dict[str, Any]] = []
    for relative in sorted(actual_paths & expected_paths):
        observed = actual_metadata[relative]
        expected_item = expected[relative]
        if isinstance(observed, str):
            modified.append(
                {
                    "path": relative,
                    "expected_sha256": expected_item[1],
                    "expected_size": expected_item[0],
                    "error": observed,
                }
            )
            continue
        actual_size, actual_sha256 = observed
        expected_size, expected_sha256 = expected_item
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            modified.append(
                {
                    "path": relative,
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                }
            )

    return {
        "status": (
            "verified"
            if not (added or removed or modified or manifest_errors)
            else "failed"
        ),
        "expected_file_count": len(expected),
        "actual_file_count": len(actual_paths),
        "added": added,
        "removed": removed,
        "modified": modified,
        "manifest_errors": manifest_errors,
    }


def _repository_root_for_trace(*, trace_root: Path) -> Path | None:
    """Infer the repository root from the canonical trace-directory layout."""

    for ancestor in trace_root.parents:
        if ancestor.name == "traces" and ancestor.parent.name == ".loopy_loop":
            return ancestor.parent.parent.resolve()
    return None


def _verify_seal_receipt(
    *,
    trace_root: Path,
    manifest: dict[str, Any],
    repo_root: Path | None,
    errors: list[str],
) -> None:
    """Append integrity errors for the session-plane receipt of a v2 trace."""

    if repo_root is None:
        return
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        errors.append("finalized trace identity is invalid")
        return
    session_id = identity.get("session_id")
    attempt_id = identity.get("attempt_id")
    if not isinstance(session_id, str) or not isinstance(attempt_id, str):
        errors.append("finalized trace identity cannot locate its seal receipt")
        return
    receipt_path = trace_seal_receipt_path(
        repo_root=repo_root, session_id=session_id, attempt_id=attempt_id
    )
    session_manifest = receipt_path.parent.parent / "session.json"
    if not session_manifest.is_file() and not receipt_path.exists():
        # Unit-created and legacy traces have no v2 session plane to anchor in.
        return
    if not receipt_path.is_file():
        errors.append("finalized v2 trace has no compact seal receipt")
        return
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        errors.append("trace seal receipt is unreadable")
        return
    expected_manifest_hash = (
        "sha256:"
        + hashlib.sha256(
            (trace_root / TRACE_MANIFEST_FILENAME).read_bytes()
        ).hexdigest()
    )
    expected_inventory_root = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                manifest.get("inventory"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )
    expected = {
        "manifest_id": manifest.get("manifest_id"),
        "session_id": session_id,
        "attempt_id": attempt_id,
        "manifest_sha256": expected_manifest_hash,
        "inventory_root_sha256": expected_inventory_root,
        "lifecycle": manifest.get("lifecycle"),
        "sealed_at": manifest.get("sealed_at"),
    }
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        errors.append("trace seal receipt schema is invalid")
        return
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(f"trace seal receipt field {key!r} does not match")


def _validated_inventory(
    *, manifest: dict[str, Any]
) -> tuple[dict[str, tuple[int, str]], list[str]]:
    """Parse safe inventory paths and expected byte hashes from a manifest."""

    expected: dict[str, tuple[int, str]] = {}
    errors: list[str] = []
    inventory = manifest.get("inventory")
    if not isinstance(inventory, list):
        return expected, ["manifest inventory is not a list"]
    for index, item in enumerate(inventory):
        label = f"inventory[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} is not an object")
            continue
        relative = item.get("path")
        if not isinstance(relative, str) or not relative:
            errors.append(f"{label}.path is not a non-empty string")
            continue
        parsed = Path(relative)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or relative == TRACE_MANIFEST_FILENAME
        ):
            errors.append(f"{label}.path is not a valid trace-relative member")
            continue
        if relative in expected:
            errors.append(f"manifest inventory repeats path {relative!r}")
            continue
        size = item.get("size")
        digest = item.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append(f"{label}.size is not a non-negative integer")
            continue
        if not (
            isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            errors.append(f"{label}.sha256 is not a canonical SHA-256 digest")
            continue
        expected[relative] = (size, digest)
    return expected, errors


def _require_finalized_trace_integrity(*, trace_root: Path) -> dict[str, Any]:
    """Return a verified report or raise when a trace is unsafe to export."""

    report = verify_trace_integrity(trace_root=trace_root)
    if report["status"] == "not_finalized":
        raise TraceError("active traces are not finalized")
    if report["status"] != "verified":
        summary = ", ".join(
            f"{key}={len(report[key])}"
            for key in ("added", "removed", "modified", "manifest_errors")
            if report[key]
        )
        raise TraceError(f"sealed trace integrity verification failed ({summary})")
    return report


def enqueue_trace_export(
    *, repo_root: Path, trace_root: Path, destination: Path
) -> Path:
    """Bind one finalized trace durably to one exact local destination."""

    manifest = _read_manifest(path=trace_root / TRACE_MANIFEST_FILENAME)
    if manifest.get("lifecycle") not in {"sealed", "incomplete"} or not manifest.get(
        "sealed_at"
    ):
        raise TraceError("active traces cannot be exported")
    _require_finalized_trace_integrity(trace_root=trace_root)
    manifest_id = str(manifest["manifest_id"])
    target = destination.resolve() / manifest_id
    outbox = trace_export_outbox_dir_path(repo_root=repo_root.resolve())
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{manifest_id}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TraceError(f"invalid trace export outbox record: {path}") from exc
        if (
            not isinstance(existing, dict)
            or Path(str(existing.get("trace_root", ""))).resolve()
            != trace_root.resolve()
        ):
            raise TraceError(
                f"trace export identity {manifest_id!r} is already bound to "
                "a different trace root"
            )
        recorded_destination = existing.get("destination")
        if recorded_destination is not None and (
            Path(str(recorded_destination)).resolve() != target
        ):
            raise TraceError(
                f"trace export identity {manifest_id!r} is already bound to "
                "a different destination"
            )
        if recorded_destination is None:
            existing["destination"] = str(target)
            write_json_atomic(path=path, payload=existing)
    else:
        write_json_atomic(
            path=path,
            payload={
                "schema_version": 1,
                "export_id": f"export-{manifest_id}",
                "manifest_id": manifest_id,
                "trace_root": str(trace_root.resolve()),
                "destination": str(target),
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "created_at": _utc_now(),
            },
        )
    return path


def export_trace_to_directory(*, outbox_path: Path, destination: Path) -> Path:
    """Atomically publish an exact unfiltered local copy from an export outbox."""

    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    trace_root = Path(str(payload["trace_root"]))
    manifest = _read_manifest(path=trace_root / TRACE_MANIFEST_FILENAME)
    _require_finalized_trace_integrity(trace_root=trace_root)
    manifest_bytes_before = (trace_root / TRACE_MANIFEST_FILENAME).read_bytes()
    target = destination.resolve() / str(manifest["manifest_id"])
    recorded_destination = payload.get("destination")
    if recorded_destination is None:
        # Compatibility for an older unbound pending record. Persist the
        # choice before publication so a crash cannot rebind it.
        payload["destination"] = str(target)
        write_json_atomic(path=outbox_path, payload=payload)
        recorded_target = target
    else:
        recorded_target = Path(str(recorded_destination)).resolve()
    if recorded_target != target:
        raise TraceError(
            "this export is bound to "
            f"{recorded_target}; refusing a different destination"
        )
    if payload.get("status") == "exported":
        if recorded_target.is_dir():
            _verify_exported_copy(source_root=trace_root, target=recorded_target)
            return recorded_target
    if target.exists():
        _verify_exported_copy(source_root=trace_root, target=target)
        payload.update(
            {
                "status": "exported",
                "attempts": int(payload.get("attempts", 0)) + 1,
                "destination": str(target),
                "exported_at": _utc_now(),
                "last_error": None,
            }
        )
        write_json_atomic(path=outbox_path, payload=payload)
        return target
    staging = target.with_name(f".{target.name}.staging-{uuid.uuid4().hex}")
    try:
        destination.resolve().mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=False, exist_ok=False)
        expected, inventory_errors = _validated_inventory(manifest=manifest)
        if inventory_errors:
            raise TraceError("cannot export a malformed sealed inventory")
        for relative, (expected_size, expected_sha256) in sorted(expected.items()):
            source = trace_root / relative
            data = source.read_bytes()
            actual_sha256 = "sha256:" + hashlib.sha256(data).hexdigest()
            if len(data) != expected_size or actual_sha256 != expected_sha256:
                raise TraceError(f"trace changed while exporting: {relative}")
            destination_path = staging / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_bytes(data)
        manifest_bytes = (trace_root / TRACE_MANIFEST_FILENAME).read_bytes()
        if manifest_bytes != manifest_bytes_before:
            raise TraceError("trace manifest changed while exporting")
        (staging / TRACE_MANIFEST_FILENAME).write_bytes(manifest_bytes)
        _verify_exported_copy(source_root=trace_root, target=staging)
        os.replace(staging, target)
    except (OSError, TraceError) as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        payload["attempts"] = int(payload.get("attempts", 0)) + 1
        payload["last_error"] = str(exc)
        write_json_atomic(path=outbox_path, payload=payload)
        raise
    payload.update(
        {
            "status": "exported",
            "attempts": int(payload.get("attempts", 0)) + 1,
            "destination": str(target),
            "exported_at": _utc_now(),
            "last_error": None,
        }
    )
    write_json_atomic(path=outbox_path, payload=payload)
    return target


def _verify_exported_copy(*, source_root: Path, target: Path) -> None:
    """Verify that an exported directory exactly matches its sealed source."""

    source_manifest = (source_root / TRACE_MANIFEST_FILENAME).read_bytes()
    target_manifest = target / TRACE_MANIFEST_FILENAME
    if not target_manifest.is_file() or target_manifest.read_bytes() != source_manifest:
        raise TraceError("exported trace manifest does not match the sealed source")
    manifest = _read_manifest(path=source_root / TRACE_MANIFEST_FILENAME)
    expected, errors = _validated_inventory(manifest=manifest)
    if errors:
        raise TraceError("sealed source inventory is malformed")
    actual: set[str] = set()
    for directory, directory_names, file_names in os.walk(target, followlinks=False):
        current = Path(directory)
        for name in list(directory_names):
            child = current / name
            if child.is_symlink():
                raise TraceError(f"exported trace contains a symlink: {child}")
        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise TraceError(f"exported trace contains a non-file: {path}")
            relative = path.relative_to(target).as_posix()
            if relative != TRACE_MANIFEST_FILENAME:
                actual.add(relative)
    if actual != set(expected):
        raise TraceError(
            "exported trace file inventory does not match the sealed source"
        )
    for relative, (expected_size, expected_sha256) in expected.items():
        data = (target / relative).read_bytes()
        if (
            len(data) != expected_size
            or ("sha256:" + hashlib.sha256(data).hexdigest()) != expected_sha256
        ):
            raise TraceError(f"exported trace artifact does not match: {relative}")


def prune_trace(*, trace_root: Path) -> dict[str, Any]:
    """Delete finalized detail after validating its authentic seal shape."""

    manifest = _read_manifest(path=trace_root / TRACE_MANIFEST_FILENAME)
    if manifest.get("lifecycle") not in {"sealed", "incomplete"} or not manifest.get(
        "sealed_at"
    ):
        raise TraceError("refusing to prune an active or unsealed trace")
    # The manifest lives inside the agent-writable trace plane. A lifecycle
    # string alone is not an engine seal, so require a valid inventory and,
    # for v2 session traces, the matching session-plane receipt. Ordinary
    # post-seal file drift is still removable: prune is the operator's cleanup
    # path and returns that last failed observation, while export remains
    # strict about every byte.
    integrity = verify_trace_integrity(trace_root=trace_root)
    manifest_errors = integrity.get("manifest_errors")
    if not isinstance(manifest_errors, list) or manifest_errors:
        summary = "; ".join(str(error) for error in manifest_errors or [])
        raise TraceError(
            "sealed trace integrity verification failed"
            + (f" ({summary})" if summary else "")
        )
    shutil.rmtree(trace_root)
    return integrity


def list_trace_manifests(*, repo_root: Path) -> list[Path]:
    """List canonical trace manifests under one repository's trace root."""

    root = traces_root_path(repo_root=repo_root.resolve())
    return sorted(root.rglob(TRACE_MANIFEST_FILENAME)) if root.exists() else []


def read_trace_manifest(*, manifest_path: Path) -> dict[str, Any]:
    """Read one trace manifest through the public operational API."""
    return _read_manifest(path=manifest_path)


def resolve_trace_manifest(*, repo_root: Path, reference: str) -> Path:
    """Resolve a trace-root path, manifest path, or manifest ID in this repo.

    Path references are deliberately confined to ``.loopy_loop/traces``. This
    makes the path-taking prune command incapable of deleting an unrelated
    directory that happens to contain a file named ``trace_manifest.json``.
    """
    root = traces_root_path(repo_root=repo_root.resolve()).resolve()
    candidate = Path(reference).expanduser()
    candidate = (
        candidate.resolve()
        if candidate.is_absolute()
        else (repo_root.resolve() / candidate).resolve()
    )
    if candidate.exists():
        manifest_path = (
            candidate / TRACE_MANIFEST_FILENAME if candidate.is_dir() else candidate
        )
        if manifest_path.name != TRACE_MANIFEST_FILENAME or not manifest_path.is_file():
            raise TraceError(
                f"trace path must name a trace root or {TRACE_MANIFEST_FILENAME}: "
                f"{candidate}"
            )
        if not manifest_path.resolve().is_relative_to(root):
            raise TraceError(
                f"trace path is outside this repository's trace root: {candidate}"
            )
        _read_manifest(path=manifest_path)
        return manifest_path.resolve()

    matches: list[Path] = []
    for manifest_path in list_trace_manifests(repo_root=repo_root):
        resolved_manifest = manifest_path.resolve()
        if not resolved_manifest.is_relative_to(root):
            raise TraceError(
                "trace manifest resolves outside this repository's trace root: "
                f"{manifest_path}"
            )
        manifest = _read_manifest(path=manifest_path)
        if manifest.get("manifest_id") == reference:
            matches.append(resolved_manifest)
    if not matches:
        raise TraceError(f"trace manifest not found: {reference}")
    if len(matches) > 1:
        locations = ", ".join(str(path) for path in matches)
        raise TraceError(f"trace manifest ID is ambiguous ({reference}): {locations}")
    return matches[0]


def _trace_member(*, trace_root: Path, relative_path: str) -> Path:
    """Resolve a trace member while rejecting absolute paths and traversal."""

    root = trace_root.resolve()
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise TraceError(f"invalid trace-relative path: {relative_path}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise TraceError(f"trace path escapes attempt root: {relative_path}")
    return path


def _read_manifest(*, path: Path) -> dict[str, Any]:
    """Read a trace manifest as a JSON object with a trace-specific error."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TraceError(f"invalid trace manifest at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TraceError(f"invalid trace manifest mapping at {path}")
    return payload


def _deep_update(*, target: dict[str, Any], updates: dict[str, Any]) -> None:
    """Merge nested manifest updates without replacing unrelated siblings."""

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target=target[key], updates=value)
        else:
            target[key] = value


def _media_type(*, path: Path) -> str:
    """Return the compact media type recorded for one raw trace artifact."""

    if path.suffix == ".json":
        return "application/json"
    if path.suffix in {".md", ".txt", ".log", ".jsonl"}:
        return "text/plain"
    return "application/octet-stream"


def _utc_now() -> str:
    """Return a stable second-precision UTC timestamp for trace artifacts."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
