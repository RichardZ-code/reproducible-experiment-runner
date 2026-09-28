"""Separate-process status and resume behavior for committed work."""

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from repro_runner import executor, scheduler
from repro_runner.config import load_workflow

RUNNER = Path(sys.prefix) / "bin/runner"


def script(root: Path, marker: Path) -> None:
    (root / "task.py").write_text(
        "import pathlib,sys\n"
        "name,out,mode,*inputs=sys.argv[1:]\n"
        f"with open({str(marker)!r},'a') as f:f.write(name+'\\n')\n"
        "value='|'.join(pathlib.Path(p).read_text() for p in inputs)\n"
        "if mode=='constant':value='same'\n"
        "if mode=='controlled' and value!='ok':\n"
        " pathlib.Path(out).parent.mkdir(parents=True,exist_ok=True)\n"
        " pathlib.Path(out).write_text('partial')\n"
        " sys.exit(7)\n"
        "p=pathlib.Path(out);p.parent.mkdir(parents=True,exist_ok=True)\n"
        "p.write_text(name+':'+value)\n"
    )


def task(name: str, *inputs: str, deps=(), mode="copy") -> dict:
    return {
        "command": ["python", "task.py", name, f"out/{name}.txt", mode, *inputs],
        "deps": list(deps),
        "inputs": ["task.py", *inputs],
        "outputs": [f"out/{name}.txt"],
    }


def workflow(root: Path, tasks: dict) -> Path:
    path = root / "workflow.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "tasks": tasks}))
    return path


def cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(RUNNER), *args],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )


def run_id(completed: subprocess.CompletedProcess[str]) -> str:
    assert "Run ID: " in completed.stdout, (completed.stdout, completed.stderr)
    return completed.stdout.split("Run ID: ", 1)[1].splitlines()[0]


