"""Cache format checks and real scheduler behavior in isolated workspaces."""

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from repro_runner import cache as cache_module
from repro_runner import executor
from repro_runner.cache import CacheConflict, CacheStorage
from repro_runner.cli import app
from repro_runner.config import Task, load_workflow
from repro_runner.hashing import Artifact, canonical_bytes, task_identity
from repro_runner.scheduler import resume_workflow, run_workflow


def call(coroutine):
    return asyncio.run(coroutine)


def identity():
    task = Task("build", ("python", "script.py"), (), ("script.py",), ("out/a.txt",))
    return task_identity(task, {"script.py": Artifact("1" * 64, 1)}, [], {"os": "test"})


def storage(tmp_path: Path):
    cache = CacheStorage(tmp_path / ".repro")
    key = identity()
    publish = tmp_path / "publish"
    (publish / "out").mkdir(parents=True)
    (publish / "out/a.txt").write_bytes(b"correct")
    artifacts = {"out/a.txt": Artifact(hashlib.sha256(b"correct").hexdigest(), 7)}
    temporary = call(cache.prepare(key, artifacts, publish, asyncio.Event()))
    call(cache.publish(temporary, key, artifacts, asyncio.Event()))
    return cache, key, artifacts, cache.root / key.key


def replace_metadata(entry: Path, change):
    path = entry / "metadata.json"
    value = json.loads(path.read_text())
    change(value)
    path.write_bytes(canonical_bytes(value))


@pytest.mark.parametrize(
    "damage",
    [
        "missing_metadata",
        "malformed_json",
        "duplicate_key",
        "unsupported_schema",
        "boolean_schema",
        "boolean_size",
        "negative_size",
        "bad_digest",
        "wrong_key",
        "wrong_identity",
        "wrong_inventory",
        "missing_artifact",
        "same_size_corruption",
        "artifact_directory",
        "artifact_symlink",
        "artifact_hardlink",
        "escaping_path",
        "conflicting_path",
        "extra_file",
        "entry_symlink",
    ],
)
def test_corrupt_entry_is_quarantined_and_repairable(
    tmp_path: Path, damage: str
) -> None:
    cache, key, artifacts, entry = storage(tmp_path)
    outside = tmp_path / "sentinel.txt"
    outside.write_text("untouched")
    metadata = entry / "metadata.json"
    artifact = entry / "files/out/a.txt"
    if damage == "missing_metadata":
        metadata.unlink()
    elif damage == "malformed_json":
        metadata.write_text("{")
    elif damage == "duplicate_key":
        metadata.write_text(
            metadata.read_text().replace('"schema":1', '"schema":1,"schema":1')
        )
    elif damage == "unsupported_schema":
        replace_metadata(entry, lambda value: value.update(schema=2))
    elif damage == "boolean_schema":
        replace_metadata(entry, lambda value: value.update(schema=True))
    elif damage == "boolean_size":
        replace_metadata(entry, lambda value: value["artifacts"][0].update(size=True))
    elif damage == "negative_size":
        replace_metadata(entry, lambda value: value["artifacts"][0].update(size=-1))
    elif damage == "bad_digest":
        replace_metadata(
            entry, lambda value: value["artifacts"][0].update(sha256="bad")
        )
    elif damage == "wrong_key":
        replace_metadata(entry, lambda value: value.update(key="0" * 64))
    elif damage == "wrong_identity":
        replace_metadata(entry, lambda value: value.update(identity={"other": 1}))
    elif damage == "wrong_inventory":
        replace_metadata(entry, lambda value: value.update(artifacts=[]))
    elif damage == "missing_artifact":
        artifact.unlink()
    elif damage == "same_size_corruption":
        artifact.write_bytes(b"CORRUPT")
    elif damage == "artifact_directory":
        artifact.unlink()
        artifact.mkdir()
    elif damage == "artifact_symlink":
        artifact.unlink()
        artifact.symlink_to(outside)
    elif damage == "artifact_hardlink":
        artifact.unlink()
        os.link(outside, artifact)
    elif damage == "escaping_path":
        replace_metadata(
            entry, lambda value: value["artifacts"][0].update(path="../sentinel.txt")
        )
    elif damage == "conflicting_path":
        replace_metadata(
            entry,
            lambda value: value["artifacts"].append(
                {"path": "out/A.txt", "sha256": "0" * 64, "size": 1}
            ),
        )
    elif damage == "extra_file":
        (entry / "extra").write_text("extra")
    else:
        moved = tmp_path / "moved"
        entry.rename(moved)
        entry.symlink_to(outside)
    assert call(cache.lookup(key, ("out/a.txt",), asyncio.Event())) is None
    assert not entry.exists()
    assert len(list(cache.root.glob(f".bad-{key.key}-*"))) == 1
    assert outside.read_text() == "untouched"
    publish = tmp_path / "publish"
    temporary = call(cache.prepare(key, artifacts, publish, asyncio.Event()))
    call(cache.publish(temporary, key, artifacts, asyncio.Event()))
    assert call(cache.lookup(key, ("out/a.txt",), asyncio.Event())) == artifacts


