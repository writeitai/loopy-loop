from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
import json
from pathlib import Path
import time

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
from loopy_loop.models import LoopState
from loopy_loop.sessions import state_path
from loopy_loop.state_store import StateStore
from loopy_loop.worker import run_worker_loop

GOAL_CHECK_WORKFLOW_ID = "goal_check"
DEFAULT_TEMPLATE_NAME = "default"
MAIN_WORKFLOW_SET_NAME = "main"
INNER_OUTER_EVAL_TEMPLATE_NAME = "inner_outer_eval"
PM_PLANNER_DISPATCHER_TEMPLATE_NAME = "pm_planner_dispatcher"
DESIGN_LOOP_TEMPLATE_NAME = "design_loop"
# Names never copied out of a template directory even if present on disk.
_TEMPLATE_SCAN_SKIP = frozenset({"__pycache__", ".DS_Store", ".pytest_cache"})


def _scan_template_relative_paths(*, template_name: str) -> list[str]:
    """Every file under a packaged template, as sorted repo-relative POSIX paths.

    The design_loop template ships ~90 files (six workflow sets with their eval
    checks, plus the plan/ skeleton and root config/seed files). Enumerating them
    by hand like the smaller templates would drift the moment a check is added, so
    its file list is derived from the template directory itself at import time.
    """
    root = files("loopy_loop").joinpath("templates", template_name)
    found: list[str] = []

    def _walk(node: Traversable, prefix: str) -> None:
        for entry in sorted(node.iterdir(), key=lambda item: item.name):
            if entry.name in _TEMPLATE_SCAN_SKIP:
                continue
            relative_path = f"{prefix}{entry.name}"
            if entry.is_dir():
                _walk(entry, f"{relative_path}/")
            else:
                found.append(relative_path)

    _walk(root, "")
    return found
