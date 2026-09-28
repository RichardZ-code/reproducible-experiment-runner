"""Committed snapshots, atomic publication, and reconstruction checks."""

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repro_runner import manifest as publication
from repro_runner.cli import app
from repro_runner.config import load_workflow
from repro_runner.errors import ValidationError
from repro_runner.scheduler import run_workflow
from repro_runner.state import StateStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "simulation"
RUNNER = Path(sys.prefix) / "bin" / "runner"


def example(tmp_path: Path) -> Path:
    target = tmp_path / "example"
    shutil.copytree(
        EXAMPLE, target, ignore=shutil.ignore_patterns(".repro", "out", "__pycache__")
    )
    return target


def cli(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(RUNNER), *args],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def identity(result: subprocess.CompletedProcess[str]) -> str:
    return next(
        line.split(": ", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("Run ID: ")
    )


def manifest_path(workspace: Path, run_id: str) -> Path:
    return workspace / ".repro/runs" / run_id / "manifest.json"


def test_manifest_projection_uses_stored_facts_after_files_change(
    tmp_path: Path,
) -> None:
    workspace = example(tmp_path)
    result = cli(workspace, "run", "workflow.yaml", "--no-cache")
    assert result.returncode == 0, result.stderr
    run_id = identity(result)
    original = json.loads(manifest_path(workspace, run_id).read_text())
    (workspace / "generate.py").write_text("changed source\n")
    (workspace / "out/summary.json").unlink()
    state = StateStore.open_existing(workspace / ".repro", readonly=True)
    try:
        rebuilt = publication.build_manifest(state.manifest_snapshot(run_id))
        with sqlite3.connect(workspace / ".repro/state.sqlite3") as connection:
            row = connection.execute(
                "SELECT workflow_hash FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            recorded = connection.execute(
                "SELECT sha256 FROM artifacts WHERE run_id=? AND task_id='summarize'",
                (run_id,),
            ).fetchone()
        assert rebuilt["run"]["workflow_sha256"] == row[0]
        assert (
            rebuilt["tasks"]["summarize"]["attempts"][0]["artifacts"][0]["sha256"]
            == recorded[0]
        )
        assert rebuilt["tasks"] == original["tasks"]
        assert rebuilt["invocations"] == original["invocations"]
    finally:
        state.close()
    before = manifest_path(workspace, run_id).read_bytes()
    status = cli(workspace, "status", run_id)
    assert (
        status.returncode == 0
        and manifest_path(workspace, run_id).read_bytes() == before
    )


def test_atomic_publication_preserves_prior_file_on_write_and_replace_failure(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "runs" / ("a" * 32)
    run_dir.mkdir(parents=True)
    final = run_dir / "manifest.json"
    final.write_text('{"old":true}\n')
    original_fdopen = publication.os.fdopen

    class BrokenWriter:
        def __init__(self, descriptor: int) -> None:
            self.stream = original_fdopen(
                descriptor, "w", encoding="utf-8", newline="\n"
            )

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data: str) -> None:
            self.stream.write(data[:5])
            raise OSError("injected temporary write failure")

    monkeypatch.setattr(
        publication.os,
        "fdopen",
        lambda descriptor, *args, **kwargs: BrokenWriter(descriptor),
    )
    with pytest.raises(OSError, match="temporary write"):
        publication.publish_manifest(run_dir, {"new": True})
    monkeypatch.setattr(publication.os, "fdopen", original_fdopen)
    assert final.read_text() == '{"old":true}\n'
    assert list(run_dir.iterdir()) == [final]
    monkeypatch.setattr(
        publication.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("injected replace failure")),
    )
    with pytest.raises(OSError, match="replace failure"):
        publication.publish_manifest(run_dir, {"new": True})
    assert final.read_text() == '{"old":true}\n'
    assert list(run_dir.iterdir()) == [final]


def test_unsafe_manifest_destination_and_parent_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / ("a" * 32)
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("untouched")
    (run_dir / "manifest.json").symlink_to(outside)
    with pytest.raises(OSError, match="regular file"):
        publication.publish_manifest(run_dir, {"safe": True})
    assert outside.read_text() == "untouched"
    unsafe = tmp_path / "linked"
    unsafe.symlink_to(tmp_path / "runs", target_is_directory=True)
    with pytest.raises(ValidationError, match="real directory"):
        publication.publish_manifest(unsafe / ("b" * 32), {"safe": True})


