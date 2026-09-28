"""Behavioral checks for bounded execution and verified publication."""

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from repro_runner import executor, scheduler
from repro_runner.config import load_workflow
from repro_runner.executor import ProcessOwner
from repro_runner.ownership import workspace_lock
from repro_runner.scheduler import resume_workflow, run_workflow

RUNNER = Path(sys.prefix) / "bin" / "runner"


def workflow_file(root: Path, tasks: dict) -> Path:
    path = root / "workflow.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "tasks": tasks}))
    return path


def script(root: Path) -> None:
    (root / "task.py").write_text(
        """import os, pathlib, sys, time
name, output, events, delay, *inputs = sys.argv[1:]
def mark(phase):
    fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f'{name} {phase}\\n'.encode())
    finally:
        os.close(fd)
mark('start')
time.sleep(float(delay))
text = name + ':' + ','.join(pathlib.Path(value).read_text() for value in inputs)
target = pathlib.Path(output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text)
print(name + ' stdout')
print(name + ' stderr', file=sys.stderr)
mark('end')
"""
    )


def task(
    name: str, events: Path, *, deps: tuple[str, ...] = (), delay: str = "0.12"
) -> dict:
    inputs = ["task.py", *(f"out/{parent}.txt" for parent in deps)]
    return {
        "command": [
            "python",
            "task.py",
            name,
            f"out/{name}.txt",
            str(events),
            delay,
            *(f"out/{parent}.txt" for parent in deps),
        ],
        "deps": list(deps),
        "inputs": inputs,
        "outputs": [f"out/{name}.txt"],
    }


def execute(path: Path, workers: int = 4):
    return asyncio.run(
        asyncio.wait_for(
            run_workflow(
                load_workflow(path), workers, asyncio.Event(), asyncio.Event(), False
            ),
            timeout=15,
        )
    )


def events(path: Path) -> list[tuple[str, str]]:
    return [tuple(line.split()) for line in path.read_text().splitlines()]


def max_active(trace: list[tuple[str, str]]) -> int:
    active = 0
    maximum = 0
    for _, phase in trace:
        active += 1 if phase == "start" else -1
        assert active >= 0
        maximum = max(active, maximum)
    assert active == 0
    return maximum


@pytest.mark.parametrize("workers", [1, 4])
def test_chain_diamond_and_bounded_overlap(tmp_path: Path, workers: int) -> None:
    script(tmp_path)
    marker = tmp_path / "events.txt"
    path = workflow_file(
        tmp_path,
        {
            "a": task("a", marker),
            "b": task("b", marker),
            "c": task("c", marker, deps=("a", "b")),
            "d": task("d", marker, deps=("c",)),
            "e": task("e", marker),
        },
    )
    result = execute(path, workers)
    assert result.state == "succeeded"
    assert result.duration_seconds > 0
    assert result.started_at.endswith("+00:00")
    assert result.ended_at.endswith("+00:00")
    assert {item.state for item in result.tasks.values()} == {"succeeded"}
    assert all(item.launched and item.exit_code == 0 for item in result.tasks.values())
    trace = events(marker)
    assert len(trace) == 10
    assert trace.index(("c", "start")) > trace.index(("a", "end"))
    assert trace.index(("c", "start")) > trace.index(("b", "end"))
    assert trace.index(("d", "start")) > trace.index(("c", "end"))
    assert max_active(trace) == (1 if workers == 1 else 3)
    assert (tmp_path / "out/d.txt").read_text() == "d:c:a:,b:"
    attempt = tmp_path / result.tasks["a"].stdout
    assert attempt.read_text() == "a stdout\n"
    assert (tmp_path / result.tasks["a"].stderr).read_text() == "a stderr\n"
    assert not (attempt.parent / "work" / "events.txt").exists()
    (attempt.parent / "work/out/a.txt").write_text("late attempt edit")
    assert (tmp_path / "out/a.txt").read_text() == "a:"
    marker.unlink()
    second = execute(path, workers)
    assert second.state == "succeeded"
    assert second.run_id != result.run_id
    assert len(events(marker)) == 10


