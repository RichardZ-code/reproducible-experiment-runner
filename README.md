# Reproducible Experiment Runner

A local Python CLI under development for dependency-aware workflows, verified content caching, and recovery of completed work after interruption.

**Current status:** `runner validate WORKFLOW` checks the workflow schema, dependency graph, declared paths, and ownership without running tasks or creating runner state. Input availability and contents are deferred until task readiness. Execution, status, and resume remain unavailable.

Local simulation and data-processing scripts often rerun work unnecessarily or leave uncertain results after interruption. This project aims to make declared inputs, task dependencies, verified outputs, and execution history explicit so completed work can be reused safely within a documented local-workspace contract.

- [Proposed implementation contract](docs/design.md)
- [Design decisions and limitations](docs/design-decisions.md)
- [Phase progress and acceptance gates](docs/progress.md)
- [Development setup and scaffold checks](docs/development.md)
