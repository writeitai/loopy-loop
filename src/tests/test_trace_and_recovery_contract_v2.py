from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loopy_loop import recovery as recovery_module
from loopy_loop.recovery import recover_interrupted_iteration
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import iteration_dir_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import import_harness_artifacts
from loopy_loop.tracing import read_trace_manifest
from loopy_loop.tracing import seal_attempt_trace
from loopy_loop.tracing import trace_write_json
from loopy_loop.tracing import trace_write_text
from loopy_loop.tracing import TraceError
from loopy_loop.tracing import verify_trace_integrity


class _WorkerResult:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class _ReapReport:
    def __init__(self, outcomes: list[str]) -> None:
        self.workers = [_WorkerResult(outcome) for outcome in outcomes]

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {"workers": [worker.outcome for worker in self.workers]}


class _Reaper:
    class ReapRefusedError(RuntimeError):
        pass

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def reap_run(self, run_json: Path, **kwargs: object) -> _ReapReport:
        self.calls.append({"run_json": run_json, **kwargs})
        return _ReapReport(["drained"])


class _HarnessConfig:
    def __init__(self, runs_dir: Path) -> None:
        self.RUNS_DIR = runs_dir


def _caller_owned_harness_run(
    *,
    repo_root: Path,
    attempt_id: str,
    run_id: str = "run-one",
    coordinator_input_status: str = "complete",
) -> tuple[Path, Path, dict[str, Any]]:
    """Create one canonical loopy session/assignment/trace/harness fixture."""

    session_id = f"session-{attempt_id}"
    workflow_id = "inner"
    create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="fixture-goal",
        goal="Exercise the harness trace contract",
        workflow_set="delivery",
        workflow_contract={
            "schema_version": 1,
            "session_protocol_version": 2,
            "layer_kind": "work",
            "roles": {workflow_id: {"responsibility": "Run the fixture"}},
            "state": [],
            "eval": {},
            "task_acceptance_role": workflow_id,
            "terminal_blocker_reporting_roles": [workflow_id],
            "child_interface": "none",
        },
        schema_version=2,
    )
    trace_root, _ = create_attempt_trace(
        repo_root=repo_root,
        root_session_id=session_id,
        session_id=session_id,
        request_id=None,
        work_item_id=None,
        workflow_set="delivery",
        workflow_id=workflow_id,
        iteration=1,
        attempt_id=attempt_id,
    )
    assignment = assignment_path(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
    ).resolve()
    write_json_atomic(
        path=assignment, payload={"schema_version": 2, "attempt_id": attempt_id}
    )
    run_dir = (trace_root / "harness" / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    caller_context: dict[str, Any] = {
        "schema_version": 1,
        "trace_root": str((trace_root / "harness").resolve()),
        "parent_assignment_path": str(assignment),
        "parent_attempt_id": attempt_id,
        "root_session_id": session_id,
        "session_id": session_id,
        "session_depth": 0,
        "workflow_role": workflow_id,
        "relevant_state_paths": [],
        "parent_harness_run_id": None,
    }
    write_json_atomic(
        path=run_dir / "coordinator_input.json",
        payload={
            "schema_version": 1,
            "status": coordinator_input_status,
            "harness_run_id": run_id,
            "messages": [{"role": "user", "content": "Run the fixture"}],
            "caller_context": caller_context,
        },
    )
    run_payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "end": "2026-07-16T12:00:00Z",
        "session_output_dir": str(run_dir),
        "coordinator_input_path": str(run_dir / "coordinator_input.json"),
        "caller_context": caller_context,
        "capabilities": [],
        "agents": [],
    }
    write_json_atomic(path=run_dir / "run.json", payload=run_payload)
    return trace_root, run_dir, run_payload


