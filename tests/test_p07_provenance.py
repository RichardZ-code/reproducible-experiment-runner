"""Git observations stay local to the source workspace and each invocation."""

import subprocess
from pathlib import Path

from repro_runner.provenance import observe_git


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".repro/\nout/\n")
    (root / "source.py").write_text("value = 1\n")
    git(root, "add", ".gitignore", "source.py")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    )
    return root


def test_git_clean_dirty_nested_detached_and_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    root = repository(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    index_before = (root / ".git/index").read_bytes()
    clean = observe_git(nested)
    assert clean.availability == "available"
    assert clean.commit == git(root, "rev-parse", "HEAD").stdout.strip()
    assert clean.dirty is False
    assert (root / ".git/index").read_bytes() == index_before
    (root / ".repro").mkdir()
    (root / ".repro/state.sqlite3").write_text("ignored")
    (root / "out").mkdir()
    (root / "out/data").write_text("ignored")
    assert observe_git(nested).dirty is False
    (root / "source.py").write_text("value = 2\n")
    assert observe_git(nested).dirty is True
    (root / "source.py").write_text("value = 1\n")
    (root / "new-config.json").write_text("{}")
    assert observe_git(nested).dirty is True
    (root / "new-config.json").unlink()
    git(root, "checkout", "--detach", "-q", "HEAD")
    assert observe_git(nested).commit == clean.commit
    assert observe_git(nested).dirty is False
    other = repository(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert observe_git(nested).commit == clean.commit
    assert (root / ".git/index").read_bytes() == index_before


def test_unborn_outside_missing_and_failed_git(tmp_path: Path, monkeypatch) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q")
    observed = observe_git(unborn)
    assert observed.availability == "unborn" and observed.commit is None
    outside = tmp_path / "outside"
    outside.mkdir()
    assert observe_git(outside).record()["availability"] == "outside_git"
    monkeypatch.setenv("PATH", "")
    assert observe_git(outside).availability == "git_unavailable"


def test_probe_timeout_and_command_failure(tmp_path: Path, monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert observe_git(tmp_path).availability == "inspection_timeout"

    def failure(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "permission denied")

    monkeypatch.setattr(subprocess, "run", failure)
    assert observe_git(tmp_path).availability == "inspection_failed"
