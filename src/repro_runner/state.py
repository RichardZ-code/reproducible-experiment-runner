"""Versioned SQLite records for one owned workflow workspace."""

import hashlib
import json
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from repro_runner.config import Workflow
from repro_runner.hashing import (
    Artifact,
    CacheIdentity,
    canonical_bytes,
    regular_reader,
)
from repro_runner.paths import check_declaration

SCHEMA_VERSION = 1
RUN_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
TASK_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)


class StateNotFound(Exception):
    """No requested run or database exists in this workspace."""


class InvalidState(Exception):
    """Stored state is unsupported or violates its declared contract."""


class RecoveryRequired(Exception):
    """A read-only query cannot perform SQLite journal recovery."""


class StateCommitUncertain(Exception):
    """The caller cannot establish whether a SQLite commit took effect."""


SCHEMA = (
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        workflow_file TEXT NOT NULL,
        workflow_json TEXT NOT NULL,
        workflow_hash TEXT NOT NULL,
        cache_enabled INTEGER NOT NULL CHECK (cache_enabled IN (0, 1)),
        initial_workers INTEGER NOT NULL CHECK (initial_workers >= 1),
        latest_invocation INTEGER NOT NULL CHECK (latest_invocation >= 1),
        outcome TEXT NOT NULL CHECK (outcome IN ('running','succeeded','failed','interrupted')),
        reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE invocations (
        run_id TEXT NOT NULL,
        invocation_no INTEGER NOT NULL CHECK (invocation_no >= 1),
        workers INTEGER NOT NULL CHECK (workers >= 1),
        environment_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_seconds REAL,
        outcome TEXT NOT NULL CHECK (outcome IN ('running','succeeded','failed','interrupted')),
        reason TEXT,
        PRIMARY KEY (run_id, invocation_no),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )""",
    """CREATE TABLE tasks (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','running','succeeded','cached','failed','blocked','interrupted')),
        selected_attempt_no INTEGER,
        reason TEXT,
        PRIMARY KEY (run_id, task_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id),
        FOREIGN KEY (run_id, task_id, selected_attempt_no)
            REFERENCES attempts(run_id, task_id, attempt_no)
    )""",
    """CREATE TABLE attempts (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
        invocation_no INTEGER NOT NULL,
        operation TEXT NOT NULL CHECK (operation IN ('prepare','restore','execute')),
        state TEXT NOT NULL CHECK (state IN ('running','succeeded','cached','failed','interrupted')),
        launch_state TEXT NOT NULL CHECK (launch_state IN ('not_started','starting','started')),
        command_json TEXT NOT NULL,
        key TEXT,
        identity_json TEXT,
        inputs_json TEXT,
        dependency_json TEXT,
        environment_json TEXT,
        attempt_path TEXT NOT NULL,
        stdout_path TEXT,
        stderr_path TEXT,
        resolution_kind TEXT,
        exit_code INTEGER,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_seconds REAL,
        reason TEXT,
        error_category TEXT,
        PRIMARY KEY (run_id, task_id, attempt_no),
        FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id),
        FOREIGN KEY (run_id, invocation_no) REFERENCES invocations(run_id, invocation_no)
    )""",
    """CREATE TABLE artifacts (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        PRIMARY KEY (run_id, task_id, attempt_no, path),
        FOREIGN KEY (run_id, task_id, attempt_no)
            REFERENCES attempts(run_id, task_id, attempt_no)
    )""",
    """CREATE TABLE resolutions (
        run_id TEXT NOT NULL,
        invocation_no INTEGER NOT NULL,
        task_id TEXT NOT NULL,
        disposition TEXT NOT NULL CHECK (disposition IN ('executed','cache_restored','retained','no_launch')),
        state TEXT NOT NULL CHECK (state IN ('succeeded','cached','failed','blocked','interrupted')),
        source_attempt_no INTEGER,
        cache_lookup INTEGER NOT NULL CHECK (cache_lookup IN (0, 1)),
        cache_hit INTEGER NOT NULL CHECK (cache_hit IN (0, 1)),
        reason TEXT,
        PRIMARY KEY (run_id, invocation_no, task_id),
        FOREIGN KEY (run_id, invocation_no) REFERENCES invocations(run_id, invocation_no),
        FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id),
        FOREIGN KEY (run_id, task_id, source_attempt_no)
            REFERENCES attempts(run_id, task_id, attempt_no)
    )""",
)


def workflow_record(workflow: Workflow) -> dict[str, object]:
    return {
        "schema_version": 1,
        "tasks": {
            task_id: {
                "command": list(task.command),
                "deps": sorted(task.deps),
                "inputs": sorted(task.inputs),
                "outputs": sorted(task.outputs),
            }
            for task_id, task in sorted(workflow.tasks.items())
        },
    }


def _validate_workflow_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != {"schema_version", "tasks"}:
        raise InvalidState("stored workflow contract is invalid")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise InvalidState("stored workflow version is unsupported")
    tasks = record["tasks"]
    if not isinstance(tasks, dict) or not tasks:
        raise InvalidState("stored workflow task inventory is invalid")
    for task_id, task in tasks.items():
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise InvalidState("stored task ID is invalid")
        if not isinstance(task, dict) or set(task) != {
            "command",
            "deps",
            "inputs",
            "outputs",
        }:
            raise InvalidState("stored task contract is invalid")
        for field in ("command", "deps", "inputs", "outputs"):
            values = task[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise InvalidState(f"stored task {field} is invalid")
            if field in ("command", "outputs") and not values:
                raise InvalidState(f"stored task {field} is empty")
            if field != "command" and values != sorted(set(values)):
                raise InvalidState(f"stored task {field} is not normalized")
        for path in task["outputs"]:
            try:
                check_declaration(path, allow_pattern=False)
            except Exception as error:
                raise InvalidState("stored output declaration is unsafe") from error
    return record


def _json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _unique_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise InvalidState(f"duplicate stored JSON field {key!r}")
        result[key] = value
    return result


def _decode(value: str) -> object:
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                InvalidState(f"invalid stored JSON constant {item}")
            ),
        )
        if _json(decoded) != value:
            raise InvalidState("stored JSON is not canonical")
        return decoded
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidState("malformed stored JSON") from error


def validate_run_id(run_id: str) -> None:
    if not RUN_ID.fullmatch(run_id):
        raise InvalidState("run ID must be 32 lowercase hexadecimal characters")


def _state_path(runtime: Path) -> Path:
    try:
        info = runtime.lstat()
    except FileNotFoundError as error:
        raise StateNotFound("no .repro state in this directory") from error
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidState(".repro is not a real directory")
    path = runtime / "state.sqlite3"
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise StateNotFound("no state.sqlite3 in this directory") from error
    if not stat.S_ISREG(info.st_mode):
        raise InvalidState("state.sqlite3 is not a regular file")
    with regular_reader(path) as source:
        header = source.read(100)
    if len(header) < 100 or header[:16] != b"SQLite format 3\0":
        raise InvalidState("state.sqlite3 is not a supported SQLite database")
    if header[18:20] != b"\x01\x01":
        raise InvalidState("state.sqlite3 does not use the selected rollback journal")
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = runtime / f"state.sqlite3{suffix}"
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise InvalidState(f"unsafe SQLite sidecar {sidecar.name}")
    return path


def require_existing_runtime(workspace: Path) -> Path:
    runtime = workspace / ".repro"
    _state_path(runtime)
    lock = runtime / "lock"
    try:
        info = lock.lstat()
    except FileNotFoundError as error:
        raise StateNotFound("no existing .repro/lock for resume") from error
    if not stat.S_ISREG(info.st_mode):
        raise InvalidState(".repro/lock is not a regular file")
    return runtime


@contextmanager
def _transaction(connection: sqlite3.Connection, *, write: bool) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        try:
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            raise StateCommitUncertain("SQLite commit outcome is uncertain") from error
    except BaseException:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                # Keep the original write or uncertain-commit error visible.
                pass
        raise


@dataclass(frozen=True)
class PriorResult:
    attempt_no: int
    state: str
    artifacts: dict[str, Artifact]


class StateStore:
    """Single-connection owner. Every mutation has an explicit short transaction."""

    def __init__(self, connection: sqlite3.Connection, *, readonly: bool) -> None:
        self.connection = connection
        self.readonly = readonly
        self.run_id: str | None = None
        self.invocation_no: int | None = None

    @classmethod
    def create_or_open(cls, runtime: Path) -> "StateStore":
        path = runtime / "state.sqlite3"
        try:
            _state_path(runtime)
        except StateNotFound:
            if path.exists() or path.is_symlink():
                raise
            if any(
                (runtime / f"state.sqlite3{suffix}").exists()
                or (runtime / f"state.sqlite3{suffix}").is_symlink()
                for suffix in ("-journal", "-wal", "-shm")
            ):
                raise InvalidState("SQLite sidecar exists without a state database")
            new = True
        else:
            new = False
        mode = "rwc" if new else "rw"
        connection = sqlite3.connect(
            path.as_uri() + f"?mode={mode}",
            uri=True,
            timeout=5,
            autocommit=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise InvalidState("SQLite foreign keys are unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise InvalidState("SQLite full synchronization is unavailable")
            if new:
                if (
                    connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    != "delete"
                ):
                    raise InvalidState("SQLite rollback journal is unavailable")
                with _transaction(connection, write=True):
                    if connection.execute("PRAGMA user_version").fetchone()[0] != 0:
                        raise InvalidState("new state has a nonzero schema version")
                    if connection.execute("SELECT name FROM sqlite_master").fetchone():
                        raise InvalidState("new state contains unknown objects")
                    for statement in SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            store = cls(connection, readonly=False)
            store.validate_schema()
            return store
        except BaseException:
            connection.close()
            raise

    @classmethod
    def open_existing(cls, runtime: Path, *, readonly: bool) -> "StateStore":
        path = _state_path(runtime)
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            timeout=5,
            autocommit=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise InvalidState("SQLite foreign keys are unavailable")
            if not readonly:
                connection.execute("PRAGMA synchronous=FULL")
                if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise InvalidState("SQLite full synchronization is unavailable")
            store = cls(connection, readonly=readonly)
            store.validate_schema()
            return store
        except sqlite3.OperationalError as error:
            connection.close()
            if readonly:
                raise RecoveryRequired(
                    "read-only state may require journal recovery; run an owned resume"
                ) from error
            raise
        except BaseException:
            connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def validate_schema(self) -> None:
        connection = self.connection
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise InvalidState("unsupported state schema version")
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if journal != "delete":
            raise InvalidState(f"unsupported SQLite journal mode {journal!r}")
        actual = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
        }
        expected = {
            statement.split("(", 1)[0].split()[-1]: statement for statement in SCHEMA
        }
        if actual != expected:
            raise InvalidState("state tables differ from schema version 1")
        extras = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE type!='table'"
        ).fetchall()
        if any(
            item["type"] != "index"
            or not item["name"].startswith("sqlite_autoindex_")
            or item["sql"] is not None
            for item in extras
        ):
            raise InvalidState("state contains unsupported schema objects")

    def start_run(
        self,
        workflow: Workflow,
        run_id: str,
        workers: int,
        use_cache: bool,
        environment: Mapping[str, object],
        started_at: str,
    ) -> None:
        validate_run_id(run_id)
        record = _json(workflow_record(workflow))
        digest = hashlib.sha256(record.encode("utf-8")).hexdigest()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    workflow.filename,
                    record,
                    digest,
                    int(use_cache),
                    workers,
                    1,
                    "running",
                    None,
                    started_at,
                    started_at,
                ),
            )
            self.connection.execute(
                "INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    1,
                    workers,
                    _json(environment),
                    started_at,
                    None,
                    None,
                    "running",
                    None,
                ),
            )
            self.connection.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?)",
                [
                    (run_id, task_id, "pending", None, None)
                    for task_id in workflow.graph.order
                ],
            )
        self.run_id = run_id
        self.invocation_no = 1

    def run_header(self, run_id: str) -> sqlite3.Row:
        validate_run_id(run_id)
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise StateNotFound(f"unknown run ID {run_id}")
        name = row["workflow_file"]
        try:
            check_declaration(name, allow_pattern=False)
        except Exception as error:
            raise InvalidState("stored workflow path is unsafe") from error
        if "/" in name:
            raise InvalidState("stored workflow path must be a basename")
        _validate_workflow_record(_decode(row["workflow_json"]))
        if (
            hashlib.sha256(row["workflow_json"].encode("utf-8")).hexdigest()
            != row["workflow_hash"]
        ):
            raise InvalidState("stored workflow hash mismatch")
        if type(row["cache_enabled"]) is not int or row["cache_enabled"] not in (0, 1):
            raise InvalidState("stored cache mode is invalid")
        return row

    def verify_workflow(self, run_id: str, workflow: Workflow) -> sqlite3.Row:
        row = self.run_header(run_id)
        if _json(workflow_record(workflow)) != row["workflow_json"]:
            raise InvalidState("workflow contract changed; start a new run")
        known = {
            item[0]
            for item in self.connection.execute(
                "SELECT task_id FROM tasks WHERE run_id=?", (run_id,)
            )
        }
        if known != set(workflow.tasks):
            raise InvalidState("stored task inventory differs from workflow")
        return row

    def start_resume(
        self,
        run_id: str,
        workers_override: int | None,
        environment: Mapping[str, object],
        started_at: str,
    ) -> tuple[int, int, bool]:
        row = self.run_header(run_id)
        previous = self.connection.execute(
            "SELECT invocation_no, workers FROM invocations WHERE run_id=? ORDER BY invocation_no DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if previous is None or previous["invocation_no"] != row["latest_invocation"]:
            raise InvalidState("run has inconsistent invocation history")
        workers = (
            workers_override if workers_override is not None else previous["workers"]
        )
        if type(workers) is not int or workers < 1:
            raise InvalidState("recorded worker count is invalid")
        number = previous["invocation_no"] + 1
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET state='interrupted', reason='owner lost before completion' "
                "WHERE run_id=? AND state='running'",
                (run_id,),
            )
            self.connection.execute(
                "UPDATE invocations SET outcome='interrupted', reason='owner lost before finalization' "
                "WHERE run_id=? AND outcome='running'",
                (run_id,),
            )
            self.connection.execute(
                "UPDATE tasks SET state='pending', selected_attempt_no=NULL, reason=NULL WHERE run_id=?",
                (run_id,),
            )
            self.connection.execute(
                "INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    number,
                    workers,
                    _json(environment),
                    started_at,
                    None,
                    None,
                    "running",
                    None,
                ),
            )
            self.connection.execute(
                "UPDATE runs SET latest_invocation=?, outcome='running', reason=NULL, updated_at=? WHERE run_id=?",
                (number, started_at, run_id),
            )
        self.run_id = run_id
        self.invocation_no = number
        return number, workers, bool(row["cache_enabled"])

    def _ids(self) -> tuple[str, int]:
        if self.run_id is None or self.invocation_no is None:
            raise RuntimeError("state invocation has not started")
        return self.run_id, self.invocation_no

    def allocate_attempt(
        self, task_id: str, command: tuple[str, ...], started_at: str
    ) -> int:
        run_id, invocation_no = self._ids()
        with _transaction(self.connection, write=True):
            number = self.connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()[0]
            relative = f".repro/runs/{run_id}/attempts/{task_id}/{number}"
            self.connection.execute(
                "INSERT INTO attempts (run_id,task_id,attempt_no,invocation_no,operation,state,launch_state,command_json,attempt_path,started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    task_id,
                    number,
                    invocation_no,
                    "prepare",
                    "running",
                    "not_started",
                    _json(list(command)),
                    relative,
                    started_at,
                ),
            )
            self.connection.execute(
                "UPDATE tasks SET state='running', selected_attempt_no=NULL, reason=NULL WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            )
        return number

    def record_identity(
        self,
        task_id: str,
        number: int,
        identity: CacheIdentity,
        snapshots: Mapping[str, Artifact],
        dependencies: list[dict[str, str]],
        environment: Mapping[str, object],
    ) -> None:
        run_id, _ = self._ids()
        inputs = [
            {"path": path, "sha256": item.digest, "size": item.size}
            for path, item in sorted(snapshots.items())
        ]
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET key=?, identity_json=?, inputs_json=?, dependency_json=?, environment_json=? "
                "WHERE run_id=? AND task_id=? AND attempt_no=?",
                (
                    identity.key,
                    _json(identity.record),
                    _json(inputs),
                    _json(dependencies),
                    _json(environment),
                    run_id,
                    task_id,
                    number,
                ),
            )

    def mark_restore(self, task_id: str, number: int) -> None:
        run_id, _ = self._ids()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET operation='restore' WHERE run_id=? AND task_id=? AND attempt_no=?",
                (run_id, task_id, number),
            )

    def launch_requested(
        self, task_id: str, number: int, stdout: str, stderr: str, resolution_kind: str
    ) -> None:
        run_id, _ = self._ids()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET operation='execute', launch_state='starting', stdout_path=?, stderr_path=?, resolution_kind=? "
                "WHERE run_id=? AND task_id=? AND attempt_no=?",
                (stdout, stderr, resolution_kind, run_id, task_id, number),
            )

    def launch_confirmed(self, task_id: str, number: int) -> None:
        run_id, _ = self._ids()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET launch_state='started' WHERE run_id=? AND task_id=? AND attempt_no=?",
                (run_id, task_id, number),
            )

    def child_exited(self, task_id: str, number: int, exit_code: int) -> None:
        run_id, _ = self._ids()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET exit_code=? WHERE run_id=? AND task_id=? AND attempt_no=?",
                (exit_code, run_id, task_id, number),
            )

    def prior_result(
        self, task_id: str, key: str, outputs: tuple[str, ...]
    ) -> PriorResult | None:
        run_id, _ = self._ids()
        row = self.connection.execute(
            "SELECT attempt_no,state,identity_json FROM attempts WHERE run_id=? AND task_id=? "
            "AND key=? AND state IN ('succeeded','cached') ORDER BY attempt_no DESC LIMIT 1",
            (run_id, task_id, key),
        ).fetchone()
        if row is None:
            return None
        identity = _decode(row["identity_json"])
        if hashlib.sha256(canonical_bytes(identity)).hexdigest() != key:
            raise InvalidState("stored completed attempt identity does not match key")
        entries = self.connection.execute(
            "SELECT path,sha256,size FROM artifacts WHERE run_id=? AND task_id=? AND attempt_no=? ORDER BY path",
            (run_id, task_id, row["attempt_no"]),
        ).fetchall()
        if tuple(item["path"] for item in entries) != tuple(sorted(outputs)):
            raise InvalidState(
                "stored artifact inventory differs from declared outputs"
            )
        artifacts: dict[str, Artifact] = {}
        for item in entries:
            path, digest, size = item["path"], item["sha256"], item["size"]
            try:
                check_declaration(path, allow_pattern=False)
            except Exception as error:
                raise InvalidState("stored artifact path is unsafe") from error
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise InvalidState("stored artifact digest is invalid")
            if type(size) is not int or size < 0:
                raise InvalidState("stored artifact size is invalid")
            artifacts[path] = Artifact(digest, size)
        return PriorResult(row["attempt_no"], row["state"], artifacts)

    def record_result(self, result: object) -> None:
        run_id, invocation_no = self._ids()
        task_id = result.task_id
        attempt_no = result.attempt_no
        if result.state in ("succeeded", "cached"):
            if attempt_no is None or result.cache_key is None:
                raise InvalidState("completed task has no recorded attempt identity")
            stored = self.connection.execute(
                "SELECT key,identity_json FROM attempts WHERE run_id=? AND task_id=? AND attempt_no=?",
                (run_id, task_id, attempt_no),
            ).fetchone()
            if (
                stored is None
                or stored["key"] != result.cache_key
                or stored["identity_json"] is None
            ):
                raise InvalidState("completed task identity differs from its attempt")
            contract = _validate_workflow_record(
                _decode(self.run_header(run_id)["workflow_json"])
            )
            if task_id not in contract["tasks"]:
                raise InvalidState("completed task is outside stored workflow")
            if set(result.artifacts) != set(contract["tasks"][task_id]["outputs"]):
                raise InvalidState("completed artifact set is incomplete")
            for path, artifact in result.artifacts.items():
                if (
                    not DIGEST.fullmatch(artifact.digest)
                    or type(artifact.size) is not int
                    or artifact.size < 0
                ):
                    raise InvalidState(f"invalid completed artifact {path!r}")
        disposition = (
            "cache_restored"
            if result.state == "cached"
            else "executed"
            if result.launched
            else "no_launch"
        )
        with _transaction(self.connection, write=True):
            if attempt_no is not None:
                self.connection.execute(
                    "UPDATE attempts SET state=?, ended_at=?, duration_seconds=?, exit_code=COALESCE(?,exit_code), "
                    "reason=?, error_category=? WHERE run_id=? AND task_id=? AND attempt_no=?",
                    (
                        result.state,
                        result.ended_at,
                        result.duration_seconds,
                        result.exit_code,
                        result.reason,
                        result.error_category,
                        run_id,
                        task_id,
                        attempt_no,
                    ),
                )
                if result.state in ("succeeded", "cached"):
                    self.connection.executemany(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                        [
                            (
                                run_id,
                                task_id,
                                attempt_no,
                                path,
                                artifact.digest,
                                artifact.size,
                            )
                            for path, artifact in sorted(result.artifacts.items())
                        ],
                    )
            selected = attempt_no if result.state in ("succeeded", "cached") else None
            self.connection.execute(
                "UPDATE tasks SET state=?, selected_attempt_no=?, reason=? WHERE run_id=? AND task_id=?",
                (result.state, selected, result.reason, run_id, task_id),
            )
            self.connection.execute(
                "INSERT INTO resolutions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    invocation_no,
                    task_id,
                    disposition,
                    result.state,
                    selected,
                    int(result.cache_lookup),
                    int(result.state == "cached"),
                    result.reason,
                ),
            )

    def record_retention(self, result: object) -> None:
        run_id, invocation_no = self._ids()
        prior = self.connection.execute(
            "SELECT state,key FROM attempts WHERE run_id=? AND task_id=? AND attempt_no=?",
            (run_id, result.task_id, result.source_attempt_no),
        ).fetchone()
        if (
            prior is None
            or prior["state"] != result.state
            or prior["key"] != result.cache_key
        ):
            raise InvalidState("retained source attempt is inconsistent")
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE tasks SET state=?, selected_attempt_no=?, reason=NULL WHERE run_id=? AND task_id=?",
                (result.state, result.source_attempt_no, run_id, result.task_id),
            )
            self.connection.execute(
                "INSERT INTO resolutions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    invocation_no,
                    result.task_id,
                    "retained",
                    result.state,
                    result.source_attempt_no,
                    0,
                    0,
                    None,
                ),
            )

    def finish_invocation(
        self,
        outcome: str,
        ended_at: str,
        duration_seconds: float,
        reason: str | None = None,
    ) -> None:
        run_id, invocation_no = self._ids()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE invocations SET outcome=?,ended_at=?,duration_seconds=?,reason=? WHERE run_id=? AND invocation_no=?",
                (outcome, ended_at, duration_seconds, reason, run_id, invocation_no),
            )
            self.connection.execute(
                "UPDATE runs SET outcome=?,reason=?,updated_at=? WHERE run_id=?",
                (outcome, reason, ended_at, run_id),
            )

    def uncertain_launches(self) -> int:
        run_id, _ = self._ids()
        return self.connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE run_id=? AND launch_state='starting'",
            (run_id,),
        ).fetchone()[0]

    def abort_invocation(self, reason: str) -> None:
        run_id, invocation_no = self._ids()
        with _transaction(self.connection, write=True):
            self.connection.execute(
                "UPDATE attempts SET state='interrupted',reason=? WHERE run_id=? AND state='running'",
                (reason, run_id),
            )
            self.connection.execute(
                "UPDATE tasks SET state='interrupted',reason=? WHERE run_id=? AND state IN ('pending','running')",
                (reason, run_id),
            )
            self.connection.execute(
                "UPDATE invocations SET outcome='interrupted',reason=? WHERE run_id=? AND invocation_no=?",
                (reason, run_id, invocation_no),
            )
            self.connection.execute(
                "UPDATE runs SET outcome='interrupted',reason=? WHERE run_id=?",
                (reason, run_id),
            )

    def status(
        self, run_id: str
    ) -> tuple[
        sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], int
    ]:
        with _transaction(self.connection, write=False):
            run = self.run_header(run_id)
            invocations = self.connection.execute(
                "SELECT * FROM invocations WHERE run_id=? ORDER BY invocation_no",
                (run_id,),
            ).fetchall()
            tasks = self.connection.execute(
                """SELECT t.*, a.state AS displayed_state, a.launch_state, a.exit_code,
                          a.stdout_path, a.stderr_path,
                          a.key, a.identity_json, a.inputs_json,
                          a.attempt_no AS displayed_attempt_no,
                          (SELECT MAX(attempt_no) FROM attempts
                           WHERE run_id=t.run_id AND task_id=t.task_id) AS latest_attempt_no,
                          r.disposition, r.source_attempt_no
                   FROM tasks t
                   LEFT JOIN attempts a ON a.run_id=t.run_id AND a.task_id=t.task_id
                     AND a.attempt_no=COALESCE(t.selected_attempt_no,
                         (SELECT MAX(attempt_no) FROM attempts
                          WHERE run_id=t.run_id AND task_id=t.task_id))
                   LEFT JOIN resolutions r ON r.run_id=t.run_id AND r.task_id=t.task_id
                     AND r.invocation_no=?
                   WHERE t.run_id=? ORDER BY t.task_id""",
                (run["latest_invocation"], run_id),
            ).fetchall()
            artifacts = self.connection.execute(
                """SELECT a.task_id,a.path,a.sha256,a.size FROM artifacts a
                   JOIN tasks t ON t.run_id=a.run_id AND t.task_id=a.task_id
                     AND t.selected_attempt_no=a.attempt_no
                   WHERE a.run_id=? ORDER BY a.task_id,a.path""",
                (run_id,),
            ).fetchall()
            uncertain = self.connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=? AND launch_state='starting'",
                (run_id,),
            ).fetchone()[0]
        declared_tasks = _decode(run["workflow_json"])["tasks"]
        if {task["task_id"] for task in tasks} != set(declared_tasks):
            raise InvalidState("stored task inventory differs from workflow")
        for task in tasks:
            number = task["displayed_attempt_no"]
            if number is not None:
                prefix = f".repro/runs/{run_id}/attempts/{task['task_id']}/{number}"
                for field, name in (
                    ("stdout_path", "stdout.log"),
                    ("stderr_path", "stderr.log"),
                ):
                    value = task[field]
                    if value is not None and value != f"{prefix}/{name}":
                        raise InvalidState("stored attempt log path is unsafe")
                if task["identity_json"] is not None:
                    identity = _decode(task["identity_json"])
                    if (
                        hashlib.sha256(canonical_bytes(identity)).hexdigest()
                        != task["key"]
                    ):
                        raise InvalidState("stored attempt identity does not match key")
                if task["inputs_json"] is not None:
                    inputs = _decode(task["inputs_json"])
                    if not isinstance(inputs, list):
                        raise InvalidState("stored input inventory is invalid")
                    for item in inputs:
                        if not isinstance(item, dict) or set(item) != {
                            "path",
                            "sha256",
                            "size",
                        }:
                            raise InvalidState("stored input record is invalid")
                        try:
                            check_declaration(item["path"], allow_pattern=False)
                        except Exception as error:
                            raise InvalidState("stored input path is unsafe") from error
                        if not isinstance(item["sha256"], str) or not DIGEST.fullmatch(
                            item["sha256"]
                        ):
                            raise InvalidState("stored input hash is invalid")
                        if type(item["size"]) is not int or item["size"] < 0:
                            raise InvalidState("stored input size is invalid")
        for artifact in artifacts:
            try:
                check_declaration(artifact["path"], allow_pattern=False)
            except Exception as error:
                raise InvalidState("stored artifact path is unsafe") from error
            if not isinstance(artifact["sha256"], str) or not DIGEST.fullmatch(
                artifact["sha256"]
            ):
                raise InvalidState("stored artifact hash is invalid")
            if type(artifact["size"]) is not int or artifact["size"] < 0:
                raise InvalidState("stored artifact size is invalid")
        artifact_paths: dict[str, set[str]] = {}
        for artifact in artifacts:
            artifact_paths.setdefault(artifact["task_id"], set()).add(artifact["path"])
        for task in tasks:
            if task["state"] in ("succeeded", "cached"):
                if (
                    task["selected_attempt_no"] is None
                    or task["displayed_state"] != task["state"]
                ):
                    raise InvalidState("selected completed attempt is inconsistent")
                expected = set(declared_tasks[task["task_id"]]["outputs"])
                if artifact_paths.get(task["task_id"], set()) != expected:
                    raise InvalidState("selected artifact inventory is incomplete")
        return run, invocations, tasks, artifacts, uncertain
