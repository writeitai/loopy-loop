from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import click
import uvicorn

from loopy_loop.config import ConfigError
from loopy_loop.config import DEFAULT_GOAL_FILENAME
from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.config import ROOT_CONFIG_FILENAME
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.models import LoopState
from loopy_loop.state_store import StateStore
from loopy_loop.worker import run_worker_loop

GOAL_CHECK_WORKFLOW_ID = "goal_check"
DEFAULT_TEMPLATE_NAME = "default"
INNER_OUTER_EVAL_TEMPLATE_NAME = "inner_outer_eval"
PACKAGED_TEMPLATE_FILES_BY_NAME = {
    INNER_OUTER_EVAL_TEMPLATE_NAME: [
        ".gitignore",
        ROOT_CONFIG_FILENAME,
        DEFAULT_GOAL_FILENAME,
        ".loopy_loop/workflows/eval_reviewer/config.yaml",
        ".loopy_loop/workflows/eval_reviewer/prompt.txt",
        ".loopy_loop/workflows/eval_runner/config.yaml",
        ".loopy_loop/workflows/eval_runner/prompt.txt",
        ".loopy_loop/workflows/inner/config.yaml",
        ".loopy_loop/workflows/inner/prompt.txt",
        ".loopy_loop/workflows/outer/config.yaml",
        ".loopy_loop/workflows/outer/prompt.txt",
    ]
}
PACKAGED_TEMPLATE_NAMES = list(PACKAGED_TEMPLATE_FILES_BY_NAME)
GITIGNORE_LINES = [
    ".loopy_loop/sessions/",
    ".loopy_loop/state.json",
    ".loopy_loop/state.json.lock",
    ".loopy_loop/state.json.archive_*.json",
]
ROOT_CONFIG_TEMPLATE = f"""goal_file: "{DEFAULT_GOAL_FILENAME}"
max_turns: 20
goal_check_consecutive_failures_cap: 3
team_harness_provider: "openai_compat"
team_harness_model: "gpt-5.5"
team_harness_agents:
  - "codex"
team_harness_agent_models: {{}}
team_harness_agent_reasoning_efforts: {{}}
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
    _ensure_gitignore(repo_root=repo_root)

    if created:
        click.echo("Created:")
        for path in created:
            click.echo(f"- {path}")
    else:
        click.echo("loopy-loop is already initialized.")


def _init_default_template(*, repo_root: Path) -> list[str]:
    loopy_dir = repo_root / LOOPY_DIRNAME
    workflow_dir = loopy_dir / "workflows" / GOAL_CHECK_WORKFLOW_ID
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
def coordinator(host: str, port: int, resume: bool) -> None:
    """Run the coordinator server with exactly two endpoints: /register and /finished."""
    repo_root = Path.cwd()
    try:
        app = create_coordinator_app(repo_root=repo_root, resume=resume)
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
def status() -> None:
    """Show loop status."""
    repo_root = Path.cwd()
    state = StateStore(repo_root=repo_root).read_state()
    if state is None:
        click.echo("No loopy-loop state found.")
        return
    click.echo(f"status: {state.status}")
    click.echo(f"session: {state.active_session_id}")
    click.echo(f"iteration_count: {state.iteration_count}")
    if state.current_task is None:
        click.echo("current_task: none")
    else:
        click.echo(
            f"current_task: {state.current_task.workflow_id} "
            f"(iteration {state.current_task.iteration}, "
            f"session {state.current_task.session_id}, "
            f"started {state.current_task.started_at})"
        )
    click.echo(f"stop_reason: {state.stop_reason or 'none'}")


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

    store.mutate(mutator)
    click.echo("stop requested")


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
