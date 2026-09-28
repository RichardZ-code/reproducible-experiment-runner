"""Verified local cache entries and isolated artifact restoration."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path
from typing import Mapping

from repro_runner.errors import ValidationError
from repro_runner.hashing import (
    CACHE_SCHEMA,
    Artifact,
    CacheIdentity,
    canonical_bytes,
    copy_file,
    hash_file,
    regular_reader,
)
from repro_runner.paths import check_declaration, ensure_real_directory

_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class InvalidEntry(Exception):
    """An exact cache entry exists but fails the declared integrity contract."""


class CacheConflict(Exception):
    """A valid entry for the same key has a different output set."""


class CacheOperationalError(Exception):
    """A cache filesystem operation failed outside the invalid-entry policy."""


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise InvalidEntry(f"duplicate metadata key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise InvalidEntry(f"unsupported JSON constant {value}")


def _entry_tree(root: Path) -> tuple[set[str], set[str]]:
    directories: set[str] = set()
    files: set[str] = set()
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    directories.add(relative)
                    pending.append((Path(entry.path), relative))
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise InvalidEntry(f"linked cache file {relative!r}")
                    files.add(relative)
                else:
                    raise InvalidEntry(f"unsafe cache entry component {relative!r}")
    return directories, files


async def validate_entry(
    directory: Path,
    identity: CacheIdentity,
    outputs: tuple[str, ...],
    stop: asyncio.Event,
    *,
    final_location: bool,
) -> dict[str, Artifact]:
    """Read every declared artifact afresh; metadata alone never grants a hit."""
    if final_location and directory.name != identity.key:
        raise InvalidEntry("entry is not at its requested key location")
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise InvalidEntry("entry root is not a real directory")
    metadata_path = directory / "metadata.json"
    try:
        metadata_info = metadata_path.lstat()
    except FileNotFoundError as error:
        raise InvalidEntry("missing metadata.json") from error
    if not stat.S_ISREG(metadata_info.st_mode):
        raise InvalidEntry("metadata.json is not a regular file")
    try:
        with regular_reader(metadata_path) as source:
            metadata = json.load(
                source, object_pairs_hook=_pairs, parse_constant=_invalid_constant
            )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidEntry("malformed metadata JSON") from error
    if not isinstance(metadata, dict) or set(metadata) != {
        "schema",
        "key",
        "identity",
        "artifacts",
    }:
        raise InvalidEntry("metadata fields are incomplete or unsupported")
    if type(metadata["schema"]) is not int or metadata["schema"] != CACHE_SCHEMA:
        raise InvalidEntry("unsupported cache metadata schema")
    if not isinstance(metadata["key"], str) or metadata["key"] != identity.key:
        raise InvalidEntry("metadata key differs from requested key")
    if not _DIGEST.fullmatch(metadata["key"]):
        raise InvalidEntry("invalid metadata key digest")
    if canonical_bytes(metadata["identity"]) != canonical_bytes(identity.record):
        raise InvalidEntry("metadata identity differs from requested identity")
    if (
        hashlib.sha256(canonical_bytes(metadata["identity"])).hexdigest()
        != identity.key
    ):
        raise InvalidEntry("metadata identity does not hash to its key")
    rows = metadata["artifacts"]
    if not isinstance(rows, list):
        raise InvalidEntry("artifact inventory must be a list")
    artifacts: dict[str, Artifact] = {}
    spellings: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}:
            raise InvalidEntry("invalid artifact metadata fields")
        path, digest, size = row["path"], row["sha256"], row["size"]
        try:
            check_declaration(path, allow_pattern=False)
        except ValidationError as error:
            raise InvalidEntry(f"unsafe artifact path {path!r}") from error
        if path.casefold() in spellings:
            raise InvalidEntry(f"duplicate artifact destination {path!r}")
        spellings.add(path.casefold())
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise InvalidEntry(f"invalid artifact digest for {path!r}")
        if type(size) is not int or size < 0:
            raise InvalidEntry(f"invalid artifact size for {path!r}")
        artifacts[path] = Artifact(digest, size)
    if tuple(artifacts) != tuple(sorted(outputs)):
        raise InvalidEntry("artifact inventory differs from declared outputs")
    expected_dirs = {"files"}
    expected_files = {"metadata.json"}
    for path in artifacts:
        components = path.split("/")
        for count in range(1, len(components)):
            expected_dirs.add("files/" + "/".join(components[:count]))
        expected_files.add("files/" + path)
    actual_dirs, actual_files = _entry_tree(directory)
    if actual_dirs != expected_dirs or actual_files != expected_files:
        raise InvalidEntry("cache entry has missing or unexpected files")
    for path, expected in artifacts.items():
        if stop.is_set():
            raise InterruptedError("run interrupted during cache validation")
        actual = await hash_file(directory / "files" / path)
        if actual != expected:
            raise InvalidEntry(f"cache artifact bytes differ: {path!r}")
    return artifacts


class CacheStorage:
    """One workspace-owned cache root, used only when caching is enabled."""

    def __init__(self, runtime: Path) -> None:
        self.root = runtime / "cache" / "v1"
        try:
            ensure_real_directory(self.root)
        except ValidationError as error:
            raise OSError(f"unsafe cache root: {error}") from error

    def _entry(self, identity: CacheIdentity) -> Path:
        return self.root / identity.key

    def _quarantine(self, entry: Path, reason: str) -> None:
        destination = self.root / f".bad-{entry.name}-{uuid.uuid4().hex}"
        os.rename(entry, destination)
        print(
            f"Invalid cache entry {entry.name}: {reason}; quarantined", file=sys.stderr
        )

    def quarantine(self, identity: CacheIdentity, reason: str) -> None:
        self._quarantine(self._entry(identity), reason)

    async def lookup(
        self, identity: CacheIdentity, outputs: tuple[str, ...], stop: asyncio.Event
    ) -> dict[str, Artifact] | None:
        entry = self._entry(identity)
        try:
            entry.lstat()
        except FileNotFoundError:
            return None
        try:
            return await validate_entry(
                entry, identity, outputs, stop, final_location=True
            )
        except InvalidEntry as error:
            if stop.is_set():
                raise InterruptedError("run interrupted during cache lookup") from error
            self._quarantine(entry, str(error))
            return None

    async def restore(
        self,
        identity: CacheIdentity,
        artifacts: Mapping[str, Artifact],
        publish: Path,
        stop: asyncio.Event,
    ) -> None:
        created: list[Path] = []
        try:
            for path, expected in sorted(artifacts.items()):
                if stop.is_set():
                    raise InterruptedError("run interrupted during cache restoration")
                destination = publish / path
                ensure_real_directory(destination.parent)
                created.append(destination)
                actual = await copy_file(
                    self._entry(identity) / "files" / path, destination
                )
                if actual != expected or await hash_file(destination) != expected:
                    raise InvalidEntry(
                        f"cache artifact changed during restore: {path!r}"
                    )
        except FileNotFoundError as error:
            for path in created:
                path.unlink(missing_ok=True)
            raise InvalidEntry(
                "cache artifact disappeared during restoration"
            ) from error
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    async def prepare(
        self,
        identity: CacheIdentity,
        artifacts: Mapping[str, Artifact],
        publish: Path,
        stop: asyncio.Event,
    ) -> Path:
        temporary = self.root / f".tmp-{uuid.uuid4().hex}"
        temporary.mkdir()
        try:
            rows = []
            for path, expected in sorted(artifacts.items()):
                if stop.is_set():
                    raise InterruptedError("run interrupted during cache preparation")
                target = temporary / "files" / path
                ensure_real_directory(target.parent)
                copied = await copy_file(publish / path, target)
                if copied != expected or await hash_file(target) != expected:
                    raise InvalidEntry(f"cache preparation bytes changed: {path!r}")
                rows.append(
                    {"path": path, "sha256": expected.digest, "size": expected.size}
                )
            metadata = {
                "schema": CACHE_SCHEMA,
                "key": identity.key,
                "identity": identity.record,
                "artifacts": rows,
            }
            with (temporary / "metadata.json").open("xb") as output:
                output.write(canonical_bytes(metadata))
            await validate_entry(
                temporary,
                identity,
                tuple(sorted(artifacts)),
                stop,
                final_location=False,
            )
            return temporary
        except BaseException:
            self.discard(temporary)
            raise

    async def publish(
        self,
        temporary: Path,
        identity: CacheIdentity,
        artifacts: Mapping[str, Artifact],
        stop: asyncio.Event,
    ) -> None:
        if stop.is_set():
            raise InterruptedError("run interrupted before cache publication")
        entry = self._entry(identity)
        existing = await self.lookup(identity, tuple(sorted(artifacts)), stop)
        if existing is not None:
            if dict(existing) != dict(artifacts):
                raise CacheConflict(
                    "valid cache entry has different outputs for the same key"
                )
            return
        if stop.is_set():
            raise InterruptedError("run interrupted before cache publication")
        os.rename(temporary, entry)

    def discard(self, temporary: Path) -> None:
        if temporary.parent != self.root or not temporary.name.startswith(".tmp-"):
            raise OSError("refusing to remove a cache path this operation does not own")
        try:
            info = temporary.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("owned cache temporary path was replaced")
        shutil.rmtree(temporary)
