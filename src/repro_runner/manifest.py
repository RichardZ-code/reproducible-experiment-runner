"""Versioned JSON projection of committed run state."""

import hashlib
import json
import math
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from repro_runner.hashing import canonical_bytes
from repro_runner.paths import check_declaration, ensure_real_directory
from repro_runner.state import (
    DIGEST,
    InvalidState,
    StateSnapshot,
    StateStore,
    _decode,
    _provenance_record,
)

MANIFEST_VERSION = 1


def _duration(value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise InvalidState("stored duration is invalid")
    return float(value)


def _file_record(row: object) -> dict[str, object]:
    path, digest, size = row["path"], row["sha256"], row["size"]
    try:
        check_declaration(path, allow_pattern=False)
    except Exception as error:
        raise InvalidState("stored artifact path is unsafe") from error
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise InvalidState("stored artifact hash is invalid")
    if type(size) is not int or size < 0:
        raise InvalidState("stored artifact size is invalid")
    return {"path": path, "sha256": digest, "size": size}


def _inputs(value: str | None) -> list[dict[str, object]] | None:
    if value is None:
        return None
    records = _decode(value)
    if not isinstance(records, list):
        raise InvalidState("stored input inventory is invalid")
    if any(
        not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}
        for item in records
    ):
        raise InvalidState("stored input record is invalid")
    return [_file_record(item) for item in records]


def _dependencies(value: str | None) -> list[dict[str, str]] | None:
    if value is None:
        return None
    records = _decode(value)
    if not isinstance(records, list):
        raise InvalidState("stored dependency inventory is invalid")
    for item in records:
        if not isinstance(item, dict) or set(item) != {"task", "path", "sha256"}:
            raise InvalidState("stored dependency record is invalid")
        try:
            check_declaration(item["path"], allow_pattern=False)
        except Exception as error:
            raise InvalidState("stored dependency path is unsafe") from error
        if not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
            raise InvalidState("stored dependency hash is invalid")
    return records


