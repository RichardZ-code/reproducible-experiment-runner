"""Command surface for the staged runner implementation."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from repro_runner.config import load_workflow
from repro_runner.errors import ValidationError

app = typer.Typer(
    help="Local workflow runner. Validation is available; execution and recovery are not."
)


def _unavailable(message: str) -> NoReturn:
    typer.echo(f"{message} is not implemented yet.", err=True)
    raise typer.Exit(code=3)


@app.command(help="Validate workflow structure; input availability is checked later.")
def validate(
    workflow: Annotated[Path, typer.Argument(help="Workflow YAML file")],
) -> None:
    try:
        validated = load_workflow(workflow)
    except ValidationError as error:
        typer.echo(f"Invalid workflow: {error}", err=True)
        raise typer.Exit(code=2) from error
    except OSError as error:
        typer.echo(f"Cannot inspect workflow: {error}", err=True)
        raise typer.Exit(code=3) from error
    count = len(validated.tasks)
    noun = "task" if count == 1 else "tasks"
    typer.echo(
        f"Workflow valid: {count} {noun}. "
        "Input availability and contents are deferred until task readiness."
    )


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
