"""Safe workflow loading and structural validation."""

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml
from yaml.events import AliasEvent

from repro_runner.errors import ValidationError
from repro_runner.graph import Graph, build_graph
from repro_runner.paths import (
    check_declaration,
    existing_matches,
    inspect_file,
    is_pattern,
    matching_outputs,
    register_spelling,
)

_TASK_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)


class _WorkflowLoader(yaml.SafeLoader):
    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node:
        event = self.peek_event()
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValidationError(
                f"line {event.start_mark.line + 1}, column {event.start_mark.column + 1}: "
                "YAML anchors and aliases are not supported"
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        result: dict[str, object] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ValidationError(
                    f"line {key_node.start_mark.line + 1}, column {key_node.start_mark.column + 1}: "
                    "YAML merge keys are not supported"
                )
            key = self.construct_object(key_node, deep=deep)
            location = (
                f"line {key_node.start_mark.line + 1}, "
                f"column {key_node.start_mark.column + 1}"
            )
            if not isinstance(key, str):
                raise ValidationError(f"{location}: mapping keys must be strings")
            if key in result:
                raise ValidationError(f"{location}: duplicate mapping key {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass(frozen=True)
class Task:
    id: str
    command: tuple[str, ...]
    deps: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class Workflow:
    workspace: Path
    filename: str
    tasks: Mapping[str, Task]
    graph: Graph
    output_owners: Mapping[str, str]


def _list_of_strings(value: object, context: str, *, nonempty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValidationError(
            f"{context} must be {'a non-empty' if nonempty else 'a'} list"
        )
    if any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{context} must contain only strings")
    return tuple(value)


def _parse_task(task_id: str, raw: object) -> Task:
    if not isinstance(raw, dict):
        raise ValidationError(f"task {task_id!r} must be a mapping")
    unknown = sorted(set(raw) - {"command", "deps", "inputs", "outputs"})
    if unknown:
        raise ValidationError(f"task {task_id!r}: unknown field {unknown[0]!r}")
    for field in ("command", "outputs"):
        if field not in raw:
            raise ValidationError(f"task {task_id!r}: missing {field}")
    command = _list_of_strings(
        raw["command"], f"task {task_id!r} command", nonempty=True
    )
    if not command[0] or "/" in command[0] or "\\" in command[0]:
        raise ValidationError(f"task {task_id!r}: executable must be a bare name")
    if any("\0" in arg for arg in command):
        raise ValidationError(f"task {task_id!r}: command contains NUL")
    deps = _list_of_strings(
        raw.get("deps", []), f"task {task_id!r} deps", nonempty=False
    )
    for dependency in deps:
        if not _TASK_ID.fullmatch(dependency):
            raise ValidationError(
                f"task {task_id!r}: invalid dependency {dependency!r}"
            )
    inputs = _list_of_strings(
        raw.get("inputs", []), f"task {task_id!r} inputs", nonempty=False
    )
    outputs = _list_of_strings(
        raw["outputs"], f"task {task_id!r} outputs", nonempty=True
    )
    for declaration in inputs:
        check_declaration(declaration, allow_pattern=True)
    for declaration in outputs:
        check_declaration(declaration, allow_pattern=False)
    if len(inputs) != len(set(inputs)):
        raise ValidationError(f"task {task_id!r}: duplicate input declaration")
    if len(outputs) != len(set(outputs)):
        raise ValidationError(f"task {task_id!r}: duplicate output declaration")
    return Task(task_id, command, deps, inputs, outputs)


def _input_candidates(workflow: Workflow, declaration: str) -> tuple[str, ...]:
    if not is_pattern(declaration):
        return (declaration,)
    return tuple(
        sorted(
            set(existing_matches(workflow.workspace, declaration))
            | set(matching_outputs(declaration, workflow.output_owners))
        )
    )


def _check_owner(workflow: Workflow, task_id: str, path: str) -> None:
    producer = workflow.output_owners.get(path)
    if producer is not None and producer not in workflow.graph.ancestors[task_id]:
        raise ValidationError(
            f"task {task_id!r}: input {path!r} is produced by {producer!r} "
            "without producer ancestry"
        )


def _check_workspace(workflow: Workflow) -> None:
    spellings: dict[str, str] = {}
    seen_inodes: dict[tuple[int, int], str] = {}
    file_paths: set[str] = set(workflow.output_owners)

    def record(path: str) -> None:
        register_spelling(spellings, path, pattern=False)
        info = inspect_file(workflow.workspace, path, required=False)
        if info is not None:
            identity = (info.st_dev, info.st_ino)
            previous = seen_inodes.setdefault(identity, path)
            if previous != path:
                raise ValidationError(
                    f"hard-link alias between {previous!r} and {path!r}"
                )

    for output in sorted(workflow.output_owners):
        if output.casefold() == workflow.filename.casefold():
            raise ValidationError(f"output would overwrite workflow file {output!r}")
        record(output)

    for task_id in workflow.graph.order:
        seen_inputs: set[str] = set()
        for declaration in workflow.tasks[task_id].inputs:
            register_spelling(spellings, declaration, pattern=is_pattern(declaration))
            if is_pattern(declaration):
                directory = declaration.rpartition("/")[0]
                if directory:
                    for output in workflow.output_owners:
                        if (
                            directory.casefold() == output.casefold()
                            or directory.casefold().startswith(output.casefold() + "/")
                        ):
                            raise ValidationError(
                                f"glob directory {directory!r} conflicts with output {output!r}"
                            )
            for path in _input_candidates(workflow, declaration):
                if path in seen_inputs:
                    raise ValidationError(
                        f"task {task_id!r}: overlapping inputs at {path!r}"
                    )
                seen_inputs.add(path)
                _check_owner(workflow, task_id, path)
                file_paths.add(path)
                record(path)

    for path in sorted(file_paths):
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in file_paths:
                raise ValidationError(
                    f"file/directory path conflict: {parent!r} and {path!r}"
                )


def load_workflow(path: Path) -> Workflow:
    """Load and structurally validate a workflow without modifying its workspace."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValidationError(f"workflow file not found: {path}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"workflow must be a regular, non-symlink file: {path}")
    workspace = path.parent.resolve(strict=True)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_WorkflowLoader)
    except UnicodeError as error:
        raise ValidationError(f"workflow is not UTF-8: {path}") from error
    except yaml.YAMLError as error:
        raise ValidationError(f"invalid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ValidationError("workflow root must be a mapping")
    unknown = sorted(set(raw) - {"schema_version", "tasks"})
    if unknown:
        raise ValidationError(f"unknown top-level field {unknown[0]!r}")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValidationError("schema_version must be integer 1")
    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, dict) or not tasks_raw:
        raise ValidationError("tasks must be a non-empty mapping")
    tasks: dict[str, Task] = {}
    for task_id, raw_task in tasks_raw.items():
        if not _TASK_ID.fullmatch(task_id):
            raise ValidationError(f"invalid task ID {task_id!r}")
        tasks[task_id] = _parse_task(task_id, raw_task)
    graph = build_graph({task_id: task.deps for task_id, task in tasks.items()})
    output_owners: dict[str, str] = {}
    for task_id, task in tasks.items():
        for output in task.outputs:
            if output in output_owners:
                raise ValidationError(
                    f"output {output!r} owned by both {output_owners[output]!r} and {task_id!r}"
                )
            output_owners[output] = task_id
    workflow = Workflow(
        workspace,
        path.name,
        MappingProxyType(tasks),
        graph,
        MappingProxyType(output_owners),
    )
    _check_workspace(workflow)
    return workflow


def resolve_inputs(workflow: Workflow, task_id: str) -> tuple[str, ...]:
    """Recheck a ready task's current input inventory without reading file bytes."""
    if task_id not in workflow.tasks:
        raise ValidationError(f"unknown task {task_id!r}")
    _check_workspace(workflow)
    found: set[str] = set()
    for declaration in workflow.tasks[task_id].inputs:
        paths = _input_candidates(workflow, declaration)
        if not paths:
            raise ValidationError(f"task {task_id!r}: empty input glob {declaration!r}")
        for path in paths:
            if path in found:
                raise ValidationError(
                    f"task {task_id!r}: overlapping inputs at {path!r}"
                )
            found.add(path)
            _check_owner(workflow, task_id, path)
            inspect_file(workflow.workspace, path, required=True)
    return tuple(sorted(found))
