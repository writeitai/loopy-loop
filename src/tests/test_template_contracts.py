from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from eval_banana.loader import load_check_definition
from eval_banana.models import HarnessJudgeCheckDefinition

from loopy_loop.config import load_workflow_definitions
from loopy_loop.config import run_preflight
from loopy_loop.scheduler import choose_next_workflow

TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "loopy_loop" / "templates"
RUNTIME_IGNORE_RULES = {
    ".loopy_loop/sessions/",
    ".loopy_loop/traces/",
    ".loopy_loop/trace_finalization_outbox/",
    ".loopy_loop/repository.json",
    ".loopy_loop/state.json",
    ".loopy_loop/state.json.lock",
    ".loopy_loop/state.json.archive_*.json",
}


def _template_root(name: str) -> Path:
    return TEMPLATES_ROOT / name


def _workflow_prompt(*, template: str, workflow_set: str, workflow: str) -> str:
    return (
        _template_root(template)
        / ".loopy_loop"
        / "workflow_sets"
        / workflow_set
        / "workflows"
        / workflow
        / "prompt.txt"
    ).read_text(encoding="utf-8")


def test_packaged_workflow_contracts_name_every_role_and_owner() -> None:
    """Packaged v2 contracts name every scheduled and artifact-owning role."""

    delivery = run_preflight(repo_root=_template_root("inner_outer_eval"))
    parent = run_preflight(repo_root=_template_root("pm_planner_dispatcher"))

    assert set(delivery.workflow_contract.roles) == {
        workflow.id for workflow in delivery.workflows
    }
    assert delivery.workflow_contract.layer_kind == "delivery"
    assert delivery.workflow_contract.session_protocol_version == 2
    assert delivery.workflow_contract.child_interface == "none"
    assert delivery.workflow_contract.eval.author_role == "eval_reviewer"
    assert delivery.workflow_contract.eval.runner_role == "eval_runner"
    assert delivery.workflow_contract.eval.goal_control_role == "eval_runner"
    assert delivery.workflow_contract.task_acceptance_role == "outer"

    assert set(parent.workflow_contract.roles) == {
        workflow.id for workflow in parent.workflows
    }
    assert parent.workflow_contract.layer_kind == "program"
    assert parent.workflow_contract.session_protocol_version == 2
    assert parent.workflow_contract.child_interface == "recursive"
    assert parent.workflow_contract.eval.author_role == "eval_reviewer"
    assert parent.workflow_contract.eval.runner_role == "eval_runner"
    assert parent.workflow_contract.eval.goal_control_role == "eval_runner"
    assert parent.workflow_contract.task_acceptance_role == "planner"
    dispatch_inputs = next(
        item
        for item in parent.workflow_contract.state
        if item["path"] == "project_state/dispatch_inputs/"
    )
    assert dispatch_inputs["accountable_roles"] == ["dispatcher"]


