from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
import json
from pathlib import Path
import time
import uuid

import click
from filelock import Timeout as FileLockTimeout
import uvicorn

from loopy_loop.config import ConfigError
from loopy_loop.config import DEFAULT_GOAL_FILENAME
from loopy_loop.config import estimate_cost_usd
from loopy_loop.config import load_root_config
from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.config import ModelPrices
from loopy_loop.config import ROOT_CONFIG_FILENAME
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.coordinator_app import session_tree_usage_totals
from loopy_loop.events import events_path
from loopy_loop.events import read_events
from loopy_loop.git_evidence import capture_git_evidence
from loopy_loop.models import LoopState
from loopy_loop.models import utc_now
from loopy_loop.sessions import append_jsonl_record
from loopy_loop.sessions import session_dir_path
from loopy_loop.sessions import SESSION_METADATA_FILENAME
from loopy_loop.sessions import state_path
from loopy_loop.sessions import user_updates_journal_path
from loopy_loop.sessions import write_json_atomic
from loopy_loop.state_store import StateStore
from loopy_loop.tracing import list_trace_manifests
from loopy_loop.tracing import read_trace_manifest
from loopy_loop.tracing import resolve_trace_manifest
from loopy_loop.tracing import TraceError
from loopy_loop.tracing import verify_trace_integrity
from loopy_loop.worker import run_worker_loop

