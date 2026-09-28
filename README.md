# Reproducible Experiment Runner

A local Python CLI under development for dependency-aware workflows, verified content caching, and recovery of completed work after interruption.

**Current status:** `runner validate WORKFLOW` checks the workflow structure without running tasks or creating runner state. `runner run WORKFLOW --workers 4 --no-cache` executes eligible tasks in private attempt directories, copies declared inputs, captures separate stdout/stderr logs, and publishes only verified declared outputs. Independent branches can overlap up to the worker limit; failed branches block their descendants. The default run mode currently executes the same way and prints a notice that caching is unfinished. `status` and `resume` remain unavailable.

Run IDs name log directories under the workflow workspace's `.repro/runs/`. Outcomes currently exist only in memory during a run. Existing outputs do not skip work, and an interrupted run cannot yet be resumed. The [development guide](docs/development.md) has the installed CLI commands and current limitations.

Local simulation and data-processing scripts often rerun work unnecessarily or leave uncertain results after interruption. This project aims to make declared inputs, task dependencies, verified outputs, and execution history explicit so completed work can be reused safely within a documented local-workspace contract.

- [Proposed implementation contract](docs/design.md)
- [Design decisions and limitations](docs/design-decisions.md)
- [Phase progress and acceptance gates](docs/progress.md)
- [Development setup and scaffold checks](docs/development.md)
