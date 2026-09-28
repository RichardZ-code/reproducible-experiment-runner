"""Bounded execution of validated workflows in private attempt directories."""

import asyncio
import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from repro_runner.cache import (
    CacheConflict,
    CacheOperationalError,
    CacheStorage,
    InvalidEntry,
)
from repro_runner.config import Task, Workflow, resolve_inputs
from repro_runner.errors import ValidationError
from repro_runner.hashing import (
    Artifact,
    environment_record,
    task_identity,
)
from repro_runner.hashing import (
    copy_file as _copy,
)
from repro_runner.hashing import (
    hash_file as _hash,
)
from repro_runner.paths import ensure_real_directory as _real_directory
from repro_runner.paths import inspect_file
from repro_runner.state import StateStore

TERM_GRACE_SECONDS = 5.0
State = Literal[
    "pending", "running", "succeeded", "cached", "failed", "blocked", "interrupted"
]


class TaskFailure(Exception):
    """A declared task cannot produce an accepted result."""

    def __init__(self, message: str, category: str = "task") -> None:
        super().__init__(message)
        self.category = category


class PublicationOperationalError(Exception):
    """Workspace output publication failed for an operational reason."""


@dataclass
class TaskResult:
    task_id: str
    state: State
    reason: str | None = None
    error_category: str | None = None
    exit_code: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: tuple[str, ...] | None = None
    resolved_executable: str | None = None
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    launched: bool = False
    cache_key: str | None = None
    cache_miss: bool = False
    cache_lookup: bool = False
    attempt_no: int | None = None
    retained: bool = False
    source_attempt_no: int | None = None


@dataclass
class RunResult:
    run_id: str
    state: Literal["succeeded", "failed", "interrupted"]
    tasks: dict[str, TaskResult]
    started_at: str
    ended_at: str
    duration_seconds: float
    invocation_no: int = 1
    uncertain_launches: int = 0
    use_cache: bool = True
    task_order: tuple[str, ...] = ()
    manifest_path: str | None = None
    manifest_error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _command(task: Task) -> tuple[str, ...]:
    executable = task.command[0]
    if executable in {"python", "python3", "python3.12"}:
        return (sys.executable, *task.command[1:])
    adjacent = Path(sys.executable).parent / executable
    if adjacent.is_file() and os.access(adjacent, os.X_OK):
        return (str(adjacent), *task.command[1:])
    located = shutil.which(executable)
    if located is None:
        raise TaskFailure(f"executable not found: {executable!r}", "launch")
    return (located, *task.command[1:])


