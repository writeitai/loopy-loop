from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from eval_banana.loader import load_check_definition
from eval_banana.models import HarnessJudgeCheckDefinition

from loopy_loop.config import load_workflow_definitions
from loopy_loop.config import load_workflow_set_preamble
from loopy_loop.config import run_preflight
from loopy_loop.scheduler import choose_next_workflow

TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "loopy_loop" / "templates"
TEMPLATE_SETS = ("inner_outer_eval", "pm_planner_dispatcher")
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


def _prompt_paths(template: str) -> list[Path]:
    return sorted(
        _template_root(template).glob(
            ".loopy_loop/workflow_sets/*/workflows/*/prompt.txt"
        )
    )


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
    # Under B (receipts retired), eval_runner records advisory results in
    # project_state/eval_results.md rather than sealed eval_receipts/, so it is
    # no longer a contract check-runner role that the engine seals receipts for.
    assert delivery.workflow_contract.evaluation.check_runner_roles == ["outer"]
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
    # The retired dispatch-snapshot state path is gone: a v3 child request is one
    # authored goal, not an immutable snapshot the dispatcher must hash and pin.
    assert all(
        item["path"] != "project_state/dispatch_inputs/"
        for item in parent.workflow_contract.state
    )


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


def test_role_prompts_fit_one_screen() -> None:
    """Every stock role prompt stays within the P8 one-screen budget."""

    for template in TEMPLATE_SETS:
        paths = _prompt_paths(template)
        assert paths
        for path in paths:
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            assert line_count <= 80, f"{path} has {line_count} lines"


def test_prompts_drop_retired_ceremony_and_model_mandates() -> None:
    """Prompts and the shared preamble carry no retired ceremony or vendor names."""

    banned = (
        "questions.md",
        "waiting-for-human",
        "waiting_for_human",
        "target_paths",
        "/_feature_planning",
        "create an agent team",
        "assignment envelope",
        "absolute_paths",
        "dispatch_inputs",
        "required_evidence",
        "think ultra",
        "ultra deeply",
        "using codex",
        "with gemini",
        "gpt-",
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
        "gemini-",
        '"schema_version": 2',
    )
    for template in TEMPLATE_SETS:
        texts = [path.read_text(encoding="utf-8") for path in _prompt_paths(template)]
        preamble = load_workflow_set_preamble(
            repo_root=_template_root(template), workflow_set=template
        )
        assert preamble, f"{template} ships a shared preamble"
        texts.append(preamble)
        for text in texts:
            lowered = text.lower()
            for needle in banned:
                assert needle.lower() not in lowered, f"{needle!r} in {template}"


def test_shared_rules_live_once_in_the_preamble() -> None:
    """Cross-role rules are single-sourced in the preamble, not per prompt."""

    for template in TEMPLATE_SETS:
        preamble = load_workflow_set_preamble(
            repo_root=_template_root(template), workflow_set=template
        )
        assert preamble is not None
        lowered = preamble.lower()
        assert "capability roster in paths.json" in lowered
        assert "previous_worker_sessions" in lowered
        assert "result card" in lowered
        assert "renaming it into place atomically" in lowered
        assert "failing checks" in lowered
        for path in _prompt_paths(template):
            prompt = path.read_text(encoding="utf-8").lower()
            assert "previous_worker_sessions" not in prompt, path
            assert "result card" not in prompt, path


def test_only_layer_orchestrators_publish_successful_terminal_control() -> None:
    """Successful control belongs to outer/planner, never an eval role."""

    writers: set[tuple[str, str]] = set()
    for template in TEMPLATE_SETS:
        for path in _prompt_paths(template):
            if '"stop_reason": "goal_met"' in path.read_text(encoding="utf-8"):
                writers.add((template, path.parent.name))

    assert writers == {
        ("inner_outer_eval", "outer"),
        ("pm_planner_dispatcher", "planner"),
    }


