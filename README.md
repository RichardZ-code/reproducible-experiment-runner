# Reproducible Experiment Runner

A local Python CLI under development for dependency-aware workflows, verified content caching, and recovery of completed work after interruption.

**Current status:** `runner validate WORKFLOW` checks the workflow without launching tasks. `runner run WORKFLOW` executes ready tasks with bounded concurrency and records attempts, outputs, and outcomes in the workflow directory's `.repro/state.sqlite3`. `runner status RUN_ID` reads those records. `runner resume RUN_ID` rechecks the original workflow and current inputs and outputs, then retains verified completed work, restores from a verified cache entry, or starts a new attempt. Resume also works for runs started with `--no-cache`.

Run and resume hold one workspace lock. A run ID remains stable across invocations, while each executed or restored attempt gets a new numbered directory and separate logs. Status and resume look for `.repro` in the current directory, so enter the workflow directory first. A P05-era attempt directory without a database cannot be resumed automatically. See the [development guide](docs/development.md) for commands and details.

Recovery verifies declared inputs and outputs within a local-workspace contract. Commands are trusted and may have undeclared inputs or external side effects; they are not exactly-once operations. Abrupt parent death can leave an old child running in its private attempt directory. Run and resume publish a versioned manifest from committed state and report publication failures. The [seeded five-task example](examples/simulation/README.md) exercises cache reuse and selective invalidation.

- [Proposed implementation contract](docs/design.md)
- [Design decisions and limitations](docs/design-decisions.md)
- [Phase progress and acceptance gates](docs/progress.md)
- [Development setup](docs/development.md)
