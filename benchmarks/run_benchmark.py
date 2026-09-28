"""Collect and analyze P09 end-to-end runner benchmarks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / ".venv/bin/runner"
EXAMPLE = ROOT / "examples/simulation"
FIXTURES = ROOT / "benchmarks/fixtures"
VERSION = 1
SUITES = ("scheduling", "compute", "cache", "recovery")
TASKS_EXAMPLE = {"generate", "simulate_a", "simulate_b", "simulate_c", "summarize"}
OUTPUTS_EXAMPLE = {
    "generate": "out/input.json",
    "simulate_a": "out/a.json",
    "simulate_b": "out/b.json",
    "simulate_c": "out/c.json",
    "summarize": "out/summary.json",
}
TIMEOUT = 120


class BenchmarkError(Exception):
    """A trial cannot be accepted as correct."""


class InvocationFailure(BenchmarkError):
    """A timed CLI process failed after producing an exit status or timeout."""

    def __init__(self, code: int | None, wall_ns: int, stderr: str):
        self.code = code
        self.wall_ns = wall_ns
        kind = "timeout" if code is None else f"exit {code}"
        super().__init__(f"runner {kind}: {stderr[-700:]}")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity() -> dict:
    paths = sorted(
        [
            *ROOT.glob("src/repro_runner/*.py"),
            *FIXTURES.glob("*.py"),
            ROOT / "benchmarks/run_benchmark.py",
            *EXAMPLE.glob("*.py"),
            *EXAMPLE.glob("config/*.json"),
            EXAMPLE / "workflow.yaml",
            ROOT / "pyproject.toml",
            ROOT / "requirements/dev-macos.lock",
        ]
    )
    inventory = {str(path.relative_to(ROOT)): sha(path) for path in paths}
    aggregate = hashlib.sha256(
        json.dumps(inventory, sort_keys=True).encode()
    ).hexdigest()
    return {"aggregate_sha256": aggregate, "files": inventory}


def command_output(*args: str) -> str | None:
    try:
        result = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def sysctl(name: str) -> str | None:
    return command_output("/usr/sbin/sysctl", "-n", name)


def metadata(
    suites: list[str],
    repetitions: int,
    smoke: bool,
    profile: dict,
    permission_context: str,
) -> dict:
    revision = command_output("git", "rev-parse", "HEAD")
    dirty = command_output("git", "status", "--porcelain=v1", "--untracked-files=all")
    cpu_model = sysctl("machdep.cpu.brand_string") or sysctl("hw.model")
    physical = sysctl("hw.physicalcpu")
    memory = sysctl("hw.memsize")
    return {
        "protocol_version": VERSION,
        "suites": suites,
        "repetitions": repetitions,
        "mode": "smoke" if smoke else "formal",
        "profile": profile,
        "source": source_identity(),
        "git_revision": revision,
        "git_dirty": bool(dirty) if dirty is not None else None,
        "package_version": importlib.metadata.version("reproducible-experiment-runner"),
        "installation": "project .venv editable (verified by package location before collection)",
        "environment": {
            "os": platform.platform(),
            "architecture": platform.machine(),
            "cpu_model": cpu_model,
            "logical_cpu_reported": os.cpu_count(),
            "allowed_cpu_count": None,
            "allowed_cpu_reason": "not established",
            "physical_cpu": int(physical) if physical and physical.isdigit() else None,
            "memory_bytes": int(memory) if memory and memory.isdigit() else None,
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "sqlite": sqlite3.sqlite_version,
            "pyyaml": importlib.metadata.version("PyYAML"),
            "typer": importlib.metadata.version("typer"),
            "lock_sha256": sha(ROOT / "requirements/dev-macos.lock"),
            "permission_context": permission_context,
            "power_and_load": "User confirmed plugged in, awake, and no heavy unrelated jobs; not independently observed",
        },
        "timer": "time.perf_counter_ns; immediately before launch to after process exit",
        "ordinary_command": "$RUNNER run $WORKSPACE/workflow.yaml --workers N [--no-cache]",
        "recovery_resume_command": "$RUNNER resume RUN_ID",
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def append_row(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_process(
    args: list[str], cwd: Path, *, timeout: int = TIMEOUT
) -> tuple[int | None, int, str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as out,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as err,
    ):
        start = time.perf_counter_ns()
        process = subprocess.Popen(
            args, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=8)
            code = None
        wall = time.perf_counter_ns() - start
        out.seek(0)
        err.seek(0)
        return code, wall, out.read(4000), err.read(4000)


def ensure_exit(code: int | None, wall_ns: int, stderr: str, expected: int = 0) -> None:
    if code != expected:
        raise InvocationFailure(code, wall_ns, stderr)


def workspace(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir(exist_ok=False)
    return path


def example_workspace(parent: Path, name: str, profile: dict) -> Path:
    path = workspace(parent, name)
    (path / "config").mkdir()
    for source in [
        *EXAMPLE.glob("*.py"),
        *EXAMPLE.glob("config/*.json"),
        EXAMPLE / "workflow.yaml",
    ]:
        relative = source.relative_to(EXAMPLE)
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    base = json.loads((path / "config/base.json").read_text())
    base["sample_count"] = profile["sample_count"]
    base["steps"] = profile["steps"]
    write_json(path / "config/base.json", base)
    return path


def change_branch_b(path: Path) -> None:
    config = path / "config/b.json"
    value = json.loads(config.read_text())
    if value["increment"] != 5:
        raise BenchmarkError("branch-b initial increment is not 5")
    value["increment"] = 6
    write_json(config, value)


def scheduling_workspace(
    parent: Path, name: str, wait: str
) -> tuple[Path, dict[str, str]]:
    path = workspace(parent, name)
    shutil.copyfile(FIXTURES / "wait_task.py", path / "wait_task.py")
    import yaml

    outputs = {f"wait_{i:02d}": f"out/wait_{i:02d}.txt" for i in range(24)}
    tasks = {
        task: {
            "command": ["python", "wait_task.py", task, wait, output],
            "inputs": ["wait_task.py"],
            "outputs": [output],
        }
        for task, output in outputs.items()
    }
    (path / "workflow.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "tasks": tasks}), encoding="utf-8"
    )
    return path, outputs


def recovery_workspace(
    parent: Path, name: str, boundary: str
) -> tuple[Path, dict[str, str]]:
    path = workspace(parent, name)
    control = path / "control"
    control.mkdir()
    if boundary == "active_child":
        (control / "hold").write_text("1", encoding="ascii")
    shutil.copyfile(FIXTURES / "recovery_task.py", path / "recovery_task.py")
    import yaml

    outputs = {name: f"out/{name}.txt" for name in "abc"}
    tasks = {}
    for name, deps in (("a", []), ("b", ["a"]), ("c", ["b"])):
        inputs = [outputs[item] for item in deps]
        tasks[name] = {
            "command": [
                "python",
                "recovery_task.py",
                name,
                outputs[name],
                str(control),
                *inputs,
            ],
            "deps": deps,
            "inputs": ["recovery_task.py", *inputs],
            "outputs": [outputs[name]],
        }
    (path / "workflow.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "tasks": tasks}), encoding="utf-8"
    )
    return path, outputs


def current_run(path: Path) -> str:
    with closing(sqlite3.connect(path / ".repro/state.sqlite3")) as connection:
        rows = connection.execute(
            "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchall()
    if not rows:
        raise BenchmarkError("no recorded run")
    return rows[0][0]


def inspect(path: Path, outputs: dict[str, str], *, run_id: str | None = None) -> dict:
    identity = run_id or current_run(path)
    with closing(
        sqlite3.connect(f"file:{path / '.repro/state.sqlite3'}?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        header = db.execute("SELECT * FROM runs WHERE run_id=?", (identity,)).fetchone()
        if header is None:
            raise BenchmarkError("run missing from SQLite")
        number = header["latest_invocation"]
        invocation = db.execute(
            "SELECT * FROM invocations WHERE run_id=? AND invocation_no=?",
            (identity, number),
        ).fetchone()
        tasks = {
            row["task_id"]: dict(row)
            for row in db.execute("SELECT * FROM tasks WHERE run_id=?", (identity,))
        }
        resolutions = {
            row["task_id"]: dict(row)
            for row in db.execute(
                "SELECT * FROM resolutions WHERE run_id=? AND invocation_no=?",
                (identity, number),
            )
        }
        attempts = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY task_id,attempt_no",
                (identity,),
            )
        ]
        artifacts = [
            dict(row)
            for row in db.execute(
                "SELECT a.* FROM artifacts a JOIN tasks t ON a.run_id=t.run_id AND a.task_id=t.task_id AND a.attempt_no=t.selected_attempt_no WHERE a.run_id=?",
                (identity,),
            )
        ]
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok" or set(tasks) != set(outputs):
        raise BenchmarkError("SQLite integrity or task inventory mismatch")
    if header["outcome"] != "succeeded" or invocation["outcome"] != "succeeded":
        raise BenchmarkError("run or invocation is not succeeded")
    if set(resolutions) != set(outputs):
        raise BenchmarkError("current invocation resolutions incomplete")
    hashes = {}
    for task, relative in outputs.items():
        file = path / relative
        if not file.is_file():
            raise BenchmarkError(f"missing declared output {relative}")
        hashes[relative] = sha(file)
    expected = {
        (task, relative): hashes[relative] for task, relative in outputs.items()
    }
    actual = {(item["task_id"], item["path"]): item["sha256"] for item in artifacts}
    if actual != expected:
        raise BenchmarkError("output and selected artifact hashes differ")
    manifest_file = path / ".repro/runs" / identity / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    if (
        manifest["run"]["id"] != identity
        or manifest["run"]["outcome"] != "succeeded"
        or manifest["snapshot"]["invocation_no"] != number
        or set(manifest["tasks"]) != set(outputs)
        or manifest["invocations"][-1]["outcome"] != "succeeded"
    ):
        raise BenchmarkError("manifest run/invocation mismatch")
    for task, resolution in resolutions.items():
        counterpart = manifest["invocations"][-1]["resolutions"][task]
        if any(
            counterpart[key] != resolution[value]
            for key, value in (
                ("disposition", "disposition"),
                ("state", "state"),
                ("selected_attempt_no", "source_attempt_no"),
            )
        ):
            raise BenchmarkError("manifest resolution mismatch")
        selected = tasks[task]["selected_attempt_no"]
        if (
            selected is None
            or manifest["tasks"][task]["selected_attempt_no"] != selected
        ):
            raise BenchmarkError("manifest selected attempt mismatch")
        candidates = [
            item
            for item in manifest["tasks"][task]["attempts"]
            if item["number"] == selected
        ]
        if len(candidates) != 1 or {
            a["path"]: a["sha256"] for a in candidates[0]["artifacts"]
        } != {outputs[task]: hashes[outputs[task]]}:
            raise BenchmarkError("manifest artifact mismatch")
    launches = sum(
        a["invocation_no"] == number and a["launch_state"] == "started"
        for a in attempts
    )
    counts = Counter(item["disposition"] for item in resolutions.values())
    eligible = sum(bool(item["cache_lookup"]) for item in resolutions.values())
    hits = sum(bool(item["cache_hit"]) for item in resolutions.values())
    if hits > eligible:
        raise BenchmarkError("cache hits exceed eligible lookups")
    return {
        "run_id": identity,
        "invocation_no": number,
        "hashes": hashes,
        "launches": launches,
        "dispositions": {
            name: item["disposition"] for name, item in resolutions.items()
        },
        "counts": dict(counts),
        "cache_eligible": eligible if header["cache_enabled"] else None,
        "cache_hits": hits if header["cache_enabled"] else None,
        "derived_misses": eligible - hits if header["cache_enabled"] else None,
        "attempts": [
            {
                "task": a["task_id"],
                "number": a["attempt_no"],
                "state": a["state"],
                "launch": a["launch_state"],
            }
            for a in attempts
        ],
        "manifest": "verified",
        "sqlite": "verified",
    }


def assert_counts(
    evidence: dict, *, launches: int, executed: int, cached: int = 0, retained: int = 0
) -> None:
    counts = evidence["counts"]
    if (
        evidence["launches"] != launches
        or counts.get("executed", 0) != executed
        or counts.get("cache_restored", 0) != cached
        or counts.get("retained", 0) != retained
        or sum(counts.values()) != executed + cached + retained
    ):
        raise BenchmarkError(
            f"task accounting mismatch: launches={evidence['launches']}, dispositions={counts}"
        )


def observed_concurrency(path: Path, evidence: dict) -> int:
    events = []
    for task in evidence["dispositions"]:
        logfile = (
            path
            / ".repro/runs"
            / evidence["run_id"]
            / "attempts"
            / task
            / "1/stdout.log"
        )
        values = [json.loads(line) for line in logfile.read_text().splitlines()]
        if (
            len(values) != 2
            or [v["event"] for v in values] != ["start", "end"]
            or values[0]["ns"] > values[1]["ns"]
        ):
            raise BenchmarkError("invalid scheduling event logs")
        events.extend([(values[0]["ns"], 1), (values[1]["ns"], -1)])
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
        if active < 0:
            raise BenchmarkError("invalid scheduling event order")
    if active != 0:
        raise BenchmarkError("unbalanced scheduling events")
    return peak


def check_summary(path: Path, profile: dict) -> None:
    result = json.loads((path / "out/summary.json").read_text())
    branches = [json.loads((path / f"out/{name}.json").read_text()) for name in "abc"]
    if (
        result["seed"] != 1729
        or result["sample_count"] != profile["sample_count"]
        or result["steps"] != profile["steps"]
        or result["branches"] != branches
        or result["combined_total"] != sum(item["aggregate"] for item in branches)
        or result["combined_weighted_checksum"]
        != sum(item["weighted_checksum"] for item in branches)
    ):
        raise BenchmarkError("simulation summary calculation mismatch")


def record_run(
    path: Path, outputs: dict[str, str], workers: int, cache: bool
) -> tuple[int, dict]:
    args = [str(RUNNER), "run", "workflow.yaml", "--workers", str(workers)]
    if not cache:
        args.append("--no-cache")
    code, wall, _, stderr = run_process(args, path)
    ensure_exit(code, wall, stderr)
    return wall, inspect(path, outputs)


def row_base(
    sample_id: str,
    suite: str,
    condition: str,
    kind: str,
    order: int,
    repetition: int,
    profile: dict,
    workers: int,
    cache: bool,
) -> dict:
    return {
        "sample_id": sample_id,
        "suite": suite,
        "condition": condition,
        "kind": kind,
        "order": order,
        "repetition": repetition,
        "profile": profile,
        "workers": workers,
        "cache_enabled": cache,
        "wall_ns": None,
        "exit_code": None,
        "included": kind == "measured",
        "evidence": None,
        "error": None,
        "command": "$RUNNER run workflow.yaml",
    }


def save_trial(raw: Path, row: dict, operation) -> dict:
    try:
        wall, evidence = operation()
        row.update(wall_ns=wall, exit_code=0, evidence=evidence)
    except (BenchmarkError, OSError, ValueError, KeyError, sqlite3.Error) as error:
        message = f"{type(error).__name__}: {error}".replace(str(ROOT), "$REPO")
        message = message.replace(str(Path.home()), "~")
        row.update(kind="failure", included=False, error=message)
        if isinstance(error, InvocationFailure):
            row.update(wall_ns=error.wall_ns, exit_code=error.code)
        append_row(raw, row)
        raise BenchmarkError(f"{row['sample_id']}: {row['error']}") from error
    append_row(raw, row)
    return row


def run_scheduling(raw: Path, temp: Path, repetitions: int, smoke: bool) -> None:
    wait = "0.02" if smoke else "0.25"
    reference = None
    order = 0
    for block in range(repetitions + 1):
        settings = (
            [1, 2, 4, 8]
            if block == 0
            else [1, 2, 4, 8][(block - 1) % 4 :] + [1, 2, 4, 8][: (block - 1) % 4]
        )
        for workers in settings:
            kind = "smoke" if smoke else "warmup" if block == 0 else "measured"
            sample_id = f"A-{block}-{workers}"
            row = row_base(
                sample_id,
                "scheduling",
                f"workers={workers}",
                kind,
                order,
                block,
                {"tasks": 24, "requested_wait_seconds": float(wait)},
                workers,
                False,
            )
            order += 1

            def operation():
                nonlocal reference
                path, outputs = scheduling_workspace(temp, sample_id, wait)
                wall, evidence = record_run(path, outputs, workers, False)
                assert_counts(evidence, launches=24, executed=24)
                evidence["observed_max_concurrency"] = observed_concurrency(
                    path, evidence
                )
                if evidence["observed_max_concurrency"] > workers:
                    raise BenchmarkError("observed concurrency exceeds worker setting")
                if reference is None:
                    reference = evidence["hashes"]
                if evidence["hashes"] != reference:
                    raise BenchmarkError("scheduling output hashes differ")
                return wall, evidence

            save_trial(raw, row, operation)


def run_compute(
    raw: Path, temp: Path, repetitions: int, smoke: bool, profile: dict
) -> None:
    reference = None
    order = 0
    for block in range(repetitions + 1):
        settings = [1, 4] if block == 0 or block % 2 else [4, 1]
        for workers in settings:
            kind = "smoke" if smoke else "warmup" if block == 0 else "measured"
            sample_id = f"B-{block}-{workers}"
            row = row_base(
                sample_id,
                "compute",
                f"workers={workers}",
                kind,
                order,
                block,
                profile,
                workers,
                False,
            )
            order += 1

            def operation():
                nonlocal reference
                path = example_workspace(temp, sample_id, profile)
                wall, evidence = record_run(path, OUTPUTS_EXAMPLE, workers, False)
                assert_counts(evidence, launches=5, executed=5)
                check_summary(path, profile)
                if reference is None:
                    reference = evidence["hashes"]
                if evidence["hashes"] != reference:
                    raise BenchmarkError(
                        "compute output hashes differ across worker settings"
                    )
                return wall, evidence

            save_trial(raw, row, operation)


def run_cache(
    raw: Path, temp: Path, repetitions: int, smoke: bool, profile: dict
) -> None:
    reference = example_workspace(temp, "C-changed-reference", profile)
    change_branch_b(reference)
    ref_row = row_base(
        "C-reference",
        "cache",
        "changed-no-cache-reference",
        "reference",
        0,
        0,
        profile,
        4,
        False,
    )

    def ref_operation():
        wall, evidence = record_run(reference, OUTPUTS_EXAMPLE, 4, False)
        assert_counts(evidence, launches=5, executed=5)
        check_summary(reference, profile)
        return wall, evidence

    changed_reference = save_trial(raw, ref_row, ref_operation)["evidence"]["hashes"]
    order = 1
    for block in range(repetitions + 1):
        path = example_workspace(temp, f"C-pair-{block}", profile)
        previous = None
        for stage in ("cold", "warm", "changed"):
            if stage == "changed":
                change_branch_b(path)
            kind = "smoke" if smoke else "warmup" if block == 0 else "measured"
            row = row_base(
                f"C-{block}-{stage}",
                "cache",
                stage,
                kind,
                order,
                block,
                profile,
                4,
                True,
            )
            row["pair_id"] = f"C-pair-{block}"
            order += 1

            def operation():
                nonlocal previous
                wall, evidence = record_run(path, OUTPUTS_EXAMPLE, 4, True)
                if evidence["cache_eligible"] != 5:
                    raise BenchmarkError("cache lookup eligibility incomplete")
                if stage == "cold":
                    assert_counts(evidence, launches=5, executed=5)
                    if evidence["cache_hits"] != 0:
                        raise BenchmarkError("cold run unexpectedly hit cache")
                elif stage == "warm":
                    assert_counts(evidence, launches=0, executed=0, cached=5)
                    if evidence["cache_hits"] != 5 or evidence["hashes"] != previous:
                        raise BenchmarkError("warm cache or output mismatch")
                else:
                    assert_counts(evidence, launches=2, executed=2, cached=3)
                    expected = {
                        "generate": "cache_restored",
                        "simulate_a": "cache_restored",
                        "simulate_b": "executed",
                        "simulate_c": "cache_restored",
                        "summarize": "executed",
                    }
                    if (
                        evidence["cache_hits"] != 3
                        or evidence["dispositions"] != expected
                        or evidence["hashes"] != changed_reference
                    ):
                        raise BenchmarkError(
                            "changed-input cache or reference mismatch"
                        )
                    for task in ("generate", "simulate_a", "simulate_c"):
                        if (
                            evidence["hashes"][OUTPUTS_EXAMPLE[task]]
                            != previous[OUTPUTS_EXAMPLE[task]]
                        ):
                            raise BenchmarkError(
                                "unrelated changed-input output differs"
                            )
                    if any(
                        evidence["hashes"][OUTPUTS_EXAMPLE[task]]
                        == previous[OUTPUTS_EXAMPLE[task]]
                        for task in ("simulate_b", "summarize")
                    ):
                        raise BenchmarkError("branch-b mutation did not affect result")
                check_summary(path, profile)
                previous = evidence["hashes"]
                return wall, evidence

            save_trial(raw, row, operation)


def wait_marker(marker: Path, process: subprocess.Popen, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while (
        not marker.exists() and process.poll() is None and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    if not marker.exists():
        raise BenchmarkError(f"boundary marker absent; parent exit={process.poll()}")


def parent_process(args: list[str], cwd: Path, out, err) -> subprocess.Popen:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.Popen(
        args, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True
    )


def kill_parent_group(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=8)


def child_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def boundary_wrapper(path: Path) -> Path:
    marker = path / "control/boundary"
    script = path / "pause.py"
    script.write_text(
        "import pathlib,sys,threading\n"
        "from repro_runner.state import StateStore\n"
        "from repro_runner.cli import app\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "original=StateStore.record_result\n"
        "def record(self,result):\n"
        " if result.task_id=='b' and result.state=='succeeded':\n"
        "  marker.write_text('after child and publication, before SQLite success')\n"
        "  threading.Event().wait(60)\n"
        " return original(self,result)\n"
        "StateStore.record_result=record\n"
        "sys.argv=['runner','run','workflow.yaml','--workers','1']+sys.argv[1:]\n"
        "app()\n",
        encoding="utf-8",
    )
    return script


def pre_resume_state(path: Path, run_id: str) -> dict:
    with closing(
        sqlite3.connect(f"file:{path / '.repro/state.sqlite3'}?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        tasks = {
            r["task_id"]: dict(r)
            for r in db.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,))
        }
        attempts = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY task_id,attempt_no",
                (run_id,),
            )
        ]
        header = db.execute(
            "SELECT cache_enabled,outcome FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if set(tasks) != {"a", "b", "c"}:
        raise BenchmarkError("recovery task inventory mismatch before resume")
    if tasks["a"]["state"] != "succeeded" or tasks["a"]["selected_attempt_no"] != 1:
        raise BenchmarkError("task a was not durably committed")
    if any(a["task_id"] == "b" and a["state"] == "succeeded" for a in attempts):
        raise BenchmarkError("task b committed before the stop")
    b = [a for a in attempts if a["task_id"] == "b"]
    if len(b) != 1 or b[0]["launch_state"] != "started":
        raise BenchmarkError("task b was not confirmed launched")
    return {
        "committed_before": [
            name for name, item in tasks.items() if item["state"] == "succeeded"
        ],
        "incomplete_before": [
            a["task_id"] for a in attempts if a["state"] != "succeeded"
        ],
        "unknown_historical_launches": sum(
            a["launch_state"] == "starting" for a in attempts
        ),
        "attempts_before": [
            {
                "task": a["task_id"],
                "number": a["attempt_no"],
                "state": a["state"],
                "launch": a["launch_state"],
            }
            for a in attempts
        ],
        "pre_run_outcome": header["outcome"],
        "cache_enabled": bool(header["cache_enabled"]),
    }


def recovery_trial(
    path: Path, outputs: dict[str, str], boundary: str, cache: bool, reference: dict
) -> tuple[int, dict]:
    control = path / "control"
    args = [str(RUNNER), "run", "workflow.yaml", "--workers", "1"]
    if not cache:
        args.append("--no-cache")
    if boundary == "after_publication":
        args = [sys.executable, str(boundary_wrapper(path))] + (
            [] if cache else ["--no-cache"]
        )
    marker = control / ("ready" if boundary == "active_child" else "boundary")
    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as out,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as err,
    ):
        process = parent_process(args, path, out, err)
        child_pid = None
        try:
            wait_marker(marker, process)
            identity = current_run(path)
            before_marker = pre_resume_state(path, identity)
            if before_marker["cache_enabled"] != cache:
                raise BenchmarkError("inherited cache mode differs")
            if boundary == "active_child":
                child_pid = int(marker.read_text())
                process.send_signal(signal.SIGINT)
                process.wait(timeout=15)
                if process.returncode != 130:
                    err.seek(0)
                    raise BenchmarkError(
                        f"SIGINT parent exit {process.returncode}, expected 130; stderr={err.read(1000)[-700:]}"
                    )
                if not child_gone(child_pid):
                    raise BenchmarkError(
                        "known active fixture child group remains after SIGINT"
                    )
                cleanup = "known child group gone after SIGINT"
            else:
                if (
                    not (control / "b_exited").exists()
                    or not (path / "out/b.txt").exists()
                ):
                    raise BenchmarkError("publication marker preceded completed output")
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=8)
                if process.returncode != -signal.SIGKILL:
                    raise BenchmarkError("SIGKILL parent exit mismatch")
                cleanup = "fixture b completed and exited before parent SIGKILL"
            out.seek(0)
            err.seek(0)
            initial_stderr = err.read(1000)
        finally:
            if process.poll() is None:
                if boundary == "active_child":
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        kill_parent_group(process)
                else:
                    kill_parent_group(process)
            if child_pid is None and boundary == "active_child" and marker.exists():
                child_pid = int(marker.read_text())
            if child_pid is not None and not child_gone(child_pid):
                # The PID is from the live fixture just started in this scope.
                os.killpg(child_pid, signal.SIGKILL)
                raise BenchmarkError("known fixture child required forced cleanup")
    before = pre_resume_state(path, identity)
    args = [str(RUNNER), "resume", identity]
    code, wall, _, stderr = run_process(args, path)
    ensure_exit(code, wall, stderr)
    evidence = inspect(path, outputs, run_id=identity)
    if evidence["invocation_no"] != 2 or evidence["hashes"] != reference:
        raise BenchmarkError("recovered invocation or output hash mismatch")
    expected = {
        "a": "retained",
        "b": "cache_restored"
        if cache and boundary == "after_publication"
        else "executed",
        "c": "executed",
    }
    if evidence["dispositions"] != expected:
        raise BenchmarkError(
            f"recovery disposition mismatch: {evidence['dispositions']}"
        )
    assert_counts(
        evidence,
        launches=1 if expected["b"] == "cache_restored" else 2,
        executed=1 if expected["b"] == "cache_restored" else 2,
        cached=1 if expected["b"] == "cache_restored" else 0,
        retained=1,
    )
    if cache is False and (path / ".repro/cache").exists():
        raise BenchmarkError("no-cache recovery created a cache directory")
    b_attempts = [a for a in evidence["attempts"] if a["task"] == "b"]
    if [a["number"] for a in b_attempts] != [1, 2] or b_attempts[0][
        "state"
    ] == "succeeded":
        raise BenchmarkError("incomplete attempt was rewritten or not retried")
    evidence.update(
        {
            "boundary": boundary,
            "stop_method": "SIGINT" if boundary == "active_child" else "SIGKILL",
            "initial_command_kind": "installed CLI"
            if boundary == "active_child"
            else "benchmark wrapper",
            "resume_command_kind": "installed CLI",
            "before": before,
            "at_boundary": before_marker,
            "cleanup": cleanup,
            "initial_exit_code": process.returncode,
            "initial_stderr_excerpt": initial_stderr[-300:],
        }
    )
    return wall, evidence


def run_recovery(raw: Path, temp: Path, repetitions: int, smoke: bool) -> None:
    reference_path, outputs = recovery_workspace(
        temp, "D-reference", "after_publication"
    )
    ref_row = row_base(
        "D-reference",
        "recovery",
        "uninterrupted-reference",
        "reference",
        0,
        0,
        {"chain": ["a", "b", "c"]},
        1,
        False,
    )

    def reference_operation():
        wall, evidence = record_run(reference_path, outputs, 1, False)
        assert_counts(evidence, launches=3, executed=3)
        return wall, evidence

    reference = save_trial(raw, ref_row, reference_operation)["evidence"]["hashes"]
    order = 1
    for boundary in ("active_child", "after_publication"):
        for cache in (True, False):
            for block in range(repetitions + 1):
                sample_id = f"D-{boundary}-{'cache' if cache else 'no-cache'}-{block}"
                kind = "smoke" if smoke else "warmup" if block == 0 else "measured"
                row = row_base(
                    sample_id,
                    "recovery",
                    f"{boundary}/{'cache' if cache else 'no-cache'}",
                    kind,
                    order,
                    block,
                    {"chain": ["a", "b", "c"]},
                    1,
                    cache,
                )
                row["command"] = "$RUNNER resume RUN_ID"
                order += 1

                def operation():
                    path, outputs = recovery_workspace(temp, sample_id, boundary)
                    return recovery_trial(path, outputs, boundary, cache, reference)

                save_trial(raw, row, operation)


def load_rows(path: Path) -> list[dict]:
    rows = []
    seen = set()
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(value)
                    ),
                )
            except (json.JSONDecodeError, ValueError) as error:
                raise BenchmarkError(f"invalid JSONL line {number}") from error
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("sample_id"), str)
                or row["sample_id"] in seen
            ):
                raise BenchmarkError(f"duplicate or invalid sample ID at line {number}")
            seen.add(row["sample_id"])
            if row.get("included"):
                wall = row.get("wall_ns")
                if (
                    type(wall) is not int
                    or wall <= 0
                    or row.get("kind") != "measured"
                    or row.get("exit_code") != 0
                    or not isinstance(row.get("evidence"), dict)
                ):
                    raise BenchmarkError(f"invalid measured row {row['sample_id']}")
            rows.append(row)
    return rows


def measured_groups(
    rows: list[dict], metadata_record: dict
) -> dict[tuple[str, str], list[dict]]:
    groups = defaultdict(list)
    for row in rows:
        if row["included"]:
            if row["suite"] not in metadata_record["suites"] or row["profile"] != (
                {"tasks": 24, "requested_wait_seconds": 0.25}
                if row["suite"] == "scheduling"
                else {"chain": ["a", "b", "c"]}
                if row["suite"] == "recovery"
                else metadata_record["profile"]
            ):
                raise BenchmarkError("measured row has incompatible suite/profile")
            groups[row["suite"], row["condition"]].append(row)
    expected = {}
    if "scheduling" in metadata_record["suites"]:
        expected.update(
            {
                ("scheduling", f"workers={n}"): metadata_record["repetitions"]
                for n in (1, 2, 4, 8)
            }
        )
    if "compute" in metadata_record["suites"]:
        expected.update(
            {
                ("compute", f"workers={n}"): metadata_record["repetitions"]
                for n in (1, 4)
            }
        )
    if "cache" in metadata_record["suites"]:
        expected.update(
            {
                ("cache", name): metadata_record["repetitions"]
                for name in ("cold", "warm", "changed")
            }
        )
    if "recovery" in metadata_record["suites"]:
        expected.update(
            {
                ("recovery", f"{boundary}/{mode}"): metadata_record["repetitions"]
                for boundary in ("active_child", "after_publication")
                for mode in ("cache", "no-cache")
            }
        )
    if set(groups) != set(expected) or any(
        len(groups[key]) != n for key, n in expected.items()
    ):
        raise BenchmarkError(
            f"incomplete or unexpected measured conditions: { {str(k): len(v) for k, v in groups.items()} }"
        )
    for key, cohort in groups.items():
        if {r["repetition"] for r in cohort} != set(range(1, expected[key] + 1)):
            raise BenchmarkError(f"repetition identities incomplete for {key}")
        if any(type(r["order"]) is not int or r["order"] < 0 for r in cohort):
            raise BenchmarkError("invalid condition order")
    for suite in metadata_record["suites"]:
        orders = [r["order"] for r in rows if r.get("included") and r["suite"] == suite]
        if len(orders) != len(set(orders)):
            raise BenchmarkError(f"duplicate measured order index in {suite}")
    if any(r["kind"] == "failure" for r in rows):
        raise BenchmarkError("batch has failure records; use a new batch after repair")
    return groups


def stats(rows: list[dict]) -> tuple[float, float, float]:
    values = [row["wall_ns"] / 1_000_000_000 for row in rows]
    return statistics.median(values), min(values), max(values)


def fmt(value: float) -> str:
    return f"{value:.4f}"


def render_summary(metadata_record: dict, rows: list[dict]) -> str:
    if metadata_record["mode"] != "formal" or metadata_record["repetitions"] < 5:
        raise BenchmarkError(
            "smoke/pilot or fewer than five repetitions cannot produce a formal summary"
        )
    groups = measured_groups(rows, metadata_record)
    lines = [
        "# P09 measured results",
        "",
        "Raw source: `samples.jsonl`. Times are external CLI wall seconds; all listed samples passed state, manifest, task, and hash checks.",
        "",
        f"Protocol {metadata_record['protocol_version']}; source fingerprint `{metadata_record['source']['aggregate_sha256']}`; Git revision `{metadata_record['git_revision']}`; dirty={metadata_record['git_dirty']}.",
        "",
    ]
    if "scheduling" in metadata_record["suites"]:
        lines += [
            "## Scheduling benchmark: 24 fixed-duration waiting tasks",
            "",
            "Requested wait: 0.25 s per task. Throughput is 24 / median wall seconds. Observed peak uses task-log start/end events.",
            "",
            "| Workers | Valid n | Median s | Min s | Max s | Tasks/s | Speedup vs 1 | Peak active range | Verified |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        baseline = stats(groups["scheduling", "workers=1"])[0]
        for workers in (1, 2, 4, 8):
            cohort = groups["scheduling", f"workers={workers}"]
            median, low, high = stats(cohort)
            peaks = [r["evidence"]["observed_max_concurrency"] for r in cohort]
            lines.append(
                f"| {workers} | {len(cohort)} | {fmt(median)} | {fmt(low)} | {fmt(high)} | {fmt(24 / median)} | {fmt(baseline / median)} | {min(peaks)}-{max(peaks)} | 24 launches/results; hashes match |"
            )
        lines.append("")
    if "compute" in metadata_record["suites"]:
        p = metadata_record["profile"]
        lines += [
            "## Five-task seeded integer computation",
            "",
            f"Seed 1729; sample_count={p['sample_count']}, steps={p['steps']}; {p['sample_count'] * p['steps']} recurrence updates per branch. No application cache.",
            "",
            "| Workers | Valid n | Median s | Min s | Max s | 1-worker / setting ratio | Verified |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        baseline = stats(groups["compute", "workers=1"])[0]
        for workers in (1, 4):
            cohort = groups["compute", f"workers={workers}"]
            median, low, high = stats(cohort)
            lines.append(
                f"| {workers} | {len(cohort)} | {fmt(median)} | {fmt(low)} | {fmt(high)} | {fmt(baseline / median)} | 5 launches/results; hashes equal |"
            )
        lines.append("")
    if "cache" in metadata_record["suites"]:
        lines += [
            "## Paired cache runs",
            "",
            "Cold means empty application cache in a new workspace; operating-system caches were not cleared. Each pair then runs unchanged warm and branch-b increment 5 to 6.",
            "",
            "| Stage | Paired n | Median s | Min s | Max s | Executed range | Restored range | Hits/eligible | Verified |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for stage in ("cold", "warm", "changed"):
            cohort = groups["cache", stage]
            median, low, high = stats(cohort)
            executed = [r["evidence"]["counts"].get("executed", 0) for r in cohort]
            cached = [r["evidence"]["counts"].get("cache_restored", 0) for r in cohort]
            hits = sum(r["evidence"]["cache_hits"] for r in cohort)
            eligible = sum(r["evidence"]["cache_eligible"] for r in cohort)
            lines.append(
                f"| {stage} | {len(cohort)} | {fmt(median)} | {fmt(low)} | {fmt(high)} | {min(executed)}-{max(executed)} | {min(cached)}-{max(cached)} | {hits}/{eligible} ({fmt(100 * hits / eligible)}%) | hashes/summary match |"
            )
        cold = stats(groups["cache", "cold"])[0]
        warm = stats(groups["cache", "warm"])[0]
        pairs = {
            stage: {r["pair_id"]: r for r in groups["cache", stage]}
            for stage in ("cold", "warm", "changed")
        }
        if not (set(pairs["cold"]) == set(pairs["warm"]) == set(pairs["changed"])):
            raise BenchmarkError("cache pair identities differ")
        lines += [
            "",
            f"Warm reduction from medians: 100 × ({fmt(cold)} - {fmt(warm)}) / {fmt(cold)} = {fmt(100 * (cold - warm) / cold)}%. Pair reductions, computed from each pair's unrounded duration:",
            "",
            "| Pair | Cold s | Warm s | Pair reduction % |",
            "| --- | ---: | ---: | ---: |",
        ]
        for pair in sorted(pairs["cold"]):
            c = pairs["cold"][pair]["wall_ns"]
            w = pairs["warm"][pair]["wall_ns"]
            lines.append(
                f"| {pair} | {fmt(c / 1e9)} | {fmt(w / 1e9)} | {fmt(100 * (c - w) / c)} |"
            )
        lines += [
            "",
            "Changed-input task dispositions (identical in each verified pair):",
            "",
            "| Task | Disposition |",
            "| --- | --- |",
        ]
        first = next(iter(pairs["changed"].values()))["evidence"]["dispositions"]
        for task in sorted(first):
            if any(
                r["evidence"]["dispositions"] != first
                for r in groups["cache", "changed"]
            ):
                raise BenchmarkError("changed-input dispositions varied")
            lines.append(f"| {task} | {first[task]} |")
        lines.append("")
    if "recovery" in metadata_record["suites"]:
        lines += [
            "## Controlled interruption and resume",
            "",
            "Only installed-CLI resume is timed. The initial after-publication run uses a benchmark wrapper; active-child uses the installed CLI. Resume durations describe remaining work after a verified boundary, not full-run speedup.",
            "",
            "| Boundary | Stop | Cache | Valid n | Committed before | Retained | Cached | Executed | Unknown launches | Resume median/min/max s | Verified |",
            "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for boundary in ("active_child", "after_publication"):
            for mode in ("cache", "no-cache"):
                cohort = groups["recovery", f"{boundary}/{mode}"]
                median, low, high = stats(cohort)

                def count_range(values):
                    return f"{min(values)}-{max(values)}"

                committed = [
                    len(r["evidence"]["before"]["committed_before"]) for r in cohort
                ]
                retained = [r["evidence"]["counts"].get("retained", 0) for r in cohort]
                cached = [
                    r["evidence"]["counts"].get("cache_restored", 0) for r in cohort
                ]
                executed = [r["evidence"]["counts"].get("executed", 0) for r in cohort]
                unknown = [
                    r["evidence"]["before"]["unknown_historical_launches"]
                    for r in cohort
                ]
                lines.append(
                    f"| {boundary} | {'SIGINT' if boundary == 'active_child' else 'SIGKILL'} | {mode} | {len(cohort)} | {count_range(committed)} | {count_range(retained)} | {count_range(cached)} | {count_range(executed)} | {count_range(unknown)} | {fmt(median)} / {fmt(low)} / {fmt(high)} | final hashes match reference |"
                )
        lines.append("")
    lines += [
        "Five valid measured trials per condition are required. Warmups, pilots, references, and smoke runs do not enter these tables. Valid slow observations are retained. No p95 or statistical confidence claim is made.",
        "",
    ]
    return "\n".join(lines)


def publish_summary(directory: Path, output: Path | None = None) -> Path:
    metadata_record = json.loads((directory / "metadata.json").read_text())
    rows = load_rows(directory / "samples.jsonl")
    summary = render_summary(metadata_record, rows)
    destination = output or directory / "summary.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False
    ) as stream:
        stream.write(summary)
        temporary = Path(stream.name)
    os.replace(temporary, destination)
    return destination


def pilot(raw: Path, temp: Path) -> None:
    for index, profile in enumerate(
        (
            {"sample_count": 1000, "steps": 500},
            {"sample_count": 3000, "steps": 1000},
            {"sample_count": 5000, "steps": 1000},
        ),
        1,
    ):
        row = row_base(
            f"pilot-{index}",
            "compute",
            "serial-sizing",
            "pilot",
            index,
            index,
            profile,
            1,
            False,
        )

        def operation():
            path = example_workspace(temp, f"pilot-{index}", profile)
            wall, evidence = record_run(path, OUTPUTS_EXAMPLE, 1, False)
            assert_counts(evidence, launches=5, executed=5)
            check_summary(path, profile)
            return wall, evidence

        result = save_trial(raw, row, operation)
        print(f"pilot {index}: {profile}, {result['wall_ns'] / 1e9:.3f}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=(*SUITES, "all"), default="all")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--pilot", action="store_true", help="At most three serial sizing trials"
    )
    parser.add_argument("--sample-count", type=int)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--summarize-only", type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument(
        "--permission-context",
        choices=("workspace-sandbox", "scoped-process-control"),
        default="workspace-sandbox",
    )
    args = parser.parse_args()
    if args.summarize_only is not None:
        if (
            args.output_dir
            or args.smoke
            or args.pilot
            or args.sample_count
            or args.steps
            or args.summary_output == args.summarize_only
        ):
            parser.error("summarize-only cannot collect or overwrite a batch")
        try:
            print(publish_summary(args.summarize_only, args.summary_output))
            return 0
        except (BenchmarkError, OSError, ValueError, KeyError) as error:
            print(f"benchmark: {error}", file=sys.stderr)
            return 1
    if args.output_dir is None or args.summary_output is not None:
        parser.error("collection requires --output-dir and no --summary-output")
    if args.repetitions != 5 and not args.smoke and not args.pilot:
        parser.error("formal P09 requires exactly five measured repetitions")
    if args.pilot and args.smoke:
        parser.error("pilot and smoke are separate modes")
    if (args.sample_count is None) != (args.steps is None):
        parser.error("--sample-count and --steps must be specified together")
    if (
        not args.smoke
        and not args.pilot
        and args.suite in ("compute", "cache", "all")
        and args.sample_count is None
    ):
        parser.error("formal compute/cache requires frozen --sample-count and --steps")
    profile = {
        "sample_count": args.sample_count or 32 if args.smoke else args.sample_count,
        "steps": args.steps or 20 if args.smoke else args.steps,
    }
    if (
        not args.pilot
        and args.suite in ("compute", "cache", "all")
        and (
            type(profile["sample_count"]) is not int
            or type(profile["steps"]) is not int
            or not 1 <= profile["sample_count"] <= 100000
            or not 1 <= profile["steps"] <= 100000
            or profile["sample_count"] * profile["steps"] > 20000000
        )
    ):
        parser.error("invalid example profile")
    if (
        not RUNNER.is_file()
        or Path(sys.executable).resolve() != (ROOT / ".venv/bin/python").resolve()
    ):
        parser.error(
            "run this controller with the project .venv Python and installed runner"
        )
    suites = list(SUITES) if args.suite == "all" else [args.suite]
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        mode = "pilot" if args.pilot else "smoke" if args.smoke else "formal"
        record = metadata(
            suites,
            1 if args.smoke else args.repetitions,
            args.smoke,
            profile,
            args.permission_context,
        )
        record["mode"] = mode
        write_json(args.output_dir / "metadata.json", record)
        raw = args.output_dir / "samples.jsonl"
        raw.touch(exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="repro-p09-") as temp_name:
            temp = Path(temp_name)
            if args.pilot:
                pilot(raw, temp)
            else:
                repetitions = 1 if args.smoke else args.repetitions
                for suite in suites:
                    print(f"Starting {suite}", flush=True)
                    {
                        "scheduling": lambda: run_scheduling(
                            raw, temp, repetitions, args.smoke
                        ),
                        "compute": lambda: run_compute(
                            raw, temp, repetitions, args.smoke, profile
                        ),
                        "cache": lambda: run_cache(
                            raw, temp, repetitions, args.smoke, profile
                        ),
                        "recovery": lambda: run_recovery(
                            raw, temp, repetitions, args.smoke
                        ),
                    }[suite]()
                    print(f"Completed {suite}", flush=True)
        if source_identity() != record["source"]:
            raise BenchmarkError(
                "measured source fingerprint changed during collection"
            )
        if mode == "formal":
            destination = publish_summary(args.output_dir)
            print(f"Summary: {destination}", flush=True)
        else:
            print(f"{mode} records: {raw}", flush=True)
        return 0
    except (
        BenchmarkError,
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"benchmark failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