GOAL_CHECK_WORKFLOW_ID = "goal_check"
DEFAULT_TEMPLATE_NAME = "default"
MAIN_WORKFLOW_SET_NAME = "main"
INNER_OUTER_EVAL_TEMPLATE_NAME = "inner_outer_eval"
PM_PLANNER_DISPATCHER_TEMPLATE_NAME = "pm_planner_dispatcher"
PACKAGED_TEMPLATE_FILES_BY_NAME = {
    INNER_OUTER_EVAL_TEMPLATE_NAME: [
        ".gitignore",
        ROOT_CONFIG_FILENAME,
        DEFAULT_GOAL_FILENAME,
        ".loopy_loop/workflow_sets/inner_outer_eval/contract.yaml",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_reviewer/config.yaml",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_reviewer/prompt.txt",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_runner/config.yaml",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_runner/prompt.txt",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/inner/config.yaml",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/inner/prompt.txt",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/outer/config.yaml",
        ".loopy_loop/workflow_sets/inner_outer_eval/workflows/outer/prompt.txt",
    ],
    PM_PLANNER_DISPATCHER_TEMPLATE_NAME: [
        ".gitignore",
        ROOT_CONFIG_FILENAME,
        DEFAULT_GOAL_FILENAME,
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/contract.yaml",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/eval_reviewer/config.yaml",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/eval_reviewer/prompt.txt",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/eval_runner/config.yaml",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/eval_runner/prompt.txt",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/planner/config.yaml",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/planner/prompt.txt",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/dispatcher/config.yaml",
        ".loopy_loop/workflow_sets/pm_planner_dispatcher/workflows/dispatcher/prompt.txt",
    ],
}
# Files a template ships FROM ANOTHER template's directory. The
# pm_planner_dispatcher dispatcher spawns child sessions running the
# inner_outer_eval workflow set, so a clean `loopy init` must ship that set
# too — sourced from the inner_outer_eval template itself so the two copies
# can never drift apart.
PACKAGED_TEMPLATE_EXTRA_SOURCES: dict[str, list[tuple[str, str]]] = {
    PM_PLANNER_DISPATCHER_TEMPLATE_NAME: [
        (INNER_OUTER_EVAL_TEMPLATE_NAME, relative_path)
        for relative_path in PACKAGED_TEMPLATE_FILES_BY_NAME[
            INNER_OUTER_EVAL_TEMPLATE_NAME
        ]
        if relative_path.startswith(".loopy_loop/workflow_sets/")
    ]
}
PACKAGED_TEMPLATE_NAMES = list(PACKAGED_TEMPLATE_FILES_BY_NAME)
GITIGNORE_LINES = [
    ".loopy_loop/sessions/",
    ".loopy_loop/traces/",
    ".loopy_loop/trace_finalization_outbox/",
    ".loopy_loop/repository.json",
    ".loopy_loop/state.json",
    ".loopy_loop/state.json.lock",
    ".loopy_loop/state.json.archive_*.json",
]
ROOT_CONFIG_TEMPLATE = f"""goal_file: "{DEFAULT_GOAL_FILENAME}"
workflow_set: "{MAIN_WORKFLOW_SET_NAME}"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "codex"
team_harness_model: "gpt-5.5"
team_harness_agents:
  - "codex"
  - "claude"
  - "gemini"
team_harness_agent_models:
  codex: "gpt-5.5"
  claude: "claude-opus-4-8"
  gemini: "gemini-3.5-flash"
team_harness_agent_reasoning_efforts: {{}}
# Optional coordinator retry controls. Omit to use team-harness defaults.
# team_harness_max_retries: 8
# team_harness_retry_base_delay_s: 2.0
# team_harness_retry_max_delay_s: 60.0
team_harness_api_base: "https://openrouter.ai/api/v1"
team_harness_api_key_env: "OPENROUTER_API_KEY"
"""
GOAL_TEMPLATE = """Ship a minimal working landing page
"""
GOAL_CHECK_CONFIG_TEMPLATE = """enabled: true
run_every: 1
must_follow: null
not_before_iteration: 1
description: "Evaluate whether the loop goal is already satisfied."
emits_goal_check: true
"""
DEFAULT_WORKFLOW_CONTRACT_TEMPLATE = """schema_version: 1
session_protocol_version: 2
layer_kind: work
roles:
  goal_check:
    responsibility: Author and run the layer-scoped LLM judgment, publish its receipt, and own goal-met control.
state:
  - path: eval_checks/
    accountable_roles: [goal_check]
  - path: eval_receipts/
    accountable_roles: [goal_check]
eval:
  author_role: goal_check
  runner_role: goal_check
  goal_control_role: goal_check
terminal_blocker_reporting_roles: [goal_check]
child_interface: recursive
"""
GOAL_CHECK_PROMPT_TEMPLATE = """Evaluate whether this session's scoped goal is satisfied.

Read the authoritative Assignment envelope and use only its absolute paths.
Confirm its root/session/goal/producer identity and the exact `repo_root`,
`goal_contract`, `eval_checks`, `eval_receipts`, `raw_eval_output`,
`git_receipts`, `control`, `trace_root`, and goal_check.json paths. Never
substitute an ancestor goal or rediscover state by searching the checkout.

Maintain at least one outcome-oriented `harness_judge` YAML check in the exact
eval_checks directory. Use only eval-banana fields `schema_version`, `id`,
`type`, `description`, optional `tags`, `instructions`, and optional `model`.
Do not create deterministic or implementation-prescriptive stock checks.

Run the hermetic evaluation from the absolute repository root:

```text
eval-banana validate --no-project-config --cwd <repo_root> --check-dir <eval_checks> --harness-agent codex
eval-banana run --no-project-config --flat-output --cwd <repo_root> --check-dir <eval_checks> --output-dir <raw_eval_output> --pass-threshold 1.0 --harness-agent codex --harness-model gpt-5.5 --harness-reasoning-effort high
loopy capture-git-receipt --repo-root <repo_root> --attempt-id <attempt_id> --output <git_receipts>/git-after-<attempt_id>.json
```

Missing tools/checks, validation or runner errors, and any failed check are a
false verdict, not permission to invent evidence. Read the generated
`<raw_eval_output>/report.json`; verify threshold 1.0, every declared check,
each `check_definition_sha256`, status, and each result's observed agent/model.
Copy each definition digest from that report into the receipt; never manually
hash the YAML because eval-banana owns the canonical definition-digest protocol.
Use the required parent harness run id from the automatic harness context.
Read the generated git-after
receipt. Atomically write a concise canonical report and EvalReceipt v1 under
eval_receipts. The receipt must bind the exact assignment subject/producer and
parent harness run,
nonempty check inventory, full check hashes, observed judge provider/model,
per-check results, canonical report hash, git `head` and `dirty_tree_digest`,
and exactly one hashed raw ref:
`trace:<trace_manifest_id>:/eval/report.json`.

Then atomically write goal_check.json v2 at the exact output path. Its verdict
and reason must exactly match the receipt and cite
`session:/eval_receipts/<eval-id>.json`. Only for a true verdict, atomically
write control v2 at the assignment control path with the exact producer,
nonblank reason, `stop_reason: goal_met`, the same receipt ref, and timestamp.
A false verdict leaves control running and records the next repair action.

Use `unresolvable_error` only for a genuinely terminal blocker after recording
specific nonblank attempted autonomous routes and evidence; ordinary failed
evaluation is repair work.
"""


