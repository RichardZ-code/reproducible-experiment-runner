"""Streaming file integrity and versioned task-content identity."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Mapping

import repro_runner
from repro_runner.config import Task
from repro_runner.errors import ValidationError

_CHUNK = 1024 * 1024
KEY_SCHEMA = 1
CACHE_SCHEMA = 1
_NAME = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class Artifact:
    digest: str
    size: int


@dataclass(frozen=True)
class CacheIdentity:
    record: dict[str, object]
    key: str


@contextmanager
def regular_reader(path: Path) -> Iterator[BinaryIO]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
    finally:
        os.close(descriptor)


async def hash_file(path: Path) -> Artifact:
    digest = hashlib.sha256()
    size = 0
    with regular_reader(path) as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            await asyncio.sleep(0)
    return Artifact(digest.hexdigest(), size)


async def copy_file(source: Path, destination: Path) -> Artifact:
    digest = hashlib.sha256()
    size = 0
    with regular_reader(source) as input_file, destination.open("xb") as output_file:
        while chunk := input_file.read(_CHUNK):
            output_file.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            await asyncio.sleep(0)
    return Artifact(digest.hexdigest(), size)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def task_identity(
    task: Task,
    snapshots: Mapping[str, Artifact],
    dependency_outputs: list[dict[str, str]],
    environment: Mapping[str, object],
) -> CacheIdentity:
    record: dict[str, object] = {
        "key_schema": KEY_SCHEMA,
        "cache_schema": CACHE_SCHEMA,
        "runner_version": repro_runner.__version__,
        "command": list(task.command),
        "outputs": sorted(task.outputs),
        "inputs": [
            {"path": path, "sha256": snapshots[path].digest}
            for path in sorted(snapshots)
        ],
        "dependency_outputs": sorted(
            dependency_outputs, key=lambda row: (row["task"], row["path"])
        ),
        "environment": dict(environment),
    }
    return CacheIdentity(record, hashlib.sha256(canonical_bytes(record)).hexdigest())


def _source_root(package_file: Path) -> Path | None:
    loaded = package_file.resolve(strict=True)
    if loaded.name != "__init__.py" or loaded.parent.name != "repro_runner":
        return None
    if loaded.parent.parent.name != "src":
        return None
    root = loaded.parent.parent.parent
    project = root / "pyproject.toml"
    try:
        info = project.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError("source pyproject.toml is not a regular file")
    with regular_reader(project) as source:
        metadata = tomllib.load(source)
    project_info = metadata.get("project", {})
    if (
        project_info.get("name") != "reproducible-experiment-runner"
        or project_info.get("version") != repro_runner.__version__
    ):
        return None
    return root


async def environment_record(
    *,
    package_file: Path | None = None,
    os_name: str | None = None,
    distributions: Iterable[tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Select identity fields without recording ambient paths or credentials."""
    system = os_name if os_name is not None else platform.system()
    if system not in {"Darwin", "Linux"}:
        raise OSError(f"unsupported cache platform: {system}")
    if distributions is None:
        installed = (
            (distribution.metadata.get("Name"), distribution.version)
            for distribution in importlib.metadata.distributions()
        )
    else:
        installed = distributions
    packages: list[dict[str, str]] = []
    for name, version in installed:
        if not name or not version:
            raise OSError("installed distribution has incomplete name or version")
        packages.append({"name": _NAME.sub("-", name).lower(), "version": version})
    packages.sort(key=lambda row: (row["name"], row["version"]))
    root = _source_root(package_file or Path(repro_runner.__file__))
    lock_digest: str | None = None
    lock_source = "unavailable"
    if root is not None:
        filename = "dev-macos.lock" if system == "Darwin" else "dev-linux.lock"
        lock = root / "requirements" / filename
        try:
            info = lock.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise OSError(
                    f"selected dependency lock is not a regular file: {filename}"
                )
            lock_digest = (await hash_file(lock)).digest
            lock_source = filename
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_cache_tag": sys.implementation.cache_tag,
        "os": system,
        "os_release": platform.release(),
        "machine": platform.machine(),
        "distributions": packages,
        "dependency_lock_sha256": lock_digest,
        "dependency_lock_source": lock_source,
    }