def test_incomplete_temp_ignored_and_existing_valid_conflict(tmp_path: Path) -> None:
    cache, key, artifacts, entry = storage(tmp_path)
    (cache.root / ".tmp-interrupted").mkdir()
    assert call(cache.lookup(key, ("out/a.txt",), asyncio.Event())) == artifacts
    other = {"out/a.txt": Artifact("0" * 64, 7)}
    temporary = cache.root / ".tmp-other"
    temporary.mkdir()
    with pytest.raises(CacheConflict):
        call(cache.publish(temporary, key, other, asyncio.Event()))
    assert entry.is_dir()
    assert temporary.is_dir()


def test_cache_lookup_permission_error_is_not_a_miss(
    tmp_path: Path, monkeypatch
) -> None:
    cache, key, _, entry = storage(tmp_path)
    original = cache_module.validate_entry

    async def denied(*args, **kwargs):
        raise PermissionError("injected read denial")

    monkeypatch.setattr(cache_module, "validate_entry", denied)
    with pytest.raises(PermissionError, match="injected read denial"):
        call(cache.lookup(key, ("out/a.txt",), asyncio.Event()))
    assert entry.is_dir()
    assert not list(cache.root.glob(".bad-*"))
    monkeypatch.setattr(cache_module, "validate_entry", original)


def workflow(root: Path, tasks: dict) -> Path:
    path = root / "workflow.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "tasks": tasks}))
    return path


def task(name: str, *, deps=(), inputs=(), output=None, mode="copy") -> dict:
    path = output or f"out/{name}.txt"
    return {
        "command": ["python", "task.py", name, path, mode, *inputs],
        "deps": list(deps),
        "inputs": ["task.py", *inputs],
        "outputs": [path],
    }


def install_script(root: Path, marker: Path) -> None:
    (root / "task.py").write_text(
        "import pathlib,sys\n"
        "name,path,mode,*inputs=sys.argv[1:]\n"
        f"with open({str(marker)!r},'a') as f: f.write(name+'\\n')\n"
        "value='|'.join(pathlib.Path(item).read_text() for item in inputs)\n"
        "if mode=='constant': value='same'\n"
        "if mode=='fail':\n"
        " pathlib.Path(path).parent.mkdir(parents=True,exist_ok=True)\n"
        " pathlib.Path(path).write_text('partial')\n"
        " sys.exit(7)\n"
        "target=pathlib.Path(path)\n"
        "target.parent.mkdir(parents=True,exist_ok=True)\n"
        "target.write_text(name+':'+value)\n"
    )


def run(path: Path, *, workers=2, use_cache=True):
    return call(
        run_workflow(
            load_workflow(path), workers, asyncio.Event(), asyncio.Event(), use_cache
        )
    )


def launched(result) -> list[str]:
    return [name for name, item in result.tasks.items() if item.launched]


