# Reproducible Experiment Runner

A planned local Python CLI for dependency-aware workflows, verified content caching, and recovery of completed work after interruption.

**Under construction:** the design is prepared for review; the runner is not implemented.

Local simulation and data-processing scripts often rerun work unnecessarily or leave uncertain results after interruption. This project aims to make declared inputs, task dependencies, verified outputs, and execution history explicit so completed work can be reused safely within a documented local-workspace contract.

- [Proposed implementation contract](docs/design.md)
- [Design decisions and limitations](docs/design-decisions.md)
- [Phase progress and acceptance gates](docs/progress.md)