@click.group()
def main() -> None:
    """loopy-loop CLI."""


@main.command("capture-git-receipt", hidden=True)
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
    required=True,
)
@click.option("--attempt-id", required=True)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False, resolve_path=True),
    required=True,
)
def capture_git_receipt(repo_root: Path, attempt_id: str, output: Path) -> None:
    """Capture the canonical after-boundary facts used by eval receipts."""
    receipt = capture_git_evidence(
        repo_root=repo_root, phase="after", attempt_id=attempt_id
    )
    write_json_atomic(path=output, payload=receipt.to_dict())
    click.echo(str(output))


@main.command()
@click.option(
    "--template",
    "template_name",
    type=click.Choice([DEFAULT_TEMPLATE_NAME, *PACKAGED_TEMPLATE_NAMES]),
    default=DEFAULT_TEMPLATE_NAME,
    show_default=True,
    help="Initial workflow template to scaffold.",
)
def init(template_name: str) -> None:
    """Initialize loopy-loop files."""
    repo_root = Path.cwd()
    if template_name == DEFAULT_TEMPLATE_NAME:
        created = _init_default_template(repo_root=repo_root)
    else:
        created = _init_packaged_template(
            repo_root=repo_root, template_name=template_name
        )
    _ensure_gitignore(repo_root=repo_root)

    if created:
        click.echo("Created:")
        for path in created:
            click.echo(f"- {path}")
    else:
        click.echo("loopy-loop is already initialized.")


def _init_default_template(*, repo_root: Path) -> list[str]:
    """Create the default workflow files that are absent from a repository."""

    loopy_dir = repo_root / LOOPY_DIRNAME
    workflow_dir = (
        loopy_dir
        / "workflow_sets"
        / MAIN_WORKFLOW_SET_NAME
        / "workflows"
        / GOAL_CHECK_WORKFLOW_ID
    )
    workflow_set_dir = workflow_dir.parent.parent
    loopy_dir.mkdir(parents=True, exist_ok=True)
    workflow_dir.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    created.extend(
        _write_if_missing(
            path=workflow_set_dir / "contract.yaml",
            content=DEFAULT_WORKFLOW_CONTRACT_TEMPLATE,
        )
    )
    created.extend(
        _write_if_missing(
            path=repo_root / ROOT_CONFIG_FILENAME, content=ROOT_CONFIG_TEMPLATE
        )
    )
    created.extend(
        _write_if_missing(path=repo_root / DEFAULT_GOAL_FILENAME, content=GOAL_TEMPLATE)
    )
    created.extend(
        _write_if_missing(
            path=workflow_dir / "config.yaml", content=GOAL_CHECK_CONFIG_TEMPLATE
        )
    )
    created.extend(
        _write_if_missing(
            path=workflow_dir / "prompt.txt", content=GOAL_CHECK_PROMPT_TEMPLATE
        )
    )
    return created


def _init_packaged_template(*, repo_root: Path, template_name: str) -> list[str]:
    template_root = files("loopy_loop").joinpath("templates", template_name)
    created: list[str] = []
    for relative_path in PACKAGED_TEMPLATE_FILES_BY_NAME[template_name]:
        created.extend(
            _copy_template_file_if_missing(
                source_root=template_root,
                relative_path=relative_path,
                repo_root=repo_root,
            )
        )
    for source_template, relative_path in PACKAGED_TEMPLATE_EXTRA_SOURCES.get(
        template_name, []
    ):
        created.extend(
            _copy_template_file_if_missing(
                source_root=files("loopy_loop").joinpath("templates", source_template),
                relative_path=relative_path,
                repo_root=repo_root,
            )
        )
    return created


