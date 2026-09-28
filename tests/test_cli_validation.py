"""Installed and in-process validation command behavior."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from repro_runner import paths
from repro_runner.cli import app

cli = CliRunner()


def write_workflow(workspace: Path, *, output: str = "out/result.txt") -> Path:
    path = workspace / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": {
                    "a": {
                        "command": [
                            "python",
                            "-c",
                            "open('marker.txt', 'w').write('ran')",
                        ],
                        "inputs": ["data.txt"],
                        "outputs": [output],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_validate_is_read_only_and_never_launches_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data.txt").write_bytes(b"original input")
    workflow = write_workflow(tmp_path)
    before = workflow.read_bytes()
    input_before = (tmp_path / "data.txt").read_bytes()

    def forbidden_launch(*args: object, **kwargs: object) -> None:
        raise AssertionError("validation launched a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden_launch)
    result = cli.invoke(app, ["validate", str(workflow)])
    assert result.exit_code == 0, result.output
    assert "Workflow valid: 1 task" in result.output
    assert "deferred until task readiness" in result.output
    assert workflow.read_bytes() == before
    assert (tmp_path / "data.txt").read_bytes() == input_before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "data.txt",
        "workflow.yaml",
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version: 1\ntasks: {}\n", "tasks"),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a], deps: [missing]}}",
            "unknown dependency",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a]}, a: {command: [python], outputs: [b]}}",
            "duplicate mapping key",
        ),
    ],
)
def test_invalid_workflow_has_usage_exit_and_no_traceback(
    tmp_path: Path, text: str, message: str
) -> None:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(text)
    result = cli.invoke(app, ["validate", str(workflow)])
    assert result.exit_code == 2
    assert message in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / ".repro").exists()


def test_installed_runner_validates_outside_checkout_without_pythonpath(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.txt").write_text("input")
    workflow = write_workflow(tmp_path)
    runner = Path(sys.prefix) / "bin" / "runner"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(runner), "validate", workflow.name],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Workflow valid: 1 task" in result.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "data.txt",
        "workflow.yaml",
    ]


def test_missing_workflow_file_is_a_validation_error(tmp_path: Path) -> None:
    result = cli.invoke(app, ["validate", str(tmp_path / "absent.yaml")])
    assert result.exit_code == 2
    assert "workflow file not found" in result.output


def test_filesystem_permission_failure_is_not_reported_as_invalid_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow(tmp_path)

    def denied(parent: Path, name: str, declaration: str) -> None:
        raise PermissionError("inspection denied")

    monkeypatch.setattr(paths, "_entry", denied)
    result = cli.invoke(app, ["validate", str(workflow)])
    assert result.exit_code == 3
    assert "Cannot inspect workflow: inspection denied" in result.output
