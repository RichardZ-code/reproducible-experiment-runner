"""Declared paths, ownership, and readiness-time input inventories."""

import os
from pathlib import Path

import pytest
import yaml

from repro_runner.config import load_workflow, resolve_inputs
from repro_runner.errors import ValidationError


def task(
    output: str, *, deps: list[str] | None = None, inputs: list[str] | None = None
) -> dict:
    result = {"command": ["python", "-c", "pass"], "outputs": [output]}
    if deps is not None:
        result["deps"] = deps
    if inputs is not None:
        result["inputs"] = inputs
    return result


def write_tasks(workspace: Path, tasks: dict) -> Path:
    workflow = workspace / "workflow.yaml"
    workflow.write_text(yaml.safe_dump({"schema_version": 1, "tasks": tasks}))
    return workflow


@pytest.mark.parametrize(
    "declaration",
    [
        "/absolute",
        "../escape",
        "a/../escape",
        "./a",
        "a//b",
        "a/",
        "a\\b",
        "a/[ab].txt",
        "a/{one,two}",
        "a/**.txt",
        "a*/b",
        "space /x",
        " x",
        "x.",
        "é.txt",
        "C:x",
        ".repro/x",
        ".GIT/x",
        ".venv/x",
        ".repro-tmp-x",
    ],
)
def test_invalid_declared_inputs(tmp_path: Path, declaration: str) -> None:
    with pytest.raises(ValidationError):
        load_workflow(write_tasks(tmp_path, {"a": task("out/a", inputs=[declaration])}))


@pytest.mark.parametrize("output", ["out/*.txt", "out/?", "a/../x", ".repro/x"])
def test_invalid_outputs(tmp_path: Path, output: str) -> None:
    with pytest.raises(ValidationError):
        load_workflow(write_tasks(tmp_path, {"a": task(output)}))


def test_workspace_is_workflow_directory_and_missing_outputs_are_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workflow_path = write_tasks(workspace, {"a": task("out/a")})
    monkeypatch.chdir(tmp_path)
    workflow = load_workflow(Path("workspace/workflow.yaml"))
    assert workflow.workspace == workspace.resolve()
    assert workflow.filename == workflow_path.name
    assert not (workspace / "out").exists()


@pytest.mark.parametrize("obstacle", ["file_link", "broken_link", "directory"])
def test_invalid_existing_output_type(tmp_path: Path, obstacle: str) -> None:
    (tmp_path / "out").mkdir()
    target = tmp_path / "out" / "a"
    if obstacle == "file_link":
        (tmp_path / "safe").write_text("ok")
        target.symlink_to(tmp_path / "safe")
    elif obstacle == "broken_link":
        target.symlink_to(tmp_path / "missing")
    else:
        target.mkdir()
    with pytest.raises(ValidationError, match="symlink|regular file"):
        load_workflow(write_tasks(tmp_path, {"a": task("out/a")}))


def test_symlinked_or_nondirectory_parent(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValidationError, match="symlink"):
        load_workflow(write_tasks(tmp_path, {"a": task("linked/a")}))
    (tmp_path / "blocked").write_text("file")
    with pytest.raises(ValidationError, match="non-directory"):
        load_workflow(write_tasks(tmp_path, {"a": task("blocked/a")}))


def test_workflow_symlink_and_overwrite_rejected(tmp_path: Path) -> None:
    original = write_tasks(tmp_path, {"a": task("out/a")})
    link = tmp_path / "link.yaml"
    link.symlink_to(original)
    with pytest.raises(ValidationError, match="non-symlink"):
        load_workflow(link)
    with pytest.raises(ValidationError, match="overwrite workflow"):
        load_workflow(write_tasks(tmp_path, {"a": task("workflow.yaml")}))


