"""Deterministic validation of explicit task dependencies."""

import heapq
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from repro_runner.errors import ValidationError


@dataclass(frozen=True)
class Graph:
    order: tuple[str, ...]
    dependents: Mapping[str, tuple[str, ...]]
    ancestors: Mapping[str, frozenset[str]]


def build_graph(dependencies: Mapping[str, tuple[str, ...]]) -> Graph:
    dependents: dict[str, list[str]] = {task_id: [] for task_id in dependencies}
    counts: dict[str, int] = {}
    for task_id, deps in dependencies.items():
        if len(deps) != len(set(deps)):
            raise ValidationError(f"task {task_id!r}: duplicate dependency")
        for dependency in deps:
            if dependency not in dependencies:
                raise ValidationError(
                    f"task {task_id!r}: unknown dependency {dependency!r}"
                )
            if dependency == task_id:
                raise ValidationError(f"task {task_id!r}: self-dependency")
            dependents[dependency].append(task_id)
        counts[task_id] = len(deps)

    ready = [task_id for task_id, count in counts.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        task_id = heapq.heappop(ready)
        order.append(task_id)
        for child in sorted(dependents[task_id]):
            counts[child] -= 1
            if counts[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(dependencies):
        unresolved = sorted(task_id for task_id, count in counts.items() if count)
        raise ValidationError(
            f"dependency cycle prevents ordering; unresolved tasks: {', '.join(unresolved)}"
        )

    ancestors: dict[str, frozenset[str]] = {}
    for task_id in order:
        parents = set(dependencies[task_id])
        for dependency in dependencies[task_id]:
            parents.update(ancestors[dependency])
        ancestors[task_id] = frozenset(parents)
    return Graph(
        order=tuple(order),
        dependents=MappingProxyType(
            {
                task_id: tuple(sorted(children))
                for task_id, children in dependents.items()
            }
        ),
        ancestors=MappingProxyType(ancestors),
    )
