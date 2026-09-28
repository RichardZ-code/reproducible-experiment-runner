"""Numerical and installed-CLI checks for the five-task example."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "simulation"
RUNNER = Path(sys.prefix) / "bin" / "runner"


def copy_example(tmp_path: Path) -> Path:
    target = tmp_path / "simulation"
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


def run_id(result: subprocess.CompletedProcess[str]) -> str:
    return next(
        line.split(": ", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("Run ID: ")
    )


def manifest(workspace: Path, result: subprocess.CompletedProcess[str]) -> dict:
    path = workspace / ".repro" / "runs" / run_id(result) / "manifest.json"
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_hand_worked_recurrence_and_summary() -> None:
    sys.path.insert(0, str(EXAMPLE))
    try:
        from simulate import simulate
        from summarize import summarize

        source = {"seed": 1, "sample_count": 2, "steps": 2, "initial_states": [1, 2]}
        # (2*x+1) mod 7: 1 -> 3 -> 0; 2 -> 5 -> 4.
        first = simulate(
            source, {"branch": "a", "multiplier": 2, "increment": 1, "modulus": 7}
        )
        assert (
            first["aggregate"],
            first["weighted_checksum"],
            first["minimum"],
            first["maximum"],
        ) == (4, 8, 0, 4)
        second = simulate(
            source, {"branch": "b", "multiplier": 2, "increment": 2, "modulus": 7}
        )
        third = simulate(
            source, {"branch": "c", "multiplier": 1, "increment": 0, "modulus": 7}
        )
        summary = summarize([first, second, third])
        assert (
            summary["combined_total"]
            == first["aggregate"] + second["aggregate"] + third["aggregate"]
        )
        assert summary["branches"] == [first, second, third]
        assert second["aggregate"] != first["aggregate"]
        with pytest.raises(ValueError, match="expected branch"):
            summarize([first, first, third])
        with pytest.raises(ValueError, match="parameters"):
            simulate(
                source, {"branch": "a", "multiplier": 7, "increment": 1, "modulus": 7}
            )
        with pytest.raises(ValueError, match="input size"):
            simulate(
                {**source, "sample_count": 0},
                {"branch": "a", "multiplier": 2, "increment": 1, "modulus": 7},
            )
    finally:
        sys.path.remove(str(EXAMPLE))
        sys.modules.pop("simulate", None)
        sys.modules.pop("summarize", None)


def test_generator_seed_counts_and_invalid_json_from_staged_paths(
    tmp_path: Path,
) -> None:
    workspace = copy_example(tmp_path)
    config = workspace / "config/base.json"

    def generate(seed: int, output: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "generate.py",
                "--config",
                "config/base.json",
                "--seed",
                str(seed),
                "--output",
                output,
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    config.write_text('{"seed":11,"sample_count":3,"steps":2}\n')
    first = generate(11, "out/first.json")
    repeated = generate(11, "out/repeated.json")
    assert first.returncode == repeated.returncode == 0
    assert (workspace / "out/first.json").read_bytes() == (
        workspace / "out/repeated.json"
    ).read_bytes()
    assert (
        len(json.loads((workspace / "out/first.json").read_text())["initial_states"])
        == 3
    )
    config.write_text('{"seed":12,"sample_count":3,"steps":2}\n')
    changed = generate(12, "out/changed.json")
    assert changed.returncode == 0
    assert (workspace / "out/first.json").read_bytes() != (
        workspace / "out/changed.json"
    ).read_bytes()
    mismatch = generate(11, "out/mismatch.json")
    assert mismatch.returncode == 1 and "seed must match" in mismatch.stderr
    config.write_text('{"seed":12,"sample_count":0,"steps":2}\n')
    invalid_count = generate(12, "out/invalid-count.json")
    assert invalid_count.returncode == 1 and "sample_count" in invalid_count.stderr
    config.write_text('{"seed":12,"sample_count":NaN,"steps":2}\n')
    nonfinite = generate(12, "out/nonfinite.json")
    assert nonfinite.returncode == 1 and "nonfinite" in nonfinite.stderr
    assert not any(
        (workspace / "out" / name).exists()
        for name in ("mismatch.json", "invalid-count.json", "nonfinite.json")
    )


def test_example_cache_invalidation_and_manifest(tmp_path: Path) -> None:
    workspace = copy_example(tmp_path)
    assert cli(workspace, "validate", "workflow.yaml").returncode == 0
    cold = cli(tmp_path, "run", str(workspace / "workflow.yaml"), "--workers", "4")
    assert cold.returncode == 0, cold.stderr
    first = manifest(workspace, cold)
    assert first["manifest_schema_version"] == 1
    assert first["run"]["outcome"] == "succeeded"
    assert {
        row["disposition"] for row in first["invocations"][0]["resolutions"].values()
    } == {"executed"}
    summary_hash = digest(workspace / "out/summary.json")
    assert (
        first["tasks"]["summarize"]["attempts"][0]["artifacts"][0]["sha256"]
        == summary_hash
    )
    warm = cli(workspace, "run", "workflow.yaml", "--workers", "4")
    assert warm.returncode == 0, warm.stderr
    second = manifest(workspace, warm)
    assert {
        row["disposition"] for row in second["invocations"][0]["resolutions"].values()
    } == {"cache_restored"}
    assert all(
        row["exit_code"] is None
        for task in second["tasks"].values()
        for row in task["attempts"]
    )
    assert summary_hash == digest(workspace / "out/summary.json")
    before = json.loads((workspace / "out/summary.json").read_text())
    unaffected = {
        branch: digest(workspace / f"out/{branch}.json") for branch in ("a", "c")
    }
    config_path = workspace / "config/b.json"
    config = json.loads(config_path.read_text())
    config["increment"] += 1
    config_path.write_text(json.dumps(config) + "\n")
    changed = cli(workspace, "run", "workflow.yaml", "--workers", "4")
    assert changed.returncode == 0, changed.stderr
    third = manifest(workspace, changed)
    dispositions = {
        key: value["disposition"]
        for key, value in third["invocations"][0]["resolutions"].items()
    }
    assert dispositions == {
        "generate": "cache_restored",
        "simulate_a": "cache_restored",
        "simulate_b": "executed",
        "simulate_c": "cache_restored",
        "summarize": "executed",
    }
    assert {
        branch: digest(workspace / f"out/{branch}.json") for branch in ("a", "c")
    } == unaffected
    after = json.loads((workspace / "out/summary.json").read_text())
    assert after["branches"][1]["aggregate"] != before["branches"][1]["aggregate"]
    assert after["combined_total"] != before["combined_total"]
    inputs = third["tasks"]["simulate_b"]["attempts"][0]["inputs"]
    assert next(
        item["sha256"] for item in inputs if item["path"] == "config/b.json"
    ) == digest(config_path)


def test_no_cache_worker_count_keeps_artifact_bytes(tmp_path: Path) -> None:
    hashes = []
    for workers in (1, 4):
        workspace = copy_example(tmp_path / str(workers))
        result = cli(
            workspace, "run", "workflow.yaml", "--workers", str(workers), "--no-cache"
        )
        assert result.returncode == 0, result.stderr
        data = manifest(workspace, result)
        assert data["run"]["cache_enabled"] is False
        assert {
            row["disposition"] for row in data["invocations"][0]["resolutions"].values()
        } == {"executed"}
        hashes.append(
            {
                name: digest(workspace / f"out/{name}.json")
                for name in ("input", "a", "b", "c", "summary")
            }
        )
    assert hashes[0] == hashes[1]


def test_failed_branch_then_resume_preserves_history(tmp_path: Path) -> None:
    workspace = copy_example(tmp_path)
    branch = workspace / "config/b.json"
    config = json.loads(branch.read_text())
    config["modulus"] = 1
    branch.write_text(json.dumps(config))
    failed = cli(workspace, "run", "workflow.yaml", "--no-cache")
    assert failed.returncode == 1, failed.stderr
    prior = manifest(workspace, failed)
    assert prior["run"]["outcome"] == "failed"
    assert prior["tasks"]["simulate_b"]["state"] == "failed"
    assert prior["tasks"]["summarize"]["state"] == "blocked"
    config["modulus"] = 10009
    branch.write_text(json.dumps(config))
    resumed = cli(workspace, "resume", run_id(failed))
    assert resumed.returncode == 0, resumed.stderr
    current = manifest(workspace, resumed)
    assert [row["outcome"] for row in current["invocations"]] == ["failed", "succeeded"]
    assert (
        current["invocations"][1]["resolutions"]["generate"]["disposition"]
        == "retained"
    )
    assert (
        current["invocations"][1]["resolutions"]["simulate_b"]["disposition"]
        == "executed"
    )
    assert current["run"]["outcome"] == "succeeded"
