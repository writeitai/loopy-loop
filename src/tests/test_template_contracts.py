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
    """Packaged v3 contracts separate orchestration from advisory eval roles."""

    delivery = run_preflight(repo_root=_template_root("inner_outer_eval"))
    parent = run_preflight(repo_root=_template_root("pm_planner_dispatcher"))

    assert set(delivery.workflow_contract.roles) == {
        workflow.id for workflow in delivery.workflows
    }
    assert delivery.workflow_contract.layer_kind == "delivery"
    assert delivery.workflow_contract.session_protocol_version == 3
    assert delivery.workflow_contract.child_interface == "none"
    assert delivery.workflow_contract.orchestration is not None
    assert delivery.workflow_contract.orchestration.completion_role == "outer"
    assert delivery.workflow_contract.orchestration.plan_owner == "outer"
    assert delivery.workflow_contract.orchestration.handoff_owner == "outer"
    assert delivery.workflow_contract.orchestration.task_acceptance_owner == "outer"
    assert delivery.workflow_contract.evaluation.advisory is True
    assert delivery.workflow_contract.evaluation.check_author_roles == [
        "eval_reviewer",
        "outer",
    ]
    assert delivery.workflow_contract.evaluation.check_runner_roles == [
        "eval_runner",
        "outer",
    ]
    delivery_plan = next(
        item
        for item in delivery.workflow_contract.state
        if item["path"] == "project_state/plan.md"
    )
    delivery_tasks = next(
        item
        for item in delivery.workflow_contract.state
        if item["path"] == "project_state/tasks/"
    )
    assert delivery_plan == {
        "path": "project_state/plan.md",
        "owner_role": "outer",
        "contributor_roles": [],
    }
    assert delivery_tasks["owner_role"] == "outer"
    assert delivery_tasks["contributor_roles"] == ["inner"]

    assert set(parent.workflow_contract.roles) == {
        workflow.id for workflow in parent.workflows
    }
    assert set(parent.workflow_contract.roles) == {"planner", "dispatcher"}
    assert parent.workflow_contract.layer_kind == "program"
    assert parent.workflow_contract.session_protocol_version == 3
    assert parent.workflow_contract.child_interface == "recursive"
    assert parent.workflow_contract.orchestration is not None
    assert parent.workflow_contract.orchestration.completion_role == "planner"
    assert parent.workflow_contract.orchestration.plan_owner == "planner"
    assert parent.workflow_contract.orchestration.handoff_owner == "planner"
    assert parent.workflow_contract.orchestration.child_acceptance_owner == "planner"
    assert parent.workflow_contract.evaluation.advisory is True
    assert parent.workflow_contract.evaluation.check_author_roles == ["planner"]
    assert parent.workflow_contract.evaluation.check_runner_roles == ["planner"]
    dispatch_inputs = next(
        item
        for item in parent.workflow_contract.state
        if item["path"] == "project_state/dispatch_inputs/"
    )
    assert dispatch_inputs["owner_role"] == "dispatcher"
    assert dispatch_inputs["contributor_roles"] == []


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


def test_inner_executes_outer_selection_without_bootstrapping_plan() -> None:
    """Inner reports a missing selection instead of taking outer's authority."""

    prompt = _workflow_prompt(
        template="inner_outer_eval", workflow_set="inner_outer_eval", workflow="inner"
    ).lower()
    prompt_words = " ".join(prompt.split())

    assert "execute exactly the one active leaf selected by outer" in prompt_words
    assert "do not choose a different leaf" in prompt_words
    assert "bootstrap or rewrite the layer plan" in prompt_words
    assert "report that precise gap" in prompt_words


def test_only_layer_orchestrators_publish_successful_terminal_control() -> None:
    """Successful control belongs to outer/planner, never an eval role."""

    writers: set[tuple[str, str]] = set()
    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        for path in _template_root(template).glob(
            ".loopy_loop/workflow_sets/*/workflows/*/prompt.txt"
        ):
            if '"stop_reason": "goal_met"' in path.read_text(encoding="utf-8"):
                writers.add((template, path.parent.name))

    assert writers == {
        ("inner_outer_eval", "outer"),
        ("pm_planner_dispatcher", "planner"),
    }


def test_dispatcher_teaches_v2_pending_child_contract() -> None:
    """Dispatcher transports a milestone in the unchanged v2 child request."""

    prompt = _workflow_prompt(
        template="pm_planner_dispatcher",
        workflow_set="pm_planner_dispatcher",
        workflow="dispatcher",
    )
    prompt_words = " ".join(prompt.split())

    assert "canonical `pending` directory" in prompt_words
    assert '"schema_version": 2' in prompt
    assert '"request_id"' in prompt
    assert '"origin"' in prompt
    assert '"assignment"' in prompt
    assert '"completion_criteria"' in prompt
    assert '"required_evidence"' in prompt
    assert "do not narrow it to an exact leaf" in prompt_words.lower()
    assert "child outer role owns its own plan and leaf decomposition" in prompt_words
    assert "project_state/dispatch_inputs/<request_id>.json" in prompt
    assert (
        '"ref": "parent:/project_state/dispatch_inputs/'
        'stable-unique-request-id.json"' in prompt
    )
    assert (
        "never declare a mutable plan or task record as a child input"
        in prompt_words.lower()
    )
    assert (
        "immutable snapshot, snapshot hash, request rename, then factual ledger link"
        in prompt_words
    )
    assert "child eval receipt" not in prompt


