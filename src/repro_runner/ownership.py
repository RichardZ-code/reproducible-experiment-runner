"""Exclusive ownership of a workflow workspace during execution."""

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class OwnershipConflict(Exception):
    """Another runner currently owns the workspace."""


def _directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir()
        except FileExistsError:
            pass
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"runtime path is not a real directory: {path}")


@contextmanager
def workspace_lock(workspace: Path) -> Iterator[Path]:
    """Create only the minimal lock bootstrap, then hold an OS lock until exit."""
    runtime = workspace / ".repro"
    _directory(runtime)
    lock_path = runtime / "lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.set_inheritable(descriptor, False)
        opened = os.fstat(descriptor)
        linked = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise OSError(f"runtime lock is not a stable regular file: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise OwnershipConflict(
                    "workspace is already owned by another run"
                ) from error
            raise
        try:
            yield runtime
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
