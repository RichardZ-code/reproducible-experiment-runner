"""Fast checks for P09 collection and analysis contracts."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import run_benchmark as benchmark


def synthetic_scheduling() -> tuple[dict, list[dict]]:
    metadata = {
        "mode": "formal",
        "repetitions": 5,
        "suites": ["scheduling"],
        "profile": {},
        "protocol_version": 1,
        "source": {"aggregate_sha256": "sample"},
        "git_revision": "sample",
        "git_dirty": True,
    }
    rows = []
    order = 0
    for workers, durations in (
        (1, [1, 2, 3, 4, 5]),
        (2, [2, 2, 2, 2, 2]),
        (4, [4, 4, 4, 4, 4]),
        (8, [6, 6, 6, 6, 6]),
    ):
        for repetition, seconds in enumerate(durations, 1):
            order += 1
            rows.append(
                {
                    "sample_id": f"A-{workers}-{repetition}",
                    "suite": "scheduling",
                    "condition": f"workers={workers}",
                    "kind": "measured",
                    "order": order,
                    "repetition": repetition,
                    "profile": {"tasks": 24, "requested_wait_seconds": 0.25},
                    "wall_ns": seconds * 1_000_000_000,
                    "exit_code": 0,
                    "included": True,
                    "evidence": {"observed_max_concurrency": workers},
                }
            )
    return metadata, rows


def test_analysis_uses_median_and_preserves_slowdown() -> None:
    metadata, rows = synthetic_scheduling()
    summary = benchmark.render_summary(metadata, rows)
    assert "| 1 | 5 | 3.0000 | 1.0000 | 5.0000 | 8.0000 | 1.0000" in summary
    assert "| 2 | 5 | 2.0000 | 2.0000 | 2.0000 | 12.0000 | 1.5000" in summary
    assert "| 8 | 5 | 6.0000 | 6.0000 | 6.0000 | 4.0000 | 0.5000" in summary
    assert benchmark.stats([rows[0], rows[1], rows[2], rows[3]])[0] == 2.5


def test_condition_order_indices_restart_per_suite() -> None:
    metadata, rows = synthetic_scheduling()
    metadata["suites"].append("compute")
    metadata["profile"] = {"sample_count": 5000, "steps": 1000}
    for workers in (1, 4):
        for repetition in range(1, 6):
            rows.append(
                {
                    "sample_id": f"B-{workers}-{repetition}",
                    "suite": "compute",
                    "condition": f"workers={workers}",
                    "kind": "measured",
                    "order": (repetition - 1) * 2 + (workers == 4),
                    "repetition": repetition,
                    "profile": metadata["profile"],
                    "wall_ns": 1_000_000_000,
                    "exit_code": 0,
                    "included": True,
                    "evidence": {},
                }
            )
    assert len(benchmark.measured_groups(rows, metadata)) == 6


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate_repetition", "duplicate_order", "profile", "nonfinite"],
)
def test_incomplete_or_incompatible_measured_data_is_rejected(mutation: str) -> None:
    metadata, rows = synthetic_scheduling()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate_repetition":
        rows[-1]["repetition"] = 1
    elif mutation == "duplicate_order":
        rows[-1]["order"] = rows[0]["order"]
    elif mutation == "profile":
        rows[-1]["profile"]["tasks"] = 23
    else:
        rows[-1]["wall_ns"] = float("nan")
        with pytest.raises(ValueError):
            json.dumps(rows[-1], allow_nan=False)
        return
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.render_summary(metadata, rows)


def test_jsonl_duplicate_id_and_truncated_line_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "samples.jsonl"
    row = {"sample_id": "once", "included": False}
    raw.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    with pytest.raises(benchmark.BenchmarkError, match="duplicate"):
        benchmark.load_rows(raw)
    raw.write_text(json.dumps(row) + "\n{" + "\n")
    with pytest.raises(benchmark.BenchmarkError, match="invalid JSONL"):
        benchmark.load_rows(raw)


def test_smoke_and_warmup_cannot_be_formal_summary() -> None:
    metadata, rows = synthetic_scheduling()
    metadata["mode"] = "smoke"
    with pytest.raises(benchmark.BenchmarkError, match="smoke/pilot"):
        benchmark.render_summary(metadata, rows)
    metadata["mode"] = "formal"
    rows[-1]["included"] = False
    rows[-1]["kind"] = "warmup"
    with pytest.raises(benchmark.BenchmarkError, match="incomplete"):
        benchmark.render_summary(metadata, rows)


def test_fresh_example_workspaces_and_source_preserved(tmp_path: Path) -> None:
    profile = {"sample_count": 32, "steps": 20}
    source_hash = benchmark.sha(benchmark.EXAMPLE / "config/b.json")
    one = benchmark.example_workspace(tmp_path, "one", profile)
    two = benchmark.example_workspace(tmp_path, "two", profile)
    benchmark.change_branch_b(one)
    assert json.loads((one / "config/b.json").read_text())["increment"] == 6
    assert json.loads((two / "config/b.json").read_text())["increment"] == 5
    assert benchmark.sha(benchmark.EXAMPLE / "config/b.json") == source_hash
    assert not (one / ".repro").exists() and not (two / ".repro").exists()
    with pytest.raises(FileExistsError):
        benchmark.example_workspace(tmp_path, "one", profile)


def test_failure_row_is_saved_before_exception(tmp_path: Path) -> None:
    raw = tmp_path / "samples.jsonl"
    row = benchmark.row_base(
        "failure", "scheduling", "workers=1", "measured", 1, 1, {}, 1, False
    )
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.save_trial(
            raw,
            row,
            lambda: (_ for _ in ()).throw(benchmark.BenchmarkError("missing output")),
        )
    saved = benchmark.load_rows(raw)
    assert (
        len(saved) == 1 and saved[0]["kind"] == "failure" and not saved[0]["included"]
    )


def test_failed_invocation_preserves_observed_exit_and_duration(tmp_path: Path) -> None:
    raw = tmp_path / "samples.jsonl"
    row = benchmark.row_base(
        "failed-cli", "compute", "workers=1", "measured", 1, 1, {}, 1, False
    )
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.save_trial(
            raw,
            row,
            lambda: (_ for _ in ()).throw(
                benchmark.InvocationFailure(3, 123456789, "error")
            ),
        )
    saved = benchmark.load_rows(raw)[0]
    assert saved["exit_code"] == 3 and saved["wall_ns"] == 123456789
    assert saved["kind"] == "failure" and saved["evidence"] is None


def test_installed_cli_and_hash_failure(tmp_path: Path) -> None:
    path, outputs = benchmark.scheduling_workspace(tmp_path, "trial", "0.001")
    wall, evidence = benchmark.record_run(path, outputs, 4, False)
    assert wall > 0 and evidence["launches"] == 24
    benchmark.assert_counts(evidence, launches=24, executed=24)
    (path / next(iter(outputs.values()))).unlink()
    with pytest.raises(benchmark.BenchmarkError, match="missing declared output"):
        benchmark.inspect(path, outputs)


def test_timeout_is_not_success(tmp_path: Path) -> None:
    code, wall, _, _ = benchmark.run_process(
        [sys.executable, "-c", "import time;time.sleep(5)"], tmp_path, timeout=0.02
    )
    assert code is None and wall > 0
    with pytest.raises(benchmark.InvocationFailure, match="timeout"):
        benchmark.ensure_exit(code, wall, "")


def test_summarize_only_never_runs_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata, rows = synthetic_scheduling()
    batch = tmp_path / "batch"
    batch.mkdir()
    benchmark.write_json(batch / "metadata.json", metadata)
    (batch / "samples.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    monkeypatch.setattr(
        benchmark, "run_process", lambda *args, **kwargs: pytest.fail("runner launched")
    )
    destination = benchmark.publish_summary(batch)
    original = destination.read_text()
    assert (
        benchmark.publish_summary(batch, tmp_path / "again.md").read_text() == original
    )


def test_command_rejects_existing_batch_and_missing_profile(tmp_path: Path) -> None:
    script = benchmark.ROOT / "benchmarks/run_benchmark.py"
    existing = tmp_path / "existing"
    existing.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "scheduling",
            "--output-dir",
            str(existing),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and list(existing.iterdir()) == []
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "compute",
            "--output-dir",
            str(tmp_path / "new"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and not (tmp_path / "new").exists()
