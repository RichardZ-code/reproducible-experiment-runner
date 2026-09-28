"""Durable-state failures stop admission without inventing task success."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from repro_runner.config import load_workflow
from repro_runner.scheduler import resume_workflow, run_workflow
from repro_runner.state import StateCommitUncertain, StateStore


def fixture(root: Path) -> tuple[Path, Path]:
    marker = root / "launches"
    (root / "task.py").write_text(
        "import pathlib,sys\n"
        "name,out,*inputs=sys.argv[1:]\n"
        f"with open({str(marker)!r},'a') as f:f.write(name+'\\n')\n"
        "p=pathlib.Path(out);p.parent.mkdir(parents=True,exist_ok=True)\n"
        "p.write_text(name+':'+','.join(pathlib.Path(i).read_text() for i in inputs))\n"
    )
    path = root / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": {
                    "a": {
                        "command": ["python", "task.py", "a", "out/a.txt"],
                        "inputs": ["task.py"],
                        "outputs": ["out/a.txt"],
                    },
                    "b": {
                        "command": ["python", "task.py", "b", "out/b.txt", "out/a.txt"],
                        "deps": ["a"],
                        "inputs": ["task.py", "out/a.txt"],
                        "outputs": ["out/b.txt"],
                    },
                },
            }
        )
    )
    return path, marker


def run(path: Path):
    return asyncio.run(
        run_workflow(load_workflow(path), 1, asyncio.Event(), asyncio.Event(), True)
    )


def record(root: Path):
    with closing(sqlite3.connect(root / ".repro/state.sqlite3")) as connection:
        run_id, outcome = connection.execute(
            "SELECT run_id,outcome FROM runs"
        ).fetchone()
        attempts = connection.execute(
            "SELECT task_id,attempt_no,state,launch_state FROM attempts ORDER BY task_id,attempt_no"
        ).fetchall()
        return run_id, outcome, attempts


@pytest.mark.parametrize(
    "boundary", ["allocate", "launch_intent", "success", "finalize"]
)
def test_state_write_failure_stops_run_and_preserves_prior_evidence(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    path, marker = fixture(tmp_path)
    method = {
        "allocate": "allocate_attempt",
        "launch_intent": "launch_requested",
        "success": "record_result",
        "finalize": "finish_invocation",
    }[boundary]
    original = getattr(StateStore, method)

    def fail(self, *args, **kwargs):
        if boundary == "success" and args[0].task_id != "a":
            return original(self, *args, **kwargs)
        raise sqlite3.OperationalError(f"injected {boundary} failure")

    monkeypatch.setattr(StateStore, method, fail)
    with pytest.raises(sqlite3.OperationalError, match=boundary):
        run(path)
    identity, outcome, attempts = record(tmp_path)
    assert outcome == "interrupted"
    if boundary == "allocate":
        assert attempts == [] and not marker.exists()
    elif boundary == "launch_intent":
        assert attempts == [("a", 1, "interrupted", "not_started")]
        assert not marker.exists()
    elif boundary == "success":
        assert attempts == [("a", 1, "interrupted", "started")]
        assert marker.read_text().splitlines() == ["a"]
        with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
                == 0
            )
    else:
        assert attempts == [
            ("a", 1, "succeeded", "started"),
            ("b", 1, "succeeded", "started"),
        ]
        assert marker.read_text().splitlines() == ["a", "b"]
    monkeypatch.setattr(StateStore, method, original)
    monkeypatch.chdir(tmp_path)
    resumed = asyncio.run(
        resume_workflow(identity, None, asyncio.Event(), asyncio.Event())
    )
    assert resumed.state == "succeeded"
    if boundary == "finalize":
        assert all(item.retained for item in resumed.tasks.values())
    elif boundary == "success":
        assert resumed.tasks["a"].state == "cached"
        assert resumed.tasks["b"].state == "succeeded"
    assert "b" in resumed.tasks


def test_post_commit_acknowledgment_gap_keeps_recorded_facts(
    tmp_path: Path, monkeypatch
) -> None:
    path, marker = fixture(tmp_path)
    original = StateStore.record_result

    def committed_then_unacknowledged(self, result):
        original(self, result)
        raise StateCommitUncertain("injected post-commit acknowledgment gap")

    monkeypatch.setattr(StateStore, "record_result", committed_then_unacknowledged)
    with pytest.raises(StateCommitUncertain, match="acknowledgment gap"):
        run(path)
    identity, outcome, attempts = record(tmp_path)
    assert outcome == "running"
    assert attempts == [("a", 1, "succeeded", "started")]
    assert marker.read_text().splitlines() == ["a"]
    monkeypatch.setattr(StateStore, "record_result", original)
    monkeypatch.chdir(tmp_path)
    resumed = asyncio.run(
        resume_workflow(identity, None, asyncio.Event(), asyncio.Event())
    )
    assert resumed.state == "succeeded"
    assert resumed.tasks["a"].retained
    assert resumed.tasks["b"].launched