def test_eval_reviewer_examples_match_eval_banana_schema(tmp_path: Path) -> None:
    """The one-layer optional reviewer teaches the real judge-check schema."""

    prompt = _workflow_prompt(
        template="inner_outer_eval",
        workflow_set="inner_outer_eval",
        workflow="eval_reviewer",
    )
    assert (
        "eval-banana validate --no-project-config --cwd <repo_root> "
        "--check-dir <eval_checks> "
        "--harness-agent <selected_harness_family>"
    ) in prompt
    assert "Omit a\nper-check `model`" in prompt
    match = re.search(r"```yaml\n(.*?)\n```", prompt, flags=re.DOTALL)
    assert match is not None
    check_path = tmp_path / "inner_outer_eval.yaml"
    check_path.write_text(match.group(1) + "\n", encoding="utf-8")

    check = load_check_definition(path=check_path)

    assert isinstance(check, HarnessJudgeCheckDefinition)
    assert check.id == "goal_outcome"
    assert check.model is None


def test_eval_runner_selects_judge_from_roster_and_publishes_advisory_receipt() -> None:
    """The optional runner records effective selection and never controls stop."""

    prompt = _workflow_prompt(
        template="inner_outer_eval",
        workflow_set="inner_outer_eval",
        workflow="eval_runner",
    )
    prompt_words = " ".join(prompt.split())

    assert "harness_capability_roster" in prompt
    assert "--harness-agent <judge_family>" in prompt
    assert "--harness-model <judge_model>" in prompt
    assert "--harness-reasoning-effort <judge_effort>" in prompt
    assert "loopy capture-git-receipt" in prompt
    assert "trace:<trace_manifest_id>:/eval/report.json" in prompt
    assert "copy those canonical definition digests" in prompt_words.lower()
    assert "do not manually hash" in prompt_words.lower()
    assert "do not write `goal_check.json` or" in prompt_words.lower()
    assert '"stop_reason": "goal_met"' not in prompt


def test_packaged_cadence_runs_eval_after_three_role_successes(
    history_entry_factory: Any,
) -> None:
    expected = {
        "inner_outer_eval": [
            "eval_reviewer",
            "outer",
            "inner",
            "outer",
            "inner",
            "outer",
            "inner",
            "eval_reviewer",
            "eval_runner",
            "outer",
        ],
        "pm_planner_dispatcher": [
            "planner",
            "dispatcher",
            "planner",
            "dispatcher",
            "planner",
            "dispatcher",
            "planner",
            "dispatcher",
            "planner",
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


def test_pm_template_has_no_scheduled_eval_workflows() -> None:
    """Program-level eval remains a planner option, not a duplicate cadence."""

    workflows_root = _template_root("pm_planner_dispatcher").joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows"
    )
    assert {path.name for path in workflows_root.iterdir()} == {"planner", "dispatcher"}


def test_stock_prompts_use_roster_families_and_semantic_tiers() -> None:
    """Role prompts stay provider-neutral while teaching collaboration defaults."""

    banned = (
        "--harness-agent codex",
        "--harness-agent claude",
        "--harness-agent gemini",
        "gpt-",
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
        "gemini-",
    )
    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        for prompt_path in _template_root(template).glob(
            ".loopy_loop/workflow_sets/*/workflows/*/prompt.txt"
        ):
            prompt = prompt_path.read_text(encoding="utf-8").lower()
            assert "harness_capability_roster" in prompt
            for text in banned:
                assert text not in prompt, f"{text!r} in {prompt_path}"


def test_eval_check_authoring_prefers_parallel_cross_family_review_without_gate() -> (
    None
):
    """Strong eval collaboration is explicit guidance, never a quorum."""

    prompt = _workflow_prompt(
        template="inner_outer_eval",
        workflow_set="inner_outer_eval",
        workflow="eval_reviewer",
    ).lower()
    prompt_words = " ".join(prompt.split())

    assert "different enabled harness families" in prompt_words
    assert "analyze goal coverage and likely failure modes in parallel" in prompt_words
    assert "different-family reviewers" in prompt_words
    assert (
        "not an agent count, family quorum, publication gate, or completion gate"
        in prompt_words
    )


def test_template_tier_examples_name_all_four_canonical_tiers() -> None:
    """Both starter configs expose the complete semantic tier vocabulary."""

    for template in ("inner_outer_eval", "pm_planner_dispatcher"):
        config = (
            _template_root(template)
            .joinpath("loopy_loop_config.yaml")
            .read_text(encoding="utf-8")
        )
        for tier in ("frontier", "strong", "standard", "economy"):
            assert f"#   {tier}:" in config
        assert '# default_tier: "standard"' in config


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