def test_dispatcher_teaches_v3_child_request() -> None:
    """Dispatcher transports the milestone as one authored v3 goal text."""

    prompt = _workflow_prompt(
        template="pm_planner_dispatcher",
        workflow_set="pm_planner_dispatcher",
        workflow="dispatcher",
    )
    words = " ".join(prompt.split())

    assert "child_requests/pending/" in prompt
    assert '"schema_version": 3' in prompt
    assert '"request_id"' in prompt
    assert '"workflow_set"' in prompt
    assert '"goal"' in prompt
    assert '"origin"' in prompt
    assert '"supersedes_request_id"' in prompt
    # v3 is one authored brief, not the retired field arrays / hashed snapshots.
    assert "completion_criteria" not in prompt
    assert "required_evidence" not in prompt
    assert "dispatch_inputs" not in prompt
    assert "sha256" not in prompt.lower()
    assert "self-contained brief" in words
    assert "the child owns its own plan" in words
    assert "request_id, not the filename, is the idempotency key" in words


def test_inner_executes_outer_selection_without_bootstrapping_plan() -> None:
    """Inner reports a missing selection instead of taking outer's authority."""

    prompt = _workflow_prompt(
        template="inner_outer_eval", workflow_set="inner_outer_eval", workflow="inner"
    )
    words = " ".join(prompt.split())

    assert "Execute exactly the one active task outer selected" in words
    assert "Do not choose a different task" in words
    assert "bootstrap or rewrite the layer plan" in words
    assert "report that precise gap" in words


def test_inner_keeps_delegation_a_judgment_call() -> None:
    """Delegation stays inner's judgment; mechanics live in the preamble only."""

    prompt = _workflow_prompt(
        template="inner_outer_eval", workflow_set="inner_outer_eval", workflow="inner"
    )
    words = " ".join(prompt.split())

    assert "is your judgment" in words
    assert "it is equally valid to do the work directly" in words
    # The spawn-metadata recipe is retired; delegation is single-sourced.
    assert "delegated_role" not in prompt
    assert "delegated_task_id" not in prompt


def test_eval_reviewer_examples_match_eval_banana_schema(tmp_path: Path) -> None:
    """The optional reviewer still teaches the real judge-check schema."""

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
    assert "Omit a per-check\n`model`" in prompt
    match = re.search(r"```yaml\n(.*?)\n```", prompt, flags=re.DOTALL)
    assert match is not None
    check_path = tmp_path / "inner_outer_eval.yaml"
    check_path.write_text(match.group(1) + "\n", encoding="utf-8")

    check = load_check_definition(path=check_path)

    assert isinstance(check, HarnessJudgeCheckDefinition)
    assert check.id == "goal_outcome"
    assert check.model is None


def test_eval_reviewer_lifts_the_deterministic_check_ban() -> None:
    """C2: objective facts go to deterministic checks; judge checks for meaning."""

    prompt = _workflow_prompt(
        template="inner_outer_eval",
        workflow_set="inner_outer_eval",
        workflow="eval_reviewer",
    )
    words = " ".join(prompt.split())

    assert "Judge checks evaluate semantics and quality" in words
    assert "belong in deterministic checks" in words
    assert "the repo's own test and lint suites" in words
    assert "Keep judge checks for what actually needs judgment" in words
    # The old blanket ban is gone.
    assert "do not invent deterministic checks" not in words.lower()


def test_eval_runner_is_advisory_and_reads_report_md() -> None:
    """The optional runner records advisory evidence and never controls stop."""

    prompt = _workflow_prompt(
        template="inner_outer_eval",
        workflow_set="inner_outer_eval",
        workflow="eval_runner",
    )
    words = " ".join(prompt.split())

    assert "eval-banana validate" in prompt
    assert "eval-banana run" in prompt
    assert "--harness-agent <judge_family>" in prompt
    assert "--harness-model <judge_model>" in prompt
    assert "--harness-reasoning-effort <judge_effort>" in prompt
    assert "Read report.md" in prompt
    assert "not report.json" in prompt
    assert "project_state/eval_results.md" in prompt
    assert "project_state/eval_request.md" in prompt
    # Advisory only: no completion authority, no retired receipt ceremony.
    assert '"stop_reason": "goal_met"' not in prompt
    assert "goal_check.json" not in prompt
    assert "you never publish control" in words


