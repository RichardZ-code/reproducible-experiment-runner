"""Fast checks for the explicit clean-installation controller."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_installation.py"
SPEC = importlib.util.spec_from_file_location("check_installation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_artifact_must_be_an_explicit_existing_distribution(tmp_path: Path) -> None:
    wheel = tmp_path / "new.whl"
    source = tmp_path / "new.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"sdist")
    assert checker.artifact_kind(wheel) == "wheel"
    assert checker.artifact_kind(source) == "sdist"
    with pytest.raises(ValueError, match="missing"):
        checker.artifact_kind(tmp_path / "old.whl")
    (tmp_path / "other.zip").write_bytes(b"zip")
    with pytest.raises(ValueError, match="unsupported"):
        checker.artifact_kind(tmp_path / "other.zip")


def test_origin_rejects_editable_and_wrong_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    probe = {
        "prefix": str(runtime),
        "origin": str(
            runtime / "lib/python3.12/site-packages/repro_runner/__init__.py"
        ),
        "version": "0.1.0",
        "entry_point": "repro_runner.cli:app",
        "editable": False,
        "requirements": ["pyyaml", "typer"],
    }
    checker.verify_origin(probe, runtime)
    with pytest.raises(ValueError, match="outside"):
        checker.verify_origin(
            {**probe, "origin": str(tmp_path / "src/repro_runner/__init__.py")}, runtime
        )
    with pytest.raises(ValueError, match="editable"):
        checker.verify_origin({**probe, "editable": True}, runtime)


def test_output_hash_mismatch_fails(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    for name in checker.OUTPUTS:
        (output / f"{name}.json").write_text("{}")
    manifest = {
        "tasks": {
            task: {
                "selected_attempt_no": 1,
                "attempts": [
                    {
                        "number": 1,
                        "artifacts": [{"path": f"out/{name}.json", "sha256": "0" * 64}],
                    }
                ],
            }
            for task, name in zip(checker.TASKS, checker.OUTPUTS, strict=True)
        }
    }
    with pytest.raises(ValueError, match="output hash mismatch"):
        checker.verify_outputs(tmp_path, manifest)


def test_failed_setup_records_failure_and_preserves_unrelated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "new.whl"
    artifact.write_bytes(b"wheel")
    example = tmp_path / "example"
    example.mkdir()
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep")
    report = tmp_path / "report"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--artifact",
            str(artifact),
            "--python",
            sys.executable,
            "--example",
            str(example),
            "--report-dir",
            str(report),
        ],
    )
    monkeypatch.setattr(
        checker,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected venv failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected venv failure"):
        checker.main()
    result = json.loads((report / "result.json").read_text())
    assert result["result"] == "failed"
    assert result["checks"] == dict.fromkeys(checker.CHECKS, "not_run")
    assert unrelated.read_text() == "keep"