def test_more_ready_tasks_than_slots(tmp_path: Path) -> None:
    script(tmp_path)
    marker = tmp_path / "events.txt"
    path = workflow_file(tmp_path, {name: task(name, marker) for name in "abcdef"})
    assert execute(path, 2).state == "succeeded"
    assert max_active(events(marker)) == 2
    assert len(events(marker)) == 12


def test_failed_branch_blocks_descendants_and_reuses_slot(tmp_path: Path) -> None:
    script(tmp_path)
    (tmp_path / "bad.py").write_text(
        "import pathlib,sys; pathlib.Path('out/bad.txt').parent.mkdir(exist_ok=True); "
        "pathlib.Path('out/bad.txt').write_text('partial'); print('oops',file=sys.stderr);sys.exit(7)"
    )
    marker = tmp_path / "events.txt"
    path = workflow_file(
        tmp_path,
        {
            "bad": {
                "command": ["python", "bad.py"],
                "inputs": ["bad.py"],
                "outputs": ["out/bad.txt"],
            },
            "child": task("child", marker, deps=("bad",)),
            "grandchild": task("grandchild", marker, deps=("child",)),
            "good": task("good", marker),
        },
    )
    result = execute(path, 1)
    assert result.state == "failed"
    assert result.tasks["bad"].exit_code == 7
    assert [
        result.tasks[name].state for name in ("bad", "child", "grandchild", "good")
    ] == ["failed", "blocked", "blocked", "succeeded"]
    assert not (tmp_path / "out/bad.txt").exists()
    assert (tmp_path / "out/good.txt").exists()
    assert events(marker) == [("good", "start"), ("good", "end")]
    assert "oops" in (tmp_path / result.tasks["bad"].stderr).read_text()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("pass", "missing input"),
        ("import pathlib; pathlib.Path('out/x').symlink_to('elsewhere')", "symlink"),
        (
            "import pathlib; pathlib.Path('out/x').mkdir(parents=True)",
            "not a regular file",
        ),
    ],
)
def test_zero_exit_requires_valid_staged_output(
    tmp_path: Path, body: str, expected: str
) -> None:
    (tmp_path / "task.py").write_text(body)
    (tmp_path / "out").mkdir()
    (tmp_path / "out/x").write_text("stale")
    path = workflow_file(
        tmp_path,
        {
            "a": {
                "command": ["python", "task.py"],
                "inputs": ["task.py"],
                "outputs": ["out/x"],
            }
        },
    )
    result = execute(path)
    assert result.state == "failed"
    assert result.tasks["a"].exit_code == 0
    assert expected in result.tasks["a"].reason
    assert (tmp_path / "out/x").read_text() == "stale"


def test_launch_failure_and_readiness_failure(tmp_path: Path) -> None:
    path = workflow_file(
        tmp_path,
        {
            "missing_input": {
                "command": ["python", "absent.py"],
                "inputs": ["absent.py"],
                "outputs": ["out/a"],
            },
            "missing_exe": {
                "command": ["not_a_real_executable_987654"],
                "outputs": ["out/b"],
            },
        },
    )
    result = execute(path)
    assert result.state == "failed"
    assert result.tasks["missing_input"].error_category == "readiness"
    assert result.tasks["missing_input"].stdout is None
    assert result.tasks["missing_input"].duration_seconds is None
    assert result.tasks["missing_exe"].exit_code is None
    assert (tmp_path / result.tasks["missing_exe"].stdout).read_bytes() == b""


def test_staging_literals_python_alias_and_large_separate_logs(tmp_path: Path) -> None:
    (tmp_path / "task.py").write_text(
        """import pathlib,sys
assert sys.argv[1:] == ['a b', '$(touch never)', '']
assert not pathlib.Path('neighbor.txt').exists()
assert pathlib.Path('data.txt').read_text() == 'source'
pathlib.Path('data.txt').write_text('changed privately')
pathlib.Path('result.txt').write_text(sys.executable)
sys.stdout.write('x' * 100000)
sys.stderr.write('y' * 100000)
"""
    )
    (tmp_path / "data.txt").write_text("source")
    (tmp_path / "neighbor.txt").write_text("not declared")
    path = workflow_file(
        tmp_path,
        {
            "a": {
                "command": ["python", "task.py", "a b", "$(touch never)", ""],
                "inputs": ["task.py", "data.txt"],
                "outputs": ["result.txt"],
            }
        },
    )
    result = execute(path)
    assert result.state == "failed"  # Mutating a staged input invalidates acceptance.
    assert "staged input changed" in result.tasks["a"].reason
    assert (tmp_path / "data.txt").read_text() == "source"
    assert not (tmp_path / "never").exists()
    assert len((tmp_path / result.tasks["a"].stdout).read_bytes()) == 100000
    assert len((tmp_path / result.tasks["a"].stderr).read_bytes()) == 100000
    assert result.tasks["a"].resolved_executable == sys.executable


