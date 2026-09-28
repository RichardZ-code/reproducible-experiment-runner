"""P02 scaffold checks; unavailable-command assertions change in later phases."""

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pytest
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
    for command in ("validate", "run", "status", "resume"):
        assert command in result.output

    for command in ("validate", "run", "status", "resume"):
        result = cli.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--help" in result.output
        if command in ("run", "resume"):
            assert "--workers" in result.output
        if command == "run":
            assert "--no-cache" in result.output
            assert "--no-no-cache" not in result.output


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


def test_future_commands_are_explicitly_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workflow.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    commands = [
        (["validate", "workflow.yaml"], "Workflow validation"),
        (["run", "workflow.yaml", "--workers", "4"], "Workflow execution"),
        (["run", "workflow.yaml", "--no-cache"], "Workflow execution"),
        (["status", "some-run"], "Run status"),
        (["resume", "some-run", "--workers", "4"], "Run recovery"),
    ]
    for args, message in commands:
        result = cli.invoke(app, args)
        assert result.exit_code == 3
        assert message in result.output
        assert "not implemented yet" in result.output

    assert sorted(path.name for path in tmp_path.iterdir()) == ["workflow.yaml"]


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
