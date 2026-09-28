"""Command surface for the staged runner implementation."""

import asyncio
import json
import signal
import sqlite3
from pathlib import Path
from typing import Annotated, Awaitable, Callable

import typer

from repro_runner.config import Workflow, load_workflow
from repro_runner.errors import ValidationError
from repro_runner.executor import RunResult
from repro_runner.ownership import OwnershipConflict
from repro_runner.scheduler import resume_workflow, run_workflow
from repro_runner.state import InvalidState, RecoveryRequired, StateNotFound, StateStore

app = typer.Typer(help="Local workflow runner with verified caching and recovery.")


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


async def _with_signals(
    action: Callable[[asyncio.Event, asyncio.Event], Awaitable[RunResult]],
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
        result = await action(stop, owner_stop_again)
        return result, signal_number
    finally:
        for number, handler in prior.items():
            loop.remove_signal_handler(number)
            signal.signal(number, handler)


async def _run_with_signals(
    validated: Workflow, workers: int, use_cache: bool
) -> tuple[RunResult, int | None]:
    return await _with_signals(
        lambda stop, repeated: run_workflow(
            validated, workers, stop, repeated, use_cache
        )
    )


def _show_result(result: RunResult, signal_number: int | None, use_cache: bool) -> None:
    counts = {
        state: sum(item.state == state for item in result.tasks.values())
        for state in ("succeeded", "cached", "failed", "blocked", "interrupted")
    }
    for task_id in result.task_order:
        task = result.tasks[task_id]
        label = f"retained ({task.state})" if task.retained else task.state
        details = f": {task.reason}" if task.reason else ""
        typer.echo(f"{task_id}: {label}{details}")
        if task.state in {"failed", "interrupted"} and task.stdout:
            typer.echo(f"  logs: {task.stdout}, {task.stderr}")
    summary = (
        f"Outcome: {result.state}; "
        f"executed={sum(item.launched for item in result.tasks.values())}, "
        f"retained={sum(item.retained for item in result.tasks.values())}"
    )
    if use_cache:
        summary += (
            f", cached={sum(item.state == 'cached' and not item.retained for item in result.tasks.values())}, "
            f"cache_misses={sum(item.cache_miss for item in result.tasks.values())}"
        )
    summary += (
        f", failed={counts['failed']}, blocked={counts['blocked']}, "
        f"interrupted={counts['interrupted']}, uncertain_launches={result.uncertain_launches}"
    )
    typer.echo(summary)
    typer.echo(f"Duration: {result.duration_seconds:.3f}s")
    if result.manifest_path is not None:
        typer.echo(f"Manifest: {result.manifest_path}")
    if result.manifest_error is not None:
        typer.echo(f"Manifest publication failed: {result.manifest_error}", err=True)
    if signal_number is not None:
        raise typer.Exit(code=128 + signal_number)
    if result.manifest_error is not None:
        raise typer.Exit(code=3)
    if result.state == "failed":
        raise typer.Exit(code=1)


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
    _show_result(result, signal_number, not no_cache)


@app.command(help="Read a recorded run in the current workspace.")
def status(run_id: Annotated[str, typer.Argument(help="Run ID")]) -> None:
    store: StateStore | None = None
    try:
        store = StateStore.open_existing(Path.cwd() / ".repro", readonly=True)
        run_record, invocations, tasks, artifacts, uncertain = store.status(run_id)
    except (StateNotFound, InvalidState) as error:
        typer.echo(f"Cannot read run: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (RecoveryRequired, sqlite3.Error, OSError) as error:
        typer.echo(f"State read requires recovery or is unavailable: {error}", err=True)
        raise typer.Exit(code=3) from error
    finally:
        if store is not None:
            store.close()
    typer.echo(f"Run ID: {run_id}")
    typer.echo(f"Recorded outcome: {run_record['outcome']}")
    if run_record["outcome"] == "running":
        typer.echo("Recorded running state does not establish a live owner.")
    typer.echo(f"Workflow: {run_record['workflow_file']}")
    typer.echo(f"Cache: {'enabled' if run_record['cache_enabled'] else 'disabled'}")
    for invocation in invocations:
        typer.echo(
            f"Invocation {invocation['invocation_no']}: {invocation['outcome']}, "
            f"workers={invocation['workers']}"
        )
    typer.echo(f"Historical uncertain launches: {uncertain}")
    for task in tasks:
        selected = task["selected_attempt_no"]
        latest = task["latest_attempt_no"]
        typer.echo(
            f"{task['task_id']}: {task['state']}; disposition={task['disposition'] or 'unresolved'}; "
            f"selected_attempt={selected if selected is not None else 'none'}; "
            f"latest_attempt={latest if latest is not None else 'none'}"
        )
        if task["reason"]:
            typer.echo(f"  reason: {task['reason']}")
        if task["displayed_attempt_no"] is not None:
            typer.echo(
                f"  launch={task['launch_state']}; exit={task['exit_code'] if task['exit_code'] is not None else 'unknown'}"
            )
            if task["key"]:
                typer.echo(f"  key: {task['key']}")
            if task["inputs_json"]:
                for item in json.loads(task["inputs_json"]):
                    typer.echo(f"  input: {item['path']} sha256={item['sha256']}")
            if task["stdout_path"]:
                typer.echo(f"  logs: {task['stdout_path']}, {task['stderr_path']}")
    for artifact in artifacts:
        typer.echo(
            f"  artifact {artifact['task_id']}: {artifact['path']} "
            f"sha256={artifact['sha256']} size={artifact['size']}"
        )


@app.command(help="Revalidate and resume a recorded run in the current workspace.")
def resume(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    workers: Annotated[
        int | None, typer.Option(min=1, help="Override the recorded worker count")
    ] = None,
) -> None:
    try:
        result, signal_number = asyncio.run(
            _with_signals(
                lambda stop, repeated: resume_workflow(run_id, workers, stop, repeated)
            )
        )
    except (StateNotFound, InvalidState, ValidationError) as error:
        typer.echo(f"Cannot resume run: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (OwnershipConflict, RecoveryRequired, sqlite3.Error, OSError) as error:
        typer.echo(f"Resume infrastructure failure: {error}", err=True)
        raise typer.Exit(code=3) from error
    except Exception as error:
        typer.echo(
            f"Resume infrastructure failure ({type(error).__name__}): {error}",
            err=True,
        )
        raise typer.Exit(code=3) from error
    _show_result(result, signal_number, result.use_cache)