def test_cold_warm_restoration_and_copy_independence(tmp_path: Path) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    (tmp_path / "input.txt").write_text("hello")
    path = workflow(
        tmp_path,
        {
            "a": task("a", inputs=("input.txt",)),
            "b": task("b", deps=("a",), inputs=("out/a.txt",)),
        },
    )
    cold = run(path, workers=1)
    assert cold.state == "succeeded"
    assert set(launched(cold)) == {"a", "b"}
    assert all(item.cache_miss for item in cold.tasks.values())
    assert marker.read_text().splitlines() == ["a", "b"]
    expected = {name: (tmp_path / f"out/{name}.txt").read_bytes() for name in "ab"}
    warm = run(path, workers=4)
    assert warm.state == "succeeded"
    assert not launched(warm)
    assert {item.state for item in warm.tasks.values()} == {"cached"}
    assert all(
        item.exit_code is None and item.stdout is None for item in warm.tasks.values()
    )
    for name in "ab":
        (tmp_path / f"out/{name}.txt").unlink()
    restored = run(path)
    assert not launched(restored)
    assert all(
        (tmp_path / f"out/{name}.txt").read_bytes() == expected[name] for name in "ab"
    )
    (tmp_path / "out/a.txt").write_text("stale")
    again = run(path)
    assert not launched(again)
    assert (tmp_path / "out/a.txt").read_bytes() == expected["a"]
    entry = tmp_path / ".repro/cache/v1" / cold.tasks["a"].cache_key
    (tmp_path / "out/a.txt").write_text("workspace edit")
    assert (entry / "files/out/a.txt").read_bytes() == expected["a"]
    attempt_output = (
        tmp_path / ".repro/runs" / cold.run_id / "attempts/a/1/work/out/a.txt"
    )
    attempt_output.write_text("attempt edit")
    assert (entry / "files/out/a.txt").read_bytes() == expected["a"]
    assert marker.read_text().splitlines() == ["a", "b"]


def test_branch_invalidation_and_identical_producer_output(tmp_path: Path) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    (tmp_path / "left.txt").write_text("L1")
    (tmp_path / "right.txt").write_text("R1")
    path = workflow(
        tmp_path,
        {
            "left": task("left", inputs=("left.txt",)),
            "right": task("right", inputs=("right.txt",)),
            "summary": task(
                "summary",
                deps=("left", "right"),
                inputs=("out/left.txt", "out/right.txt"),
            ),
        },
    )
    first = run(path)
    assert first.state == "succeeded"
    original = (tmp_path / "out/summary.txt").read_text()
    (tmp_path / "left.txt").write_text("L2")
    second = run(path)
    assert {name: item.state for name, item in second.tasks.items()} == {
        "left": "succeeded",
        "right": "cached",
        "summary": "succeeded",
    }
    assert (tmp_path / "out/summary.txt").read_text() != original
    assert marker.read_text().splitlines().count("right") == 1

    constant = workflow(
        tmp_path,
        {
            "left": task("left", inputs=("left.txt",), mode="constant"),
            "summary": task("summary", deps=("left",), inputs=("out/left.txt",)),
        },
    )
    third = run(constant)
    assert set(launched(third)) == {"left", "summary"}
    (tmp_path / "left.txt").write_text("L3")
    fourth = run(constant)
    assert fourth.tasks["left"].state == "succeeded"
    assert fourth.tasks["summary"].state == "cached"


