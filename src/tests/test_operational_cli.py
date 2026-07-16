from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from loopy_loop.cli import main
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import state_path
from loopy_loop.sessions import trace_export_outbox_dir_path
from loopy_loop.sessions import user_updates_journal_path
from loopy_loop.state_store import StateStore
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import seal_attempt_trace
from loopy_loop.tracing import trace_write_text


def _create_session(
    *, repo_root: Path, session_id: str, parent_session_id: str | None = None
) -> None:
    create_session_dir(
        repo_root=repo_root,
        session_id=session_id,
        goal_hash="goalhash",
        goal="Test the operational CLI.",
        workflow_set="main",
        parent_session_id=parent_session_id,
    )


def _journal(repo_root: Path, session_id: str) -> list[dict[str, Any]]:
    path = user_updates_journal_path(repo_root=repo_root, session_id=session_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_update_defaults_to_deepest_active_tree_layer_and_is_append_only(
    tmp_path: Path, monkeypatch: Any, state_factory: Any
) -> None:
    root_id = "root-session"
    child_id = "child-session"
    _create_session(repo_root=tmp_path, session_id=root_id)
    _create_session(repo_root=tmp_path, session_id=child_id, parent_session_id=root_id)
    root_state = state_factory(
        active_session_id=root_id,
        root_session_id=root_id,
        active_child_session_id=child_id,
    )
    child_state = state_factory(
        active_session_id=child_id, root_session_id=root_id, parent_session_id=root_id
    )
    StateStore(repo_root=tmp_path).write_state(state=root_state)
    StateStore(
        repo_root=tmp_path,
        state_path=state_path(repo_root=tmp_path, session_id=child_id),
    ).write_state(state=child_state)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    first = runner.invoke(main, ["update", "Prioritize", "the", "eval"])
    second = runner.invoke(main, ["update", "Then document the decision"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert _journal(tmp_path, root_id) == []
    records = _journal(tmp_path, child_id)
    assert [record["text"] for record in records] == [
        "Prioritize the eval",
        "Then document the decision",
    ]
    assert records[0]["target_scope"] == "tree"
    assert records[0]["target_session_id"] is None
    assert records[0]["delivered_to_session_id"] == child_id
    assert records[0]["delivery_state"] == "routed"
    assert records[0]["acknowledgement_state"] == "pending"
    assert records[0]["acknowledged_at"] is None
    assert records[0]["input_id"].startswith("input-")
    assert records[0]["created_at"].endswith("Z")
    assert records[0]["input_id"] != records[1]["input_id"]


def test_update_can_address_a_specific_session(
    tmp_path: Path, monkeypatch: Any, state_factory: Any
) -> None:
    root_id = "root-session"
    child_id = "child-session"
    _create_session(repo_root=tmp_path, session_id=root_id)
    _create_session(repo_root=tmp_path, session_id=child_id, parent_session_id=root_id)
    StateStore(repo_root=tmp_path).write_state(
        state=state_factory(
            active_session_id=root_id,
            root_session_id=root_id,
            active_child_session_id=child_id,
        )
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        main, ["update", "--session", root_id, "A parent-only note"]
    )

    assert result.exit_code == 0, result.output
    record = _journal(tmp_path, root_id)[0]
    assert record["target_scope"] == "session"
    assert record["target_session_id"] == root_id
    assert record["delivered_to_session_id"] == root_id
    assert _journal(tmp_path, child_id) == []


def test_update_rejects_missing_state_or_unknown_explicit_session(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    without_state = runner.invoke(main, ["update", "note"])
    missing_session = runner.invoke(main, ["update", "--session", "missing", "note"])

    assert without_state.exit_code != 0
    assert "No loopy-loop state found" in without_state.output
    assert missing_session.exit_code != 0
    assert "session not found: missing" in missing_session.output


def test_trace_commands_list_inspect_export_idempotently_and_prune(
    tmp_path: Path, monkeypatch: Any
) -> None:
    trace_root, manifest = create_attempt_trace(
        repo_root=tmp_path,
        root_session_id="root",
        session_id="leaf",
        request_id="request-1",
        work_item_id="item-1",
        workflow_set="main",
        workflow_id="implement",
        iteration=2,
        attempt_id="attempt-1",
    )
    trace_write_text(
        trace_root=trace_root,
        relative_path="agents/output.txt",
        content="raw local output\n",
    )
    manifest_id = str(manifest["manifest_id"])
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    listed = runner.invoke(main, ["traces", "list"])
    inspected = runner.invoke(main, ["traces", "inspect", manifest_id])
    active_export = runner.invoke(
        main,
        ["traces", "export", manifest_id, "--destination", str(tmp_path / "cloud")],
    )
    active_prune = runner.invoke(main, ["traces", "prune", manifest_id])

    assert listed.exit_code == 0, listed.output
    assert manifest_id in listed.output
    assert "active" in listed.output
    assert str(trace_root / "trace_manifest.json") in listed.output
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["identity"]["attempt_id"] == "attempt-1"
    assert active_export.exit_code != 0
    assert "active traces cannot be exported" in active_export.output
    assert active_prune.exit_code != 0
    assert "active or unsealed" in active_prune.output
    assert trace_root.exists()

    seal_attempt_trace(trace_root=trace_root, usage={"prompt_tokens": 10})
    destination = tmp_path / "cloud"
    first_export = runner.invoke(
        main, ["traces", "export", manifest_id, "--destination", str(destination)]
    )
    second_export = runner.invoke(
        main, ["traces", "export", manifest_id, "--destination", str(destination)]
    )

    assert first_export.exit_code == 0, first_export.output
    assert second_export.exit_code == 0, second_export.output
    exported_root = destination / manifest_id
    assert first_export.output.strip() == str(exported_root.resolve())
    assert second_export.output.strip() == str(exported_root.resolve())
    assert (
        exported_root.joinpath("agents/output.txt").read_text(encoding="utf-8")
        == "raw local output\n"
    )
    outbox = trace_export_outbox_dir_path(repo_root=tmp_path) / f"{manifest_id}.json"
    outbox_payload = json.loads(outbox.read_text(encoding="utf-8"))
    assert outbox_payload["status"] == "exported"
    assert outbox_payload["attempts"] == 1

    pruned = runner.invoke(main, ["traces", "prune", str(trace_root)])
    empty_list = runner.invoke(main, ["traces", "list"])
    assert pruned.exit_code == 0, pruned.output
    assert not trace_root.exists()
    assert empty_list.output == "No traces found.\n"
    assert outbox.exists()


def test_trace_path_commands_are_confined_to_repository_trace_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    outside = tmp_path / "unrelated"
    outside.mkdir()
    outside.joinpath("trace_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_id": "trace-unrelated",
                "lifecycle": "sealed",
                "sealed_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["traces", "prune", str(outside)])

    assert result.exit_code != 0
    assert "outside this repository's trace root" in result.output
    assert outside.exists()


def test_trace_commands_report_drift_refuse_export_and_allow_observed_prune(
    tmp_path: Path, monkeypatch: Any
) -> None:
    trace_root, manifest = create_attempt_trace(
        repo_root=tmp_path,
        root_session_id="root",
        session_id="leaf",
        request_id=None,
        work_item_id=None,
        workflow_set="main",
        workflow_id="implement",
        iteration=1,
        attempt_id="attempt-drifted",
    )
    artifact = trace_write_text(
        trace_root=trace_root,
        relative_path="agents/output.txt",
        content="sealed output",
    )
    seal_attempt_trace(trace_root=trace_root, usage=None)
    artifact.write_text("changed after seal", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    manifest_id = str(manifest["manifest_id"])

    listed = runner.invoke(main, ["traces", "list"])
    inspected = runner.invoke(main, ["traces", "inspect", manifest_id])
    exported = runner.invoke(
        main,
        ["traces", "export", manifest_id, "--destination", str(tmp_path / "cloud")],
    )

    assert listed.exit_code == 0, listed.output
    assert "integrity=failed" in listed.output
    assert inspected.exit_code == 0, inspected.output
    observed = json.loads(inspected.output)["observed_integrity"]
    assert observed["status"] == "failed"
    assert [item["path"] for item in observed["modified"]] == ["agents/output.txt"]
    assert exported.exit_code != 0
    assert "sealed trace integrity verification failed" in exported.output
    assert not (tmp_path / "cloud").exists()

    pruned = runner.invoke(main, ["traces", "prune", manifest_id])

    assert pruned.exit_code == 0, pruned.output
    assert "integrity=failed" in pruned.output
    assert not trace_root.exists()