def _copy_template_file_if_missing(
    *, source_root: Traversable, relative_path: str, repo_root: Path
) -> list[str]:
    source = source_root.joinpath(*relative_path.split("/"))
    return _write_if_missing(
        path=repo_root / relative_path, content=source.read_text(encoding="utf-8")
    )


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--resume", is_flag=True, default=False)
@click.option(
    "--workflow-set",
    default=None,
    help="Workflow set to run instead of the workflow_set in config.",
)
@click.option(
    "--goal-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Goal file to copy into the new session as goal.md.",
)
def coordinator(
    host: str, port: int, resume: bool, workflow_set: str | None, goal_file: Path | None
) -> None:
    """Run the coordinator server with exactly two endpoints: /register and /finished."""
    repo_root = Path.cwd()
    try:
        app = create_coordinator_app(
            repo_root=repo_root,
            resume=resume,
            workflow_set=workflow_set,
            goal_file=goal_file,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    uvicorn.run(app, host=host, port=port)


@main.command()
@click.option("--coordinator", "coordinator_url", required=True)
def worker(coordinator_url: str) -> None:
    """Run a loopy-loop worker.

    Calls /register once to get the first task, then loops calling /finished
    after each completed task until it receives a stop response.
    """
    repo_root = Path.cwd()
    run_worker_loop(repo_root=repo_root, coordinator_url=coordinator_url)


@main.command()
@click.option(
    "--watch",
    is_flag=True,
    default=False,
    help="Re-render every 2 seconds until interrupted.",
)
def status(watch: bool) -> None:
    """Show loop status (the whole session stack, with usage totals)."""
    repo_root = Path.cwd()
    if not watch:
        try:
            lines = _status_lines(repo_root=repo_root)
        except FileLockTimeout:
            raise click.ClickException(
                "coordinator state is locked (likely mid-request); retry shortly"
            ) from None
        click.echo("\n".join(lines))
        return
    try:
        while True:
            click.clear()
            click.echo(
                "\n".join(_status_lines(repo_root=repo_root, tolerate_lock=True))
            )
            time.sleep(2.0)
    except KeyboardInterrupt:
        pass


def _status_lines(*, repo_root: Path, tolerate_lock: bool = False) -> list[str]:
    try:
        state = StateStore(repo_root=repo_root).read_state()
    except FileLockTimeout:
        if tolerate_lock:
            return ["coordinator state is locked (likely mid-request); retry shortly"]
        raise
    if state is None:
        return ["No loopy-loop state found."]
    prices = _configured_model_prices(repo_root=repo_root)
    lines = _session_status_lines(
        repo_root=repo_root, state=state, indent="", prices=prices
    )
    # Walk the durable session stack so a running child is visible instead of
    # the suspended parent's "current_task: none".
    seen: set[str] = {state.active_session_id}
    while state.active_child_session_id:
        child_id = state.active_child_session_id
        if child_id in seen:
            break
        seen.add(child_id)
        try:
            child_state = StateStore(
                repo_root=repo_root,
                state_path=state_path(repo_root=repo_root, session_id=child_id),
            ).read_state()
        except FileLockTimeout:
            lines.append(f"active child {child_id}: state locked; retry shortly")
            break
        if child_state is None:
            lines.append(
                f"active_child_session_id points at {child_id}, but its state "
                "is missing (stale pointer)"
            )
            break
        lines.append("")
        lines.append(f"active child session {child_id}:")
        lines.extend(
            _session_status_lines(
                repo_root=repo_root, state=child_state, indent="  ", prices=prices
            )
        )
        state = child_state
    return lines


def _session_status_lines(
    *, repo_root: Path, state: LoopState, indent: str, prices: ModelPrices | None
) -> list[str]:
    lines = [
        f"{indent}status: {state.status}",
        f"{indent}session: {state.active_session_id}",
        f"{indent}iteration_count: {state.iteration_count}",
    ]
    if state.current_task is None:
        lines.append(f"{indent}current_task: none")
    else:
        lines.append(
            f"{indent}current_task: {state.current_task.workflow_id} "
            f"(iteration {state.current_task.iteration}, "
            f"session {state.current_task.session_id}, "
            f"started {state.current_task.started_at})"
        )
    lines.append(f"{indent}stop_reason: {state.stop_reason or 'none'}")
    # Subtree totals: this session's own iterations plus finalized children.
    totals = session_tree_usage_totals(repo_root=repo_root, state=state)
    lines.append(
        f"{indent}subtree_usage: prompt_tokens={totals.prompt_tokens} "
        f"completion_tokens={totals.completion_tokens} "
        f"(iterations fully measured: {totals.iterations_with_usage}, "
        f"unknown: {totals.iterations_without_usage})"
    )
    lines.append(f"{indent}subtree_harness_duration_s: {totals.duration_s:.0f}")
    cost = estimate_cost_usd(
        prompt_tokens=totals.prompt_tokens,
        completion_tokens=totals.completion_tokens,
        prices=prices,
    )
    if cost is not None:
        lines.append(f"{indent}subtree_estimated_cost_usd: {cost:.4f}")
    return lines


def _configured_model_prices(*, repo_root: Path) -> ModelPrices | None:
    try:
        return load_root_config(repo_root=repo_root).model_prices
    except ConfigError:
        return None


@main.command()
@click.option("--follow", is_flag=True, default=False, help="Keep tailing new events.")
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Raw JSON, one per line."
)
def events(follow: bool, as_json: bool) -> None:
    """Print the deepest active session's event stream (events.jsonl)."""
    repo_root = Path.cwd()
    session_id = _deepest_active_session_id(repo_root=repo_root)
    if session_id is None:
        raise click.ClickException("No loopy-loop state found.")
    printed = 0
    try:
        while True:
            entries = read_events(
                path=events_path(repo_root=repo_root, session_id=session_id)
            )
            for event in entries[printed:]:
                click.echo(
                    json.dumps(event, separators=(",", ":"))
                    if as_json
                    else _format_event(event)
                )
            printed = len(entries)
            if not follow:
                break
            time.sleep(1.0)
            # The active session moves (child dispatched, child finished,
            # fresh session after archive): re-resolve and switch streams.
            try:
                current = _deepest_active_session_id(repo_root=repo_root)
            except click.ClickException:
                current = None  # transient lock; keep following the old file
            if current is not None and current != session_id:
                if not as_json:
                    click.echo(f"--- now following session {current} ---")
                session_id = current
                printed = 0
    except KeyboardInterrupt:
        pass


