"""Dependency-aware, bounded scheduling for one workflow invocation."""

import asyncio
import heapq
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

from repro_runner.cache import CacheStorage
from repro_runner.config import Workflow, load_workflow
from repro_runner.errors import ValidationError
from repro_runner.executor import (
    Artifact,
    ProcessOwner,
    RunResult,
    TaskFailure,
    TaskResult,
    _now,
    execute_task,
    retain_task,
)
from repro_runner.hashing import environment_record
from repro_runner.ownership import workspace_lock
from repro_runner.paths import ensure_real_directory
from repro_runner.state import (
    StateCommitUncertain,
    StateStore,
    require_existing_runtime,
)


async def _schedule(
    workflow: Workflow,
    run_dir: Path,
    workers: int,
    owner: ProcessOwner,
    cache: CacheStorage | None,
    environment: dict[str, object],
    state: StateStore,
    resume_mode: bool,
) -> dict[str, TaskResult]:
    remaining = {task_id: len(task.deps) for task_id, task in workflow.tasks.items()}
    ready = [task_id for task_id, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    results: dict[str, TaskResult] = {}
    accepted: dict[str, Artifact] = {}
    active: dict[asyncio.Task[TaskResult], str] = {}
    stop_wait = asyncio.create_task(owner.stop.wait())

    async def evaluate(task_id: str) -> TaskResult:
        if resume_mode:
            try:
                retained = await retain_task(
                    workflow, task_id, accepted, owner.stop, environment, state
                )
            except (ValidationError, FileNotFoundError):
                retained = None
            except TaskFailure as error:
                return TaskResult(task_id, "failed", str(error), error.category)
            except InterruptedError as error:
                return TaskResult(task_id, "interrupted", str(error), "interruption")
            if retained is not None:
                return retained
        return await execute_task(
            workflow, task_id, run_dir, accepted, owner, cache, environment, state
        )

    def complete(result: TaskResult) -> None:
        queue = [result]
        while queue:
            current = queue.pop(0)
            if current.retained:
                state.record_retention(current)
            else:
                state.record_result(current)
            results[current.task_id] = current
            if current.state in {"succeeded", "cached"}:
                accepted.update(current.artifacts)
            for child in workflow.graph.dependents[current.task_id]:
                remaining[child] -= 1
                if remaining[child] != 0:
                    continue
                if any(
                    results[parent].state not in {"succeeded", "cached"}
                    for parent in workflow.tasks[child].deps
                ):
                    queue.append(
                        TaskResult(child, "blocked", "prerequisite failed or blocked")
                    )
                else:
                    heapq.heappush(ready, child)

    try:
        while ready or active:
            while ready and len(active) < workers and not owner.stop.is_set():
                task_id = heapq.heappop(ready)
                active[asyncio.create_task(evaluate(task_id))] = task_id
            if not active:
                break
            done, _ = await asyncio.wait(
                {*active, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done:
                await owner.cancel_all()
            for task_future in sorted(
                done - {stop_wait}, key=lambda item: active[item]
            ):
                task_id = active.pop(task_future)
                result = await task_future
                if result.task_id != task_id:
                    raise RuntimeError("task result identity mismatch")
                complete(result)
            if owner.stop.is_set() and active:
                # Drain all owned tasks before releasing the workspace lock.
                completed = await asyncio.gather(*active, return_exceptions=True)
                for task_id, value in zip(active.values(), completed, strict=True):
                    if isinstance(value, BaseException):
                        raise value
                    if value.task_id != task_id:
                        raise RuntimeError("task result identity mismatch")
                    complete(value)
                active.clear()
                break
    finally:
        stop_wait.cancel()
        await asyncio.gather(stop_wait, return_exceptions=True)
        if active:
            owner.stop.set()
            await owner.cancel_all()
            await asyncio.gather(*active, return_exceptions=True)
    if owner.stop.is_set():
        for task_id in workflow.graph.order:
            if task_id not in results:
                result = TaskResult(
                    task_id, "interrupted", "run interrupted before launch"
                )
                state.record_result(result)
                results[task_id] = result
    elif len(results) != len(workflow.tasks):
        raise RuntimeError("scheduler stopped with unresolved tasks")
    return results


async def run_workflow(
    workflow: Workflow,
    workers: int,
    stop: asyncio.Event,
    repeated_stop: asyncio.Event,
    use_cache: bool,
) -> RunResult:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    with workspace_lock(workflow.workspace) as runtime:
        environment = await environment_record()
        state = StateStore.create_or_open(runtime)
        try:
            run_id = uuid.uuid4().hex
            started_at = _now()
            state.start_run(
                workflow, run_id, workers, use_cache, environment, started_at
            )
            return await _invoke(
                workflow,
                runtime,
                run_id,
                workers,
                use_cache,
                environment,
                stop,
                repeated_stop,
                state,
                started_at,
                resume_mode=False,
            )
        finally:
            state.close()


async def resume_workflow(
    run_id: str,
    workers_override: int | None,
    stop: asyncio.Event,
    repeated_stop: asyncio.Event,
) -> RunResult:
    workspace = Path.cwd().resolve(strict=True)
    require_existing_runtime(workspace)
    with workspace_lock(workspace) as runtime:
        state = StateStore.open_existing(runtime, readonly=False)
        try:
            header = state.run_header(run_id)
            workflow = load_workflow(workspace / header["workflow_file"])
            state.verify_workflow(run_id, workflow)
            environment = await environment_record()
            started_at = _now()
            _, workers, use_cache = state.start_resume(
                run_id, workers_override, environment, started_at
            )
            return await _invoke(
                workflow,
                runtime,
                run_id,
                workers,
                use_cache,
                environment,
                stop,
                repeated_stop,
                state,
                started_at,
                resume_mode=True,
            )
        finally:
            state.close()


async def _invoke(
    workflow: Workflow,
    runtime: Path,
    run_id: str,
    workers: int,
    use_cache: bool,
    environment: dict[str, object],
    stop: asyncio.Event,
    repeated_stop: asyncio.Event,
    state: StateStore,
    started_at: str,
    *,
    resume_mode: bool,
) -> RunResult:
    started = time.monotonic()
    try:
        if environment["dependency_lock_sha256"] is None:
            print(
                "Dependency lock identity unavailable; using installed distribution inventory.",
                file=sys.stderr,
            )
        cache = CacheStorage(runtime) if use_cache else None
        run_dir = runtime / "runs" / run_id
        ensure_real_directory(run_dir.parent)
        if resume_mode:
            ensure_real_directory(run_dir)
        else:
            run_dir.mkdir()
        print(
            f"Run ID: {run_id}\nInvocation: {state.invocation_no}\n"
            f"Workflow: {workflow.filename}\nLogs: .repro/runs/{run_id}/attempts"
        )
        owner = ProcessOwner(stop, repeated_stop)
        results = await _schedule(
            workflow, run_dir, workers, owner, cache, environment, state, resume_mode
        )
        outcome: Literal["succeeded", "failed", "interrupted"]
        if stop.is_set():
            outcome = "interrupted"
        elif any(result.state in {"failed", "blocked"} for result in results.values()):
            outcome = "failed"
        else:
            outcome = "succeeded"
        ended_at = _now()
        duration = time.monotonic() - started
        state.finish_invocation(outcome, ended_at, duration)
        return RunResult(
            run_id,
            outcome,
            results,
            started_at,
            ended_at,
            duration,
            state.invocation_no,
            state.uncertain_launches(),
            use_cache,
            workflow.graph.order,
        )
    except BaseException as error:
        if not isinstance(error, StateCommitUncertain):
            try:
                state.abort_invocation(
                    f"infrastructure interruption: {type(error).__name__}"
                )
            except Exception:
                pass
        raise