def test_packaged_schedule_authors_checks_on_start_then_evals_on_request(
    history_entry_factory: Any,
) -> None:
    """eval_reviewer authors on start; thereafter eval fires only when asked."""

    workflows = load_workflow_definitions(
        repo_root=_template_root("inner_outer_eval"), workflow_set="inner_outer_eval"
    )

    def _run(*, history: list[Any], iteration_count: int, eval_requested: bool) -> str:
        chosen = choose_next_workflow(
            workflows=workflows,
            history=history,
            iteration_count=iteration_count,
            eval_requested=eval_requested,
        )
        assert chosen is not None
        return chosen.id

    # No request standing: eval_reviewer authors the initial check-set on start
    # (run_on_start), then outer/inner carry the work and no eval role recurs.
    history: list[Any] = []
    seen: list[str] = []
    for iteration_count in range(8):
        chosen_id = _run(
            history=history, iteration_count=iteration_count, eval_requested=False
        )
        seen.append(chosen_id)
        history.append(
            history_entry_factory(
                iteration=iteration_count + 1,
                workflow_id=chosen_id,
                workflow_set="inner_outer_eval",
                success=True,
            )
        )
    assert seen[0] == "eval_reviewer"
    assert "eval_reviewer" not in seen[1:]
    assert "eval_runner" not in seen
    assert set(seen[1:]) == {"outer", "inner"}

    # Once real work has run, a pending request re-authors then runs the checks:
    # eval_reviewer (refresh) is followed by eval_runner (must_follow).
    requested_history = [
        history_entry_factory(
            iteration=index + 1,
            workflow_id=workflow_id,
            workflow_set="inner_outer_eval",
            success=True,
        )
        for index, workflow_id in enumerate(["eval_reviewer", "outer", "inner"])
    ]
    assert (
        _run(history=requested_history, iteration_count=3, eval_requested=True)
        == "eval_reviewer"
    )
    requested_history.append(
        history_entry_factory(
            iteration=4,
            workflow_id="eval_reviewer",
            workflow_set="inner_outer_eval",
            success=True,
        )
    )
    assert (
        _run(history=requested_history, iteration_count=4, eval_requested=True)
        == "eval_runner"
    )


def test_pm_template_has_no_scheduled_eval_workflows() -> None:
    """Program-level eval remains a planner option, not a duplicate cadence."""

    workflows_root = _template_root("pm_planner_dispatcher").joinpath(
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows"
    )
    assert {path.name for path in workflows_root.iterdir()} == {"planner", "dispatcher"}


def test_template_tier_examples_name_all_four_canonical_tiers() -> None:
    """Both starter configs expose the complete semantic tier vocabulary."""

    for template in TEMPLATE_SETS:
        config = (
            _template_root(template)
            .joinpath("loopy_loop_config.yaml")
            .read_text(encoding="utf-8")
        )
        for tier in ("frontier", "strong", "standard", "economy"):
            assert f"#   {tier}:" in config
        assert '# default_tier: "standard"' in config


def test_template_gitignores_cover_runtime_state_and_trace_roots() -> None:
    for template in TEMPLATE_SETS:
        rules = set(
            _template_root(template)
            .joinpath(".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert RUNTIME_IGNORE_RULES <= rules


def test_root_goal_files_carry_no_loop_mechanics_vocabulary() -> None:
    """Root goals read like a product owner wrote them (single-goal §2)."""

    banned = re.compile(r"\b(loop|iteration|workflow|session|cadence)\b", re.IGNORECASE)
    for template in TEMPLATE_SETS:
        goal = (
            _template_root(template)
            .joinpath("loopy_loop_goal.txt")
            .read_text(encoding="utf-8")
        )
        match = banned.search(goal)
        assert match is None, f"{template} goal uses {match and match.group(0)!r}"


def test_pm_goal_scaffold_requires_a_target_outcome() -> None:
    goal = (
        _template_root("pm_planner_dispatcher")
        .joinpath("loopy_loop_goal.txt")
        .read_text(encoding="utf-8")
    )

    assert goal.startswith("REPLACE THIS TEXT")
    assert "observable" in goal.lower()
    assert "product" in goal.lower()