def _format_event(event: dict) -> str:
    payload = event.get("payload")
    detail = ""
    if isinstance(payload, dict) and payload:
        detail = " " + " ".join(
            f"{key}={value}" for key, value in payload.items() if value is not None
        )
    return f"{event.get('ts', '?')} {event.get('type', '?')}{detail}"


def _deepest_active_session_id(*, repo_root: Path) -> str | None:
    try:
        state = StateStore(repo_root=repo_root).read_state()
    except FileLockTimeout:
        raise click.ClickException(
            "coordinator state is locked (likely mid-request); retry shortly"
        ) from None
    if state is None:
        return None
    seen: set[str] = {state.active_session_id}
    while state.active_child_session_id:
        child_id = state.active_child_session_id
        if child_id in seen:
            break
        seen.add(child_id)
        try:
            child_state = StateStore(
                repo_root=repo_root,
                state_path=state_path(repo_root=repo_root, session_id=child_id),
            ).read_state()
        except FileLockTimeout:
            raise click.ClickException(
                "coordinator state is locked (likely mid-request); retry shortly"
            ) from None
        if child_state is None:
            break
        state = child_state
    return state.active_session_id


@main.command()
def stop() -> None:
    """Request a tree-wide stop at the next safe assignment boundary."""
    repo_root = Path.cwd()
    store = StateStore(repo_root=repo_root)

    def mutator(state: LoopState | None) -> tuple[LoopState, None]:
        if state is None:
            raise click.ClickException("No loopy-loop state found.")
        state.stop_requested = True
        return state, None

    try:
        store.mutate(mutator)
        state = store.read_state()
        seen: set[str] = set()
        while state is not None and state.active_child_session_id is not None:
            child_id = state.active_child_session_id
            if child_id in seen:
                break
            seen.add(child_id)
            child_store = StateStore(
                repo_root=repo_root,
                state_path=state_path(repo_root=repo_root, session_id=child_id),
            )
            child_store.mutate(mutator=mutator)
            state = child_store.read_state()
    except FileLockTimeout:
        raise click.ClickException(
            "coordinator state is locked (likely mid-request); retry shortly"
        ) from None
    click.echo("stop requested")


