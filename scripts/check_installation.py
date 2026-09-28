"""Exercise one newly built distribution in an independent Python runtime."""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

DIST_NAME = "reproducible-experiment-runner"
OUTPUTS = ("input", "a", "b", "c", "summary")
TASKS = ("generate", "simulate_a", "simulate_b", "simulate_c", "summarize")
CHECKS = (
    "fresh_venv",
    "artifact_install",
    "isolated_origin_and_metadata",
    "help_and_validation",
    "cold_run_and_hashes",
    "warm_restore_without_launch",
    "status_resume_and_manifest_rebuild",
    "no_cache_run_resume_and_sqlite",
)


def artifact_kind(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"artifact is missing: {path}")
    if path.name.endswith(".whl"):
        return "wheel"
    if path.name.endswith(".tar.gz"):
        return "sdist"
    raise ValueError(f"unsupported artifact: {path.name}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_origin(probe: dict, runtime: Path) -> None:
    site = (runtime / "lib").resolve()
    origin = Path(probe["origin"]).resolve()
    if not origin.is_relative_to(site) or "site-packages" not in origin.parts:
        raise ValueError("package import is outside the fresh runtime site-packages")
    if Path(probe["prefix"]).resolve() != runtime.resolve():
        raise ValueError("Python prefix is not the fresh runtime")
    if probe["version"] != "0.1.0" or probe["entry_point"] != "repro_runner.cli:app":
        raise ValueError("installed distribution or runner entry point is incorrect")
    if probe["editable"]:
        raise ValueError("installed distribution is editable")
    if not {"pyyaml", "typer"}.issubset(set(probe["requirements"])):
        raise ValueError("declared runtime dependencies are missing")


def verify_outputs(workspace: Path, manifest: dict) -> dict[str, str]:
    if set(manifest["tasks"]) != set(TASKS):
        raise ValueError("manifest task inventory differs from the example")
    hashes = {}
    for task, filename in zip(TASKS, OUTPUTS, strict=True):
        output = workspace / "out" / f"{filename}.json"
        actual = sha256(output)
        selected = manifest["tasks"][task]["selected_attempt_no"]
        attempts = manifest["tasks"][task]["attempts"]
        matching = [a for a in attempts if a["number"] == selected]
        if len(matching) != 1:
            raise ValueError(f"missing selected attempt for {task}")
        artifacts = matching[0]["artifacts"]
        if len(artifacts) != 1 or artifacts[0]["path"] != f"out/{filename}.json":
            raise ValueError(f"artifact inventory mismatch for {task}")
        if artifacts[0]["sha256"] != actual:
            raise ValueError(f"output hash mismatch for {task}")
        hashes[filename] = actual
    summary = json.loads((workspace / "out/summary.json").read_text())
    branches = [
        json.loads((workspace / "out" / f"{name}.json").read_text())
        for name in ("a", "b", "c")
    ]
    base = json.loads((workspace / "config/base.json").read_text())
    generated = json.loads((workspace / "out/input.json").read_text())
    generator = random.Random(base["seed"])
    expected_states = [generator.randrange(10_000) for _ in range(base["sample_count"])]
    if generated != {**base, "initial_states": expected_states}:
        raise ValueError("generated example input differs from seeded expectation")
    for name, branch in zip(("a", "b", "c"), branches, strict=True):
        config = json.loads((workspace / "config" / f"{name}.json").read_text())
        final = expected_states.copy()
        for _ in range(base["steps"]):
            final = [
                (config["multiplier"] * value + config["increment"]) % config["modulus"]
                for value in final
            ]
        if (
            branch["branch"] != name
            or branch["aggregate"] != sum(final)
            or branch["weighted_checksum"]
            != sum(i * value for i, value in enumerate(final, 1))
            or branch["minimum"] != min(final)
            or branch["maximum"] != max(final)
        ):
            raise ValueError(f"branch computation differs from expected result: {name}")
    if (
        summary["branches"] != branches
        or summary["combined_total"] != sum(branch["aggregate"] for branch in branches)
        or summary["combined_weighted_checksum"]
        != sum(branch["weighted_checksum"] for branch in branches)
    ):
        raise ValueError("example summary does not match branch outputs")
    return hashes


def dispositions(manifest: dict, expected: str) -> None:
    rows = manifest["invocations"][-1]["resolutions"]
    if set(rows) != set(TASKS) or any(
        row["disposition"] != expected for row in rows.values()
    ):
        raise ValueError(f"expected five {expected} task resolutions")


def run(
    command: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: int = 120
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        log.write_text(f"timed out after {timeout}s\n")
        raise RuntimeError(f"{log.name} timed out") from error
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"{log.name} failed with exit {result.returncode}")
    return result.stdout


def copy_example(source: Path, target: Path) -> None:
    target.mkdir()
    (target / "config").mkdir()
    for name in ("workflow.yaml", "generate.py", "simulate.py", "summarize.py"):
        shutil.copyfile(source / name, target / name)
    for name in ("base", "a", "b", "c"):
        shutil.copyfile(
            source / "config" / f"{name}.json", target / "config" / f"{name}.json"
        )


def load_manifest(workspace: Path, run_id: str) -> dict:
    data = json.loads(
        (workspace / ".repro/runs" / run_id / "manifest.json").read_text()
    )
    if data["run"]["id"] != run_id or data["run"]["outcome"] != "succeeded":
        raise ValueError("manifest run identity or outcome is incorrect")
    return data


def parse_id(output: str) -> str:
    matches = re.findall(r"^Run ID: ([0-9a-f]{32})$", output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("runner did not print one run ID")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--example", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    artifact = args.artifact.resolve(strict=True)
    kind = artifact_kind(artifact)
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=True)
    summary = {
        "kind": kind,
        "artifact": artifact.name,
        "artifact_sha256": sha256(artifact),
        "checks": dict.fromkeys(CHECKS, "not_run"),
    }
    (report / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    env = os.environ.copy()
    for key in tuple(env):
        if key in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "GIT_DIR",
            "GIT_WORK_TREE",
        } or key.startswith("PIP_"):
            env.pop(key)
    env["PIP_CONFIG_FILE"] = os.devnull
    with tempfile.TemporaryDirectory(prefix=f"repro-p10-{kind}-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        workspace = root / "example"
        try:
            run(
                [str(args.python.resolve()), "-m", "venv", str(runtime)],
                root,
                env,
                report / "venv.log",
            )
            python = runtime / "bin/python"
            runner = runtime / "bin/runner"
            summary["checks"]["fresh_venv"] = "passed"
            run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--no-input",
                    str(artifact),
                ],
                root,
                env,
                report / "install.log",
                300,
            )
            summary["checks"]["artifact_install"] = "passed"
            run(
                [str(python), "-m", "pip", "check"], root, env, report / "pip-check.log"
            )
            probe_code = (
                "import importlib.metadata as m,json,repro_runner,sys; "
                "d=m.distribution('reproducible-experiment-runner'); "
                "e=[x.value for x in d.entry_points if x.name=='runner' and x.group=='console_scripts']; "
                "u=d.read_text('direct_url.json'); "
                "print(json.dumps(dict(prefix=sys.prefix,origin=repro_runner.__file__,version=d.version,"
                "entry_point=e[0] if len(e)==1 else None,editable=bool(u and json.loads(u).get('dir_info',{}).get('editable')),"
                "requirements=[__import__('re').split(r'[\\s<>=!~;\\[]',r,1)[0].lower() for r in d.requires or []],"
                "installed={n:m.version(n) for n in ('PyYAML','typer')},"
                "python=sys.version.split()[0])))"
            )
            probe = json.loads(
                run(
                    [str(python), "-I", "-c", probe_code],
                    root,
                    env,
                    report / "origin.log",
                )
            )
            verify_origin(probe, runtime)
            summary["runtime"] = {
                "python": probe["python"],
                "version": probe["version"],
                "requirements": probe["requirements"],
                "installed": probe["installed"],
            }
            summary["checks"]["isolated_origin_and_metadata"] = "passed"
            copy_example(args.example.resolve(strict=True), workspace)
            for label, command in (
                ("help", ["--help"]),
                ("run-help", ["run", "--help"]),
                ("validate", ["validate", "workflow.yaml"]),
            ):
                run([str(runner), *command], workspace, env, report / f"{label}.log")
            if (workspace / ".repro").exists():
                raise ValueError("validation created runtime state")
            summary["checks"]["help_and_validation"] = "passed"
            cold = run(
                [str(runner), "run", "workflow.yaml"],
                workspace,
                env,
                report / "cold.log",
            )
            cold_id = parse_id(cold)
            first = load_manifest(workspace, cold_id)
            dispositions(first, "executed")
            if len(first["invocations"]) != 1 or any(
                len(t["attempts"]) != 1 for t in first["tasks"].values()
            ):
                raise ValueError("cold task launch history is incorrect")
            if first["invocations"][0]["git"]["availability"] != "outside_git":
                raise ValueError("fixture did not record outside-Git provenance")
            identity = first["invocations"][0]["environment"]
            if (
                identity["dependency_lock_source"] != "unavailable"
                or identity["dependency_lock_sha256"] is not None
            ):
                raise ValueError("installed runtime reported a source lock")
            cold_hashes = verify_outputs(workspace, first)
            summary["checks"]["cold_run_and_hashes"] = "passed"
            warm = run(
                [str(runner), "run", "workflow.yaml"],
                workspace,
                env,
                report / "warm.log",
            )
            warm_id = parse_id(warm)
            if warm_id == cold_id:
                raise ValueError("new warm run reused old run ID")
            second = load_manifest(workspace, warm_id)
            dispositions(second, "cache_restored")
            if any(
                t["attempts"][0]["exit_code"] is not None
                or t["attempts"][0]["launch_state"] != "not_started"
                for t in second["tasks"].values()
            ):
                raise ValueError("warm run launched a task")
            if verify_outputs(workspace, second) != cold_hashes:
                raise ValueError("warm outputs differ from cold outputs")
            summary["checks"]["warm_restore_without_launch"] = "passed"
            status = run(
                [str(runner), "status", cold_id], workspace, env, report / "status.log"
            )
            if f"Run ID: {cold_id}" not in status:
                raise ValueError("status did not read the cold run")
            resumed = run(
                [str(runner), "resume", cold_id], workspace, env, report / "resume.log"
            )
            if parse_id(resumed) != cold_id:
                raise ValueError("resume changed run ID")
            retained = load_manifest(workspace, cold_id)
            dispositions(retained, "retained")
            if (
                len(retained["invocations"]) != 2
                or verify_outputs(workspace, retained) != cold_hashes
            ):
                raise ValueError("completed resume did not retain outputs")
            manifest_file = workspace / ".repro/runs" / cold_id / "manifest.json"
            manifest_file.unlink()
            run(
                [str(runner), "resume", cold_id], workspace, env, report / "rebuild.log"
            )
            rebuilt = load_manifest(workspace, cold_id)
            dispositions(rebuilt, "retained")
            if (
                len(rebuilt["invocations"]) != 3
                or verify_outputs(workspace, rebuilt) != cold_hashes
            ):
                raise ValueError("manifest reconstruction changed output history")
            summary["checks"]["status_resume_and_manifest_rebuild"] = "passed"
            no_cache = run(
                [str(runner), "run", "workflow.yaml", "--no-cache"],
                workspace,
                env,
                report / "no-cache.log",
            )
            no_cache_id = parse_id(no_cache)
            disabled = load_manifest(workspace, no_cache_id)
            if disabled["run"]["cache_enabled"] or len(disabled["invocations"]) != 1:
                raise ValueError("no-cache run mode was not recorded")
            dispositions(disabled, "executed")
            if verify_outputs(workspace, disabled) != cold_hashes:
                raise ValueError("no-cache outputs differ")
            run(
                [str(runner), "resume", no_cache_id],
                workspace,
                env,
                report / "no-cache-resume.log",
            )
            disabled_resume = load_manifest(workspace, no_cache_id)
            if (
                disabled_resume["run"]["cache_enabled"]
                or len(disabled_resume["invocations"]) != 2
            ):
                raise ValueError("no-cache resume mode was not preserved")
            dispositions(disabled_resume, "retained")
            verify_outputs(workspace, disabled_resume)
            with sqlite3.connect(workspace / ".repro/state.sqlite3") as connection:
                rows = connection.execute(
                    "SELECT run_id, cache_enabled FROM runs WHERE run_id IN (?, ?)",
                    (cold_id, no_cache_id),
                ).fetchall()
                launches = connection.execute(
                    "SELECT run_id, SUM(launch_state='started') FROM attempts "
                    "WHERE run_id IN (?, ?, ?) GROUP BY run_id",
                    (cold_id, warm_id, no_cache_id),
                ).fetchall()
            if set(rows) != {(cold_id, 1), (no_cache_id, 0)}:
                raise ValueError("SQLite run state differs from manifest")
            if set(launches) != {(cold_id, 5), (warm_id, 0), (no_cache_id, 5)}:
                raise ValueError("SQLite task launch counts are incorrect")
            summary["checks"]["no_cache_run_resume_and_sqlite"] = "passed"
            summary["result"] = "passed"
        except Exception as error:
            summary["result"] = "failed"
            summary["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            (report / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
