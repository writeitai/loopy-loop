from __future__ import annotations

from pathlib import Path

import click
import uvicorn

from loopy_loop.config import ConfigError
from loopy_loop.config import LOOPY_DIRNAME
from loopy_loop.config import ROOT_CONFIG_FILENAME
from loopy_loop.coordinator_app import create_coordinator_app
from loopy_loop.state_store import StateStore
from loopy_loop.worker import run_worker_loop

GOAL_CHECK_WORKFLOW_ID = "goal_check"
GITIGNORE_LINES = [
    ".loopy_loop/sessions/",
    ".loopy_loop/state.json",
    ".loopy_loop/state.json.lock",
    ".loopy_loop/state.json.archive_*.json",
]
ROOT_CONFIG_TEMPLATE = """goal: "Ship a minimal working landing page"
goal_slug: "ship-landing-page"
completion_criteria:
  - "Homepage renders without errors"
  - "Primary CTA is wired"
  - "README explains how to run locally"
stop_criteria:
  - "A workflow writes an unresolvable error flag"
  - "The repo requires a missing secret or external dependency the agent cannot obtain"
max_turns: 20
goal_check_consecutive_failures_cap: 3
model: "gpt-5.4"
agents:
  - "codex"
api_base: "https://openrouter.ai/api/v1"
api_key_env: "OPENROUTER_API_KEY"
system_prompt_extension: ""
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
"""


@click.group()
def main() -> None:
    """loopy-loop CLI."""


@main.command()
def init() -> None:
    """Initialize loopy-loop files."""
    repo_root = Path.cwd()
    loopy_dir = repo_root / LOOPY_DIRNAME
    workflow_dir = loopy_dir / "workflows" / GOAL_CHECK_WORKFLOW_ID
    loopy_dir.mkdir(parents=True, exist_ok=True)
    workflow_dir.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    created.extend(
        _write_if_missing(
            path=repo_root / ROOT_CONFIG_FILENAME,
            content=ROOT_CONFIG_TEMPLATE,
        )
    )
    created.extend(
        _write_if_missing(
            path=workflow_dir / "config.yaml",
            content=GOAL_CHECK_CONFIG_TEMPLATE,
        )
    )
    created.extend(
        _write_if_missing(
            path=workflow_dir / "prompt.txt",
            content=GOAL_CHECK_PROMPT_TEMPLATE,
        )
    )
    _ensure_gitignore(repo_root=repo_root)

    if created:
        click.echo("Created:")
        for path in created:
            click.echo(f"- {path}")
    else:
        click.echo("loopy-loop is already initialized.")


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--resume", is_flag=True, default=False)
def coordinator(host: str, port: int, resume: bool) -> None:
    """Run the coordinator server."""
    repo_root = Path.cwd()
    try:
        app = create_coordinator_app(repo_root=repo_root, resume=resume)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    uvicorn.run(app, host=host, port=port)


@main.command()
@click.option("--coordinator", "coordinator_url", required=True)
def worker(coordinator_url: str) -> None:
    """Run a loopy-loop worker."""
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
    if state.active_assignment is None:
        click.echo("active_assignment: none")
    else:
        click.echo(
            "active_assignment: "
            f"{state.active_assignment.workflow_id} "
            f"(iteration {state.active_assignment.iteration}, "
            f"worker {state.active_assignment.worker_id})"
        )
    click.echo(f"stop_reason: {state.stop_reason or 'none'}")


@main.command()
def stop() -> None:
    """Request loop stop."""
    repo_root = Path.cwd()
    store = StateStore(repo_root=repo_root)

    def mutator(state):
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
