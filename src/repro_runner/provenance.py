"""Small, local Git observation for one workflow invocation."""

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_OBJECT_ID = re.compile(r"[0-9a-f]+\Z", re.ASCII)
_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class GitObservation:
    commit: str | None
    dirty: bool | None
    availability: str
    observed_at: str

    def record(self) -> dict[str, object]:
        return {
            "commit": self.commit,
            "dirty": self.dirty,
            "availability": self.availability,
            "observed_at": self.observed_at,
        }


def observe_git(workspace: Path) -> GitObservation:
    """Observe the workspace repository without changing its index or routing."""
    observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["LC_ALL"] = "C"

    def unavailable(reason: str) -> GitObservation:
        return GitObservation(None, None, reason, observed_at)

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(workspace), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            if "not a git repository" in inside.stderr.lower():
                return unavailable("outside_git")
            return unavailable("inspection_failed")
        if inside.stdout.strip() != "true":
            return unavailable("outside_git")
        revision = git("rev-parse", "--verify", "HEAD^{commit}")
        status = git("status", "--porcelain=v1", "--untracked-files=normal")
        if status.returncode != 0:
            return unavailable("inspection_failed")
        dirty = bool(status.stdout)
        if revision.returncode != 0:
            return GitObservation(None, dirty, "unborn", observed_at)
        commit = revision.stdout.strip()
        if not _OBJECT_ID.fullmatch(commit) or len(commit) < 16:
            return unavailable("inspection_failed")
        return GitObservation(commit, dirty, "available", observed_at)
    except FileNotFoundError:
        return unavailable("git_unavailable")
    except subprocess.TimeoutExpired:
        return unavailable("inspection_timeout")
    except OSError:
        return unavailable("inspection_failed")
