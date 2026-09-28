"""Declared-path grammar and read-only workspace inspection."""

import fnmatch
import os
import re
import stat
from pathlib import Path
from typing import Mapping

from repro_runner.errors import ValidationError

_COMPONENT = re.compile(r"[A-Za-z0-9 ._-]+\Z", re.ASCII)
_PATTERN_COMPONENT = re.compile(r"[A-Za-z0-9 ._*?-]+\Z", re.ASCII)
_RESERVED = {".repro", ".git", ".venv"}


def is_pattern(declaration: str) -> bool:
    return "*" in declaration or "?" in declaration


def check_declaration(declaration: str, *, allow_pattern: bool) -> None:
    if not isinstance(declaration, str) or not declaration:
        raise ValidationError(f"invalid declared path {declaration!r}")
    parts = declaration.split("/")
    if parts[0].casefold() in _RESERVED:
        raise ValidationError(f"reserved path {declaration!r}")
    if "\\" in declaration or declaration.startswith("/") or declaration.endswith("/"):
        raise ValidationError(f"noncanonical path {declaration!r}")
    if is_pattern(declaration) and not allow_pattern:
        raise ValidationError(f"output cannot contain a glob: {declaration!r}")
    if "**" in declaration:
        raise ValidationError(f"unsupported glob {declaration!r}")
    for index, part in enumerate(parts):
        if (
            not part
            or part in {".", ".."}
            or part != part.strip(" ")
            or part.endswith(".")
        ):
            raise ValidationError(f"noncanonical path {declaration!r}")
        expression = (
            _PATTERN_COMPONENT
            if allow_pattern and index == len(parts) - 1
            else _COMPONENT
        )
        if not expression.fullmatch(part):
            raise ValidationError(f"unsupported path component in {declaration!r}")
        if part.startswith(".repro-tmp-") and not is_pattern(part):
            raise ValidationError(f"reserved temporary name {declaration!r}")


def _entry(parent: Path, name: str, declaration: str) -> os.DirEntry[str] | None:
    with os.scandir(parent) as entries:
        matches = [item for item in entries if item.name.casefold() == name.casefold()]
    if any(item.name != name for item in matches) or len(matches) > 1:
        raise ValidationError(f"case collision for {declaration!r} at {name!r}")
    return matches[0] if matches else None


def inspect_file(
    workspace: Path, declaration: str, *, required: bool
) -> os.stat_result | None:
    """Check existing components without following links or creating missing paths."""
    parent = workspace
    parts = declaration.split("/")
    for index, part in enumerate(parts):
        entry = _entry(parent, part, declaration)
        if entry is None:
            if required:
                raise ValidationError(f"missing input {declaration!r}")
            return None
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValidationError(f"symlink in declared path {declaration!r}")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValidationError(f"non-directory parent in {declaration!r}")
            parent /= part
        elif not stat.S_ISREG(info.st_mode):
            raise ValidationError(f"not a regular file: {declaration!r}")
        else:
            return info
    return None


def existing_matches(workspace: Path, pattern: str) -> tuple[str, ...]:
    """Expand a final-basename pattern without following directory links."""
    directory, _, basename = pattern.rpartition("/")
    if directory:
        parent = workspace
        for part in directory.split("/"):
            entry = _entry(parent, part, pattern)
            if entry is None:
                return ()
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValidationError(f"invalid glob directory in {pattern!r}")
            parent /= part
    else:
        parent = workspace
    with os.scandir(parent) as entries:
        names = sorted(
            item.name
            for item in entries
            if not item.name.startswith(".repro-tmp-")
            and fnmatch.fnmatchcase(item.name, basename)
        )
    result: list[str] = []
    for name in names:
        relative = f"{directory}/{name}" if directory else name
        check_declaration(relative, allow_pattern=False)
        inspect_file(workspace, relative, required=True)
        result.append(relative)
    return tuple(result)


def matching_outputs(pattern: str, outputs: Mapping[str, str]) -> tuple[str, ...]:
    directory, _, basename = pattern.rpartition("/")
    return tuple(
        sorted(
            path
            for path in outputs
            if path.rpartition("/")[0] == directory
            and fnmatch.fnmatchcase(path.rpartition("/")[2], basename)
            and not path.rpartition("/")[2].startswith(".repro-tmp-")
        )
    )


def register_spelling(
    spellings: dict[str, str], declaration: str, *, pattern: bool
) -> None:
    parts = declaration.split("/")
    if pattern:
        parts = parts[:-1]
    for index in range(len(parts)):
        prefix = "/".join(parts[: index + 1])
        folded = prefix.casefold()
        prior = spellings.setdefault(folded, prefix)
        if prior != prefix:
            raise ValidationError(f"case collision between {prior!r} and {prefix!r}")