def test_sealed_trace_preserves_raw_artifacts_and_has_verifiable_inventory(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    root_session_id = "root-session"
    session_id = "leaf-session"
    attempt_id = "attempt-raw-capture"
    trace_root, active_manifest = create_attempt_trace(
        repo_root=repo_root,
        root_session_id=root_session_id,
        session_id=session_id,
        request_id="request-1",
        work_item_id="work-1",
        workflow_set="inner",
        workflow_id="implement",
        iteration=1,
        attempt_id=attempt_id,
    )
    assert active_manifest["channels"]["loopy_assignment"] == "pending"
    trace_write_json(
        trace_root=trace_root,
        relative_path="protocol/provider-input.json",
        payload={"input": "structured-observable-value", "nested": {"result": "ok"}},
    )
    trace_write_text(
        trace_root=trace_root,
        relative_path="agents/output.txt",
        content="stream-observable-value",
    )
    # Workflow tools are not path-sandboxed under D8 and may bypass the
    # product writers. Sealing inventories their raw local artifacts too.
    direct_eval = trace_root / "eval" / "direct-report.json"
    direct_eval.write_text(
        json.dumps({"input": "direct-observable-value", "result": "ok"}),
        encoding="utf-8",
    )
    direct_agent_output = trace_root / "agents" / "direct-output.log"
    direct_agent_output.write_text("direct-log-observable-value\n", encoding="utf-8")
    direct_binary = trace_root / "eval" / "opaque.bin"
    direct_binary.write_bytes(b"\x00opaque-trace-fixture")

    manifest = seal_attempt_trace(
        trace_root=trace_root, usage={"prompt_tokens": 10, "completion_tokens": 5}
    )

    persisted = trace_root.joinpath("protocol/provider-input.json").read_text(
        encoding="utf-8"
    )
    agent_output = trace_root.joinpath("agents/output.txt").read_text(encoding="utf-8")
    assert "structured-observable-value" in persisted
    assert agent_output == "stream-observable-value"
    assert "direct-observable-value" in direct_eval.read_text(encoding="utf-8")
    direct_output = direct_agent_output.read_text(encoding="utf-8")
    assert direct_output == "direct-log-observable-value\n"
    assert direct_binary.read_bytes() == b"\x00opaque-trace-fixture"
    assert manifest["lifecycle"] == "incomplete"
    assert "missing:protocol/assignment.json" in manifest["incompleteness_reasons"]
    assert manifest["sealed_at"] is not None
    assert "redaction" not in manifest
    inventory_paths = {item["path"] for item in manifest["inventory"]}
    assert "protocol/provider-input.json" in inventory_paths
    assert "agents/output.txt" in inventory_paths
    assert "eval/opaque.bin" in inventory_paths
    assert verify_trace_integrity(trace_root=trace_root)["status"] == "verified"


def test_missing_harness_artifacts_are_never_claimed_complete(tmp_path: Path) -> None:
    trace_root, _ = create_attempt_trace(
        repo_root=tmp_path,
        root_session_id="root",
        session_id="leaf",
        request_id=None,
        work_item_id=None,
        workflow_set="delivery",
        workflow_id="inner",
        iteration=1,
        attempt_id="attempt-before-harness",
    )

    import_harness_artifacts(
        trace_root=trace_root,
        run_json_path="",
        session_output_dir="",
        harness_run_id="",
    )

    manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
    assert manifest["channels"]["coordinator_input"] == "incomplete"
    assert manifest["channels"]["coordinator_output"] == "incomplete"
    assert manifest["channels"]["direct_agents"] == "incomplete"


@pytest.mark.parametrize(
    ("artifact_state", "expected"),
    [
        ("complete", "complete"),
        ("missing_stdout", "incomplete"),
        ("outside", "incomplete"),
    ],
)
def test_direct_agent_channel_requires_canonical_stdout_and_stderr(
    tmp_path: Path, artifact_state: str, expected: str
) -> None:
    trace_root, harness_output, run_payload = _caller_owned_harness_run(
        repo_root=tmp_path, attempt_id=f"attempt-{artifact_state}"
    )
    run_json = harness_output / "run.json"
    worker_dir = harness_output / "workers" / "agent-one"
    worker_dir.mkdir(parents=True)
    stdout_path = worker_dir / "stdout.jsonl"
    stderr_path = worker_dir / "stderr.log"
    if artifact_state != "missing_stdout":
        stdout_path.write_text('{"event":"done"}\n', encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    if artifact_state == "outside":
        stdout_path = tmp_path / "outside" / "stdout.jsonl"
        stdout_path.parent.mkdir()
        stdout_path.write_text('{"event":"done"}\n', encoding="utf-8")
    run_payload["agents"] = [
        {
            "id": "agent-one",
            "stdout_log": str(stdout_path.resolve()),
            "stderr_log": str(stderr_path.resolve()),
        }
    ]
    write_json_atomic(path=run_json, payload=run_payload)

    import_harness_artifacts(
        trace_root=trace_root,
        run_json_path=str(run_json),
        session_output_dir=str(harness_output),
        harness_run_id="run-one",
    )

    manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
    assert manifest["channels"]["direct_agents"] == expected


def test_incomplete_coordinator_input_is_not_claimed_complete(tmp_path: Path) -> None:
    trace_root, run_dir, _ = _caller_owned_harness_run(
        repo_root=tmp_path,
        attempt_id="attempt-incomplete-input",
        coordinator_input_status="incomplete",
    )

    import_harness_artifacts(
        trace_root=trace_root,
        run_json_path=str(run_dir / "run.json"),
        session_output_dir=str(run_dir),
        harness_run_id=run_dir.name,
    )

    manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
    assert manifest["channels"]["coordinator_input"] == "incomplete"
    assert manifest["channels"]["coordinator_output"] == "complete"
    assert manifest["channels"]["direct_agents"] == "complete"


@pytest.mark.parametrize("contradiction", ["run_id", "caller_context"])
def test_harness_import_rejects_wrong_run_or_caller_identity(
    tmp_path: Path, contradiction: str
) -> None:
    trace_root, run_dir, run_payload = _caller_owned_harness_run(
        repo_root=tmp_path, attempt_id=f"attempt-wrong-{contradiction}"
    )
    if contradiction == "run_id":
        run_payload["run_id"] = "different-run"
    else:
        run_payload["caller_context"]["session_id"] = "different-session"
    write_json_atomic(path=run_dir / "run.json", payload=run_payload)

    with pytest.raises(TraceError, match="run ID|caller context session_id"):
        import_harness_artifacts(
            trace_root=trace_root,
            run_json_path=str(run_dir / "run.json"),
            session_output_dir=str(run_dir),
            harness_run_id=run_dir.name,
        )


def test_incomplete_nested_harness_keeps_direct_agent_channel_incomplete(
    tmp_path: Path,
) -> None:
    trace_root, run_dir, run_payload = _caller_owned_harness_run(
        repo_root=tmp_path, attempt_id="attempt-incomplete-nested"
    )
    assignment = (run_dir / "agents" / "nested" / "agent_assignment.json").resolve()
    write_json_atomic(path=assignment, payload={"schema_version": 1})
    nested_run_dir = (assignment.parent / "harness_runs" / "nested-run").resolve()
    nested_run_dir.mkdir(parents=True)
    parent_context = run_payload["caller_context"]
    nested_context = {
        **{
            key: parent_context[key]
            for key in (
                "parent_attempt_id",
                "root_session_id",
                "session_id",
                "session_depth",
                "workflow_role",
                "relevant_state_paths",
            )
        },
        "schema_version": 1,
        "trace_root": str(assignment.parent / "harness_runs"),
        "parent_assignment_path": str(assignment),
        "parent_harness_run_id": run_dir.name,
    }
    write_json_atomic(
        path=nested_run_dir / "coordinator_input.json",
        payload={
            "schema_version": 1,
            "status": "incomplete",
            "harness_run_id": nested_run_dir.name,
            "messages": [{"role": "user", "content": "Nested task"}],
            "caller_context": nested_context,
        },
    )
    write_json_atomic(
        path=nested_run_dir / "run.json",
        payload={
            "schema_version": 1,
            "run_id": nested_run_dir.name,
            "end": "2026-07-16T12:00:00Z",
            "session_output_dir": str(nested_run_dir),
            "coordinator_input_path": str(nested_run_dir / "coordinator_input.json"),
            "caller_context": nested_context,
            "capabilities": [],
            "agents": [],
        },
    )
    run_payload["agents"] = [
        {
            "id": "nested",
            "agent_type": "harness",
            "assignment_path": str(assignment),
            "exit_code": 0,
            "stdout_log": str((run_dir / "workers/nested/stdout.jsonl").resolve()),
            "stderr_log": str((run_dir / "workers/nested/stderr.log").resolve()),
        }
    ]
    run_dir.joinpath("workers/nested").mkdir(parents=True)
    run_dir.joinpath("workers/nested/stdout.jsonl").write_text("", encoding="utf-8")
    run_dir.joinpath("workers/nested/stderr.log").write_text("", encoding="utf-8")
    write_json_atomic(path=run_dir / "run.json", payload=run_payload)

    import_harness_artifacts(
        trace_root=trace_root,
        run_json_path=str(run_dir / "run.json"),
        session_output_dir=str(run_dir),
        harness_run_id=run_dir.name,
    )

    manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
    assert manifest["channels"]["direct_agents"] == "incomplete"


def test_sealed_trace_integrity_detects_drift(tmp_path: Path) -> None:
    trace_root, _ = create_attempt_trace(
        repo_root=tmp_path,
        root_session_id="root",
        session_id="leaf",
        request_id=None,
        work_item_id=None,
        workflow_set="delivery",
        workflow_id="inner",
        iteration=1,
        attempt_id="attempt-drift",
    )
    trace_write_text(
        trace_root=trace_root, relative_path="agents/modified.txt", content="original"
    )
    trace_write_text(
        trace_root=trace_root, relative_path="agents/removed.txt", content="remove me"
    )
    seal_attempt_trace(trace_root=trace_root, usage=None)

    verified = verify_trace_integrity(trace_root=trace_root)
    assert verified == {
        "status": "verified",
        "expected_file_count": 2,
        "actual_file_count": 2,
        "added": [],
        "removed": [],
        "modified": [],
        "manifest_errors": [],
    }
    trace_root.joinpath("agents/modified.txt").write_text("changed", encoding="utf-8")
    trace_root.joinpath("agents/removed.txt").unlink()
    trace_root.joinpath("agents/added.txt").write_text("added", encoding="utf-8")

    drift = verify_trace_integrity(trace_root=trace_root)
    assert drift["status"] == "failed"
    assert drift["added"] == ["agents/added.txt"]
    assert drift["removed"] == ["agents/removed.txt"]
    assert [item["path"] for item in drift["modified"]] == ["agents/modified.txt"]


def test_sealed_trace_integrity_rejects_malformed_inventory(tmp_path: Path) -> None:
    trace_root, _ = create_attempt_trace(
        repo_root=tmp_path,
        root_session_id="root",
        session_id="leaf",
        request_id=None,
        work_item_id=None,
        workflow_set="delivery",
        workflow_id="inner",
        iteration=1,
        attempt_id="attempt-malformed-inventory",
    )
    trace_write_text(
        trace_root=trace_root, relative_path="agents/output.txt", content="done"
    )
    manifest = seal_attempt_trace(trace_root=trace_root, usage=None)
    manifest["inventory"][0]["sha256"] = "not-a-digest"
    write_json_atomic(path=trace_root / "trace_manifest.json", payload=manifest)

    report = verify_trace_integrity(trace_root=trace_root)

    assert report["status"] == "failed"
    assert report["manifest_errors"] == [
        "inventory[0].sha256 is not a canonical SHA-256 digest"
    ]


def test_recovery_discovers_caller_owned_run_json_in_explicit_attempt_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    root_session_id = "root-session"
    session_id = "leaf-session"
    attempt_id = "attempt-explicit-run"
    workflow_id = "implement"
    trace_root, _ = create_attempt_trace(
        repo_root=repo_root,
        root_session_id=root_session_id,
        session_id=session_id,
        request_id="request-1",
        work_item_id="work-1",
        workflow_set="inner",
        workflow_id=workflow_id,
        iteration=1,
        attempt_id=attempt_id,
    )
    run_dir = trace_root / "harness" / "run-explicit"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_json = run_dir / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-explicit",
                "session_output_dir": str(run_dir.resolve()),
                "caller_context": {
                    "schema_version": 1,
                    "trace_root": str((trace_root / "harness").resolve()),
                    "parent_assignment_path": str(
                        (
                            iteration_dir_path(
                                repo_root=repo_root,
                                session_id=session_id,
                                iteration=1,
                                workflow_id=workflow_id,
                            )
                            / "assignment.json"
                        ).resolve()
                    ),
                    "parent_attempt_id": attempt_id,
                    "root_session_id": root_session_id,
                    "session_id": session_id,
                    "session_depth": 2,
                    "workflow_role": workflow_id,
                    "relevant_state_paths": [],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    legacy_runs = repo_root / "legacy-team-harness-runs"
    legacy_runs.mkdir()
    reaper = _Reaper()
    monkeypatch.setattr(
        recovery_module, "_load_reaper", lambda: (reaper, _HarnessConfig(legacy_runs))
    )

    outcome = recover_interrupted_iteration(
        repo_root=repo_root,
        session_id=session_id,
        iteration=1,
        workflow_id=workflow_id,
        policy="drain",
        drain_timeout_s=10,
        attempt_id=attempt_id,
    )

    assert outcome.reaped_runs == 1
    assert outcome.settled_workers == 1
    assert len(reaper.calls) == 1
    assert reaper.calls[0]["run_json"] == run_json
    salvage = (
        iteration_dir_path(
            repo_root=repo_root,
            session_id=session_id,
            iteration=1,
            workflow_id=workflow_id,
        )
        / "salvage.json"
    )
    assert salvage.is_file()
    salvage_payload: dict[str, Any] = json.loads(salvage.read_text(encoding="utf-8"))
    assert salvage_payload["reaped_runs"] == 1
    assert salvage_payload["reports"] == [{"workers": ["drained"]}]
