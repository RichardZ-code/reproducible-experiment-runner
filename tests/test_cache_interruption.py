"""Abrupt stops at real store and restore publication boundaries."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

RUNNER = Path(sys.prefix) / "bin/runner"


def fixture(root: Path, *, two_outputs: bool) -> tuple[Path, Path]:
    (root / "task.py").write_text(
        "import pathlib,sys\n"
        "with open(sys.argv[1],'a') as f: f.write('launched\\n')\n"
        "for path in sys.argv[2:]:\n"
        " p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True); "
        "p.write_text(path+' verified')\n"
    )
    marker = root / "launches"
    outputs = ["out/one.txt", "out/two.txt"] if two_outputs else ["out/one.txt"]
    path = root / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": {
                    "build": {
                        "command": ["python", "task.py", str(marker), *outputs],
                        "inputs": ["task.py"],
                        "outputs": outputs,
                    }
                },
            }
        )
    )
    return path, marker


def cli(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RUNNER), "run", str(path)],
        cwd=path.parent.parent,
        capture_output=True,
        text=True,
        timeout=20,
    )


def wrapper(root: Path, mode: str, workflow: Path, signal_file: Path) -> Path:
    source = root / "pause.py"
    source.write_text(
        "import asyncio, os, pathlib, threading, sys\n"
        "from repro_runner import cache, executor\n"
        "from repro_runner.cli import app\n"
        f"mode={mode!r}\n"
        f"marker=pathlib.Path({str(signal_file)!r})\n"
        "def pause():\n"
        " marker.write_text('boundary reached')\n"
        " threading.Event().wait(30)\n"
        "if mode=='before_rename':\n"
        " original=cache.os.rename\n"
        " def rename(source,destination):\n"
        "  if pathlib.Path(source).name.startswith('.tmp-') "
        "and pathlib.Path(destination).name != pathlib.Path(source).name: pause()\n"
        "  return original(source,destination)\n"
        " cache.os.rename=rename\n"
        "elif mode=='after_rename':\n"
        " original=cache.CacheStorage.publish\n"
        " async def publish(self,*args):\n"
        "  result=await original(self,*args)\n"
        "  pause()\n"
        "  return result\n"
        " cache.CacheStorage.publish=publish\n"
        "else:\n"
        " original=executor.os.replace\n"
        " def replace(source,destination):\n"
        "  result=original(source,destination)\n"
        "  if pathlib.Path(destination).name=='one.txt': pause()\n"
        "  return result\n"
        " executor.os.replace=replace\n"
        f"sys.argv=['runner','run',{str(workflow)!r}]\n"
        "app()\n"
    )
    return source


@pytest.mark.parametrize("mode", ["before_rename", "after_rename", "during_restore"])
def test_abrupt_stop_then_new_run(tmp_path: Path, mode: str) -> None:
    path, launches = fixture(tmp_path, two_outputs=mode == "during_restore")
    if mode == "during_restore":
        first = cli(path)
        assert first.returncode == 0, (first.stdout, first.stderr)
        for output in (tmp_path / "out").iterdir():
            output.unlink()
    marker = tmp_path / "at_boundary"
    script = wrapper(tmp_path, mode, path, marker)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=tmp_path.parent,
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
        assert process.poll() is None
        if mode == "before_rename":
            cache_root = tmp_path / ".repro/cache/v1"
            assert list(cache_root.glob(".tmp-*"))
            assert not [p for p in cache_root.iterdir() if len(p.name) == 64]
        elif mode == "after_rename":
            assert [
                p for p in (tmp_path / ".repro/cache/v1").iterdir() if len(p.name) == 64
            ]
        else:
            assert (tmp_path / "out/one.txt").is_file()
            assert not (tmp_path / "out/two.txt").exists()
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL, (stdout, stderr)
        resumed = cli(path)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        if mode == "before_rename":
            assert "executed=1, cached=0" in resumed.stdout
            assert launches.read_text().splitlines() == ["launched", "launched"]
        else:
            assert "executed=0, cached=1" in resumed.stdout
            assert launches.read_text().splitlines() == ["launched"]
        declared = ("one.txt", "two.txt") if mode == "during_restore" else ("one.txt",)
        for name in declared:
            output = tmp_path / "out" / name
            assert output.read_text() == f"out/{output.name} verified"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
