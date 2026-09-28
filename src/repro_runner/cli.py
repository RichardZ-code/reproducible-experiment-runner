"""Command surface for the staged runner implementation."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

app = typer.Typer(
    help="Local workflow runner. Commands are registered but unavailable in P02."
)


def _unavailable(message: str) -> NoReturn:
    typer.echo(f"{message} is not implemented yet.", err=True)
    raise typer.Exit(code=3)


@app.command(help="Validate a workflow (available in P03).")
def validate(
    workflow: Annotated[Path, typer.Argument(help="Workflow YAML file")],
) -> None:
    _unavailable("Workflow validation")


@app.command(help="Run a workflow (execution begins in P04).")
def run(
    workflow: Annotated[Path, typer.Argument(help="Workflow YAML file")],
    workers: Annotated[int, typer.Option(min=1, help="Maximum concurrent tasks")] = 4,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Bypass cache reads and writes")
    ] = False,
) -> None:
    _unavailable("Workflow execution")


@app.command(help="Show a recorded run (available in P06).")
def status(run_id: Annotated[str, typer.Argument(help="Run ID")]) -> None:
    _unavailable("Run status")


@app.command(help="Resume a recorded run (available in P06).")
def resume(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    workers: Annotated[
        int | None, typer.Option(min=1, help="Override the recorded worker count")
    ] = None,
) -> None:
    _unavailable("Run recovery")
