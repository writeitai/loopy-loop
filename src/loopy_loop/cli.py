from __future__ import annotations

import click


@click.group()
def main() -> None:
    """loopy-loop CLI."""


@main.command()
def init() -> None:
    """Initialize loopy-loop files."""
    click.echo("Not implemented yet.")


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--resume", is_flag=True, default=False)
def coordinator(host: str, port: int, resume: bool) -> None:
    """Run the coordinator server."""
    click.echo(f"Not implemented yet: host={host} port={port} resume={resume}")


@main.command()
@click.option("--coordinator", "coordinator_url", required=True)
def worker(coordinator_url: str) -> None:
    """Run a loopy-loop worker."""
    click.echo(f"Not implemented yet: coordinator={coordinator_url}")


@main.command()
def status() -> None:
    """Show loop status."""
    click.echo("Not implemented yet.")


@main.command()
def stop() -> None:
    """Request loop stop."""
    click.echo("Not implemented yet.")
