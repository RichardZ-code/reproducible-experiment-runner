"""Dependency-aware, bounded scheduling for one workflow invocation."""

import asyncio
import heapq
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

from repro_runner.cache import CacheStorage
from repro_runner.config import Workflow
from repro_runner.executor import (
    Artifact,
    ProcessOwner,
    RunResult,
    TaskResult,
    _now,
    execute_task,
)
from repro_runner.hashing import environment_record
from repro_runner.ownership import workspace_lock
from repro_runner.paths import ensure_real_directory


async def _schedule(
    workflow: Workflow,
    run_dir: Path,
    workers: int,
    owner: ProcessOwner,
    cache: CacheStorage | None,
    environment: dict[str, object],
) -> dict[str, TaskResult]:
    remaining = {task_id: len(task.deps) for task_id, task in workflow.tasks.items()}
    ready = [task_id for task_id, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    results: dict[str, TaskResult] = {}
    accepted: dict[str, Artifact] = {}
    active: dict[asyncio.Task[TaskResult], str] = {}
    stop_wait = asyncio.create_task(owner.stop.wait())

    def complete(result: TaskResult) -> None:
        queue = [result]
        while queue:
            current = queue.pop(0)
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
                active[
                    asyncio.create_task(
                        execute_task(
                            workflow,
                            task_id,
                            run_dir,
                            accepted,
                            owner,
                            cache,
                            environment,
                        )
                    )
                ] = task_id
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
                results[task_id] = TaskResult(
                    task_id, "interrupted", "run interrupted before launch"
                )
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
        if environment["dependency_lock_sha256"] is None:
            print(
                "Dependency lock identity unavailable; using installed distribution inventory.",
                file=sys.stderr,
            )
        cache = CacheStorage(runtime) if use_cache else None
        runs = runtime / "runs"
        ensure_real_directory(runs)
        run_id = uuid.uuid4().hex
        run_dir = runs / run_id
        run_dir.mkdir()
        started_at = _now()
        started = time.monotonic()
        print(
            f"Run ID: {run_id}\nWorkflow: {workflow.filename}\nLogs: .repro/runs/{run_id}/attempts"
        )
        owner = ProcessOwner(stop, repeated_stop)
        results = await _schedule(workflow, run_dir, workers, owner, cache, environment)
        state: Literal["succeeded", "failed", "interrupted"]
        if stop.is_set():
            state = "interrupted"
        elif any(result.state in {"failed", "blocked"} for result in results.values()):
            state = "failed"
        else:
            state = "succeeded"
        return RunResult(
            run_id, state, results, started_at, _now(), time.monotonic() - started
        )
