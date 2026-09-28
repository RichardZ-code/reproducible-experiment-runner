"""On-disk schema, transaction, and read-only opening checks."""

import sqlite3
from pathlib import Path

import pytest
import yaml

from repro_runner.config import Task, load_workflow
from repro_runner.executor import TaskResult
from repro_runner.hashing import Artifact, task_identity
from repro_runner.state import InvalidState, StateNotFound, StateStore


def workflow(root: Path):
    (root / "task.py").write_text("print('fixture')\n")
    path = root / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": {
                    "a": {
                        "command": ["python", "task.py"],
                        "inputs": ["task.py"],
                        "outputs": ["out/a.txt"],
                    }
                },
            }
        )
    )
    return load_workflow(path)


def started(tmp_path: Path):
    runtime = tmp_path / ".repro"
    runtime.mkdir()
    state = StateStore.create_or_open(runtime)
    state.start_run(
        workflow(tmp_path), "a" * 32, 2, True, {"test": "environment"}, "start"
    )
    return state


def test_schema_reopen_foreign_keys_and_explicit_transactions(tmp_path: Path) -> None:
    state = started(tmp_path)
    connection = state.connection
    assert connection.autocommit is True
    assert not connection.in_transaction
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
            ("a" * 32, "missing", 1, "out/x", "0" * 64, 1),
        )
    assert not connection.in_transaction
    state.close()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    writable = StateStore.open_existing(tmp_path / ".repro", readonly=False)
    try:
        assert writable.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert writable.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        writable.close()
    reopened = StateStore.open_existing(tmp_path / ".repro", readonly=True)
    try:
        run, invocations, tasks, artifacts, uncertain = reopened.status("a" * 32)
        assert run["outcome"] == "running"
        assert len(invocations) == 1 and len(tasks) == 1
        assert artifacts == [] and uncertain == 0
        assert not reopened.connection.in_transaction
    finally:
        reopened.close()


def test_success_artifacts_and_selection_rollback_as_one_unit(tmp_path: Path) -> None:
    state = started(tmp_path)
    try:
        number = state.allocate_attempt("a", ("python", "task.py"), "start")
        assert number == 1
        identity = task_identity(
            Task("a", ("python", "task.py"), (), ("task.py",), ("out/a.txt",)),
            {"task.py": Artifact("2" * 64, 4)},
            [],
            {"test": "environment"},
        )
        state.record_identity(
            "a",
            number,
            identity,
            {"task.py": Artifact("2" * 64, 4)},
            [],
            {"test": "environment"},
        )
        state.connection.execute(
            "CREATE TRIGGER fail_artifact BEFORE INSERT ON artifacts "
            "BEGIN SELECT RAISE(ABORT, 'injected artifact failure'); END"
        )
        result = TaskResult(
            "a",
            "succeeded",
            attempt_no=number,
            launched=True,
            cache_key=identity.key,
            artifacts={"out/a.txt": Artifact("1" * 64, 3)},
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected artifact failure"):
            state.record_result(result)
        assert not state.connection.in_transaction
        assert (
            state.connection.execute(
                "SELECT state FROM attempts WHERE run_id=? AND task_id='a'",
                ("a" * 32,),
            ).fetchone()[0]
            == "running"
        )
        assert (
            state.connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            == 0
        )
        assert (
            state.connection.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
            == 0
        )
        state.connection.execute("DROP TRIGGER fail_artifact")
        state.record_result(result)
        assert state.connection.execute(
            "SELECT state,selected_attempt_no FROM tasks"
        ).fetchone()[:] == ("succeeded", 1)
        assert state.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert state.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        state.close()


def test_existing_unversioned_and_unsupported_schema_rejected(tmp_path: Path) -> None:
    runtime = tmp_path / ".repro"
    runtime.mkdir()
    path = runtime / "state.sqlite3"
    sqlite3.connect(path).close()
    with pytest.raises(InvalidState, match="not a supported SQLite database"):
        StateStore.create_or_open(runtime)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value INTEGER)")
    with pytest.raises(InvalidState, match="unsupported state schema"):
        StateStore.create_or_open(runtime)
    path.unlink()
    state = StateStore.create_or_open(runtime)
    state.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(InvalidState, match="unsupported state schema"):
        StateStore.open_existing(runtime, readonly=True)


def test_missing_or_unsafe_state_does_not_initialize(tmp_path: Path) -> None:
    runtime = tmp_path / ".repro"
    with pytest.raises(StateNotFound, match="no .repro"):
        StateStore.open_existing(runtime, readonly=True)
    assert not runtime.exists()
    runtime.mkdir()
    with pytest.raises(StateNotFound, match="no state.sqlite3"):
        StateStore.open_existing(runtime, readonly=True)
    assert list(runtime.iterdir()) == []
    sentinel = tmp_path / "outside"
    sentinel.write_text("untouched")
    (runtime / "state.sqlite3").symlink_to(sentinel)
    with pytest.raises(InvalidState, match="not a regular file"):
        StateStore.open_existing(runtime, readonly=True)
    assert sentinel.read_text() == "untouched"


def test_new_state_rejects_dangling_journal_link(tmp_path: Path) -> None:
    runtime = tmp_path / ".repro"
    runtime.mkdir()
    (runtime / "state.sqlite3-journal").symlink_to(tmp_path / "absent")
    with pytest.raises(InvalidState, match="sidecar exists without"):
        StateStore.create_or_open(runtime)
    assert not (runtime / "state.sqlite3").exists()