PACKAGED_TEMPLATE_FILES_BY_NAME = {
    INNER_OUTER_EVAL_TEMPLATE_NAME: [
        ".gitignore",
        ROOT_CONFIG_FILENAME,
        DEFAULT_GOAL_FILENAME,
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
# The design_loop template is a full design-phase repo scaffold: its six workflow
# sets ship their own fixed eval checks, and it also lays down the plan/ artifact
# tree, decisions.md/questions.md, CLAUDE.md, and the eval-banana config. Its ~90-file
# list is scanned on demand (see _resolve_packaged_template_files) rather than
# hand-written. It is named here WITHOUT scanning, so importing this module never
# touches template resources — a missing/corrupt resource surfaces only when the
# template is actually used, not on every `loopy status`/`events`/`stop`.
PACKAGED_TEMPLATE_NAMES = [*PACKAGED_TEMPLATE_FILES_BY_NAME, DESIGN_LOOP_TEMPLATE_NAME]
# Gitignore entries the design_loop scaffold needs beyond the shared GITIGNORE_LINES
# (its shipped .gitignore carries all of them, but a pre-existing target .gitignore is
# left untouched by init, so these are appended idempotently).
DESIGN_LOOP_EXTRA_GITIGNORE = [".eval-banana/results/", "_additional_context/"]
GITIGNORE_LINES = [".loopy_loop/sessions/"]
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
"""
GOAL_CHECK_PROMPT_TEMPLATE = """Evaluate whether the repo now satisfies the loopy-loop goal.

Write exactly one JSON file to the provided goal_check.json output path using:
{
  "goal_met": false,
  "reason": "brief explanation",
  "schema_version": 1
}

If and only if goal_met is true, update the Session control path to stop the
loop using:
{
  "state": "stopped",
  "reason": "goal_check verified the loop goal is satisfied",
  "stop_reason": "goal_met",
  "schema_version": 1
}
"""


@click.group()
def main() -> None:
    """loopy-loop CLI."""


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
    extra_gitignore = (
        DESIGN_LOOP_EXTRA_GITIGNORE
        if template_name == DESIGN_LOOP_TEMPLATE_NAME
        else None
    )
    _ensure_gitignore(repo_root=repo_root, extra_lines=extra_gitignore)

    if created:
        click.echo("Created:")
        for path in created:
            click.echo(f"- {path}")
    else:
        click.echo("loopy-loop is already initialized.")

    if template_name == DESIGN_LOOP_TEMPLATE_NAME:
        for warning in _design_loop_integration_warnings(
            repo_root=repo_root, created=created
        ):
            click.echo(f"WARNING: {warning}")


def _init_default_template(*, repo_root: Path) -> list[str]:
    loopy_dir = repo_root / LOOPY_DIRNAME
    workflow_dir = (
        loopy_dir
        / "workflow_sets"
        / MAIN_WORKFLOW_SET_NAME
        / "workflows"
        / GOAL_CHECK_WORKFLOW_ID
    )
    loopy_dir.mkdir(parents=True, exist_ok=True)
    workflow_dir.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
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


def _resolve_packaged_template_files(*, template_name: str) -> list[str]:
    """Repo-relative paths a packaged template ships.

    Static templates use their hand-written list; templates absent from that map
    (design_loop) are scanned on demand, so the scan runs only when the template is
    used and a missing/corrupt resource raises an actionable error instead of
    breaking every CLI command at import.
    """
    static = PACKAGED_TEMPLATE_FILES_BY_NAME.get(template_name)
    if static is not None:
        return static
    try:
        scanned = _scan_template_relative_paths(template_name=template_name)
    except OSError as exc:
        raise click.ClickException(
            f"template '{template_name}' resources are missing or unreadable: {exc}"
        ) from exc
    if not scanned:
        raise click.ClickException(
            f"template '{template_name}' shipped no files — the installation is corrupt"
        )
    return scanned


def _init_packaged_template(*, repo_root: Path, template_name: str) -> list[str]:
    template_root = files("loopy_loop").joinpath("templates", template_name)
    created: list[str] = []
    for relative_path in _resolve_packaged_template_files(template_name=template_name):
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
    """Request loop stop."""
    repo_root = Path.cwd()
    store = StateStore(repo_root=repo_root)

    def mutator(state: LoopState | None) -> tuple[LoopState, None]:
        if state is None:
            raise click.ClickException("No loopy-loop state found.")
        state.stop_requested = True
        return state, None

    try:
        store.mutate(mutator)
    except FileLockTimeout:
        raise click.ClickException(
            "coordinator state is locked (likely mid-request); retry shortly"
        ) from None
    click.echo("stop requested")


def _write_if_missing(*, path: Path, content: str) -> list[str]:
    if path.exists():
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return [str(path)]


def _ensure_gitignore(*, repo_root: Path, extra_lines: list[str] | None = None) -> None:
    path = repo_root / ".gitignore"
    existing_lines: list[str]
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    else:
        existing_lines = []
    updated_lines = list(existing_lines)
    for line in [*GITIGNORE_LINES, *(extra_lines or [])]:
        if line not in updated_lines:
            updated_lines.append(line)
    content = "\n".join(updated_lines).rstrip() + "\n"
    path.write_text(content, encoding="utf-8")


def _design_loop_integration_warnings(
    *, repo_root: Path, created: list[str]
) -> list[str]:
    """Warn when init left a required design_loop integration file untouched.

    `_write_if_missing` never overwrites, so initializing into a repo that already
    has `.eval-banana/config.toml` or `CLAUDE.md` silently keeps the old one — which
    can leave the qualitative gates unrunnable or the design rules unenforced. The
    scaffold still succeeds; these warnings tell the user exactly what to reconcile.
    """
    created_paths = set(created)
    warnings: list[str] = []

    eval_config = repo_root / ".eval-banana" / "config.toml"
    if eval_config.exists() and str(eval_config) not in created_paths:
        text = eval_config.read_text(encoding="utf-8")
        if "[harness]" not in text or "agent" not in text:
            warnings.append(
                "existing .eval-banana/config.toml has no [harness] agent — every "
                "design-loop gate will refuse to run until you configure one "
                "(see the template's .eval-banana/config.toml for the required keys, "
                "including the .loopy_loop discovery exclusion)."
            )

    claude_md = repo_root / "CLAUDE.md"
    if claude_md.exists() and str(claude_md) not in created_paths:
        warnings.append(
            "existing CLAUDE.md was left unchanged — merge the design-phase rules "
            "from the template's CLAUDE.md so the bind workflow can enforce them."
        )

    return warnings
