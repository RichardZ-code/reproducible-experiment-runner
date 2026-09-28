"""Versioned content identity and selected environment checks."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from repro_runner import hashing
from repro_runner.config import Task
from repro_runner.hashing import Artifact, environment_record, hash_file, task_identity


def artifact(content: bytes) -> Artifact:
    return Artifact(hashlib.sha256(content).hexdigest(), len(content))


def test_streaming_hash_tiny_and_multiple_chunks(tmp_path: Path) -> None:
    for content in (b"abc", b"x" * (2 * 1024 * 1024 + 17) + b"z"):
        path = tmp_path / "data"
        path.write_bytes(content)
        assert asyncio.run(hash_file(path)) == artifact(content)


def test_expected_canonical_record_and_identity_changes() -> None:
    task = Task(
        "a",
        ("python", "task.py", "a b", "$(literal)"),
        (),
        ("task.py", "data.txt"),
        ("out/b", "out/a"),
    )
    snapshots = {"task.py": artifact(b"script"), "data.txt": artifact(b"data")}
    dependencies = [
        {"task": "root", "path": "out/root", "sha256": artifact(b"root").digest}
    ]
    environment = {"python_version": "3.12.14", "dependency_lock_sha256": "lock"}
    identity = task_identity(task, snapshots, dependencies, environment)
    expected = {
        "key_schema": 1,
        "cache_schema": 1,
        "runner_version": "0.1.0",
        "command": ["python", "task.py", "a b", "$(literal)"],
        "outputs": ["out/a", "out/b"],
        "inputs": [
            {"path": "data.txt", "sha256": hashlib.sha256(b"data").hexdigest()},
            {"path": "task.py", "sha256": hashlib.sha256(b"script").hexdigest()},
        ],
        "dependency_outputs": dependencies,
        "environment": environment,
    }
    expected_bytes = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert identity.record == expected
    assert identity.key == hashlib.sha256(expected_bytes).hexdigest()
    assert (
        task_identity(
            task, dict(reversed(list(snapshots.items()))), dependencies, environment
        ).key
        == identity.key
    )
    assert task_identity(task, snapshots, dependencies, environment).key == identity.key

    changed_command = Task(
        "another_label",
        ("python", "task.py", "$(literal)", "a b"),
        (),
        task.inputs,
        task.outputs,
    )
    assert (
        task_identity(changed_command, snapshots, dependencies, environment).key
        != identity.key
    )
    changed_script = dict(snapshots, **{"task.py": artifact(b"script edit")})
    assert (
        task_identity(task, changed_script, dependencies, environment).key
        != identity.key
    )
    changed_data = dict(snapshots, **{"data.txt": artifact(b"new data")})
    assert (
        task_identity(task, changed_data, dependencies, environment).key != identity.key
    )
    renamed = {"task.py": snapshots["task.py"], "renamed.txt": snapshots["data.txt"]}
    assert task_identity(task, renamed, dependencies, environment).key != identity.key
    added = dict(snapshots, **{"extra.txt": artifact(b"data")})
    assert task_identity(task, added, dependencies, environment).key != identity.key
    assert (
        task_identity(
            task, {"task.py": snapshots["task.py"]}, dependencies, environment
        ).key
        != identity.key
    )
    other_outputs = Task("a", task.command, (), task.inputs, ("out/a",))
    assert (
        task_identity(other_outputs, snapshots, dependencies, environment).key
        != identity.key
    )
    changed_dependency = [
        {"task": "root", "path": "out/root", "sha256": artifact(b"changed").digest}
    ]
    assert (
        task_identity(task, snapshots, changed_dependency, environment).key
        != identity.key
    )
    assert (
        task_identity(
            task, snapshots, dependencies, dict(environment, python_version="3.12.15")
        ).key
        != identity.key
    )
    assert (
        task_identity(
            task,
            snapshots,
            dependencies,
            dict(environment, dependency_lock_sha256="other"),
        ).key
        != identity.key
    )
    assert (
        task_identity(
            Task("different_label", task.command, (), task.inputs, task.outputs),
            snapshots,
            dependencies,
            environment,
        ).key
        == identity.key
    )


def test_timestamps_unrelated_file_workers_and_workspace_do_not_enter_key(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    task = Task("a", ("python", "task.py"), (), ("data.txt",), ("out.txt",))
    for root in (first, second):
        (root / "data.txt").write_bytes(b"same")
    snapshot_a = {"data.txt": asyncio.run(hash_file(first / "data.txt"))}
    before = task_identity(task, snapshot_a, [], {"env": "same"}).key
    os.utime(first / "data.txt", None)
    (first / "unrelated.txt").write_bytes(b"changed")
    snapshot_b = {"data.txt": asyncio.run(hash_file(first / "data.txt"))}
    snapshot_c = {"data.txt": asyncio.run(hash_file(second / "data.txt"))}
    assert before == task_identity(task, snapshot_b, [], {"env": "same"}).key
    assert before == task_identity(task, snapshot_c, [], {"env": "same"}).key


def fake_source(root: Path) -> Path:
    package = root / "src/repro_runner"
    package.mkdir(parents=True)
    loaded = package / "__init__.py"
    loaded.write_text("__version__ = '0.1.0'\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "reproducible-experiment-runner"\nversion = "0.1.0"\n'
    )
    (root / "requirements").mkdir()
    return loaded


def test_environment_lock_selection_inventory_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = fake_source(tmp_path / "source")
    locks = source.parents[2] / "requirements"
    (locks / "dev-macos.lock").write_bytes(b"mac lock")
    (locks / "dev-linux.lock").write_bytes(b"linux lock")
    packages = [("Z_Pkg", "2"), ("a.pkg", "1")]
    mac = asyncio.run(
        environment_record(
            package_file=source, os_name="Darwin", distributions=packages
        )
    )
    linux = asyncio.run(
        environment_record(
            package_file=source, os_name="Linux", distributions=reversed(packages)
        )
    )
    assert mac["python_implementation"]
    assert mac["python_version"]
    assert mac["python_cache_tag"]
    assert mac["os"] == "Darwin"
    assert linux["os"] == "Linux"
    assert mac["machine"]
    assert mac["os_release"]
    assert mac["distributions"] == [
        {"name": "a-pkg", "version": "1"},
        {"name": "z-pkg", "version": "2"},
    ]
    assert mac["dependency_lock_sha256"] == hashlib.sha256(b"mac lock").hexdigest()
    assert linux["dependency_lock_sha256"] == hashlib.sha256(b"linux lock").hexdigest()
    assert mac["dependency_lock_source"] == "dev-macos.lock"
    assert linux["dependency_lock_source"] == "dev-linux.lock"
    monkeypatch.chdir(tmp_path)
    again = asyncio.run(
        environment_record(
            package_file=source, os_name="Darwin", distributions=packages
        )
    )
    assert again == mac
    assert str(tmp_path) not in json.dumps(mac)


def test_unavailable_lock_and_unexpected_lock_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = fake_source(tmp_path / "source")
    missing = asyncio.run(
        environment_record(package_file=source, os_name="Darwin", distributions=[])
    )
    assert missing["dependency_lock_sha256"] is None
    assert missing["dependency_lock_source"] == "unavailable"
    lock = source.parents[2] / "requirements/dev-macos.lock"
    lock.write_bytes(b"present")
    present = asyncio.run(
        environment_record(package_file=source, os_name="Darwin", distributions=[])
    )
    assert present["dependency_lock_sha256"] != missing["dependency_lock_sha256"]
    lock.unlink()
    lock.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError, match="not a regular file"):
        asyncio.run(
            environment_record(package_file=source, os_name="Darwin", distributions=[])
        )
    lock.unlink()
    lock.write_bytes(b"present")

    async def denied(path: Path) -> Artifact:
        raise PermissionError("controlled lock read failure")

    monkeypatch.setattr(hashing, "hash_file", denied)
    with pytest.raises(PermissionError, match="controlled lock read failure"):
        asyncio.run(
            environment_record(package_file=source, os_name="Darwin", distributions=[])
        )


def test_wheel_like_package_has_explicit_null_lock(tmp_path: Path) -> None:
    loaded = tmp_path / "site-packages/repro_runner/__init__.py"
    loaded.parent.mkdir(parents=True)
    loaded.write_text("package")
    result = asyncio.run(
        environment_record(package_file=loaded, os_name="Darwin", distributions=[])
    )
    assert result["dependency_lock_sha256"] is None
    assert result["dependency_lock_source"] == "unavailable"
    assert str(tmp_path) not in json.dumps(result)
