"""YAML, schema, and graph behavior for P03 validation."""

from pathlib import Path

import pytest
import yaml

from repro_runner.config import load_workflow
from repro_runner.errors import ValidationError


def write_yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def task(output: str, deps: list[str] | None = None) -> dict:
    result = {"command": ["python", "-c", "pass"], "outputs": [output]}
    if deps is not None:
        result["deps"] = deps
    return result


def write_tasks(tmp_path: Path, tasks: dict) -> Path:
    return write_yaml(tmp_path, yaml.safe_dump({"schema_version": 1, "tasks": tasks}))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "root"),
        ("[]", "root"),
        ("{}", "schema_version"),
        ("schema_version: 1\n", "tasks"),
        (
            "schema_version: true\ntasks: {a: {command: [python], outputs: [a]}}",
            "schema_version",
        ),
        (
            "schema_version: 1.0\ntasks: {a: {command: [python], outputs: [a]}}",
            "schema_version",
        ),
        (
            "schema_version: '1'\ntasks: {a: {command: [python], outputs: [a]}}",
            "schema_version",
        ),
        (
            "schema_version: 2\ntasks: {a: {command: [python], outputs: [a]}}",
            "schema_version",
        ),
        ("schema_version: 1\ntasks: {}", "tasks"),
        ("schema_version: 1\ntasks: []", "tasks"),
        (
            "schema_version: 1\nextra: x\ntasks: {a: {command: [python], outputs: [a]}}",
            "unknown",
        ),
        (
            "schema_version: 1\ntasks: {Bad: {command: [python], outputs: [a]}}",
            "task ID",
        ),
        (
            "schema_version: 1\ntasks: {1: {command: [python], outputs: [a]}}",
            "mapping keys",
        ),
        ("schema_version: 1\ntasks: {a: null}", "must be a mapping"),
        ("schema_version: 1\ntasks: {a: {outputs: [a]}}", "missing command"),
        ("schema_version: 1\ntasks: {a: {command: [python]}}", "missing outputs"),
        ("schema_version: 1\ntasks: {a: {command: python, outputs: [a]}}", "command"),
        ("schema_version: 1\ntasks: {a: {command: [], outputs: [a]}}", "command"),
        (
            "schema_version: 1\ntasks: {a: {command: [python, 7], outputs: [a]}}",
            "strings",
        ),
        (
            'schema_version: 1\ntasks: {a: {command: [python, "bad\\0arg"], outputs: [a]}}',
            "NUL",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: ['', x], outputs: [a]}}",
            "executable",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: [./python], outputs: [a]}}",
            "executable",
        ),
        ("schema_version: 1\ntasks: {a: {command: [python], outputs: []}}", "outputs"),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a], deps: null}}",
            "deps",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a], inputs: x}}",
            "inputs",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a], extra: x}}",
            "unknown",
        ),
        ("schema_version: 1\nschema_version: 1\ntasks: {}", "duplicate mapping key"),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a]}, a: {command: [python], outputs: [b]}}",
            "duplicate mapping key",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: [python], command: [echo], outputs: [a]}}",
            "duplicate mapping key",
        ),
        (
            "schema_version: 1\ntasks: {a: {command: [python], outputs: [a], outputs: [b]}}",
            "duplicate mapping key",
        ),
        ("schema_version: [", "invalid YAML"),
        ("schema_version: 1\ntasks: {}\n---\nschema_version: 1", "invalid YAML"),
        (
            "schema_version: 1\ntasks: !python/object/apply:os.system [echo]",
            "invalid YAML",
        ),
        (
            "schema_version: 1\ntasks: {a: &base {command: [python], outputs: [a]}, b: *base}",
            "aliases",
        ),
        (
            "schema_version: 1\ntasks: {a: &base {command: [python], outputs: [a]}}",
            "anchors",
        ),
        (
            "schema_version: 1\ntasks: {a: {<<: {command: [python]}, outputs: [a]}}",
            "merge keys",
        ),
    ],
)
def test_invalid_yaml_and_schema(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        load_workflow(write_yaml(tmp_path, text))


def test_command_order_arguments_defaults_and_immutable_model(tmp_path: Path) -> None:
    workflow = load_workflow(
        write_yaml(
            tmp_path,
            "schema_version: 1\ntasks:\n  a:\n    command: [python, '-c', 'print(1 + 2)', '', '$HOME', '--out', 'out/a']\n    outputs: [out/a]\n",
        )
    )
    item = workflow.tasks["a"]
    assert item.command == (
        "python",
        "-c",
        "print(1 + 2)",
        "",
        "$HOME",
        "--out",
        "out/a",
    )
    assert item.deps == item.inputs == ()
    assert workflow.graph.order == ("a",)
    with pytest.raises(TypeError):
        workflow.tasks["new"] = item


def test_diamond_order_and_relationships_are_stable(tmp_path: Path) -> None:
    tasks = {
        "finish": task("out/finish", ["left", "right"]),
        "right": task("out/right", ["root"]),
        "left": task("out/left", ["root"]),
        "root": task("out/root"),
    }
    first = load_workflow(write_tasks(tmp_path, tasks))
    second = load_workflow(write_tasks(tmp_path, dict(reversed(list(tasks.items())))))
    assert (
        first.graph.order == second.graph.order == ("root", "left", "right", "finish")
    )
    assert first.graph.dependents["root"] == ("left", "right")
    assert first.graph.ancestors["finish"] == frozenset({"root", "left", "right"})


def test_disconnected_and_isolated_tasks(tmp_path: Path) -> None:
    workflow = load_workflow(
        write_tasks(tmp_path, {"z": task("z"), "a": task("a"), "b": task("b", ["a"])})
    )
    assert workflow.graph.order == ("a", "b", "z")
    assert workflow.graph.ancestors["z"] == frozenset()


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ({"a": task("a", ["missing"])}, "unknown dependency"),
        ({"a": task("a", ["a"])}, "self-dependency"),
        ({"a": task("a", ["b"]), "b": task("b", ["a"])}, "cycle"),
        ({"a": task("a", ["b"]), "b": task("b", ["a"]), "z": task("z")}, "cycle"),
        ({"a": task("a"), "b": task("b", ["a", "a"])}, "duplicate dependency"),
    ],
)
def test_invalid_graph(tmp_path: Path, tasks: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        load_workflow(write_tasks(tmp_path, tasks))
