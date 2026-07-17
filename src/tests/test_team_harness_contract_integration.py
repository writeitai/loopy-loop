from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from team_harness import CallerContext
from team_harness import get_capabilities
from team_harness import TeamHarness
import team_harness.config as harness_config_module

from loopy_loop.models import REQUIRED_HARNESS_CAPABILITIES
from loopy_loop.sessions import assignment_path
from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import project_state_dir_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.tracing import create_attempt_trace
from loopy_loop.tracing import import_harness_artifacts
from loopy_loop.tracing import read_trace_manifest
from loopy_loop.worker import _worker_capabilities


def test_real_team_harness_writes_and_loopy_imports_caller_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "session-one"
    workflow_id = "inner"
    attempt_id = "attempt-one"
    create_session_dir(
        repo_root=tmp_path,
        session_id=session_id,
        goal_hash="integration-goal",
        goal="Exercise the real team-harness caller contract",
        workflow_set="inner_outer_eval",
        workflow_contract={
            "schema_version": 1,
            "session_protocol_version": 2,
            "layer_kind": "work",
            "roles": {workflow_id: {"responsibility": "Run the integration"}},
            "state": [],
            "eval": {},
            "task_acceptance_role": workflow_id,
            "terminal_blocker_reporting_roles": [workflow_id],
            "child_interface": "none",
        },
        schema_version=2,
    )
    trace_root, _ = create_attempt_trace(
        repo_root=tmp_path,
        root_session_id=session_id,
        session_id=session_id,
        request_id="request-one",
        work_item_id="work-one",
        workflow_set="inner_outer_eval",
        workflow_id=workflow_id,
        iteration=1,
        attempt_id=attempt_id,
    )
    assignment = assignment_path(
        repo_root=tmp_path,
        session_id=session_id,
        iteration=1,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
    ).resolve()
    write_json_atomic(
        path=assignment, payload={"schema_version": 2, "attempt_id": attempt_id}
    )
    state_path = project_state_dir_path(
        repo_root=tmp_path, session_id=session_id
    ).resolve()
    context = CallerContext(
        trace_root=(trace_root / "harness").resolve(),
        parent_assignment_path=assignment,
        parent_attempt_id=attempt_id,
        root_session_id=session_id,
        session_id=session_id,
        session_depth=0,
        workflow_role=workflow_id,
        relevant_state_paths=(state_path,),
    )

    class FakeClient:
        api_base = "http://localhost:11434/v1"

        async def aclose(self) -> None:
            return None

    async def fake_resolve_model_limit(**_: object) -> int:
        return 128_000

    async def fake_run(messages: list[dict[str, object]], **_: object) -> None:
        messages.append({"role": "assistant", "content": "integrated"})

    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr(
        harness_config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml"
    )
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.load_skill_metadata", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    result = asyncio.run(
        TeamHarness(
            api_base="http://localhost:11434/v1",
            cwd=str(tmp_path),
            caller_context=context,
        ).run("Implement the integrated task")
    )

    capabilities = get_capabilities().capabilities
    assert REQUIRED_HARNESS_CAPABILITIES <= capabilities
    assert REQUIRED_HARNESS_CAPABILITIES <= _worker_capabilities()
    run_path = Path(result.run_json_path)
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    assert run_payload["caller_context"]["parent_attempt_id"] == attempt_id
    assert set(run_payload["capabilities"]) == capabilities

    import_harness_artifacts(
        trace_root=trace_root,
        run_json_path=result.run_json_path,
        session_output_dir=result.session_output_dir,
        harness_run_id=result.run_id,
    )
    manifest = read_trace_manifest(manifest_path=trace_root / "trace_manifest.json")
    assert manifest["identity"]["harness_run_id"] == result.run_id
    assert manifest["channels"]["coordinator_input"] == "complete"
    assert manifest["channels"]["coordinator_output"] == "complete"
    assert manifest["channels"]["direct_agents"] == "complete"