def test_failures_do_not_publish_cache_and_independent_branch_survives(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(
        tmp_path,
        {
            "bad": task("bad", mode="fail"),
            "blocked": task("blocked", deps=("bad",), inputs=("out/bad.txt",)),
            "good": task("good"),
        },
    )
    first = run(path)
    assert first.state == "failed"
    assert first.tasks["bad"].state == "failed" and first.tasks["bad"].launched
    assert (
        first.tasks["blocked"].state == "blocked"
        and not first.tasks["blocked"].cache_miss
    )
    assert first.tasks["good"].state == "succeeded"
    root = tmp_path / ".repro/cache/v1"
    assert not (root / first.tasks["bad"].cache_key).exists()
    assert (root / first.tasks["good"].cache_key).is_dir()
    second = run(path)
    assert second.tasks["bad"].launched
    assert second.tasks["good"].state == "cached"


def test_no_cache_bypasses_unusable_cache_root(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(tmp_path, {"a": task("a")})
    assert run(path).tasks["a"].state == "succeeded"
    root = tmp_path / ".repro/cache/v1"
    before = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("cache operation under --no-cache")

    monkeypatch.setattr(CacheStorage, "__init__", forbidden)
    bypass = run(path, use_cache=False)
    assert bypass.tasks["a"].state == "succeeded" and bypass.tasks["a"].launched
    assert not calls
    after = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert before == after
    assert marker.read_text().splitlines() == ["a", "a"]


def test_uncached_run_with_unusable_root(tmp_path: Path) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(tmp_path, {"a": task("a")})
    (tmp_path / ".repro/cache").parent.mkdir()
    (tmp_path / ".repro/cache").symlink_to(tmp_path / "nonexistent")
    assert run(path, use_cache=False).tasks["a"].state == "succeeded"
    with pytest.raises(OSError, match="unsafe cache root"):
        run(path)


@pytest.mark.parametrize("warm", [False, True])
def test_source_mutation_before_publication_rejects_old_identity(
    tmp_path: Path, monkeypatch, warm: bool
) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    source = tmp_path / "input.txt"
    source.write_text("first")
    path = workflow(tmp_path, {"a": task("a", inputs=("input.txt",))})
    if warm:
        initial = run(path)
        assert initial.state == "succeeded"
    original = executor._publish

    async def mutate_then_publish(*args, **kwargs):
        source.write_text("second")
        return await original(*args, **kwargs)

    monkeypatch.setattr(executor, "_publish", mutate_then_publish)
    result = run(path)
    assert result.tasks["a"].state == "failed"
    assert "input source changed" in result.tasks["a"].reason
    assert result.tasks["a"].state != "cached"
    entry = tmp_path / ".repro/cache/v1" / result.tasks["a"].cache_key
    assert entry.exists() is warm


def test_cache_store_copy_and_rename_errors_are_infrastructure_failures(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(tmp_path, {"a": task("a")})
    original_copy = cache_module.copy_file

    async def denied_copy(source, destination):
        if ".tmp-" in str(destination):
            raise PermissionError("injected copy denial")
        return await original_copy(source, destination)

    monkeypatch.setattr(cache_module, "copy_file", denied_copy)
    with pytest.raises(cache_module.CacheOperationalError, match="copy denial"):
        run(path)
    root = tmp_path / ".repro/cache/v1"
    assert list(root.iterdir()) == []
    monkeypatch.setattr(cache_module, "copy_file", original_copy)
    original_rename = cache_module.os.rename

    def denied_rename(source, destination):
        if Path(source).name.startswith(".tmp-"):
            raise PermissionError("injected rename denial")
        return original_rename(source, destination)

    monkeypatch.setattr(cache_module.os, "rename", denied_rename)
    with pytest.raises(cache_module.CacheOperationalError, match="rename denial"):
        run(path)
    assert list(root.iterdir()) == []
    assert (tmp_path / "out/a.txt").read_text() == "a:"


def test_restore_publication_failure_aborts_and_preserves_entry(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(
        tmp_path,
        {
            "a": task("a"),
            "b": task("b", deps=("a",), inputs=("out/a.txt",)),
        },
    )
    cold = run(path)
    assert cold.state == "succeeded"
    entry = tmp_path / ".repro/cache/v1" / cold.tasks["a"].cache_key
    assert entry.is_dir()
    original = executor.os.replace

    def denied(source, destination):
        if Path(destination).name == "a.txt":
            raise PermissionError("injected destination denial")
        return original(source, destination)

    monkeypatch.setattr(executor.os, "replace", denied)
    aborted = CliRunner().invoke(app, ["run", str(path)])
    assert aborted.exit_code == 3, aborted.output
    assert "PublicationOperationalError" in aborted.output
    assert "injected destination denial" in aborted.output
    assert entry.is_dir()
    assert marker.read_text().splitlines() == ["a", "b"]
    with closing(sqlite3.connect(tmp_path / ".repro/state.sqlite3")) as connection:
        run_id = connection.execute(
            "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT outcome FROM runs WHERE run_id=?", (run_id,)
        ).fetchone() == ("interrupted",)
        assert connection.execute(
            "SELECT task_id,state FROM tasks WHERE run_id=? ORDER BY task_id",
            (run_id,),
        ).fetchall() == [("a", "interrupted"), ("b", "interrupted")]
        assert connection.execute(
            "SELECT task_id,state,launch_state FROM attempts WHERE run_id=?",
            (run_id,),
        ).fetchall() == [("a", "interrupted", "not_started")]
        assert connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE run_id=?", (run_id,)
        ).fetchone() == (0,)
    monkeypatch.setattr(executor.os, "replace", original)
    monkeypatch.chdir(tmp_path)
    resumed = call(resume_workflow(run_id, None, asyncio.Event(), asyncio.Event()))
    assert resumed.state == "succeeded"
    assert resumed.tasks["a"].state == "cached"
    assert resumed.tasks["b"].state == "cached"
    assert marker.read_text().splitlines() == ["a", "b"]


def test_missing_output_does_not_populate_entry(tmp_path: Path) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    declaration = task("a")
    declaration["outputs"].append("out/missing.txt")
    (tmp_path / "out").mkdir()
    (tmp_path / "out/a.txt").write_text("old")
    path = workflow(tmp_path, {"a": declaration, "b": task("b")})
    result = run(path, workers=1)
    assert result.state == "failed"
    assert result.tasks["a"].state == "failed"
    assert result.tasks["a"].launched
    assert result.tasks["b"].state == "succeeded"
    assert (tmp_path / "out/a.txt").read_text() == "old"
    assert (tmp_path / "out/b.txt").read_text() == "b:"
    assert not (tmp_path / ".repro/cache/v1" / result.tasks["a"].cache_key).exists()


def test_glob_inventory_changes_identity(tmp_path: Path) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.txt").write_text("A")
    declaration = task("a", inputs=("data/*.txt",))
    declaration["command"] = ["python", "task.py", "a", "out/a.txt", "constant"]
    path = workflow(tmp_path, {"a": declaration})
    first = run(path)
    assert first.tasks["a"].state == "succeeded"
    (data / "b.txt").write_text("B")
    second = run(path)
    assert second.tasks["a"].state == "succeeded"
    assert second.tasks["a"].cache_key != first.tasks["a"].cache_key
    (data / "b.txt").unlink()
    third = run(path)
    assert third.tasks["a"].state == "cached"
    assert third.tasks["a"].cache_key == first.tasks["a"].cache_key


def test_declared_script_edit_invalidates_unchanged_command(tmp_path: Path) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(tmp_path, {"a": task("a")})
    first = run(path)
    assert first.tasks["a"].state == "succeeded"
    script = tmp_path / "task.py"
    script.write_text(script.read_text() + "\n# changed script bytes\n")
    second = run(path)
    assert second.tasks["a"].state == "succeeded"
    assert second.tasks["a"].launched
    assert second.tasks["a"].cache_key != first.tasks["a"].cache_key
    assert marker.read_text().splitlines() == ["a", "a"]


def test_metadata_write_error_leaves_no_final_entry(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "markers"
    install_script(tmp_path, marker)
    path = workflow(tmp_path, {"a": task("a")})
    original = Path.open

    def denied(self, *args, **kwargs):
        if self.name == "metadata.json" and self.parent.name.startswith(".tmp-"):
            raise PermissionError("injected metadata denial")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(cache_module.CacheOperationalError, match="metadata denial"):
        run(path)
    assert list((tmp_path / ".repro/cache/v1").iterdir()) == []