def test_invalid_workers_internal_boundary(tmp_path: Path) -> None:
    path = workflow_file(
        tmp_path, {"a": {"command": ["python", "x.py"], "outputs": ["out/a"]}}
    )
    with pytest.raises(ValueError, match="workers"):
        execute(path, 0)
    assert not (tmp_path / ".repro").exists()


def test_installed_cli_from_other_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script(workspace)
    marker = tmp_path / "events.txt"
    path = workflow_file(workspace, {"a": task("a", marker, delay="0")})
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(RUNNER), "run", str(path), "--no-cache"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "a: succeeded" in completed.stdout
    assert (workspace / "out/a.txt").read_text() == "a:"
    assert (workspace / ".repro/state.sqlite3").is_file()
    assert not list((workspace / ".repro").glob("cache/*"))
    assert len(list((workspace / ".repro/runs").glob("*/manifest.json"))) == 1
    assert list(tmp_path.glob(".repro")) == []
    default = subprocess.run(
        [str(RUNNER), "run", str(path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert default.returncode == 0, default.stderr
    assert "executed=1, retained=0, cached=0, cache_misses=1" in default.stdout
    assert len(events(marker)) == 4
    assert len(list((workspace / ".repro/runs").iterdir())) == 2
    warm = subprocess.run(
        [str(RUNNER), "run", str(path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert warm.returncode == 0, warm.stderr
    assert "executed=0, retained=0, cached=1, cache_misses=0" in warm.stdout
    assert len(events(marker)) == 4


def test_changed_source_during_child_execution_is_rejected(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    (tmp_path / "data.txt").write_text("before")
    (tmp_path / "task.py").write_text(
        """import os,pathlib,sys,time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.01)
pathlib.Path('out.txt').write_text(pathlib.Path('data.txt').read_text())
"""
    )
    path = workflow_file(
        tmp_path,
        {
            "a": {
                "command": ["python", "task.py", str(ready), str(release)],
                "inputs": ["task.py", "data.txt"],
                "outputs": ["out.txt"],
            }
        },
    )
    proc = subprocess.Popen(
        [str(RUNNER), "run", str(path), "--no-cache"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while (
            not ready.exists() and proc.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()
        (tmp_path / "data.txt").write_text("after")
        release.write_text("go")
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 1, (stdout, stderr)
        assert "input source changed" in stdout
        assert not (tmp_path / "out.txt").exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
        if ready.exists():
            try:
                os.killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_changed_producer_artifact_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(tmp_path)
    marker = tmp_path / "events.txt"
    path = workflow_file(
        tmp_path,
        {
            "a": task("a", marker, delay="0"),
            "b": task("b", marker, deps=("a",), delay="0"),
        },
    )
    original = scheduler.execute_task

    async def corrupt_after_accept(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result.task_id == "a" and result.state == "succeeded":
            (tmp_path / "out/a.txt").write_text("corrupt")
        return result

    monkeypatch.setattr(scheduler, "execute_task", corrupt_after_accept)
    result = execute(path)
    assert result.state == "failed"
    assert result.tasks["a"].state == "succeeded"
    assert result.tasks["b"].state == "failed"
    assert "producer artifact changed" in result.tasks["b"].reason
    assert result.tasks["b"].launched is False


def test_publication_failure_aborts_without_accepting_partial_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "task.py").write_text(
        """import pathlib,sys
if len(sys.argv) == 1:
    pathlib.Path('a').write_text('new a')
    pathlib.Path('b').write_text('new b')
else:
    pathlib.Path('c').write_text(pathlib.Path('a').read_text())
"""
    )
    (tmp_path / "b").write_text("old b")
    path = workflow_file(
        tmp_path,
        {
            "first": {
                "command": ["python", "task.py"],
                "inputs": ["task.py"],
                "outputs": ["a", "b"],
            },
            "next": {
                "deps": ["first"],
                "command": ["python", "task.py", "next"],
                "inputs": ["task.py", "a"],
                "outputs": ["c"],
            },
        },
    )
    original = os.replace

    def fail_second(source, target):
        if Path(target) == tmp_path / "b":
            raise OSError("injected second replacement failure")
        return original(source, target)

    monkeypatch.setattr(executor.os, "replace", fail_second)
    with pytest.raises(
        executor.PublicationOperationalError,
        match="injected second replacement failure",
    ):
        execute(path)
    assert (tmp_path / "a").read_text() == "new a"
    assert (tmp_path / "b").read_text() == "old b"
    assert not (tmp_path / "c").exists()
    assert not list(tmp_path.glob(".repro-tmp-*"))
    with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
        run_id = connection.execute("SELECT run_id FROM runs").fetchone()[0]
        assert connection.execute("SELECT outcome FROM runs").fetchone() == (
            "interrupted",
        )
        assert connection.execute(
            "SELECT task_id,state FROM tasks ORDER BY task_id"
        ).fetchall() == [("first", "interrupted"), ("next", "interrupted")]
        assert connection.execute(
            "SELECT task_id,state,exit_code FROM attempts"
        ).fetchall() == [("first", "interrupted", 0)]
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM resolutions").fetchone() == (0,)
    monkeypatch.setattr(executor.os, "replace", original)
    monkeypatch.chdir(tmp_path)
    resumed = asyncio.run(
        resume_workflow(run_id, None, asyncio.Event(), asyncio.Event())
    )
    assert resumed.state == "succeeded"
    assert resumed.tasks["first"].state == "succeeded"
    assert resumed.tasks["next"].state == "succeeded"
    assert (tmp_path / "a").read_text() == "new a"
    assert (tmp_path / "b").read_text() == "new b"
    assert (tmp_path / "c").read_text() == "new a"
    with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
        assert connection.execute(
            "SELECT state FROM attempts WHERE task_id='first' ORDER BY attempt_no"
        ).fetchall() == [("interrupted",), ("succeeded",)]


def test_publication_error_exits_three_and_reaps_active_child(tmp_path: Path) -> None:
    active = tmp_path / "active.pid"
    queued = tmp_path / "queued.marker"
    (tmp_path / "task.py").write_text(
        """import os, pathlib, sys, time
name, active, queued = sys.argv[1:]
if name == 'a':
    while not pathlib.Path(active).exists():
        time.sleep(0.01)
    pathlib.Path('locked').mkdir(exist_ok=True)
    pathlib.Path('locked/a').write_text('complete')
elif name == 'b':
    pathlib.Path(active).write_text(str(os.getpid()))
    print('b started', flush=True)
    time.sleep(30)
    pathlib.Path('out').mkdir()
    pathlib.Path('out/b').write_text('late')
else:
    pathlib.Path(queued).write_text('launched')
    pathlib.Path('out').mkdir()
    pathlib.Path('out/c').write_text('late')
"""
    )
    path = workflow_file(
        tmp_path,
        {
            name: {
                "command": ["python", "task.py", name, str(active), str(queued)],
                "inputs": ["task.py"],
                "outputs": ["locked/a" if name == "a" else f"out/{name}"],
            }
            for name in ("a", "b", "c")
        },
    )
    # The separate CLI process injects a destination failure after both active
    # children start. The wrapper works under root on Linux and on macOS.
    wrapper = tmp_path / "deny_publication.py"
    wrapper.write_text(
        """import os, sys
from pathlib import Path
from repro_runner.cli import app
original = os.replace
def denied(source, destination):
    if Path(destination).name == 'a' and Path(destination).parent.name == 'locked':
        raise PermissionError('injected publication denial')
    return original(source, destination)
os.replace = denied
sys.argv[0] = 'runner'
app()
"""
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(wrapper),
            "run",
            str(path),
            "--workers",
            "2",
            "--no-cache",
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    active_pid: int | None = None
    try:
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 3, (stdout, stderr)
        assert "injected publication denial" in stderr
        assert active.exists()
        active_pid = int(active.read_text())
        with pytest.raises(ProcessLookupError):
            os.killpg(active_pid, 0)
        assert not queued.exists()
        assert not (tmp_path / "locked/a").exists()
        assert not (tmp_path / "out/b").exists()
        with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
            assert connection.execute("SELECT outcome FROM runs").fetchone() == (
                "interrupted",
            )
            assert connection.execute(
                "SELECT task_id,state FROM tasks ORDER BY task_id"
            ).fetchall() == [
                ("a", "interrupted"),
                ("b", "interrupted"),
                ("c", "interrupted"),
            ]
        assert (
            "b started"
            in next(
                (tmp_path / ".repro/runs").glob("*/attempts/b/1/stdout.log")
            ).read_text()
        )
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
        if active.exists():
            active_pid = int(active.read_text())
            try:
                os.killpg(active_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stop_during_publication_does_not_accept_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "task.py").write_text(
        "import pathlib; pathlib.Path('a').write_text('a'); pathlib.Path('b').write_text('b')"
    )
    path = workflow_file(
        tmp_path,
        {
            "first": {
                "command": ["python", "task.py"],
                "inputs": ["task.py"],
                "outputs": ["a", "b"],
            }
        },
    )
    stop = asyncio.Event()
    original = os.replace

    def stop_after_first(source, target):
        original(source, target)
        if Path(target) == tmp_path / "a":
            stop.set()

    monkeypatch.setattr(executor.os, "replace", stop_after_first)
    result = asyncio.run(
        run_workflow(load_workflow(path), 1, stop, asyncio.Event(), False)
    )
    assert result.state == "interrupted"
    assert result.tasks["first"].state == "interrupted"
    assert (tmp_path / "a").read_text() == "a"
    assert not (tmp_path / "b").exists()


def test_launch_handoff_cancel_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = asyncio.create_subprocess_exec

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        launched: list[asyncio.subprocess.Process] = []

        async def delayed(*args, **kwargs):
            process = await original(*args, **kwargs)
            launched.append(process)
            entered.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
        owner = ProcessOwner(asyncio.Event(), asyncio.Event())
        with (
            (tmp_path / "stdout").open("wb") as stdout,
            (tmp_path / "stderr").open("wb") as stderr,
        ):
            launch = asyncio.create_task(
                owner.launch(
                    (sys.executable, "-c", "import time; time.sleep(30)"),
                    tmp_path,
                    stdout,
                    stderr,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                launch.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(launch, 10)
                assert len(launched) == 1
                assert launched[0].returncode is not None
                assert not owner._group_exists(launched[0].pid)
            finally:
                release.set()
                if launched and launched[0].returncode is None:
                    owner._signal_group(launched[0].pid, signal.SIGKILL)
                    await launched[0].wait()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("signal_number", "expected_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_signals_clean_groups_and_pending_work(
    tmp_path: Path, signal_number: int, expected_code: int
) -> None:
    ready_a = tmp_path / "ready_a"
    ready_b = tmp_path / "ready_b"
    descendant_pid = tmp_path / "descendant_pid"
    descendant_ready = tmp_path / "descendant_ready"
    (tmp_path / "task.py").write_text(
        """import os,pathlib,signal,subprocess,sys,time
name, marker, descendant, descendant_ready = sys.argv[1:]
if name == 'a':
    child = subprocess.Popen([sys.executable, '-c',
        'import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(30)', descendant_ready])
    pathlib.Path(descendant).write_text(str(child.pid))
    while not pathlib.Path(descendant_ready).exists():
        time.sleep(0.01)
pathlib.Path(marker).write_text(str(os.getpid()))
print(name + ' started', flush=True)
print(name + ' stderr', file=sys.stderr, flush=True)
time.sleep(30)
pathlib.Path(name).write_text('late')
"""
    )
    path = workflow_file(
        tmp_path,
        {
            name: {
                "command": [
                    "python",
                    "task.py",
                    name,
                    str(marker),
                    str(descendant_pid),
                    str(descendant_ready),
                ],
                "inputs": ["task.py"],
                "outputs": [name],
            }
            for name, marker in (
                ("a", ready_a),
                ("b", ready_b),
                ("c", tmp_path / "ready_c"),
            )
        },
    )
    proc = subprocess.Popen(
        [str(RUNNER), "run", str(path), "--workers", "2", "--no-cache"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    descendant: int | None = None
    try:
        deadline = time.monotonic() + 8
        while (
            (not ready_a.exists() or not ready_b.exists())
            and proc.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready_a.exists() and ready_b.exists()
        descendant = int(descendant_pid.read_text())
        os.kill(proc.pid, signal_number)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == expected_code, (stdout, stderr)
        assert "Outcome: interrupted" in stdout
        assert "c: interrupted" in stdout
        assert not (tmp_path / "ready_c").exists()
        assert not (tmp_path / "a").exists()
        assert (
            "a started"
            in next(
                (tmp_path / ".repro/runs").glob("*/attempts/a/1/stdout.log")
            ).read_text()
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("known descendant survived cancellation")
        # The ordinary owner is gone, so a new invocation can acquire the lock.
        with workspace_lock(tmp_path):
            pass
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
        for marker in (ready_a, ready_b):
            if marker.exists():
                try:
                    os.killpg(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_workspace_contention_and_release(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    (tmp_path / "task.py").write_text(
        """import os,pathlib,sys,time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.01)
pathlib.Path('out').write_text('ok')
"""
    )
    path = workflow_file(
        tmp_path,
        {
            "a": {
                "command": ["python", "task.py", str(ready), str(release)],
                "inputs": ["task.py"],
                "outputs": ["out"],
            }
        },
    )
    first = subprocess.Popen(
        [str(RUNNER), "run", str(path), "--no-cache"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while (
            not ready.exists() and first.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()
        before = list((tmp_path / ".repro/runs").iterdir())
        second = subprocess.run(
            [str(RUNNER), "run", str(path), "--no-cache"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert second.returncode == 3
        assert "ownership conflict" in second.stderr.lower()
        assert list((tmp_path / ".repro/runs").iterdir()) == before
        release.write_text("go")
        first_out, first_err = first.communicate(timeout=10)
        assert first.returncode == 0, (first_out, first_err)
        third = subprocess.run(
            [str(RUNNER), "run", str(path), "--no-cache"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert third.returncode == 0, (third.stdout, third.stderr)
        assert len(list((tmp_path / ".repro/runs").iterdir())) == 2
    finally:
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
            first.communicate(timeout=5)
        if ready.exists():
            try:
                os.killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_child_cannot_keep_workspace_lock_after_runner_exits(tmp_path: Path) -> None:
    survivor_pid = tmp_path / "survivor_pid"
    (tmp_path / "task.py").write_text(
        """import pathlib,subprocess,sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                         start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
pathlib.Path('out').write_text('done')
"""
    )
    path = workflow_file(
        tmp_path,
        {
            "a": {
                "command": ["python", "task.py", str(survivor_pid)],
                "inputs": ["task.py"],
                "outputs": ["out"],
            }
        },
    )
    survivor: int | None = None
    try:
        result = execute(path)
        assert result.state == "succeeded"
        survivor = int(survivor_pid.read_text())
        os.kill(survivor, 0)
        with workspace_lock(tmp_path):
            pass
    finally:
        if survivor is not None:
            try:
                os.killpg(survivor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(survivor, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("controlled survivor was not cleaned up")


def test_repeated_interrupt_skips_grace(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    (tmp_path / "task.py").write_text(
        """import os,pathlib,signal,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
pathlib.Path('out').write_text('late')
"""
    )
    path = workflow_file(
        tmp_path,
        {
            "a": {
                "command": ["python", "task.py", str(ready)],
                "inputs": ["task.py"],
                "outputs": ["out"],
            }
        },
    )
    proc = subprocess.Popen(
        [str(RUNNER), "run", str(path), "--no-cache"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while (
            not ready.exists() and proc.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 130, (stdout, stderr)
        assert time.monotonic() - started < 4
        assert not (tmp_path / "out").exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
        if ready.exists():
            try:
                os.killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
