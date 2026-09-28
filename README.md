# Reproducible Experiment Runner

A local Python CLI under development for dependency-aware workflows, verified content caching, and recovery of completed work after interruption.

**Current status:** `runner validate WORKFLOW` checks the workflow structure without running tasks or creating runner state. `runner run WORKFLOW` executes eligible tasks in private attempt directories and stores verified declared outputs in `.repro/cache/v1/`. An unchanged run verifies each entry and restores outputs without launching its task command. `--no-cache` executes tasks without reading or writing the cache. Independent branches can overlap up to the worker limit; failed branches block their descendants. `status` and `resume` remain unavailable.

Run IDs name attempt directories under the workflow workspace's `.repro/runs/`. Outcomes currently exist only in memory during a run. A cache hit still requires current declared inputs and restores the full declared output set; old workspace outputs alone never authorize reuse. Cached commands do not replay external side effects. An interrupted run cannot yet be resumed, although a new run can verify and reuse a complete cache entry. The [development guide](docs/development.md) has the installed CLI commands and current limitations.

Local simulation and data-processing scripts often rerun work unnecessarily or leave uncertain results after interruption. This project aims to make declared inputs, task dependencies, verified outputs, and execution history explicit so completed work can be reused safely within a documented local-workspace contract.

- [Proposed implementation contract](docs/design.md)
- [Design decisions and limitations](docs/design-decisions.md)
- [Phase progress and acceptance gates](docs/progress.md)
- [Development setup and scaffold checks](docs/development.md)