class ProcessOwner:
    """Track only groups created for this invocation, including launch handoffs."""

    def __init__(self, stop: asyncio.Event, repeated_stop: asyncio.Event) -> None:
        self.stop = stop
        self.repeated_stop = repeated_stop
        self.groups: dict[int, asyncio.subprocess.Process] = {}
        self._cleanup: asyncio.Task[None] | None = None
        self._covered: set[int] = set()

    async def launch(
        self, command: tuple[str, ...], work: Path, stdout: object, stderr: object
    ) -> asyncio.subprocess.Process:
        # Keep the creation task until its result is collected even if its caller is cancelled.
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=work,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            process = await launch
            self.groups[process.pid] = process
            await self.cancel_all()
            await process.wait()
            raise
        self.groups[process.pid] = process
        if self.stop.is_set():
            await self.cancel_all()
        return process

    @staticmethod
    def _signal_group(pid: int, number: int) -> bool:
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            return False
        return True

    @staticmethod
    def _group_exists(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @staticmethod
    async def _owned_group_action(
        process: asyncio.subprocess.Process, number: int
    ) -> bool:
        pid = process.pid

        def action() -> bool:
            if number == 0:
                return ProcessOwner._group_exists(pid)
            return ProcessOwner._signal_group(pid, number)

        try:
            return action()
        except PermissionError:
            try:
                os.getpgid(pid)
            except ProcessLookupError:
                # Darwin may deny group signaling while its exited leader is unreaped.
                # Reap that known child, then retry for any surviving descendants.
                await process.wait()
                return action()
            raise

    async def cancel_all(self) -> None:
        while True:
            if self._cleanup is not None and not self._cleanup.done():
                await asyncio.shield(self._cleanup)
            outstanding = tuple(pid for pid in self.groups if pid not in self._covered)
            if not outstanding:
                return
            self._cleanup = asyncio.create_task(self._terminate_groups(outstanding))
            await asyncio.shield(self._cleanup)
            self._covered.update(outstanding)

    async def _terminate_groups(self, groups: tuple[int, ...]) -> None:
        processes = [self.groups[pid] for pid in groups if pid in self.groups]
        try:
            for process in processes:
                await self._owned_group_action(process, signal.SIGTERM)
            if not self.repeated_stop.is_set():
                deadline = time.monotonic() + TERM_GRACE_SECONDS
                while time.monotonic() < deadline:
                    remaining = [
                        await self._owned_group_action(process, 0)
                        for process in processes
                    ]
                    if not any(remaining):
                        break
                    if self.repeated_stop.is_set():
                        break
                    await asyncio.sleep(0.05)
            for process in processes:
                if await self._owned_group_action(process, 0):
                    await self._owned_group_action(process, signal.SIGKILL)
        except PermissionError as group_error:
            # Group access failed. Attempt every direct child before reporting errors.
            cleanup_errors: list[tuple[int, str, Exception]] = []
            waiters: dict[asyncio.Task[int], int] = {}
            for process in processes:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        cleanup_errors.append((process.pid, "kill", error))
                waiters[asyncio.create_task(process.wait())] = process.pid
            if waiters:
                done, pending = await asyncio.wait(waiters, timeout=TERM_GRACE_SECONDS)
                for waiter in pending:
                    waiter.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for waiter in done:
                    try:
                        waiter.result()
                    except Exception as error:
                        cleanup_errors.append((waiters[waiter], "reap", error))
                for waiter in pending:
                    cleanup_errors.append(
                        (
                            waiters[waiter],
                            "reap",
                            TimeoutError("child did not exit during cleanup"),
                        )
                    )
            if cleanup_errors:
                details = "; ".join(
                    f"child {pid} {action}: {type(error).__name__}: {error}"
                    for pid, action, error in cleanup_errors
                )
                raise ExceptionGroup(
                    f"group cleanup failed ({type(group_error).__name__}: "
                    f"{group_error}); direct-child cleanup failed ({details})",
                    [group_error, *(error for _, _, error in cleanup_errors)],
                ) from group_error
            raise
        await asyncio.gather(*(process.wait() for process in processes))


async def _stage_inputs(
    workflow: Workflow,
    task_id: str,
    work: Path,
    accepted: dict[str, Artifact],
    stop: asyncio.Event,
) -> tuple[tuple[str, ...], dict[str, Artifact]]:
    names = resolve_inputs(workflow, task_id)
    snapshots: dict[str, Artifact] = {}
    for name in names:
        if stop.is_set():
            raise InterruptedError("run interrupted during staging")
        inspect_file(workflow.workspace, name, required=True)
        source = workflow.workspace / name
        destination = work / name
        _real_directory(destination.parent)
        before = await _hash(source)
        if name in accepted and before != accepted[name]:
            raise TaskFailure(f"producer artifact changed: {name!r}", "input")
        copied = await _copy(source, destination)
        inspect_file(workflow.workspace, name, required=True)
        after = await _hash(source)
        if copied != before or after != before:
            raise TaskFailure(f"input changed while staging: {name!r}", "input")
        snapshots[name] = copied
    return names, snapshots


async def _check_inputs(
    workflow: Workflow,
    task_id: str,
    names: tuple[str, ...],
    snapshots: dict[str, Artifact],
    work: Path,
) -> None:
    if resolve_inputs(workflow, task_id) != names:
        raise TaskFailure("input glob membership changed during execution", "input")
    for name in names:
        inspect_file(workflow.workspace, name, required=True)
        inspect_file(work, name, required=True)
        if await _hash(workflow.workspace / name) != snapshots[name]:
            raise TaskFailure(f"input source changed: {name!r}", "input")
        if await _hash(work / name) != snapshots[name]:
            raise TaskFailure(f"staged input changed: {name!r}", "input")


async def _dependency_outputs(
    workflow: Workflow, task: Task, accepted: dict[str, Artifact]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for dependency in sorted(task.deps):
        for path in sorted(workflow.tasks[dependency].outputs):
            expected = accepted.get(path)
            if expected is None:
                raise RuntimeError(f"missing accepted dependency artifact {path!r}")
            inspect_file(workflow.workspace, path, required=True)
            if await _hash(workflow.workspace / path) != expected:
                raise TaskFailure(f"dependency output changed: {path!r}", "input")
            rows.append({"task": dependency, "path": path, "sha256": expected.digest})
    return rows


async def retain_task(
    workflow: Workflow,
    task_id: str,
    accepted: dict[str, Artifact],
    stop: asyncio.Event,
    environment: dict[str, object],
    state: StateStore,
) -> TaskResult | None:
    """Select a committed result only after checking current inputs and outputs."""
    if stop.is_set():
        raise InterruptedError("run interrupted before retention")
    task = workflow.tasks[task_id]
    names = resolve_inputs(workflow, task_id)
    snapshots: dict[str, Artifact] = {}
    for name in names:
        if stop.is_set():
            raise InterruptedError("run interrupted during retention")
        inspect_file(workflow.workspace, name, required=True)
        snapshot = await _hash(workflow.workspace / name)
        if name in accepted and snapshot != accepted[name]:
            raise TaskFailure(f"producer artifact changed: {name!r}", "input")
        snapshots[name] = snapshot
    dependencies = await _dependency_outputs(workflow, task, accepted)
    identity = task_identity(task, snapshots, dependencies, environment)
    prior = state.prior_result(task_id, identity.key, task.outputs)
    if prior is None:
        return None
    for path, expected in prior.artifacts.items():
        if stop.is_set():
            raise InterruptedError("run interrupted during retention")
        if inspect_file(workflow.workspace, path, required=False) is None:
            return None
        if await _hash(workflow.workspace / path) != expected:
            return None
    if resolve_inputs(workflow, task_id) != names:
        raise TaskFailure("input glob membership changed during retention", "input")
    for name, expected in snapshots.items():
        inspect_file(workflow.workspace, name, required=True)
        if await _hash(workflow.workspace / name) != expected:
            raise TaskFailure(
                f"input source changed during retention: {name!r}", "input"
            )
    if await _dependency_outputs(workflow, task, accepted) != dependencies:
        raise TaskFailure("dependency outputs changed during retention", "input")
    if await environment_record() != environment:
        raise TaskFailure("environment changed during retention", "environment")
    if stop.is_set():
        raise InterruptedError("run interrupted during retention")
    return TaskResult(
        task_id,
        prior.state,
        command=task.command,
        artifacts=prior.artifacts,
        cache_key=identity.key,
        retained=True,
        source_attempt_no=prior.attempt_no,
    )


async def _freeze_outputs(task: Task, work: Path, publish: Path) -> dict[str, Artifact]:
    artifacts: dict[str, Artifact] = {}
    for name in sorted(task.outputs):
        inspect_file(work, name, required=True)
        frozen = publish / name
        _real_directory(frozen.parent)
        before = await _hash(work / name)
        copied = await _copy(work / name, frozen)
        inspect_file(work, name, required=True)
        if copied != before or await _hash(work / name) != before:
            raise TaskFailure(f"output changed while freezing: {name!r}", "output")
        artifacts[name] = copied
    return artifacts


async def _publish(
    workflow: Workflow,
    artifacts: dict[str, Artifact],
    publish: Path,
    task_id: str,
    names: tuple[str, ...],
    snapshots: dict[str, Artifact],
    work: Path,
    stop: asyncio.Event,
    accepted: dict[str, Artifact],
    dependency_rows: list[dict[str, str]],
    environment: dict[str, object],
) -> None:
    temporaries: dict[str, Path] = {}
    try:
        for name, expected in sorted(artifacts.items()):
            frozen = publish / name
            inspect_file(publish, name, required=True)
            if await _hash(frozen) != expected:
                raise TaskFailure(f"frozen output changed: {name!r}", "publication")
            target = workflow.workspace / name
            _real_directory(target.parent)
            inspect_file(workflow.workspace, name, required=False)
            temporary = target.with_name(f".repro-tmp-{uuid.uuid4().hex}")
            temporaries[name] = temporary
            copied = await _copy(frozen, temporary)
            if copied != expected:
                raise TaskFailure(f"publication copy changed: {name!r}", "publication")
        # Validate the complete destination set before the first replacement.
        for name in sorted(artifacts):
            inspect_file(workflow.workspace, name, required=False)
        await _check_inputs(workflow, task_id, names, snapshots, work)
        if (
            await _dependency_outputs(workflow, workflow.tasks[task_id], accepted)
            != dependency_rows
        ):
            raise TaskFailure("dependency outputs changed before publication", "input")
        if await environment_record() != environment:
            raise TaskFailure("environment changed before publication", "environment")
        for name in sorted(artifacts):
            if stop.is_set():
                raise InterruptedError("run interrupted during publication")
            os.replace(temporaries[name], workflow.workspace / name)
            del temporaries[name]
            if await _hash(workflow.workspace / name) != artifacts[name]:
                raise TaskFailure(f"published output changed: {name!r}", "publication")
    finally:
        for path in temporaries.values():
            path.unlink(missing_ok=True)


async def execute_task(
    workflow: Workflow,
    task_id: str,
    run_dir: Path,
    accepted: dict[str, Artifact],
    owner: ProcessOwner,
    cache: CacheStorage | None,
    environment: dict[str, object],
    state: StateStore,
) -> TaskResult:
    task = workflow.tasks[task_id]
    result = TaskResult(task_id, "running", command=task.command)
    start: float | None = None
    process: asyncio.subprocess.Process | None = None
    cache_temporary: Path | None = None
    try:
        if owner.stop.is_set():
            raise InterruptedError("run interrupted before task start")
        # Readiness errors have no attempt directory or child launch.
        names = resolve_inputs(workflow, task_id)
        result.started_at = _now()
        start = time.monotonic()
        result.attempt_no = state.allocate_attempt(
            task_id, task.command, result.started_at
        )
        attempt = run_dir / "attempts" / task_id / str(result.attempt_no)
        work = attempt / "work"
        publish = attempt / "publish"
        work.mkdir(parents=True)
        publish.mkdir()
        staged_names, snapshots = await _stage_inputs(
            workflow, task_id, work, accepted, owner.stop
        )
        if staged_names != names:
            raise TaskFailure("input inventory changed while staging", "input")
        for name in task.outputs:
            _real_directory((work / name).parent)
        dependency_rows = await _dependency_outputs(workflow, task, accepted)
        identity = task_identity(task, snapshots, dependency_rows, environment)
        result.cache_key = identity.key
        state.record_identity(
            task_id,
            result.attempt_no,
            identity,
            snapshots,
            dependency_rows,
            environment,
        )

        async def publish_outputs(artifacts: dict[str, Artifact]) -> None:
            try:
                await _publish(
                    workflow,
                    artifacts,
                    publish,
                    task_id,
                    staged_names,
                    snapshots,
                    work,
                    owner.stop,
                    accepted,
                    dependency_rows,
                    environment,
                )
            except InterruptedError:
                raise
            except OSError as error:
                raise PublicationOperationalError(
                    f"publication failed: {error}"
                ) from error

        if cache is not None:
            result.cache_lookup = True
            try:
                cached = await cache.lookup(identity, task.outputs, owner.stop)
            except OSError as error:
                raise CacheOperationalError(f"cache lookup failed: {error}") from error
            if cached is not None:
                state.mark_restore(task_id, result.attempt_no)
                try:
                    await cache.restore(identity, cached, publish, owner.stop)
                except InvalidEntry as error:
                    try:
                        cache.quarantine(identity, str(error))
                    except OSError as failure:
                        raise CacheOperationalError(
                            f"cache quarantine failed: {failure}"
                        ) from failure
                except OSError as error:
                    raise CacheOperationalError(
                        f"cache restoration failed: {error}"
                    ) from error
                else:
                    await publish_outputs(cached)
                    if owner.stop.is_set():
                        raise InterruptedError("run interrupted during restoration")
                    result.artifacts = cached
                    result.state = "cached"
                    return result
            result.cache_miss = True

        result.stdout = str((attempt / "stdout.log").relative_to(workflow.workspace))
        result.stderr = str((attempt / "stderr.log").relative_to(workflow.workspace))
        with (
            (attempt / "stdout.log").open("wb") as stdout,
            (attempt / "stderr.log").open("wb") as stderr,
        ):
            if owner.stop.is_set():
                raise InterruptedError("run interrupted before launch")
            command = _command(task)
            result.resolved_executable = command[0]
            resolution_kind = (
                "runner_python"
                if task.command[0] in {"python", "python3", "python3.12"}
                else "path_program"
            )
            state.launch_requested(
                task_id,
                result.attempt_no,
                result.stdout,
                result.stderr,
                resolution_kind,
            )
            try:
                process = await owner.launch(command, work, stdout, stderr)
            except OSError as error:
                raise TaskFailure(f"launch failed: {error}", "launch") from error
            result.launched = True
            state.launch_confirmed(task_id, result.attempt_no)
            wait_child = asyncio.create_task(process.wait())
            wait_stop = asyncio.create_task(owner.stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {wait_child, wait_stop}, return_when=asyncio.FIRST_COMPLETED
                )
                if wait_stop in done and owner.stop.is_set():
                    await owner.cancel_all()
                result.exit_code = await wait_child
                state.child_exited(task_id, result.attempt_no, result.exit_code)
            finally:
                wait_stop.cancel()
                await asyncio.gather(wait_stop, return_exceptions=True)
        if owner.stop.is_set():
            raise InterruptedError("run interrupted")
        if result.exit_code != 0:
            raise TaskFailure(
                f"child exited with code {result.exit_code}", "child_exit"
            )
        artifacts = await _freeze_outputs(task, work, publish)
        await _check_inputs(workflow, task_id, staged_names, snapshots, work)
        if owner.stop.is_set():
            raise InterruptedError("run interrupted before publication")
        if cache is not None:
            try:
                cache_temporary = await cache.prepare(
                    identity, artifacts, publish, owner.stop
                )
            except OSError as error:
                raise CacheOperationalError(
                    f"cache preparation failed: {error}"
                ) from error
        await publish_outputs(artifacts)
        if cache is not None:
            try:
                await cache.publish(cache_temporary, identity, artifacts, owner.stop)
            except OSError as error:
                raise CacheOperationalError(
                    f"cache publication failed: {error}"
                ) from error
        if owner.stop.is_set():
            raise InterruptedError("run interrupted during publication")
        result.artifacts = artifacts
        result.state = "succeeded"
    except (
        ValidationError,
        TaskFailure,
        CacheConflict,
        InvalidEntry,
        FileNotFoundError,
        PermissionError,
    ) as error:
        result.state = "failed"
        result.reason = str(error)
        result.error_category = (
            "readiness" if start is None else getattr(error, "category", "task")
        )
    except InterruptedError as error:
        result.state = "interrupted"
        result.reason = str(error)
        result.error_category = "interruption"
    except asyncio.CancelledError:
        owner.stop.set()
        if process is not None:
            await owner.cancel_all()
            await process.wait()
        raise
    except BaseException:
        owner.stop.set()
        if process is not None and process.returncode is None:
            try:
                await owner.cancel_all()
                await process.wait()
            except BaseException:
                pass
        raise
    finally:
        if cache is not None and cache_temporary is not None:
            try:
                cache.discard(cache_temporary)
            except OSError as error:
                raise CacheOperationalError(f"cache cleanup failed: {error}") from error
        if process is not None:
            owner.groups.pop(process.pid, None)
        if start is not None:
            result.ended_at = _now()
            result.duration_seconds = time.monotonic() - start
    return result
