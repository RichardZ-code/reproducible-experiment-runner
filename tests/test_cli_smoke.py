"""Installed command surface and non-mutating error checks."""

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

import repro_runner
from repro_runner.cli import app

cli = CliRunner()


def test_installed_package_and_entry_point() -> None:
    distribution = importlib.metadata.distribution("reproducible-experiment-runner")
    assert distribution.version == repro_runner.__version__ == "0.1.0"
    assert Path(repro_runner.__file__).is_file()

    entry_points = [
        point
        for point in distribution.entry_points
        if point.group == "console_scripts" and point.name == "runner"
    ]
    assert len(entry_points) == 1
    assert entry_points[0].value == "repro_runner.cli:app"
    assert entry_points[0].load() is app


def test_help_exposes_command_surface() -> None:
    result = cli.invoke(app, ["--help"])
    assert result.exit_code == 0
    help_output = strip_ansi(result.output)
    for command in ("validate", "run", "status", "resume"):
        assert command in help_output

    for command in ("validate", "run", "status", "resume"):
        result = cli.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        help_output = strip_ansi(result.output)
        assert "--help" in help_output
        if command in ("run", "resume"):
            assert "--workers" in help_output
        if command == "run":
            assert "--no-cache" in help_output
            assert "--no-no-cache" not in help_output


@pytest.mark.parametrize(
    "args",
    [
        ["unknown"],
        ["run", "workflow.yaml", "--unknown"],
        ["validate"],
        ["run"],
        ["status"],
        ["resume"],
        ["run", "workflow.yaml", "--workers", "0"],
        ["run", "workflow.yaml", "--workers", "-1"],
        ["run", "workflow.yaml", "--workers", "many"],
        ["resume", "some-run", "--workers", "0"],
    ],
)
def test_usage_errors(args: list[str]) -> None:
    result = cli.invoke(app, args)
    assert result.exit_code == 2
    assert "Error" in result.output or "Usage" in result.output


def test_recovery_commands_reject_absent_state_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    commands = [["status", "some-run"], ["resume", "some-run", "--workers", "4"]]
    for args in commands:
        result = cli.invoke(app, args)
        assert result.exit_code == 2
        assert "no .repro state" in result.output

    assert list(tmp_path.iterdir()) == []


def test_run_rejects_missing_workflow_without_runtime_state(tmp_path: Path) -> None:
    result = cli.invoke(app, ["run", str(tmp_path / "workflow.yaml")])
    assert result.exit_code == 2
    assert "workflow file not found" in result.output
    assert list(tmp_path.iterdir()) == []


def test_installed_runner_help_outside_checkout(tmp_path: Path) -> None:
    runner = Path(sys.prefix) / "bin" / "runner"
    assert runner.is_file()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [str(runner), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert all(
        name in result.stdout for name in ("validate", "run", "status", "resume")
    )
    assert list(tmp_path.iterdir()) == []
