"""Owned cancellation, abrupt death, and a known late old-attempt writer."""

import hashlib
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

RUNNER = Path(sys.prefix) / "bin/runner"


def fixture(root: Path, *, mode: str) -> tuple[Path, Path, Path, Path, Path]:
    root.mkdir()
    ready = root / "ready"
    release = root / "release"
    done = root / "old_wrote"
    launches = root / "launches"
    (root / "task.py").write_text(
        "import os,pathlib,sys,time\n"
        "name,out,ready,release,done,launches,mode,*inputs=sys.argv[1:]\n"
        "attempt=pathlib.Path.cwd().parent.name\n"
        "with open(launches,'a') as f:f.write(name+':'+attempt+'\\n')\n"
        "if name=='b' and (attempt=='1' or mode=='always'):\n"
        " pathlib.Path(ready).write_text(str(os.getpid()))\n"
        " while not pathlib.Path(release).exists():time.sleep(0.02)\n"
        "value=name+':'+','.join(pathlib.Path(p).read_text() for p in inputs)\n"
        "if name=='b' and attempt=='1' and mode=='late':value='b:late'\n"
        "p=pathlib.Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)\n"
        "if name=='b' and attempt=='1':pathlib.Path(done).write_text(value)\n"
    )

    def task(name: str, *, deps=(), inputs=()):
        return {
            "command": [
                "python",
                "task.py",
                name,
                f"out/{name}.txt",
                str(ready),
                str(release),
                str(done),
                str(launches),
                mode,
                *inputs,
            ],
            "deps": list(deps),
            "inputs": ["task.py", *inputs],
            "outputs": [f"out/{name}.txt"],
        }

    path = root / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": {
                    "a": task("a"),
                    "b": task("b", deps=("a",), inputs=("out/a.txt",)),
                    "c": task("c", deps=("b",), inputs=("out/b.txt",)),
                },
            }
        )
    )
    return path, ready, release, done, launches


def invoke(root: Path, *args: str):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(RUNNER), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def current_run(root: Path) -> str:
    with closing(sqlite3.connect(root / ".repro/state.sqlite3")) as connection:
        return connection.execute(
            "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]


def wait_for(path: Path, process: subprocess.Popen[str], *, timeout=15) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), process.communicate(timeout=3)


def output_hashes(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / f"out/{name}.txt").read_bytes()).hexdigest()
        for name in "abc"
    }


