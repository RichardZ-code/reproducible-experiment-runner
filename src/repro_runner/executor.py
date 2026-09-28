"""Bounded execution of validated workflows in private attempt directories."""

import asyncio
import hashlib
import os
import shutil
import signal
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, Literal

from repro_runner.config import Task, Workflow, resolve_inputs
from repro_runner.errors import ValidationError
from repro_runner.paths import inspect_file

_CHUNK = 1024 * 1024
TERM_GRACE_SECONDS = 5.0
State = Literal["pending", "running", "succeeded", "failed", "blocked", "interrupted"]


class TaskFailure(Exception):
    """A declared task cannot produce an accepted result."""

    def __init__(self, message: str, category: str = "task") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Artifact:
    digest: str
    size: int


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


@dataclass
class RunResult:
    run_id: str
    state: Literal["succeeded", "failed", "interrupted"]
    tasks: dict[str, TaskResult]
    started_at: str
    ended_at: str
    duration_seconds: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _real_directory(path: Path) -> None:
    """Create missing parents one component at a time without following links."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError(f"not a real directory: {current}")


@contextmanager
def _reader(path: Path) -> Iterator[BinaryIO]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
    finally:
        os.close(descriptor)


async def _hash(path: Path) -> Artifact:
    digest = hashlib.sha256()
    size = 0
    with _reader(path) as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            await asyncio.sleep(0)
    return Artifact(digest.hexdigest(), size)


async def _copy(source: Path, destination: Path) -> Artifact:
    digest = hashlib.sha256()
    size = 0
    with _reader(source) as input_file, destination.open("xb") as output_file:
        while chunk := input_file.read(_CHUNK):
            output_file.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            await asyncio.sleep(0)
    return Artifact(digest.hexdigest(), size)


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
    def _signal_group(pid: int, number: int) -> None:
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            pass

    @staticmethod
    def _group_exists(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        return True

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
        for pid in groups:
            self._signal_group(pid, signal.SIGTERM)
        if not self.repeated_stop.is_set():
            deadline = time.monotonic() + TERM_GRACE_SECONDS
            while time.monotonic() < deadline and any(
                self._group_exists(pid) for pid in groups
            ):
                if self.repeated_stop.is_set():
                    break
                await asyncio.sleep(0.05)
        for pid in groups:
            if self._group_exists(pid):
                self._signal_group(pid, signal.SIGKILL)
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
) -> TaskResult:
    task = workflow.tasks[task_id]
    result = TaskResult(task_id, "running", command=task.command)
    start: float | None = None
    process: asyncio.subprocess.Process | None = None
    try:
        if owner.stop.is_set():
            raise InterruptedError("run interrupted before task start")
        # Readiness errors have no attempt directory or child launch.
        names = resolve_inputs(workflow, task_id)
        result.started_at = _now()
        start = time.monotonic()
        attempt = run_dir / "attempts" / task_id / "1"
        work = attempt / "work"
        publish = attempt / "publish"
        work.mkdir(parents=True)
        publish.mkdir()
        result.stdout = str((attempt / "stdout.log").relative_to(workflow.workspace))
        result.stderr = str((attempt / "stderr.log").relative_to(workflow.workspace))
        staged_names, snapshots = await _stage_inputs(
            workflow, task_id, work, accepted, owner.stop
        )
        if staged_names != names:
            raise TaskFailure("input inventory changed while staging", "input")
        for name in task.outputs:
            _real_directory((work / name).parent)
        with (
            (attempt / "stdout.log").open("wb") as stdout,
            (attempt / "stderr.log").open("wb") as stderr,
        ):
            if owner.stop.is_set():
                raise InterruptedError("run interrupted before launch")
            command = _command(task)
            result.resolved_executable = command[0]
            try:
                process = await owner.launch(command, work, stdout, stderr)
            except OSError as error:
                raise TaskFailure(f"launch failed: {error}", "launch") from error
            result.launched = True
            wait_child = asyncio.create_task(process.wait())
            wait_stop = asyncio.create_task(owner.stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {wait_child, wait_stop}, return_when=asyncio.FIRST_COMPLETED
                )
                if wait_stop in done and owner.stop.is_set():
                    await owner.cancel_all()
                result.exit_code = await wait_child
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
            )
        except InterruptedError:
            raise
        except OSError as error:
            raise TaskFailure(f"publication failed: {error}", "publication") from error
        if owner.stop.is_set():
            raise InterruptedError("run interrupted during publication")
        result.artifacts = artifacts
        result.state = "succeeded"
    except (ValidationError, TaskFailure, FileNotFoundError, PermissionError) as error:
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
    finally:
        if process is not None:
            owner.groups.pop(process.pid, None)
        if start is not None:
            result.ended_at = _now()
            result.duration_seconds = time.monotonic() - start
    return result