def db(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / ".repro/state.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_status_is_separate_read_only_query_without_workflow_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    first = cli(tmp_path, "run", str(path))
    assert first.returncode == 0, first.stderr
    identity = run_id(first)
    database = workspace / ".repro/state.sqlite3"
    before = database.read_bytes()
    path.rename(workspace / "workflow.hidden")
    status = cli(workspace, "status", identity)
    assert status.returncode == 0, status.stderr
    assert f"Run ID: {identity}" in status.stdout
    assert "Recorded outcome: succeeded" in status.stdout
    assert (
        "a: succeeded; disposition=executed; selected_attempt=1; latest_attempt=1"
        in status.stdout
    )
    assert "input: task.py sha256=" in status.stdout
    assert (
        f"artifact a: out/a.txt sha256={sha(workspace / 'out/a.txt')}" in status.stdout
    )
    assert f".repro/runs/{identity}/attempts/a/1/stdout.log" in status.stdout
    assert database.read_bytes() == before
    assert marker.read_text().splitlines() == ["a"]
    unknown = cli(workspace, "status", "f" * 32)
    assert unknown.returncode == 2 and "unknown run ID" in unknown.stderr
    assert database.read_bytes() == before


@pytest.mark.parametrize("use_cache", [True, False])
def test_completed_resume_retains_then_restores_or_executes_missing_output(
    tmp_path: Path, use_cache: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    first = cli(tmp_path, "run", str(path), *([] if use_cache else ["--no-cache"]))
    assert first.returncode == 0, first.stderr
    identity = run_id(first)
    original = sha(workspace / "out/a.txt")
    second = cli(workspace, "resume", identity, "--workers", "1")
    assert second.returncode == 0, second.stderr
    assert run_id(second) == identity
    assert "Invocation: 2" in second.stdout
    assert "executed=0, retained=1" in second.stdout
    assert marker.read_text().splitlines() == ["a"]
    (workspace / "out/a.txt").unlink()
    third = cli(workspace, "resume", identity)
    assert third.returncode == 0, third.stderr
    assert "Invocation: 3" in third.stdout
    if use_cache:
        assert "executed=0, retained=0, cached=1" in third.stdout
        assert marker.read_text().splitlines() == ["a"]
    else:
        assert "executed=1, retained=0" in third.stdout
        assert marker.read_text().splitlines() == ["a", "a"]
        assert not (workspace / ".repro/cache").exists()
    assert sha(workspace / "out/a.txt") == original
    with closing(db(workspace)) as connection:
        rows = connection.execute(
            "SELECT attempt_no,state,invocation_no FROM attempts WHERE run_id=? ORDER BY attempt_no",
            (identity,),
        ).fetchall()
        assert [row["attempt_no"] for row in rows] == (
            [1, 2] if not use_cache else [1, 2]
        )
        assert [row["state"] for row in rows] == (
            ["succeeded", "succeeded"] if not use_cache else ["succeeded", "cached"]
        )
        resolutions = connection.execute(
            "SELECT disposition,source_attempt_no FROM resolutions WHERE run_id=? ORDER BY invocation_no",
            (identity,),
        ).fetchall()
        assert [row["disposition"] for row in resolutions] == [
            "executed",
            "retained",
            "cache_restored" if use_cache else "executed",
        ]
        assert resolutions[1]["source_attempt_no"] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_workflow_contract_rejection_and_formatting_equivalence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    first = cli(tmp_path, "run", str(path), "--no-cache")
    assert first.returncode == 0, first.stderr
    identity = run_id(first)
    path.write_text("# equivalent syntax\n" + path.read_text())
    equivalent = cli(workspace, "resume", identity)
    assert equivalent.returncode == 0 and "retained=1" in equivalent.stdout
    with closing(db(workspace)) as connection:
        before = connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
    changed = path.read_text().replace("out/a.txt", "out/different.txt")
    path.write_text(changed)
    rejected = cli(workspace, "resume", identity)
    assert rejected.returncode == 2
    assert "workflow contract changed" in rejected.stderr
    assert marker.read_text().splitlines() == ["a"]
    with closing(db(workspace)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
            == before
        )


def test_explicit_resume_repairs_failure_and_unblocks_descendant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    (workspace / "control.txt").write_text("fail")
    path = workflow(
        workspace,
        {
            "a": task("a", "control.txt", mode="controlled"),
            "b": task("b", "out/a.txt", deps=("a",)),
        },
    )
    failed = cli(tmp_path, "run", str(path), "--no-cache")
    assert failed.returncode == 1
    identity = run_id(failed)
    assert "a: failed" in failed.stdout and "b: blocked" in failed.stdout
    failed_status = cli(workspace, "status", identity)
    assert failed_status.returncode == 0, failed_status.stderr
    assert "Recorded outcome: failed" in failed_status.stdout
    assert "a: failed;" in failed_status.stdout
    assert "b: blocked;" in failed_status.stdout
    (workspace / "control.txt").write_text("ok")
    repaired = cli(workspace, "resume", identity)
    assert repaired.returncode == 0, repaired.stderr
    assert "executed=2, retained=0" in repaired.stdout
    assert marker.read_text().splitlines() == ["a", "a", "b"]
    with closing(db(workspace)) as connection:
        attempts = connection.execute(
            "SELECT task_id,attempt_no,state FROM attempts WHERE run_id=? ORDER BY task_id,attempt_no",
            (identity,),
        ).fetchall()
        assert [
            (row["task_id"], row["attempt_no"], row["state"]) for row in attempts
        ] == [("a", 1, "failed"), ("a", 2, "succeeded"), ("b", 1, "succeeded")]


def test_identical_producer_bytes_allow_downstream_retention(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    (workspace / "input.txt").write_text("first")
    path = workflow(
        workspace,
        {
            "a": task("a", "input.txt", mode="constant"),
            "b": task("b", "out/a.txt", deps=("a",)),
        },
    )
    first = cli(tmp_path, "run", str(path), "--no-cache")
    assert first.returncode == 0
    identity = run_id(first)
    prior = sha(workspace / "out/b.txt")
    (workspace / "input.txt").write_text("second")
    resumed = cli(workspace, "resume", identity)
    assert resumed.returncode == 0, resumed.stderr
    assert (
        "a: succeeded" in resumed.stdout and "b: retained (succeeded)" in resumed.stdout
    )
    assert "executed=1, retained=1" in resumed.stdout
    assert sha(workspace / "out/b.txt") == prior
    assert marker.read_text().splitlines() == ["a", "b", "a"]


def test_script_edit_and_glob_membership_invalidate_resume(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    data = workspace / "data"
    data.mkdir()
    (data / "one.txt").write_text("one")
    declaration = task("a")
    declaration["inputs"].append("data/*.txt")
    path = workflow(workspace, {"a": declaration})
    first = cli(tmp_path, "run", str(path), "--no-cache")
    assert first.returncode == 0, first.stderr
    identity = run_id(first)
    script_path = workspace / "task.py"
    script_path.write_text(script_path.read_text() + "\n# edit\n")
    second = cli(workspace, "resume", identity)
    assert second.returncode == 0 and "executed=1, retained=0" in second.stdout
    (data / "two.txt").write_text("two")
    third = cli(workspace, "resume", identity)
    assert third.returncode == 0 and "executed=1, retained=0" in third.stdout
    (data / "two.txt").unlink()
    fourth = cli(workspace, "resume", identity)
    assert fourth.returncode == 0 and "executed=0, retained=1" in fourth.stdout
    assert marker.read_text().splitlines() == ["a", "a", "a"]


def test_no_cache_resume_ignores_unusable_cache_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    first = cli(tmp_path, "run", str(path), "--no-cache")
    assert first.returncode == 0
    identity = run_id(first)
    sentinel = tmp_path / "outside"
    sentinel.write_text("untouched")
    (workspace / ".repro/cache").symlink_to(sentinel)
    retained = cli(workspace, "resume", identity)
    assert retained.returncode == 0 and "retained=1" in retained.stdout
    (workspace / "out/a.txt").unlink()
    executed = cli(workspace, "resume", identity)
    assert executed.returncode == 0 and "executed=1, retained=0" in executed.stdout
    assert sentinel.read_text() == "untouched"
    assert marker.read_text().splitlines() == ["a", "a"]


def test_stored_unsafe_artifact_path_rejected_without_touching_sentinel(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    first = cli(tmp_path, "run", str(path))
    assert first.returncode == 0
    identity = run_id(first)
    sentinel = tmp_path / "outside"
    sentinel.write_text("untouched")
    with closing(db(workspace)) as connection:
        connection.execute(
            "UPDATE artifacts SET path='../outside' WHERE run_id=?",
            (identity,),
        )
        connection.commit()
    rejected = cli(workspace, "resume", identity)
    assert rejected.returncode == 2 and "artifact inventory" in rejected.stderr
    assert sentinel.read_text() == "untouched"
    assert marker.read_text().splitlines() == ["a"]


def test_source_change_during_retention_is_not_accepted(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    source = workspace / "data.txt"
    source.write_text("first")
    path = workflow(workspace, {"a": task("a", "data.txt")})
    first = asyncio.run(
        scheduler.run_workflow(
            load_workflow(path), 1, asyncio.Event(), asyncio.Event(), False
        )
    )
    assert first.state == "succeeded"
    original = executor._hash
    changed = False

    async def mutate_after_output(path_to_hash):
        nonlocal changed
        value = await original(path_to_hash)
        if path_to_hash.name == "a.txt" and not changed:
            source.write_text("second")
            changed = True
        return value

    monkeypatch.setattr(executor, "_hash", mutate_after_output)
    monkeypatch.chdir(workspace)
    second = asyncio.run(
        scheduler.resume_workflow(first.run_id, None, asyncio.Event(), asyncio.Event())
    )
    assert second.state == "failed"
    assert second.tasks["a"].state == "failed"
    assert "input source changed during retention" in second.tasks["a"].reason
    assert not second.tasks["a"].retained and not second.tasks["a"].launched
    assert marker.read_text().splitlines() == ["a"]


def test_current_environment_identity_recomputed_on_resume(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    selected = asyncio.run(executor.environment_record())
    selected["python_version"] = "A"

    async def chosen_environment():
        return dict(selected)

    monkeypatch.setattr(scheduler, "environment_record", chosen_environment)
    monkeypatch.setattr(executor, "environment_record", chosen_environment)
    first = asyncio.run(
        scheduler.run_workflow(
            load_workflow(path), 1, asyncio.Event(), asyncio.Event(), False
        )
    )
    assert first.state == "succeeded"
    selected["python_version"] = "B"
    monkeypatch.chdir(workspace)
    changed = asyncio.run(
        scheduler.resume_workflow(first.run_id, None, asyncio.Event(), asyncio.Event())
    )
    assert changed.state == "succeeded"
    assert changed.tasks["a"].launched and not changed.tasks["a"].retained
    assert changed.tasks["a"].cache_key != first.tasks["a"].cache_key
    selected["python_version"] = "A"
    returned = asyncio.run(
        scheduler.resume_workflow(first.run_id, None, asyncio.Event(), asyncio.Event())
    )
    assert returned.tasks["a"].retained
    assert not returned.tasks["a"].launched
    assert marker.read_text().splitlines() == ["a", "a"]


def test_another_run_overwriting_output_does_not_authorize_old_retention(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    source = workspace / "data.txt"
    source.write_text("A")
    path = workflow(workspace, {"a": task("a", "data.txt")})
    first = cli(tmp_path, "run", str(path))
    assert first.returncode == 0
    old_run = run_id(first)
    original_hash = sha(workspace / "out/a.txt")
    source.write_text("B")
    second = cli(tmp_path, "run", str(path))
    assert second.returncode == 0
    new_hash = sha(workspace / "out/a.txt")
    assert new_hash != original_hash
    resumed = cli(workspace, "resume", old_run)
    assert resumed.returncode == 0, resumed.stderr
    assert "retained=0" in resumed.stdout
    assert sha(workspace / "out/a.txt") == new_hash
    assert marker.read_text().splitlines() == ["a", "a"]


def test_corrupt_cache_during_resume_is_quarantined_and_recomputed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launches"
    script(workspace, marker)
    path = workflow(workspace, {"a": task("a")})
    first = cli(tmp_path, "run", str(path))
    assert first.returncode == 0, first.stderr
    identity = run_id(first)
    with closing(db(workspace)) as connection:
        key = connection.execute(
            "SELECT key FROM attempts WHERE run_id=? AND task_id='a'", (identity,)
        ).fetchone()[0]
    entry = workspace / ".repro/cache/v1" / key
    (entry / "files/out/a.txt").write_text("corrupted")
    (workspace / "out/a.txt").unlink()
    resumed = cli(workspace, "resume", identity)
    assert resumed.returncode == 0, resumed.stderr
    assert "executed=1, retained=0, cached=0" in resumed.stdout
    assert marker.read_text().splitlines() == ["a", "a"]
    assert any(entry.parent.glob(f".bad-{key}-*"))
    with closing(db(workspace)) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT attempt_no,state FROM attempts WHERE run_id=? ORDER BY attempt_no",
                (identity,),
            )
        ] == [(1, "succeeded"), (2, "succeeded")]
