"""Synchronized abrupt stops at launch, publication, and commit boundaries."""

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


def fixture(root: Path, *, outputs: tuple[str, ...]) -> tuple[Path, Path]:
    launches = root / "launches"
    (root / "task.py").write_text(
        "import pathlib,sys\n"
        f"with open({str(launches)!r},'a') as f:f.write('build\\n')\n"
        "for path in sys.argv[1:]:\n"
        " target=pathlib.Path(path);target.parent.mkdir(parents=True,exist_ok=True)\n"
        " target.write_text(path+' verified')\n"
    )
    path = root / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": {
                    "build": {
                        "command": ["python", "task.py", *outputs],
                        "inputs": ["task.py"],
                        "outputs": list(outputs),
                    }
                },
            }
        )
    )
    return path, launches


def cli(root: Path, *args: str):
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


def wrapper(root: Path, mode: str, workflow: Path, marker: Path) -> Path:
    path = root / "pause.py"
    path.write_text(
        "import os,pathlib,sys,threading\n"
        "from repro_runner import cache,executor\n"
        "from repro_runner.executor import ProcessOwner\n"
        "from repro_runner.state import StateStore\n"
        "from repro_runner.cli import app\n"
        f"mode={mode!r}\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "def pause():\n"
        " marker.write_text('reached')\n"
        " threading.Event().wait(30)\n"
        "if mode=='before_launch':\n"
        " original=ProcessOwner.launch\n"
        " async def launch(self,*args):\n"
        "  pause()\n"
        "  return await original(self,*args)\n"
        " ProcessOwner.launch=launch\n"
        "elif mode=='before_cache_rename':\n"
        " original=cache.CacheStorage.publish\n"
        " async def publish(self,*args):\n"
        "  pause()\n"
        "  return await original(self,*args)\n"
        " cache.CacheStorage.publish=publish\n"
        "elif mode=='during_restore':\n"
        " original=executor.os.replace\n"
        " def replace(source,destination):\n"
        "  result=original(source,destination)\n"
        "  if pathlib.Path(destination).name=='one.txt':pause()\n"
        "  return result\n"
        " executor.os.replace=replace\n"
        "elif mode=='during_transaction':\n"
        " original=StateStore.create_or_open\n"
        " def create(cls,runtime):\n"
        "  store=original(runtime)\n"
        "  store.connection.create_function('pause_sql',0,pause)\n"
        "  store.connection.execute(\"CREATE TEMP TRIGGER pause_success AFTER UPDATE OF state ON main.attempts WHEN NEW.state='succeeded' BEGIN SELECT pause_sql(); END\")\n"
        "  return store\n"
        " StateStore.create_or_open=classmethod(create)\n"
        "else:\n"
        " original=StateStore.record_result\n"
        " def record(self,result):\n"
        "  if result.task_id=='build' and result.state=='succeeded' and mode!='after_sqlite_commit':pause()\n"
        "  value=original(self,result)\n"
        "  if result.task_id=='build' and result.state=='succeeded' and mode=='after_sqlite_commit':pause()\n"
        "  return value\n"
        " StateStore.record_result=record\n"
        f"sys.argv=['runner','run',{str(workflow)!r}]"
        "+(['--no-cache'] if mode=='no_cache_before_commit' else [])\n"
        "app()\n"
    )
    return path


def latest_run(root: Path) -> str:
    with closing(sqlite3.connect(root / ".repro/state.sqlite3")) as connection:
        return connection.execute(
            "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("before_launch", "execute"),
        ("before_cache_rename", "execute"),
        ("after_cache_rename", "cached"),
        ("during_restore", "cached"),
        ("after_sqlite_commit", "retained"),
        ("no_cache_before_commit", "execute"),
        ("during_transaction", "cached"),
    ],
)
def test_kill_at_boundary_then_status_and_resume(
    tmp_path: Path, mode: str, expected: str
) -> None:
    outputs = (
        ("out/one.txt", "out/two.txt") if mode == "during_restore" else ("out/one.txt",)
    )
    workflow, launches = fixture(tmp_path, outputs=outputs)
    if mode == "during_restore":
        warm = cli(tmp_path.parent, "run", str(workflow))
        assert warm.returncode == 0, (warm.stdout, warm.stderr)
        for name in outputs:
            (tmp_path / name).unlink()
    marker = tmp_path / "boundary"
    pause = wrapper(tmp_path, mode, workflow, marker)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [sys.executable, str(pause)],
        cwd=tmp_path.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while (
            not marker.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert marker.exists(), process.communicate(timeout=3)
        identity = latest_run(tmp_path)
        with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
            state = connection.execute(
                "SELECT state,launch_state FROM attempts WHERE run_id=? AND task_id='build' ORDER BY attempt_no DESC LIMIT 1",
                (identity,),
            ).fetchone()
            assert state[0] == (
                "succeeded" if mode == "after_sqlite_commit" else "running"
            )
            if mode == "before_launch":
                assert state[1] == "starting"
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL, (stdout, stderr)
        if mode == "during_transaction":
            journal = tmp_path / ".repro/state.sqlite3-journal"
            assert journal.is_file() and journal.stat().st_size > 512
        recorded = cli(tmp_path, "status", identity)
        if mode == "during_transaction" and recorded.returncode == 3:
            assert "recovery" in recorded.stderr.lower()
        else:
            assert recorded.returncode == 0, recorded.stderr
            assert "Recorded outcome: running" in recorded.stdout
        if mode == "before_launch":
            assert "Historical uncertain launches: 1" in recorded.stdout
        resumed = cli(tmp_path, "resume", identity)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert f"Run ID: {identity}" in resumed.stdout
        assert "Invocation: 2" in resumed.stdout
        if expected == "execute":
            assert "executed=1, retained=0" in resumed.stdout
        elif expected == "cached":
            assert "executed=0, retained=0, cached=1" in resumed.stdout
        else:
            assert "executed=0, retained=1" in resumed.stdout
        for name in outputs:
            assert (tmp_path / name).read_text() == f"{name} verified"
        with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
            attempts = connection.execute(
                "SELECT attempt_no,state FROM attempts WHERE run_id=? ORDER BY attempt_no",
                (identity,),
            ).fetchall()
            assert [row[0] for row in attempts] == (
                [1] if expected == "retained" else [1, 2]
            )
            assert attempts[0][1] == (
                "succeeded" if expected == "retained" else "interrupted"
            )
            if expected == "cached":
                assert attempts[-1][1] == "cached"
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        expected_launches = (
            1 if mode == "before_launch" or expected in {"cached", "retained"} else 2
        )
        assert launches.read_text().splitlines() == ["build"] * expected_launches
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