@main.command()
@click.argument("text", nargs=-1, required=True)
@click.option(
    "--session",
    "target_session_id",
    default=None,
    help="Address this update to one session instead of the active tree.",
)
def update(text: tuple[str, ...], target_session_id: str | None) -> None:
    """Append a user update without rewriting earlier input records."""
    repo_root = Path.cwd().resolve()
    message = " ".join(text).strip()
    if not message:
        raise click.ClickException("update text must not be empty")

    if target_session_id is None:
        delivery_session_id = _deepest_active_session_id(repo_root=repo_root)
        if delivery_session_id is None:
            raise click.ClickException("No loopy-loop state found.")
        target_scope = "tree"
    else:
        delivery_session_id = _validate_update_session(
            repo_root=repo_root, session_id=target_session_id
        )
        target_scope = "session"

    created_at = utc_now().isoformat().replace("+00:00", "Z")
    input_id = f"input-{uuid.uuid4().hex}"
    record = {
        "schema_version": 1,
        "record_type": "user_input",
        "input_id": input_id,
        "target_scope": target_scope,
        "target_session_id": target_session_id,
        "delivered_to_session_id": delivery_session_id,
        "delivery_state": "routed",
        "created_at": created_at,
        "text": message,
        "acknowledgement_state": "pending",
        "acknowledged_at": None,
        "acknowledged_by_attempt_id": None,
    }
    journal = user_updates_journal_path(
        repo_root=repo_root, session_id=delivery_session_id
    )
    journal.parent.mkdir(parents=True, exist_ok=True)
    try:
        append_jsonl_record(path=journal, payload=record)
    except OSError as exc:
        raise click.ClickException(f"cannot append user update: {exc}") from exc
    click.echo(f"queued {input_id} for session {delivery_session_id}")


def _validate_update_session(*, repo_root: Path, session_id: str) -> str:
    """Return a session ID after verifying its manifest identity."""

    session_root = session_dir_path(repo_root=repo_root, session_id=session_id)
    manifest_path = session_root / SESSION_METADATA_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"session not found: {session_id}") from exc
    if not isinstance(manifest, dict) or manifest.get("session_id") != session_id:
        raise click.ClickException(f"session not found: {session_id}")
    return session_id


@main.group()
def traces() -> None:
    """Inspect local attempt traces."""


@traces.command("list")
def list_traces() -> None:
    """List attempt trace manifests in this repository."""
    repo_root = Path.cwd().resolve()
    manifests = list_trace_manifests(repo_root=repo_root)
    if not manifests:
        click.echo("No traces found.")
        return
    try:
        for manifest_path in manifests:
            manifest = read_trace_manifest(manifest_path=manifest_path)
            integrity = verify_trace_integrity(trace_root=manifest_path.parent)
            identity = manifest.get("identity")
            identity = identity if isinstance(identity, dict) else {}
            click.echo(
                "\t".join(
                    [
                        str(manifest.get("manifest_id", "?")),
                        str(manifest.get("lifecycle", "?")),
                        f"integrity={integrity['status']}",
                        f"session={identity.get('session_id', '?')}",
                        f"workflow={identity.get('workflow_id', '?')}",
                        str(manifest_path.resolve()),
                    ]
                )
            )
    except TraceError as exc:
        raise click.ClickException(str(exc)) from exc


@traces.command("inspect")
@click.argument("manifest_or_id")
def inspect_trace(manifest_or_id: str) -> None:
    """Print one trace manifest and its observed integrity as JSON."""
    repo_root = Path.cwd().resolve()
    try:
        manifest_path = resolve_trace_manifest(
            repo_root=repo_root, reference=manifest_or_id
        )
        manifest = read_trace_manifest(manifest_path=manifest_path)
        manifest["observed_integrity"] = verify_trace_integrity(
            trace_root=manifest_path.parent
        )
    except TraceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(manifest, indent=2, sort_keys=True))


def _write_if_missing(*, path: Path, content: str) -> list[str]:
    if path.exists():
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return [str(path)]


def _ensure_gitignore(*, repo_root: Path) -> None:
    path = repo_root / ".gitignore"
    existing_lines: list[str]
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    else:
        existing_lines = []
    updated_lines = list(existing_lines)
    for line in GITIGNORE_LINES:
        if line not in existing_lines:
            updated_lines.append(line)
    content = "\n".join(updated_lines).rstrip() + "\n"
    path.write_text(content, encoding="utf-8")
