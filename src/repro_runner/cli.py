"""Command surface for the staged runner implementation."""

import asyncio
import signal
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from repro_runner.config import Workflow, load_workflow
from repro_runner.errors import ValidationError
from repro_runner.executor import RunResult
from repro_runner.ownership import OwnershipConflict
from repro_runner.scheduler import run_workflow

app = typer.Typer(
    help="Local workflow runner. Execution is available; recovery is not."
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


async def _run_with_signals(
    validated: Workflow, workers: int, use_cache: bool
) -> tuple[RunResult, int | None]:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signal_number: int | None = None
    prior = {
        number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
    }

    def request_stop(number: int) -> None:
        nonlocal signal_number
        if signal_number is None:
            signal_number = number
        else:
            owner_stop_again.set()
        stop.set()

    owner_stop_again = asyncio.Event()
    for number in prior:
        loop.add_signal_handler(number, request_stop, number)
    try:
        result = await run_workflow(
            validated, workers, stop, owner_stop_again, use_cache
        )
        return result, signal_number
    finally:
        for number, handler in prior.items():
            loop.remove_signal_handler(number)
            signal.signal(number, handler)


@app.command(help="Execute a workflow with bounded concurrent tasks.")
def run(
    workflow: Annotated[Path, typer.Argument(help="Workflow YAML file")],
    workers: Annotated[int, typer.Option(min=1, help="Maximum concurrent tasks")] = 4,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Bypass cache reads and writes")
    ] = False,
) -> None:
    try:
        validated = load_workflow(workflow)
    except ValidationError as error:
        typer.echo(f"Invalid workflow: {error}", err=True)
        raise typer.Exit(code=2) from error
    except OSError as error:
        typer.echo(f"Cannot inspect workflow: {error}", err=True)
        raise typer.Exit(code=3) from error
    try:
        result, signal_number = asyncio.run(
            _run_with_signals(validated, workers, not no_cache)
        )
    except OwnershipConflict as error:
        typer.echo(f"Workspace ownership conflict: {error}", err=True)
        raise typer.Exit(code=3) from error
    except Exception as error:
        typer.echo(
            f"Execution infrastructure failure ({type(error).__name__}): {error}",
            err=True,
        )
        raise typer.Exit(code=3) from error
    counts = {
        state: sum(item.state == state for item in result.tasks.values())
        for state in ("succeeded", "cached", "failed", "blocked", "interrupted")
    }
    for task_id in validated.graph.order:
        task = result.tasks[task_id]
        details = f": {task.reason}" if task.reason else ""
        typer.echo(f"{task_id}: {task.state}{details}")
        if task.state in {"failed", "interrupted"} and task.stdout:
            typer.echo(f"  logs: {task.stdout}, {task.stderr}")
    summary = (
        f"Outcome: {result.state}; "
        f"executed={sum(item.launched for item in result.tasks.values())}"
    )
    if not no_cache:
        summary += (
            f", cached={counts['cached']}, "
            f"cache_misses={sum(item.cache_miss for item in result.tasks.values())}"
        )
    summary += (
        f", failed={counts['failed']}, blocked={counts['blocked']}, "
        f"interrupted={counts['interrupted']}"
    )
    typer.echo(summary)
    typer.echo(f"Duration: {result.duration_seconds:.3f}s")
    if signal_number is not None:
        raise typer.Exit(code=128 + signal_number)
    if result.state == "failed":
        raise typer.Exit(code=1)


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