def test_explicit_contract_without_protocol_version_defaults_to_v2(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """An explicit current contract cannot inherit legacy-v1 model defaults."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    contract_path = (
        repo_root / ".loopy_loop" / "workflow_sets" / "main" / "contract.yaml"
    )
    contract_path.write_text(
        """schema_version: 1
layer_kind: delivery
roles:
  planner:
    responsibility: Plan work.
  goal_check:
    responsibility: Evaluate completion.
state: []
eval:
  author_role: goal_check
  runner_role: goal_check
  goal_control_role: goal_check
task_acceptance_role: planner
terminal_blocker_reporting_roles: [planner, goal_check]
child_interface: recursive
""",
        encoding="utf-8",
    )

    explicit = run_preflight(repo_root=repo_root)
    contract_path.unlink()
    derived = run_preflight(repo_root=repo_root)

    assert explicit.workflow_contract.session_protocol_version == 2
    assert derived.workflow_contract.session_protocol_version == 1


def test_packaged_prompt_contract_uses_absolute_assignment_paths() -> None:
    banned = (
        "questions.md",
        "waiting-for-human",
        "waiting_for_human",
        "target_paths",
        "/_feature_planning",
        "<session directory>/eval_results",
        "create an agent team",
    )
    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        prompt_paths = sorted(
            _template_root(template).glob(
                ".loopy_loop/workflow_sets/*/workflows/*/prompt.txt"
            )
        )
        assert prompt_paths
        for path in prompt_paths:
            prompt = path.read_text(encoding="utf-8")
            lowered = prompt.lower()
            assert "assignment envelope" in lowered, path
            assert "absolute" in lowered, path
            for text in banned:
                assert text not in lowered, f"{text!r} in {path}"


def test_inner_prompt_keeps_delegation_dynamic() -> None:
    prompt = _workflow_prompt(
        template="inner_outer_eval", workflow_set="inner_outer_eval", workflow="inner"
    ).lower()
    prompt_words = " ".join(prompt.split())

    assert "dynamic delegation" in prompt
    assert "decide at runtime whether delegation helps" in prompt_words
    assert "it is valid to do the work directly" in prompt_words
    assert "delegated_role" in prompt
    assert "delegated_task_id" in prompt
    assert "expected_outputs" in prompt
    assert "state_responsibility" in prompt


def test_only_eval_runners_publish_successful_terminal_control() -> None:
    writers: set[tuple[str, str]] = set()
    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        for path in _template_root(template).glob(
            ".loopy_loop/workflow_sets/*/workflows/*/prompt.txt"
        ):
            if '"stop_reason": "goal_met"' in path.read_text(encoding="utf-8"):
                writers.add((template, path.parent.name))

    assert writers == {
        ("inner_outer_eval", "eval_runner"),
        ("pm_planner_dispatcher", "eval_runner"),
    }


def test_dispatcher_teaches_v2_pending_child_contract() -> None:
    """The stock dispatcher publishes v2 requests from immutable input bytes."""

    prompt = _workflow_prompt(
        template="pm_planner_dispatcher",
        workflow_set="pm_planner_dispatcher",
        workflow="dispatcher",
    )
    prompt_words = " ".join(prompt.split())

    assert "canonical `pending` directory" in prompt
    assert '"schema_version": 2' in prompt
    assert '"request_id"' in prompt
    assert '"origin"' in prompt
    assert '"assignment"' in prompt
    assert '"completion_criteria"' in prompt
    assert '"required_evidence"' in prompt
    assert "Do not copy this parent session's broader" in prompt_words
    assert "project_state/dispatch_inputs/<request_id>.json" in prompt
    assert (
        '"ref": "parent:/project_state/dispatch_inputs/'
        'stable-unique-request-id.json"' in prompt
    )
    assert "Never declare mutable `project_state/work_items.md`" in prompt
    assert (
        "immutable snapshot, snapshot hash, request rename, then mutable "
        "ledger update" in prompt_words
    )


def test_eval_reviewer_examples_match_eval_banana_schema(tmp_path: Path) -> None:
    for template, check_id in (
        ("inner_outer_eval", "goal_outcome"),
        ("pm_planner_dispatcher", "parent_goal_outcome"),
    ):
        prompt = _workflow_prompt(
            template=template, workflow_set=template, workflow="eval_reviewer"
        )
        assert (
            "eval-banana validate --no-project-config --cwd <repo_root> "
            "--check-dir <eval_checks> "
            "--harness-agent codex"
        ) in prompt
        assert "omit\nper-check `model`" in prompt
        match = re.search(r"```yaml\n(.*?)\n```", prompt, flags=re.DOTALL)
        assert match is not None
        check_path = tmp_path / f"{template}.yaml"
        check_path.write_text(match.group(1) + "\n", encoding="utf-8")

        check = load_check_definition(path=check_path)

        assert isinstance(check, HarnessJudgeCheckDefinition)
        assert check.id == check_id
        assert check.model is None


def test_eval_runners_pin_judge_and_use_trace_output() -> None:
    """Packaged runners pin the judge and reuse eval-banana's reported digest."""

    validate_command = (
        "eval-banana validate --no-project-config --cwd <repo_root> "
        "--check-dir <eval_checks> "
        "--harness-agent codex"
    )
    command = (
        "--output-dir <raw_eval_output> --pass-threshold 1.0 "
        "--harness-agent codex --harness-model gpt-5.5 "
        "--harness-reasoning-effort high"
    )
    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        prompt = _workflow_prompt(
            template=template, workflow_set=template, workflow="eval_runner"
        )
        prompt_words = " ".join(prompt.split())
        assert validate_command in prompt
        assert command in prompt
        assert "loopy capture-git-receipt" in prompt
        assert "trace:<trace_manifest_id>:/eval/report.json" in prompt
        assert '"reasoning_effort": "high"' in prompt
        assert '"schema_version": 2' in prompt
        assert '"eval_receipt_ref"' in prompt
        assert "copy the exact `check_definition_sha256`" in prompt_words
        assert "Do not manually hash" in prompt_words


def test_packaged_cadence_runs_eval_after_three_role_successes(
    history_entry_factory: Any,
) -> None:
    expected = {
        "inner_outer_eval": [
            "eval_reviewer",
            "eval_runner",
            "outer",
            "inner",
            "outer",
            "inner",
            "outer",
            "inner",
            "eval_reviewer",
            "eval_runner",
        ],
        "pm_planner_dispatcher": [
            "eval_reviewer",
            "eval_runner",
            "planner",
            "dispatcher",
            "planner",
            "dispatcher",
            "planner",
            "eval_reviewer",
            "eval_runner",
        ],
    }
    for template, sequence in expected.items():
        workflows = load_workflow_definitions(
            repo_root=_template_root(template), workflow_set=template
        )
        history = []
        actual: list[str] = []
        for iteration_count in range(len(sequence)):
            chosen = choose_next_workflow(
                workflows=workflows, history=history, iteration_count=iteration_count
            )
            assert chosen is not None
            actual.append(chosen.id)
            history.append(
                history_entry_factory(
                    iteration=iteration_count + 1,
                    workflow_id=chosen.id,
                    workflow_set=template,
                    success=True,
                )
            )
        assert actual == sequence


def test_template_gitignores_cover_runtime_state_and_trace_roots() -> None:
    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        rules = set(
            _template_root(template)
            .joinpath(".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert RUNTIME_IGNORE_RULES <= rules


def test_pm_goal_scaffold_requires_a_target_outcome() -> None:
    goal = (
        _template_root("pm_planner_dispatcher")
        .joinpath("loopy_loop_goal.txt")
        .read_text(encoding="utf-8")
    )

    assert goal.startswith("REPLACE THIS TEXT")
    assert "observable completion criteria" in goal.lower()
    assert "mechanism" in goal.lower()