def test_final_publication_failure_preserves_successful_database(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = example(tmp_path)
    original = publication.write_manifest
    from repro_runner import scheduler

    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected final publication failure")
        return original(*args)

    monkeypatch.setattr(scheduler, "write_manifest", fail_second)
    result = asyncio.run(
        run_workflow(
            load_workflow(workspace / "workflow.yaml"),
            4,
            asyncio.Event(),
            asyncio.Event(),
            False,
        )
    )
    assert result.state == "succeeded" and result.manifest_error is not None
    assert result.manifest_path is None
    with sqlite3.connect(workspace / ".repro/state.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (result.run_id,)
            ).fetchone()[0]
            == "succeeded"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM attempts WHERE run_id=? AND state='succeeded'",
                (result.run_id,),
            ).fetchone()[0]
            == 5
        )
    assert (
        json.loads(manifest_path(workspace, result.run_id).read_text())["run"][
            "outcome"
        ]
        == "running"
    )
    from repro_runner import cli as command_line

    async def report_only(*args):
        return result, None

    monkeypatch.setattr(command_line, "_run_with_signals", report_only)
    reported = CliRunner().invoke(app, ["run", str(workspace / "workflow.yaml")])
    assert reported.exit_code == 3
    assert "Outcome: succeeded" in reported.output
    assert "Manifest publication failed" in reported.output

    async def interrupted_report(*args):
        return result, signal.SIGINT

    monkeypatch.setattr(command_line, "_run_with_signals", interrupted_report)
    interrupted = CliRunner().invoke(app, ["run", str(workspace / "workflow.yaml")])
    assert interrupted.exit_code == 130


def test_initial_publication_failure_prevents_task_admission(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = example(tmp_path)
    from repro_runner import scheduler

    def fail(*args):
        raise OSError("injected initial publication failure")

    monkeypatch.setattr(scheduler, "write_manifest", fail)
    with pytest.raises(OSError, match="initial publication"):
        asyncio.run(
            run_workflow(
                load_workflow(workspace / "workflow.yaml"),
                4,
                asyncio.Event(),
                asyncio.Event(),
                False,
            )
        )
    with sqlite3.connect(workspace / ".repro/state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert (
            connection.execute("SELECT outcome FROM runs").fetchone()[0]
            == "interrupted"
        )


def test_completed_no_cache_resume_regenerates_missing_manifest(tmp_path: Path) -> None:
    workspace = example(tmp_path)
    first = cli(workspace, "run", "workflow.yaml", "--no-cache")
    assert first.returncode == 0, first.stderr
    run_id = identity(first)
    manifest_path(workspace, run_id).unlink()
    resumed = cli(workspace, "resume", run_id)
    assert resumed.returncode == 0, resumed.stderr
    data = json.loads(manifest_path(workspace, run_id).read_text())
    assert data["snapshot"]["invocation_no"] == 2
    assert all(
        row["disposition"] == "retained"
        for row in data["invocations"][1]["resolutions"].values()
    )
    assert all(len(task["attempts"]) == 1 for task in data["tasks"].values())
    assert data["invocations"][1]["git"]["availability"] == "outside_git"


def test_revision_change_preserves_original_attempt_and_retains_content(
    tmp_path: Path,
) -> None:
    workspace = example(tmp_path)
    (workspace / ".gitignore").write_text(".repro/\nout/\n")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(workspace), *args], check=True, capture_output=True
        )

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "first",
    )
    first = cli(workspace, "run", "workflow.yaml", "--no-cache")
    assert first.returncode == 0, first.stderr
    run_id = identity(first)
    initial = json.loads(manifest_path(workspace, run_id).read_text())
    first_sha = initial["invocations"][0]["git"]["commit"]
    assert initial["invocations"][0]["git"]["dirty"] is False
    (workspace / "notes.txt").write_text("unrelated revision\n")
    git("add", "notes.txt")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "second",
    )
    second = cli(workspace, "resume", run_id)
    assert second.returncode == 0, second.stderr
    current = json.loads(manifest_path(workspace, run_id).read_text())
    assert current["invocations"][0]["git"]["commit"] == first_sha
    assert current["invocations"][1]["git"]["commit"] != first_sha
    assert current["invocations"][1]["git"]["dirty"] is False
    assert all(
        row["disposition"] == "retained" and row["selected_attempt_invocation_no"] == 1
        for row in current["invocations"][1]["resolutions"].values()
    )
    assert all(len(task["attempts"]) == 1 for task in current["tasks"].values())


def test_sigkill_before_final_replace_then_resume(tmp_path: Path) -> None:
    workspace = example(tmp_path)
    wrapper = """import asyncio, os, signal
from repro_runner import scheduler
from repro_runner.config import load_workflow
original = scheduler.write_manifest
count = 0
def boundary(*args):
    global count
    count += 1
    if count == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return original(*args)
scheduler.write_manifest = boundary
asyncio.run(scheduler.run_workflow(load_workflow('workflow.yaml'), 4, asyncio.Event(), asyncio.Event(), False))
"""
    stopped = subprocess.run(
        [sys.executable, "-c", wrapper],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert stopped.returncode == -signal.SIGKILL
    with sqlite3.connect(workspace / ".repro/state.sqlite3") as connection:
        run_id, outcome = connection.execute(
            "SELECT run_id,outcome FROM runs"
        ).fetchone()
        assert outcome == "succeeded"
    old = json.loads(manifest_path(workspace, run_id).read_text())
    assert old["snapshot"]["invocation_no"] == 1 and old["run"]["outcome"] == "running"
    resumed = cli(workspace, "resume", run_id)
    assert resumed.returncode == 0, resumed.stderr
    current = json.loads(manifest_path(workspace, run_id).read_text())
    assert current["snapshot"]["invocation_no"] == 2
    assert [item["outcome"] for item in current["invocations"]] == [
        "succeeded",
        "succeeded",
    ]
    assert all(
        row["disposition"] == "retained"
        for row in current["invocations"][1]["resolutions"].values()
    )
