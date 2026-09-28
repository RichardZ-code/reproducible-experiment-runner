# Reproducible Experiment Runner

A local Python CLI under development for dependency-aware workflows, verified content caching, and recovery of completed work after interruption.

**Current status:** the installable package and CLI help surface are scaffolded. Workflow validation, execution, status, and resume are not implemented yet; their commands return an explicit unavailable message.

Local simulation and data-processing scripts often rerun work unnecessarily or leave uncertain results after interruption. This project aims to make declared inputs, task dependencies, verified outputs, and execution history explicit so completed work can be reused safely within a documented local-workspace contract.

- [Proposed implementation contract](docs/design.md)
- [Design decisions and limitations](docs/design-decisions.md)
- [Phase progress and acceptance gates](docs/progress.md)
- [Development setup and scaffold checks](docs/development.md)