def test_shared_parent_and_shared_readonly_input(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("shared")
    workflow = load_workflow(
        write_tasks(
            tmp_path,
            {
                "a": task("out/a", inputs=["data.txt"]),
                "b": task("out/b", inputs=["data.txt"]),
            },
        )
    )
    assert (
        resolve_inputs(workflow, "a") == resolve_inputs(workflow, "b") == ("data.txt",)
    )


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ({"a": task("out/a"), "b": task("out/a")}, "owned by both"),
        ({"a": task("out/a"), "b": task("out/a/child")}, "path conflict"),
        ({"a": task("Out/a"), "b": task("out/b")}, "case collision"),
        ({"a": task("out/A"), "b": task("out/a")}, "case collision"),
        ({"a": task("out/a", inputs=["out/a"])}, "producer ancestry"),
        ({"a": task("out/a.txt", inputs=["out/*.txt"])}, "producer ancestry"),
        (
            {"a": task("out/a"), "b": task("out/b", inputs=["out/a"])},
            "producer ancestry",
        ),
        (
            {"a": task("out/a.txt"), "b": task("out/b", inputs=["out/*.txt"])},
            "producer ancestry",
        ),
        ({"a": task("out/a", inputs=["x", "x"])}, "duplicate input"),
        (
            {"a": {"command": ["python"], "outputs": ["out/a", "out/a"]}},
            "duplicate output",
        ),
    ],
)
def test_ownership_and_declared_collision_errors(
    tmp_path: Path, tasks: dict, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        load_workflow(write_tasks(tmp_path, tasks))


def test_stale_existing_output_does_not_hide_missing_ancestry(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out/a").write_text("stale")
    with pytest.raises(ValidationError, match="producer ancestry"):
        load_workflow(
            write_tasks(
                tmp_path, {"a": task("out/a"), "b": task("out/b", inputs=["out/a"])}
            )
        )


def test_direct_and_indirect_ancestry_accept_absent_produced_input(
    tmp_path: Path,
) -> None:
    workflow = load_workflow(
        write_tasks(
            tmp_path,
            {
                "a": task("out/a"),
                "b": task("out/b", deps=["a"], inputs=["out/a"]),
                "c": task("out/c", deps=["b"], inputs=["out/a"]),
            },
        )
    )
    assert workflow.graph.ancestors["c"] == frozenset({"a", "b"})
    with pytest.raises(ValidationError, match="missing input"):
        resolve_inputs(workflow, "c")
    (tmp_path / "out").mkdir()
    (tmp_path / "out/a").write_text("produced fixture")
    assert resolve_inputs(workflow, "b") == resolve_inputs(workflow, "c") == ("out/a",)


def test_existing_case_alias_and_hardlink_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "Out").mkdir()
    with pytest.raises(ValidationError, match="case collision"):
        load_workflow(write_tasks(tmp_path, {"a": task("out/a")}))
    (tmp_path / "Out").rename(tmp_path / "out")
    (tmp_path / "out/a").write_text("x")
    os.link(tmp_path / "out/a", tmp_path / "out/b")
    with pytest.raises(ValidationError, match="hard-link alias"):
        load_workflow(write_tasks(tmp_path, {"a": task("out/a"), "b": task("out/b")}))


def test_glob_order_dotfiles_and_membership_refresh(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name in ("z.txt", "a.txt", ".hidden.txt", ".repro-tmp-scratch.txt"):
        (data / name).write_text(name)
    workflow = load_workflow(
        write_tasks(tmp_path, {"a": task("out/a", inputs=["data/*.txt"])})
    )
    assert resolve_inputs(workflow, "a") == (
        "data/.hidden.txt",
        "data/a.txt",
        "data/z.txt",
    )
    (data / "b.txt").write_text("new")
    assert "data/b.txt" in resolve_inputs(workflow, "a")
    (data / "a.txt").unlink()
    assert "data/a.txt" not in resolve_inputs(workflow, "a")


def test_glob_rejects_matched_symlink_and_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.txt").symlink_to(data / "missing")
    with pytest.raises(ValidationError, match="symlink"):
        load_workflow(
            write_tasks(tmp_path, {"a": task("out/a", inputs=["data/*.txt"])})
        )
    (data / "a.txt").unlink()
    (data / "a.txt").mkdir()
    with pytest.raises(ValidationError, match="regular file"):
        load_workflow(
            write_tasks(tmp_path, {"a": task("out/a", inputs=["data/*.txt"])})
        )


def test_overlap_missing_literal_and_empty_glob_at_readiness(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.txt").write_text("a")
    with pytest.raises(ValidationError, match="overlapping inputs"):
        load_workflow(
            write_tasks(
                tmp_path, {"a": task("out/a", inputs=["data/a.txt", "data/*.txt"])}
            )
        )
    literal = load_workflow(
        write_tasks(tmp_path, {"a": task("out/a", inputs=["missing.txt"])})
    )
    with pytest.raises(ValidationError, match="missing input"):
        resolve_inputs(literal, "a")
    empty = load_workflow(
        write_tasks(tmp_path, {"a": task("out/a", inputs=["data/*.csv"])})
    )
    with pytest.raises(ValidationError, match="empty input glob"):
        resolve_inputs(empty, "a")


def test_overlap_detected_after_glob_membership_changes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workflow = load_workflow(
        write_tasks(tmp_path, {"a": task("out/a", inputs=["data/a*", "data/*.txt"])})
    )
    (data / "a.txt").write_text("later")
    with pytest.raises(ValidationError, match="overlapping inputs"):
        resolve_inputs(workflow, "a")


def test_glob_ownership_checked_before_producer_runs(tmp_path: Path) -> None:
    workflow = load_workflow(
        write_tasks(
            tmp_path,
            {
                "a": task("out/a.txt"),
                "b": task("out/b.txt", deps=["a"], inputs=["out/a*.txt"]),
            },
        )
    )
    with pytest.raises(ValidationError, match="missing input"):
        resolve_inputs(workflow, "b")
    (tmp_path / "out").mkdir()
    (tmp_path / "out/a.txt").write_text("generated")
    assert resolve_inputs(workflow, "b") == ("out/a.txt",)