def kill_known_child(ready: Path) -> None:
    if ready.exists():
        pid = int(ready.read_text())
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def cleanup_known_child(ready: Path) -> None:
    if not ready.exists():
        return
    pid = int(ready.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    kill_known_child(ready)


@pytest.mark.parametrize("method,use_cache", [("sigint", True), ("sigkill", False)])
def test_interrupted_run_retains_completed_work_and_retries_new_attempt(
    tmp_path: Path, method: str, use_cache: bool
) -> None:
    workspace = tmp_path / "workspace"
    path, ready, release, _, launches = fixture(workspace, mode="normal")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [
            str(RUNNER),
            "run",
            str(path),
            "--workers",
            "1",
            *([] if use_cache else ["--no-cache"]),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    old_child_cleaned = False
    try:
        wait_for(ready, process)
        identity = current_run(workspace)
        with closing(
            sqlite3.connect(
                workspace / ".repro/state.sqlite3", timeout=1, isolation_level=None
            )
        ) as probe:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
        if method == "sigint":
            process.send_signal(signal.SIGINT)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == (130 if method == "sigint" else -signal.SIGKILL), (
            stdout,
            stderr,
        )
        if method == "sigkill":
            kill_known_child(ready)
            old_child_cleaned = True
        before = invoke(workspace, "status", identity)
        assert before.returncode == 0, before.stderr
        assert "a: succeeded" in before.stdout
        assert "b: " in before.stdout
        assert (
            "Recorded outcome: " + ("interrupted" if method == "sigint" else "running")
            in before.stdout
        )
        resumed = invoke(workspace, "resume", identity)
        assert resumed.returncode == 0, resumed.stderr
        assert f"Run ID: {identity}" in resumed.stdout
        assert "a: retained (succeeded)" in resumed.stdout
        assert "executed=2, retained=1" in resumed.stdout
        assert launches.read_text().splitlines() == ["a:1", "b:1", "b:2", "c:1"]
        reference = tmp_path / "reference"
        ref_path, _, ref_release, _, _ = fixture(reference, mode="normal")
        ref_release.write_text("go")
        fresh = invoke(tmp_path, "run", str(ref_path), "--workers", "1", "--no-cache")
        assert fresh.returncode == 0, fresh.stderr
        assert output_hashes(workspace) == output_hashes(reference)
        with closing(sqlite3.connect(workspace / ".repro/state.sqlite3")) as connection:
            attempts = connection.execute(
                "SELECT task_id,attempt_no,state FROM attempts WHERE run_id=? ORDER BY task_id,attempt_no",
                (identity,),
            ).fetchall()
            assert attempts == [
                ("a", 1, "succeeded"),
                ("b", 1, "interrupted"),
                ("b", 2, "succeeded"),
                ("c", 1, "succeeded"),
            ]
            workers = connection.execute(
                "SELECT workers FROM invocations WHERE run_id=? ORDER BY invocation_no",
                (identity,),
            ).fetchall()
            assert workers == [(1,), (1,)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if not use_cache:
            assert not (workspace / ".repro/cache").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        if method == "sigkill" and not old_child_cleaned:
            kill_known_child(ready)


def test_old_attempt_writes_after_resume_without_changing_published_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    path, ready, release, done, launches = fixture(workspace, mode="late")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [str(RUNNER), "run", str(path), "--workers", "1"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        wait_for(ready, process)
        identity = current_run(workspace)
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL
        resumed = invoke(workspace, "resume", identity)
        assert resumed.returncode == 0, resumed.stderr
        assert "executed=2, retained=1" in resumed.stdout
        before = output_hashes(workspace)
        with closing(sqlite3.connect(workspace / ".repro/state.sqlite3")) as connection:
            key = connection.execute(
                "SELECT key FROM attempts WHERE run_id=? AND task_id='b' AND attempt_no=2",
                (identity,),
            ).fetchone()[0]
        cache_artifact = workspace / ".repro/cache/v1" / key / "files/out/b.txt"
        cache_before = hashlib.sha256(cache_artifact.read_bytes()).hexdigest()
        release.write_text("go")
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert done.exists() and done.read_text() == "b:late"
        old_work = workspace / f".repro/runs/{identity}/attempts/b/1/work/out/b.txt"
        assert old_work.read_text() == "b:late"
        assert output_hashes(workspace) == before
        assert hashlib.sha256(cache_artifact.read_bytes()).hexdigest() == cache_before
        assert launches.read_text().splitlines() == ["a:1", "b:1", "b:2", "c:1"]
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        cleanup_known_child(ready)


def test_second_resume_cannot_change_owned_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path, ready, release, _, _ = fixture(workspace, mode="always")
    release.write_text("go")
    first = invoke(tmp_path, "run", str(path), "--no-cache", "--workers", "1")
    assert first.returncode == 0, first.stderr
    identity = first.stdout.split("Run ID: ", 1)[1].splitlines()[0]
    release.unlink()
    ready.unlink()
    (workspace / "out/b.txt").unlink()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    active = subprocess.Popen(
        [str(RUNNER), "resume", identity],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        wait_for(ready, active)
        with closing(sqlite3.connect(workspace / ".repro/state.sqlite3")) as connection:
            before = (
                connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            )
        competing = invoke(workspace, "resume", identity)
        assert competing.returncode == 3
        assert "already owned" in competing.stderr
        competing_run = invoke(tmp_path, "run", str(path))
        assert competing_run.returncode == 3
        assert "already owned" in competing_run.stderr
        with closing(sqlite3.connect(workspace / ".repro/state.sqlite3")) as connection:
            after = (
                connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            )
        assert after == before
        release.write_text("go")
        stdout, stderr = active.communicate(timeout=12)
        assert active.returncode == 0, (stdout, stderr)
    finally:
        if active.poll() is None:
            os.killpg(active.pid, signal.SIGKILL)
            active.communicate(timeout=5)
            kill_known_child(ready)
