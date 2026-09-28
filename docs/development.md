# Development setup

The local development baseline is Python 3.12 on macOS and Linux. Check that `python3.12` resolves to an installed Python 3.12 interpreter before using the portable commands below. On the initial macOS setup, that command was not on PATH; a separately verified compatible interpreter created the project-local `.venv`. Its machine-specific path is not part of this repository.

Create the environment and install the existing macOS lock:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements/dev-macos.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e '.[dev]'
.venv/bin/python -m pip check
```

For an isolated fresh-install check, create a temporary `.venv` with the same verified Python 3.12 interpreter, run the commands from the repository root, and substitute that temporary environment path for `.venv` in the install and check commands. This leaves the project `.venv` untouched.

On Linux, use `requirements/dev-linux.lock` after that lock has been resolved and verified on Linux. The lock installs runtime, development, and build dependencies first. The editable install then builds with those installed tools and avoids a second dependency resolution. It verifies the source checkout's entry point, not a separately built wheel.

P10 uses the same four commands at the top of this document on each platform, substituting `requirements/dev-linux.lock` on Linux. That lock was generated and inspected from a hosted Linux candidate, but its installation and test suite on Linux remain pending the final matrix. After installing, verify `.venv/bin/python -m pip check`, `.venv/bin/python -m ruff check .`, `.venv/bin/python -m ruff format --check .`, and `.venv/bin/python -m pytest`.

To build and check both consumer installation paths, run the following from the repository root with a fresh temporary output directory. Replace `DIST_DIR` and `REPORT_DIR` with paths outside the checkout. The build uses the already installed locked backend and runs the normal source-archive-then-wheel sequence. Each checker invocation creates a new independent runtime and workspace, installs dependencies normally, and retains small logs under its report directory.

```sh
.venv/bin/python -m build --no-isolation --outdir DIST_DIR
.venv/bin/python scripts/check_installation.py --artifact DIST_DIR/reproducible_experiment_runner-0.1.0-py3-none-any.whl --python .venv/bin/python --example examples/simulation --report-dir REPORT_DIR/wheel
.venv/bin/python scripts/check_installation.py --artifact DIST_DIR/reproducible_experiment_runner-0.1.0.tar.gz --python .venv/bin/python --example examples/simulation --report-dir REPORT_DIR/sdist
.venv/bin/python benchmarks/run_benchmark.py --suite compute --smoke --output-dir REPORT_DIR/benchmark
```

Use one newly created `DIST_DIR` per build and confirm it contains exactly one wheel and one source archive. The benchmark output directory must not already exist. The clean-install probes need package-index access for consumer runtime dependencies and isolated source builds. They do not inherit the development lock or the editable-import repair.

On the initial Codex macOS checkout, files inside the ignored project `.venv` received the macOS `hidden` flag. Python skips a hidden editable-install `.pth` file, so the install could report success while `runner` failed to import `repro_runner`. A fresh temporary macOS environment installed and imported the package normally without a link. If this specific issue recurs in a project-local `.venv`, confirm it with `ls -lO .venv/lib/python3.12/site-packages/*.pth` and `.venv/bin/python -v -c 'import repro_runner'`. Only when Python reports `Skipping hidden .pth file`, run this from the repository root:

```sh
ln -s ../../../../src/repro_runner .venv/lib/python3.12/site-packages/repro_runner
.venv/bin/runner --help
```

The link stays inside the ignored `.venv` and points to the editable source. Do not add it when the ordinary editable install already works.

Check the current CLI and tests:

```sh
.venv/bin/runner --help
.venv/bin/runner validate --help
.venv/bin/runner validate WORKFLOW
.venv/bin/runner run WORKFLOW --workers 4
.venv/bin/runner run WORKFLOW --no-cache
(
  cd WORKFLOW_DIRECTORY
  PATH_TO_CHECKOUT/.venv/bin/runner status RUN_ID
  PATH_TO_CHECKOUT/.venv/bin/runner resume RUN_ID
  PATH_TO_CHECKOUT/.venv/bin/runner resume RUN_ID --workers 2
)
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

Replace `WORKFLOW` with a workflow YAML path, `WORKFLOW_DIRECTORY` with its containing directory, `PATH_TO_CHECKOUT` with the checkout's absolute path, and `RUN_ID` with the ID printed by run. The subshell changes directory only for status and resume: these commands look only in the current directory's `.repro`, even when the workflow was launched elsewhere. Validation checks structure and currently visible path hazards without launching tasks or creating `.repro`; it permits a correctly declared generated input to be absent until readiness. Run uses the workflow file's directory as its workspace. It takes a nonblocking `.repro/lock`, then stages each ready task's declared inputs into `.repro/runs/<run-id>/attempts/<task-id>/<attempt-number>/work/`. It runs exact argument lists with that directory as cwd and writes separate `stdout.log` and `stderr.log` beside `work/`. A task succeeds only after its declared outputs pass file and input checks and the runner copies and publishes them. Use `--workers N` (default 4, minimum 1) to bound active attempts. Resume inherits the prior invocation's worker count unless overridden.

The default run hashes staged declared inputs, direct dependency outputs, and a selected environment record into a task key. A cold task executes and stores only verified declared outputs under `.repro/cache/v1/<key>/`. A warm task validates metadata and every artifact byte, makes independent restoration copies, and publishes all declared outputs without launching its command. The CLI reports `executed`, `retained`, `cached`, `cache_misses`, and uncertain historical launches separately; an executed failure still counts as launched. Cached attempts have no child exit code or child logs. `--no-cache` avoids all cache reads and writes while retaining the ordinary input, output, and publication checks. It can run and resume even when the cache root is unusable.

An invalid final entry is renamed to `.bad-<key>-<uuid>`, diagnosed, and recomputed. Incomplete `.tmp-*` entries are ignored. Permission, disk, unsafe-root, and rename failures are infrastructure errors, not misses. Cache entries have no automatic cleanup. A result becomes accepted only after verified workspace output replacements, the complete cache entry rename when enabled, and a successful SQLite attempt and artifact commit. Only then can dependents start. Multi-file workspace publication is ordered but not atomic; abrupt interruption can leave sibling temporary files. Resume revalidates the full result and either retains it, restores a verified cache entry, or starts a new attempt.

The key uses canonical UTF-8 JSON with sorted object keys, compact separators, and no NaN. It includes key/cache schema versions, runner version, ordered logical command arguments, sorted output paths, sorted staged input paths and hashes, all direct dependency output paths and hashes, and the environment fingerprint. The environment records Python implementation/version/cache tag, OS name/release/machine, installed distribution names/versions, and the selected source lock digest when available. An editable source install uses the matching OS lock; a wheel or absent lock records a null digest and `unavailable` source, with a diagnostic. A present unreadable or symlinked lock fails. This selected fingerprint does not verify installed bytes against lock pins. Change the key schema or runner semantics version when an implementation change alters cache meaning.

Task scripts and configs must be declared inputs to affect the key. Modification times, absolute workspace paths, worker count, run IDs, task labels, and producer cache keys do not enter it. Undeclared files or packages, external executables/services, ambient variables, clock values, randomness, and arbitrary side effects remain outside this non-hermetic cache contract. A cache hit or retained result does not replay the command's side effects. See the [design](design.md) for the full contract and [progress](progress.md) for phase evidence.

SQLite state uses schema version 2 at `.repro/state.sqlite3`. A logical run keeps one ID across invocations; each run or resume process has a numbered invocation, and each task execution or cache restoration has a monotonically numbered attempt. Retention points to an earlier committed attempt and creates no new attempt. Version 2 adds `invocation_provenance`; opening a supported version 1 database for mutation upgrades it in one transaction, preserving earlier records with unknown Git provenance. Read-only status can inspect version 1 without upgrading. Other schema versions and malformed stored records are rejected. The store uses SQLite rollback `DELETE` journaling, `synchronous=FULL`, foreign keys, a five-second busy timeout, and Python 3.12 `autocommit=True` with explicit short `BEGIN` and SQL `COMMIT`/`ROLLBACK`. No write transaction spans child execution, hashing, restoration, or publication.

`status` is a read-only stored snapshot. It does not need the workflow file or rehash current outputs, and a recorded `running` outcome does not prove a process is alive. A pending hot rollback journal may require SQLite recovery that read-only access cannot perform; in that case status reports that recovery is needed, and an owned resume opens state read-write. Resume checks that the workflow basename and normalized task contract match the original before changing run state; YAML formatting alone does not change that contract. It then reevaluates current declared inputs, dependency output bytes, environment identity, and the full published output inventory in dependency order. Matching prior success is retained, missing or changed output may be restored from a verified cache entry, and work without valid reuse evidence gets a new attempt. A prior failed or completed run follows the same explicit resume path. Original cache mode is inherited; a `--no-cache` run can retain verified output but cannot restore from cache.

An attempt records `starting` before the OS launch and `started` only after the parent observes a successful launch. A parent death in between leaves an unknown historical launch count; resume never infers an exit code from a PID or leftover file. Ctrl-C and SIGTERM stop admission and terminate owned process groups with a five-second TERM grace before KILL. Abrupt parent death can leave an old child writing only to its private attempt directory; the runner does not search for or kill orphan processes. Commands are trusted, and external side effects are not exactly once. P05-era log directories without `state.sqlite3` are not automatically resumable.

P07 adds the [integer simulation example](../examples/simulation/README.md). Each invocation observes the workspace's Git commit and dirty flag before task admission, then persists that observation. The runner writes `.repro/runs/<run-id>/manifest.json` after the initial state commit and after the final outcome commit. Its independent manifest schema version is 1. The JSON projects recorded run, invocation, attempt, artifact, environment, and resolution rows; it does not reread today's files to infer historical hashes. A failed initial publication aborts task admission. A failed final publication reports exit 3 while successful task/database results remain successful. Handled signals retain their signal exit code. Abrupt death may leave an older snapshot. `runner resume RUN_ID` revalidates committed work and rebuilds the manifest; `runner status RUN_ID` remains read-only and does not rebuild it. The current Git observation never proves where cached bytes originally came from.

Regenerate the macOS lock only when intentionally reviewing dependency changes. In a Python 3.12 environment with pip-tools installed, run:

```sh
.venv/bin/python -m piptools compile --all-build-deps --extra dev --strip-extras --allow-unsafe --output-file requirements/dev-macos.lock pyproject.toml
.venv/bin/python -m pip install -r requirements/dev-macos.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e '.[dev]'
.venv/bin/python -m pip check
```

For a first lock generation, install `pip-tools` in `.venv` before compiling; later runs use its recorded pin. Re-run tests and Ruff after a refresh, review the lock diff and generated provenance, and record the resolver and Python versions. Generate the Linux lock with the same command and `requirements/dev-linux.lock` on Linux. Do not copy or rename the macOS resolution as Linux evidence. The lock includes build requirements; development package builds use `.venv/bin/python -m build --no-isolation` once distribution verification is in scope. P10's temporary Linux generator produced the reviewed candidate recorded in [CI verification](ci.md); normal CI installs the checked-in lock rather than regenerating it.

Locally verified in P02: macOS Python 3.12 environment, macOS lock resolution and installation, editable package/entry point, CLI help and unavailable behavior, smoke tests, and Ruff. A second fresh temporary macOS environment passed the documented install and check sequence without an import link. P10 separately verified fresh wheel and source-distribution installations on local macOS and generated the Linux lock candidate on a hosted Linux runner. Linux installation, Linux behavior, and the final hosted matrix remain pending gates.

P03 locally verified `runner validate` from another directory using the installed entry point, plus schema, graph, path, ownership, glob, and input-readiness tests. These checks do not establish task execution.

## Preliminary clean-wheel check

P08 built a wheel with the installed, declared build tools and installed it into a separate temporary Python 3.12 environment. This checks the wheel independently of the editable source installation. It does not replace P10's source-distribution or hosted-platform checks. To repeat it without copying development packages or state:

```sh
BASE_PYTHON=$(.venv/bin/python -c 'import sys; print(sys._base_executable)')
WHEEL_CHECK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/repro-wheel.XXXXXX")
.venv/bin/python -m build --wheel --no-isolation --outdir "$WHEEL_CHECK_DIR/dist"
"$BASE_PYTHON" -m venv "$WHEEL_CHECK_DIR/runtime"
"$WHEEL_CHECK_DIR/runtime/bin/python" -m pip install --no-cache-dir "$WHEEL_CHECK_DIR"/dist/*.whl
mkdir -p "$WHEEL_CHECK_DIR/workspace/config"
cp examples/simulation/workflow.yaml examples/simulation/*.py "$WHEEL_CHECK_DIR/workspace/"
cp examples/simulation/config/*.json "$WHEEL_CHECK_DIR/workspace/config/"
cd "$WHEEL_CHECK_DIR/workspace"
env -u PYTHONPATH "$WHEEL_CHECK_DIR/runtime/bin/runner" validate workflow.yaml
env -u PYTHONPATH "$WHEEL_CHECK_DIR/runtime/bin/runner" run workflow.yaml
env -u PYTHONPATH "$WHEEL_CHECK_DIR/runtime/bin/runner" run workflow.yaml
```

Use the cold run's printed ID for `status RUN_ID` and `resume RUN_ID` from that temporary workspace. Confirm the module origin with `"$WHEEL_CHECK_DIR/runtime/bin/python" -c 'import repro_runner; print(repro_runner.__file__)'`; it must be under the temporary environment's site-packages. This procedure intentionally copies the example source because it is not included in the wheel. P08's observed versions, artifact hash and outcomes are in [verification](verification.md).