def _attempt(
    row: object, run_id: str, artifacts: list[dict[str, object]]
) -> dict[str, object]:
    task_id, number = row["task_id"], row["attempt_no"]
    prefix = f".repro/runs/{run_id}/attempts/{task_id}/{number}"
    if row["attempt_path"] != prefix:
        raise InvalidState("stored attempt path is unsafe")
    for field, basename in (
        ("stdout_path", "stdout.log"),
        ("stderr_path", "stderr.log"),
    ):
        if row[field] is not None and row[field] != f"{prefix}/{basename}":
            raise InvalidState("stored attempt log path is unsafe")
    command = _decode(row["command_json"])
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) for arg in command)
    ):
        raise InvalidState("stored attempt command is invalid")
    identity = (
        _decode(row["identity_json"]) if row["identity_json"] is not None else None
    )
    if identity is not None:
        if hashlib.sha256(canonical_bytes(identity)).hexdigest() != row["key"]:
            raise InvalidState("stored attempt identity does not match key")
    elif row["key"] is not None:
        raise InvalidState("stored key lacks an identity")
    if row["state"] in ("succeeded", "cached") and identity is None:
        raise InvalidState("completed attempt lacks identity")
    environment = (
        _decode(row["environment_json"])
        if row["environment_json"] is not None
        else None
    )
    if environment is not None and not isinstance(environment, dict):
        raise InvalidState("stored attempt environment is invalid")
    return {
        "number": number,
        "invocation_no": row["invocation_no"],
        "operation": row["operation"],
        "state": row["state"],
        "launch_state": row["launch_state"],
        "command": command,
        "comparison_key": row["key"],
        "identity": identity,
        "inputs": _inputs(row["inputs_json"]),
        "dependency_outputs": _dependencies(row["dependency_json"]),
        "environment": environment,
        "attempt_path": prefix,
        "stdout_path": row["stdout_path"],
        "stderr_path": row["stderr_path"],
        "resolution_kind": row["resolution_kind"],
        "exit_code": row["exit_code"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_seconds": _duration(row["duration_seconds"]),
        "reason": row["reason"],
        "error_category": row["error_category"],
        "artifacts": artifacts,
        "cache_origin": (
            {"git_commit": None, "availability": "not_recorded_in_cache"}
            if row["state"] == "cached"
            else None
        ),
    }


def build_manifest(snapshot: StateSnapshot) -> dict[str, object]:
    """Map database facts explicitly; never inspect current workspace files."""
    run = snapshot.run
    run_id = run["run_id"]
    workflow = _decode(run["workflow_json"])
    invocations = {row["invocation_no"]: row for row in snapshot.invocations}
    if not invocations or max(invocations) != run["latest_invocation"]:
        raise InvalidState("stored invocation history is incomplete")
    provenance = {
        row["invocation_no"]: _provenance_record(_decode(row["observation_json"]))
        for row in snapshot.provenance
    }
    absent = {
        "commit": None,
        "dirty": None,
        "availability": "not_collected",
        "observed_at": None,
    }
    artifacts: dict[tuple[str, int], list[dict[str, object]]] = {}
    for row in snapshot.artifacts:
        artifacts.setdefault((row["task_id"], row["attempt_no"]), []).append(
            _file_record(row)
        )
    attempts: dict[str, list[dict[str, object]]] = {}
    attempt_invocations: dict[tuple[str, int], int] = {}
    for row in snapshot.attempts:
        key = (row["task_id"], row["attempt_no"])
        if row["invocation_no"] not in invocations:
            raise InvalidState("attempt refers to missing invocation")
        attempt_invocations[key] = row["invocation_no"]
        attempts.setdefault(row["task_id"], []).append(
            _attempt(row, run_id, artifacts.get(key, []))
        )
    tasks: dict[str, dict[str, object]] = {}
    for row in snapshot.tasks:
        task_id = row["task_id"]
        selected = row["selected_attempt_no"]
        if selected is not None and (task_id, selected) not in attempt_invocations:
            raise InvalidState("selected attempt is missing")
        tasks[task_id] = {
            "state": row["state"],
            "selected_attempt_no": selected,
            "reason": row["reason"],
            "attempts": attempts.get(task_id, []),
        }
    if set(tasks) != set(workflow["tasks"]):
        raise InvalidState("stored task inventory differs from workflow")
    for task_id, item in tasks.items():
        for attempt in item["attempts"]:
            if attempt["command"] != workflow["tasks"][task_id]["command"]:
                raise InvalidState("stored attempt command differs from workflow")
            if attempt["state"] in ("succeeded", "cached"):
                expected = set(workflow["tasks"][task_id]["outputs"])
                if {artifact["path"] for artifact in attempt["artifacts"]} != expected:
                    raise InvalidState(
                        "completed attempt artifact inventory is incomplete"
                    )
    resolutions: dict[int, dict[str, dict[str, object]]] = {
        number: {} for number in invocations
    }
    for row in snapshot.resolutions:
        number, task_id = row["invocation_no"], row["task_id"]
        if number not in resolutions or task_id not in tasks:
            raise InvalidState("resolution refers to missing invocation or task")
        source = row["source_attempt_no"]
        source_invocation = (
            attempt_invocations.get((task_id, source)) if source is not None else None
        )
        if source is not None and source_invocation is None:
            raise InvalidState("resolution refers to missing attempt")
        resolutions[number][task_id] = {
            "disposition": row["disposition"],
            "state": row["state"],
            "selected_attempt_no": source,
            "selected_attempt_invocation_no": source_invocation,
            "cache_lookup": bool(row["cache_lookup"]),
            "cache_hit": bool(row["cache_hit"]),
            "reason": row["reason"],
        }
    invocation_records = []
    for number, row in invocations.items():
        environment = _decode(row["environment_json"])
        if not isinstance(environment, dict):
            raise InvalidState("stored invocation environment is invalid")
        invocation_records.append(
            {
                "number": number,
                "workers": row["workers"],
                "environment": environment,
                "git": provenance.get(number, absent),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration_seconds": _duration(row["duration_seconds"]),
                "outcome": row["outcome"],
                "reason": row["reason"],
                "resolutions": resolutions[number],
            }
        )
    return {
        "manifest_schema_version": MANIFEST_VERSION,
        "state_schema_version": snapshot.schema_version,
        "snapshot": {
            "invocation_no": run["latest_invocation"],
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "run_updated_at": run["updated_at"],
        },
        "run": {
            "id": run_id,
            "outcome": run["outcome"],
            "reason": run["reason"],
            "workflow_file": run["workflow_file"],
            "workflow": workflow,
            "workflow_sha256": run["workflow_hash"],
            "cache_enabled": bool(run["cache_enabled"]),
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
        },
        "tasks": tasks,
        "invocations": invocation_records,
    }


def publish_manifest(run_dir: Path, document: dict[str, object]) -> Path:
    """Replace one complete JSON file without following runtime symlinks."""
    ensure_real_directory(run_dir.parent)
    ensure_real_directory(run_dir)
    final = run_dir / "manifest.json"

    def check_final() -> None:
        try:
            info = final.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise OSError("manifest destination is not a regular file")

    check_final()
    data = (
        json.dumps(
            document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )
    temporary = run_dir / f".manifest-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
        check_final()
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return final


def write_manifest(state: StateStore, run_dir: Path) -> Path:
    if state.run_id is None:
        raise RuntimeError("state run has not started")
    return publish_manifest(
        run_dir, build_manifest(state.manifest_snapshot(state.run_id))
    )
