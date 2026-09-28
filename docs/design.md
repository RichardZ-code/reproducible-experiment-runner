# Reproducible Experiment Runner: v0.1.0 design

**Implementation contract for staged phases.**

The requirements below remain the project contract; implementation is staged through the phase plan. [Decisions](design-decisions.md) record clarifications, and [progress](progress.md) identifies behavior verified so far and work still pending.

## A. Purpose and scope

The runner will execute local simulation and data-processing workflows, reuse verified results when declared inputs are unchanged, and recover completed work after process interruption. The target is Python 3.12 on Linux and macOS, with trusted commands, one owner of a local workspace, and declared regular-file inputs and outputs.

The stack is Python, `asyncio` for subprocess scheduling, `sqlite3` for execution records, `hashlib` for SHA-256, PyYAML for configuration, Typer for the CLI, and pytest for tests. Ruff and standard Python build tools support development. This is neither an operating-system sandbox nor a hermetic build system.

Proposed packaging, to be created in P02:

- Distribution `reproducible-experiment-runner`, version `0.1.0`, package `repro_runner` under `src/`, and console entry point `runner = repro_runner.cli:app`.
- Initially declare `requires-python = ">=3.12,<3.13"`. Widening support requires tests and an explicit contract change.
- Runtime bounds: `PyYAML>=6,<7` and `typer>=0.12,<1`. These are proposed compatibility ranges, not claims about tested or resolved versions. P02 must verify the selected versions against their official documentation.
- Use setuptools with `setuptools.build_meta` and wheel. The `dev` extra includes pytest, Ruff, build, and pip-tools. No type checker is planned.
- Create `.venv` in P02. Commit `requirements/dev-macos.lock` and `requirements/dev-linux.lock` with exact resolved development, runtime, and build dependencies for their respective Python 3.12 environments. P02 resolves the local platform; the other target must be resolved and checked before its P10 gate. Do not resolve or install dependencies in P01.
- Regenerate on each target OS using `python -m piptools compile --all-build-deps --extra dev --strip-extras --allow-unsafe --output-file requirements/dev-macos.lock pyproject.toml` on macOS, substituting `requirements/dev-linux.lock` on Linux. Install the selected lock with `python -m pip install -r requirements/dev-macos.lock`, then `python -m pip install --no-build-isolation --no-deps -e '.[dev]'`; development builds use `python -m build --no-isolation` so the installed pinned build tools apply. Review diffs and record resolver/tool versions. These are future commands, not executed P01 checks. Do not refresh pins as unrelated maintenance. The [pip-tools documentation](https://pip-tools.readthedocs.io/en/stable/#cross-environment-usage-of-requirements-in-requirements-txt-and-pip-compile) explains why resolution is performed per environment.
- Each lock constrains development installations on its target OS; a fresh wheel install still tests ordinary dependency resolution. Published metadata retains runtime ranges; installed runtime identities are recorded separately, as defined in section I. A clean wheel installation cannot assume the development lock is present.

Excluded from v0.1.0: a web UI or HTTP service, accounts, multitenancy, remote workers, distributed coordination, PostgreSQL, Redis, Kafka or another queue service, Kubernetes, LLM/API integration, remote/S3 caches, directory outputs, cache compression/eviction/concurrent garbage collection, a custom language beyond the YAML schema, automatic retries, task timeouts, and `explain`/`graph` commands. Docker is optional and deferred. PyPI and cloud deployment are not release prerequisites.

## B. Architecture and ownership

```text
CLI -> configuration + DAG validation -> scheduler (one event-loop owner)
                                           |
                      snapshot + hash -> cache lookup or subprocess
                                           |
                       verify -> runner-only artifact publication
                                           |
                           SQLite result -> dependent readiness
                                           |
                               versioned JSON manifest
```

The eventual compact layout is a proposal, not a request to create empty modules:

```text
src/repro_runner/
  __init__.py       package version
  cli.py           command arguments, presentation, exit codes
  config.py        safe YAML, typed normalized workflow, path declarations
  graph.py         dependency graph, ownership, deterministic traversal
  scheduler.py     task states, worker slots, failure propagation
  executor.py      attempt directories, subprocesses, logs, cancellation
  hashing.py       file snapshots, canonical records, environment identity
  cache.py         verified entries, restoration, output publication
  state.py         SQLite, workspace lock, resume records
  manifest.py      provenance projection and staged JSON publication
tests/unit/ and tests/integration/
requirements/        per-OS development lock files
examples/simulation/
benchmarks/
```

Only the scheduler mutates in-memory scheduling state. Child processes perform task work. Blocking file operations may initially run synchronously in bounded chunks; subprocess waiting must yield to the event loop. Do not add a thread pool or database service without measured need. All database access has one runner owner, short transactions, and no transaction held across subprocess execution, hashing, copying, or waiting.

Only the runner publishes to declared shared output locations. Children receive private working directories. Detailed semantics belong in ordinary functions and small data records, without plugin interfaces, generic factories, or speculative services.

## C. Workflow schema and validation

The root is a mapping with exactly `schema_version` and `tasks`. Version is integer `1`, excluding booleans. `tasks` is a non-empty mapping. Task IDs match `[a-z][a-z0-9_]{0,63}`. Each task has these fields:

| Field | Required | Type and rule |
|---|---|---|
| `command` | Yes | Non-empty list of strings; executable is non-empty; no NUL in any argument. Empty later arguments are valid literal arguments. |
| `deps` | No | List of task IDs, default `[]`; order is not semantic. |
| `inputs` | No | List of relative file declarations or supported globs, default `[]`. |
| `outputs` | Yes | Non-empty list of explicit relative regular-file destinations; globs are forbidden. |

Output-free tasks are excluded to keep reuse tied to observable artifacts. Input-free tasks are allowed but remain subject to undeclared-dependency limitations. Reject unknown fields, implicit scalar coercion, nulls in place of lists, duplicate entries, and duplicate YAML mapping keys at every level. Use a customized PyYAML SafeLoader mapping constructor that detects duplicates before construction can overwrite them. Reject YAML merge keys, anchors/aliases, and custom tags for this small schema. Reject multiple YAML documents.

Illustrative design syntax only; these scripts do not yet exist:

```yaml
schema_version: 1
tasks:
  generate:
    command: [python, generate.py, --config, config/base.json, --output, out/input.json]
    inputs: [generate.py, config/base.json]
    outputs: [out/input.json]
  simulate_a:
    deps: [generate]
    command: [python, simulate.py, --input, out/input.json, --config, config/a.json, --output, out/a.json]
    inputs: [simulate.py, config/a.json, out/input.json]
    outputs: [out/a.json]
```

Structural validation happens for the whole workflow before launching any child. Reject unknown dependencies, self-dependencies, cycles, malformed commands, unsupported versions, invalid declarations, duplicate output ownership, and all statically detectable collisions. Use Kahn's algorithm with a lexicographically sorted ready queue; use sorted IDs for diagnostics and descendant propagation. Invalid worker options are rejected before runtime state is created.

An output map assigns every produced path to exactly one task. Any consumer of that path must have its producer as a transitive ancestor. An explicit direct edge is not required when a dependency path already exists. Filenames never create edges. A task cannot read its own output. Equal paths across different tasks' read-only inputs are allowed; duplicate declarations within one task are rejected, including two declarations that expand to the same path.

Supported globs are deliberately small: `*` and `?` within the final filename component only, matched case-sensitively against the whole basename; the directory prefix must be literal. `*` does not cross `/`; dotfiles are included when their names match. Reject `**`, bracket classes, braces, escapes, and metacharacters in directory components. Enumerate only the named directory, without following symlinks, and sort matches by relative path. Runner-owned temporary basenames beginning `.repro-tmp-` are excluded from expansion. Any other matched directory, symlink, device, socket, or non-regular file is an error, not an ignored match.

Before execution, match each pattern against all declared output names as well as currently observable filesystem names. Every potentially matching producer must be an ancestor, including a producer whose file is not present yet. At readiness, the expansion is the union of filesystem matches and matching declared output paths, deduplicated across those two sources only. A declared produced match must exist as a verified artifact then; it cannot disappear silently from the input set. A genuinely empty expansion fails that task. Membership additions and removals affect identity. Any overlap between separate input declarations fails that task if detectable only at readiness.

`validate` checks schema, graph, ownership, canonical spellings, and existing filesystem components/matches. It does not require absent input files or glob directories to exist, and reports that existence and content checks are deferred until readiness. This allows upstream-generated inputs. Once dependencies are verified, readiness-time checks require every expanded input to be a regular file, recheck aliases and ownership, and verify produced input bytes against the selected ancestor result. A readiness failure marks the consumer failed, blocks descendants, and allows independent branches to proceed. It does not retroactively turn an earlier historical result into success or failure.

## D. Workspace and paths

The workspace root is the workflow file's directory. `run`/`validate` resolve the CLI workflow argument from the caller's directory once; all declared paths thereafter are workspace-relative. The workflow itself must be a regular, non-symlink file. Resolve its parent directory once to the physical workspace root; caller-path aliases outside that root (such as a system temporary-directory alias) do not relax checks on declared paths within it. Store its basename and normalized contract, never a personal absolute workspace path.

Intended runtime locations:

| Location relative to workspace | Purpose |
|---|---|
| `.repro/state.sqlite3` | State database; schema version 2 after P07 writable opening |
| `.repro/lock` | Stable lockfile used by an OS lock |
| `.repro/cache/v1/<sha256>/metadata.json` | Complete cache metadata |
| `.repro/cache/v1/<sha256>/files/<output-path>` | Cache artifacts |
| `.repro/cache/v1/.tmp-<uuid>/` | Unpublished entry, never a hit |
| `.repro/cache/v1/.bad-<key>-<uuid>/` | Quarantined invalid entry, never a hit |
| `.repro/runs/<run-id>/attempts/<task-id>/<number>/work/` | Child working directory |
| `.repro/runs/<run-id>/attempts/<task-id>/<number>/publish/` | Runner's frozen output copies |
| `.repro/runs/<run-id>/attempts/<task-id>/<number>/stdout.log` | Child stdout |
| `.repro/runs/<run-id>/attempts/<task-id>/<number>/stderr.log` | Child stderr |
| `.repro/runs/<run-id>/manifest.json` | Latest completed manifest projection |

Declared paths must use canonical relative POSIX syntax: no leading `/`, drive prefix, backslash, empty component, `.` or `..` component, repeated slash, or trailing slash. For a simple portable v0.1.0 namespace, components use ASCII letters, digits, spaces, `_`, `-`, and `.`, with no leading/trailing spaces and no trailing dot. Patterns additionally allow the final-component glob syntax in section C. Reject other characters rather than silently rewriting them.

Reject any declaration under top-level `.repro`, `.git`, or `.venv`, case-insensitively. These are reserved; every basename beginning `.repro-tmp-` is also reserved for publication files and forbidden in literal declarations. Outputs also cannot overwrite the workflow file. At each validation, staging, and publication checkpoint, reject symlinks and non-directory parent components using `lstat`, not a resolve-and-follow policy. Runtime roots and their relevant components must also be real directories/files, not symlinks. Reject existing output directories or special files. Create missing output parents only when mutation is authorized and the workspace lock is held.

Maintain a case-folded path namespace even on case-sensitive Linux. Distinct spellings such as `out/A.json` and `out/a.json`, or parent aliases `Out/a` and `out/b`, conflict. Check both declarations and actual relevant directory entries, including glob expansions. Exact repeated read-only input paths across tasks are legitimate. If distinct existing declared paths share a device/inode identity, reject that hard-link alias rather than assigning independent ownership. Reject file/directory prefix conflicts such as `out/result` and `out/result/data.json`, and input/output collisions in the same task. These rules deliberately reject some Linux-only layouts to avoid macOS alias collisions.

Cache roots must be real directories; a symlink at an individual cache-entry/artifact location is rejected as an invalid entry under section J without following its target.

The workspace is on a local filesystem with ordinary atomic rename and advisory lock support. External programs must not edit inputs, outputs, cache, environment, or runtime metadata during a publication critical section. Pre/post copy checks and a pre-publication source check detect ordinary changes during task execution; they cannot make arbitrary external writers participate in a transaction. Commands can deliberately access absolute OS paths or escape via arguments. Validation is not confinement. Moving the workspace alone must not alter content keys.

## E. CLI contract

The following commands describe the intended public interface:

```text
runner --help
runner validate examples/simulation/workflow.yaml
runner run examples/simulation/workflow.yaml --workers 4
runner run examples/simulation/workflow.yaml --workers 4 --no-cache
runner status RUN_ID
runner resume RUN_ID --workers 4
```

`run` starts a new run with a random UUID4 rendered as 32 lowercase hex digits. Default workers: 4. Workers must be an integer at least 1; no automatic CPU-count tuning or arbitrary upper cap. Print `Run ID: <id>` and the logical workflow name to stdout after the run record is created and before task scheduling. Finish with outcome and counts for executed, cache-restored, retained, failed, blocked, and interrupted tasks. Diagnostics go to stderr with task/path context, without dumping environment variables. Counters have the precise definitions in section F.

`status` and `resume` look only for `.repro` in the current directory. For the example commands above, first enter `examples/simulation` before either command. Do not search parents, the home directory, or other projects, and do not add a workspace option in v0.1.0. An absent database or unknown run ID is an error and must not initialize a workspace, create a run, or allocate new attempt/manifest paths. Recovery of an existing SQLite journal during resume is allowed only under ownership.

`status` is a read-only snapshot of persisted run/task/attempt state, counts, and relative log locations. It neither verifies current artifact bytes nor repairs state. A persisted `running` status may be stale after abrupt death; print that caveat rather than interpreting the row as proof of a live process. A known failed/interrupted run still gives a successful status-query exit code.

`validate` never starts a task or creates `.repro`; it prints structural validity and the deferred file-check boundary. `--help` is also read-only. `run` and `resume` acquire the workspace lock and may create/update runtime data, output parents, and outputs. `--no-cache` belongs only to `run`: it bypasses all cache reads and writes, including quarantine, but retains keys, records, manifests, output verification, and recovery.

`resume` inherits the original cache mode. Its omitted worker count uses the last invocation's recorded value; an explicit value changes this invocation and is recorded. It has no cache override flag. It prints the existing run ID, a new invocation number, and a new summary. Resuming an already completed run still performs verification; a fully valid result executes zero children and is recorded as retained work.

| Exit code | Meaning |
|---|---|
| 0 | Successful help/validation/status query, or all required tasks verified complete |
| 1 | Task/readiness/output failure or blocked required work |
| 2 | Usage, structural validation, unknown run, incompatible workflow, or unsupported state schema |
| 3 | Workspace ownership, filesystem/database infrastructure failure, or unfinished command |
| 130 | Runner handled SIGINT |
| 143 | Runner handled SIGTERM |

The run outcome remains interrupted for either handled signal. SIGKILL supplies no runner-generated exit report. In incremental phases, unavailable commands must return 3 with a clear message. P02 exposes help and command entry points; functional validation arrives in P03, execution in P04, cache in P05, persisted status/resume in P06, and full manifests in P07. Earlier phases must label the missing capabilities accurately.

## F. Scheduling and outcomes

`ready` is derived, not a persisted task state. A pending task becomes eligible when every direct prerequisite has a verified `succeeded` or `cached` result in the current invocation. Current-invocation verification includes resume reconciliation; stale success rows alone do not qualify. Before using produced inputs, recheck their integrity. Process eligible task IDs in lexical order when worker capacity permits; completion order can vary.

Task states are exactly `pending`, `running`, `succeeded`, `cached`, `failed`, `blocked`, and `interrupted`. The normal transitions are:

```text
pending -> running -> succeeded | cached | failed | interrupted
pending -> failed       readiness check failure
pending -> blocked      failed/blocked prerequisite
pending -> interrupted  cancellation before launch
pending -> succeeded | cached  verified retention during resume only
```

`running` includes preparation/restoration/publication, not just time with a child PID. At most `workers` task slots and child subprocesses owned by the current invocation may be active. Release a slot only after its child is reaped and task finalization has completed. A cache restoration occupies a slot but launches no child. Orphans from an abruptly killed earlier owner are outside the new invocation's concurrency count; this limitation is explicit in section H.

A zero subprocess exit is necessary but insufficient for `succeeded`: verify and publish the complete output set and commit its result first. `cached` means verified cache artifacts were fully restored and their result committed. Readiness failures before attempt allocation are recorded on the task with a diagnostic and no attempt. Preparation failures after allocation have a failed attempt with no launched subprocess.

Continue independent branches after task failures; mark all descendants blocked with prerequisite reasons. Never return a blocked task to readiness during that invocation. A new resume reconciliation may reset failed, blocked, interrupted, or invalid completed tasks to pending; previous attempt history remains immutable.

Run outcomes are separate: `running`, `succeeded`, `failed`, `interrupted`. All tasks verified succeeded/cached means run succeeded. Any failed/blocked task means failed, unless cancellation or an infrastructure abort prevented normal completion, in which case the outcome is interrupted with a reason. Persistence failure may leave the last durable outcome running. The CLI still reports the infrastructure error.

An invocation records a resolution for each completed task: `executed`, `cache_restored`, or `retained`, separate from task state. Retained work keeps its original succeeded/cached state and references its prior attempt; it does not allocate a fictitious new execution attempt. Executed count counts confirmed actual child launches, including failed/interrupted children. Persist a launch intent before spawning and confirmation immediately after spawning. A crash between those writes leaves an uncertain launch, reported separately as `uncertain_launches`; do not invent an exact historical execution count for that interval. Cache-restored and retained counts count successful reuse operations. Failed/blocked/interrupted counts describe final task states, so these counts are not all disjoint. Also record cache hits/misses among tasks whose cache lookup was actually performed; retained, blocked, and never-ready tasks are excluded from that denominator.

## G. Attempts and command execution

For work not retained during resume, allocate a monotonically numbered attempt per run/task and create its directory exclusively, never reuse or truncate an earlier attempt. P04 uses new run IDs and in-memory numbering; P06 persists allocation before creating directories. A missing directory after a crash is an incomplete attempt, not a reason to reuse its number.

After eligibility and readiness-time declaration checks, new execution/restoration work follows this sequence:

1. Expand and validate declared inputs and obtain verified ancestor artifact records.
2. Create the private `work` directory. Copy only declared inputs, preserving relative paths. Produced inputs are ordinary copies of verified workspace artifacts; do not copy undeclared dependency files, the repository, environment, or cache tree.
3. Hash the exact staged input bytes, record sorted paths and sizes, and compare their hashes with freshly read source bytes and any producer hashes. Check file type/components and before/after file metadata while copying; retrying an unstable snapshot is not automatic. Fail with an input-changed diagnostic on disagreement.
4. Build the task key from this snapshot and the dependency/environment record, even with cache disabled. Prepare output parent directories in `work`.
5. If reuse is unavailable, launch via `asyncio.create_subprocess_exec(*argv, cwd=work, ...)` without implicit shell interpretation. Do not expand shell variables, tilde, wildcard arguments, pipelines, or redirections.
6. After child exit, reverify staged inputs (a child must not modify them), current source contents, glob membership, and prerequisite artifact identity. Missing or changed files fail the task. Verify every staged output and freeze ordinary copies under the sibling `publish` directory before publishing.

The executable token must be a bare program name without path separators. `python`, `python3`, and `python3.12` resolve to `sys.executable` of the Python 3.12 runner environment. Other names resolve once through that environment's executable directory followed by PATH. Failure to resolve/start is a task failure. Record the logical argument list separately from the runtime resolved executable. Never substitute system Python for a reserved Python token.

Arguments may contain spaces and are passed unchanged. Scripts must use explicit relative input/output arguments and declared scripts/configuration. Commands must not depend on undeclared siblings. The runner cannot infer that an arbitrary argument names a file. A command must wait for its own output-writing descendants before returning; deliberate detached work is outside the successful-task contract.

The resolved absolute executable is used internally and may be shown only in a local diagnostic when necessary. Portable keys/manifests store logical executable, resolution kind (`runner_python` or `path_program`), Python identity where applicable, and basename, not a personal absolute interpreter path. Versions/content of arbitrary PATH tools are not automatically captured and remain an explicit limitation.

Re-expand/re-hash original sources immediately before entering publication, and fail on a difference from the staged snapshot. Copying staged outputs to runner-owned frozen copies prevents later writes to the child's `work` files from changing the bytes being published. No accepted key may describe different bytes from its recorded snapshot. Source writes after the final check are outside the cooperative publication boundary in section D; the next use/resume revalidates them. Do not claim a globally atomic snapshot of arbitrary external writers.

Attempt isolation, copy/hash checks, and ownership begin in P04; P05 adds canonical cache identity and reuse; P06 persists attempt history and recovery without changing child path semantics.

## H. Logs, failures, and interruption

For each attempt that will launch a child, open distinct binary stdout/stderr files and redirect subprocess descriptors directly to them. No unbounded in-memory capture or pipe-draining dependency is allowed. A failed launch has empty log files and a recorded startup error; a restored cache attempt has no child logs and stores null log fields. Runner diagnostics belong to records/CLI, not fabricated child output.

Nonzero child exit, changed inputs, missing output, invalid output type, or failed output verification means task failure. A partial file in `work` does not authorize publication or caching. Undeclared work files stay private and are ignored. Task-local failures permit independent branches to continue. Permission errors, disk exhaustion, SQLite errors, or loss of safe publication/ownership abort the invocation with code 3; cancel owned active children, preserve available evidence, and do not conceal them as cache misses.

Each child starts a new POSIX session/process group. On SIGINT or SIGTERM, stop launching tasks, mark cancellation requested, send SIGTERM to process groups owned by this live invocation, allow 5 seconds, then SIGKILL groups that remain. Await/reap direct children and close log descriptors. Repeated interruption requests skip the remaining grace period. Cleanup also runs on infrastructure abort. Do not kill by process name, replay saved PIDs, or signal possibly recycled process groups on resume.

Prepare copies/hashes in bounded chunks that yield to cancellation between chunks. Only final workspace replacements, cache rename, and the short SQLite result commit form the non-yielding publication critical section; defer cancellation through that section. The five-second grace governs child termination after handling the signal, not a hard real-time guarantee over filesystem stalls. Either finish committing a verified task result, or leave incomplete evidence for reconciliation. Already committed results remain completed; other active/pending work becomes interrupted. Record an interrupted run and stage a manifest if the database/filesystem still permits it. A stopped task is never automatically retried in the same invocation.

SIGKILL cannot run these handlers. The OS releases the parent's lock, but an old child may continue writing into its old unique `work` directory and logs. A resumed attempt uses a different directory and only the new runner can publish, so ordinary late fixture writes cannot overwrite new final outputs. Resume does not continue, adopt, or kill that child. Tests must track and explicitly clean their own surviving fixtures, including a test that releases an old writer after a new attempt completes. Overlap with orphaned work can consume resources beyond the new invocation's worker limit. Detached descendants, deliberate writes outside `work`, and arbitrary external side effects are outside the guarantee.

## I. Cache identity and environment

Use SHA-256 of UTF-8 canonical JSON: object keys sorted, no insignificant whitespace, no NaN/Infinity, and no ambiguous concatenation. Preserve command argument order. Sort set-like lists and normalize omitted defaults before hashing. File hashes cover bytes, not modification times. Key record version 1 contains:

| Field | Identity |
|---|---|
| `key_schema`, `cache_schema`, `runner_version` | Format/semantic identity; incompatible implementation changes require an identity bump |
| `command` | Logical argument list, including seeds supplied as arguments |
| `outputs` | Sorted canonical relative output destinations |
| `inputs` | Sorted expanded relative paths and SHA-256 hashes of staged bytes |
| `dependency_outputs` | Sorted direct-dependency task IDs, output paths, and verified output hashes |
| `environment` | Explicit record below |

Include all outputs of direct dependencies, even if only some are declared consumer inputs. This is conservative and avoids implicit relevance inference. Transitive produced inputs also enter through their declared input bytes. Do not include producer cache keys, run/attempt IDs, absolute workspace paths, Git revision/dirty status, timestamps, or undeclared repository files. Workflow structure has its own resume identity; the task key is not the hash of the whole workflow.

The environment fingerprint contains Python implementation and full version, `sys.implementation.cache_tag`, OS name (`Linux`/`Darwin`), OS release, machine architecture, and sorted installed Python distribution names/versions from `importlib.metadata`. This inventory is a conservative selected dependency identity: even development-package version changes can invalidate reuse. Include runner version explicitly. Do not record installation paths, usernames, hostnames, ambient environment variables, credentials, or a full environment dump. Inventory is not proof that installed package bytes are unmodified.

Record `dependency_lock_sha256` and `dependency_lock_source`. In an editable source installation, recognize the checkout only when the loaded package is exactly its `src/repro_runner` and the derived project root has matching distribution metadata in `pyproject.toml`; hash its regular, non-symlink `requirements/dev-macos.lock` or `requirements/dev-linux.lock`, selected by the current OS, if present. Do not search arbitrary parents/workspaces for a lock. In a wheel installation, or when that source lock is absent, set the digest to null and source to `unavailable`, with a clear diagnostic. A present but unreadable or symlinked lock is an environment error, not an unavailable digest. The actual installed distribution inventory is still required, so caching remains enabled. A wheel outside the source checkout does not pretend to have a source lock. Source-with-lock and wheel-without-lock fingerprints can differ even with equivalent packages; this reduces reuse, not correctness.

Capture the environment at invocation start and recheck it before publication; changes fail the affected task and require a fresh invocation. Hashing every task input ensures a declared script edit invalidates the task even when command text is unchanged. Glob membership changes are reflected in the expanded list. If a producer reruns but outputs identical bytes, its consumers' effective keys may stay unchanged. Do not promise all descendants always rerun.

Ambient variables, external executables, network services, clocks, unseeded randomness, native libraries not identified by the selected record, and undeclared file dependencies remain limitations. Content keys assert equality under the declared contract, not hermetic execution. Users must not put secrets in commands/configurations intended for recorded provenance.

## J. Cache, restoration, and publication

Cache metadata schema 1 contains the full canonical key record, its digest, and the exact declared output set with relative paths, SHA-256 hashes, and nonnegative integer byte sizes. Artifact bytes live under `files`. Validate metadata types, schema, recomputed key, requested declarations, path safety, unique destinations, every artifact's size/hash, and absence of symlinks. Treat unknown metadata fields or unexpected artifact files as invalid. There is no trust in a directory name alone.

An absent or structurally/content-invalid entry is a cache miss with a diagnostic. Under the exclusive workspace lock, atomically rename an invalid final entry to a unique `.bad-<key>-<uuid>` path before reuse/storage continues. A missing entry needs no quarantine. If quarantine fails due to I/O/permissions, abort with infrastructure failure. Leave quarantined and incomplete `.tmp-*` entries ignored; no automatic garbage collector is introduced. Thus replacement can publish at the original key without repeatedly encountering the same invalid entry. EACCES, disk errors, and other genuine filesystem failures are not corruption misses.

Ordered execution publication protocol:

1. Require exit zero, unchanged staged/source inputs and environment, and all staged outputs regular and valid. Freeze and hash the whole output set in `publish`. Check no output is an alias of an input or another output.
2. When caching is enabled, prepare a complete `.tmp-<uuid>` entry on the cache filesystem by ordinary copies of the frozen outputs plus metadata. Reverify all copied hashes. Do not expose this temporary entry as a hit.
3. Prepare unique exclusive temporary sibling files for every workspace destination, copy frozen bytes, and verify hashes. After all preparation copies finish, re-expand/re-hash current inputs and staged inputs, recheck dependency hashes and environment, and compare with the accepted snapshot again. On disagreement, fail before replacement. Recheck safe parent components, enter the publication critical section, then replace destinations individually with `os.replace` in sorted path order. Failures leave the task incomplete. Only declared destinations can be replaced.
4. When caching is enabled, atomically rename the complete temporary entry to its final key. Never overwrite a valid final entry blindly. If one already exists, verify it and require matching artifact hashes; keep it if equal, otherwise fail with a nondeterministic-output/cache-conflict diagnostic. Invalid entries follow quarantine above.
5. Commit SQLite artifact records and attempt/task success together in one short transaction. Only then release dependents. P04/P05 use the same ordering with in-memory success until P06 supplies persistence.

Temporary destination files use exclusive runner-generated `.repro-tmp-<uuid>` basenames and are never input artifacts or evidence of success. Pre-publication failures do not expose staged outputs. A failure partway through step 3 may leave a mixed workspace set; do not delete unrelated pre-existing files to simulate rollback. Fail/interrupt the attempt and require reconciliation before reuse. A cache-write error after workspace publication also prevents success; it is not silently downgraded. There is no multi-file atomic commit shared with SQLite.

On a valid cache hit, use the already allocated preparation attempt, set its operation to `restore`, copy verified cache artifacts into its private `publish` directory, verify those copies, recheck source snapshot/environment, and perform step 3 for the full output set. Commit the artifact rows and `cached` state only after all outputs are restored. No child is launched. Always use copies, never writable hard links into the cache. Interruption during restoration leaves an incomplete attempt; resume may restore the whole set again.

Cache entries describe verified successful executions independent of the run that produced them. If the parent dies after step 4 but before step 5, a later invocation may validate and restore that entry in a new cached attempt. The stale attempt is still interrupted, not retroactively declared succeeded. With cache disabled, steps 2 and 4 and every cache lookup are omitted; the other checks/order remain mandatory.

Process-interruption safety is the target. Atomic rename publishes one filesystem entry. No guarantee of universal machine-power-loss durability, fsync ordering across multiple devices, or transactional visibility to external readers is made.

## K. SQLite, ownership, and resume

Use SQLite schema version 2 via `PRAGMA user_version`, foreign keys enabled, rollback journaling (`DELETE`), and a 5-second busy timeout. P07 adds one transactional version 1 to version 2 upgrade for invocation Git observations; earlier observations remain unknown. Reject other unsupported schema versions without destructive initialization. Version zero is allowed only for a newly created empty database owned by this invocation; a pre-existing unversioned database is an error. Read-only status opens an existing database using URI `mode=ro`, takes a short consistent read transaction, and does not initialize or upgrade schema. A journal-recovery/write requirement is an explicit status error; execution/resume under ownership performs recovery.

Proposed tables, all within one workspace database:

| Table | Primary key and contents |
|---|---|
| `runs` | `run_id`; original workflow basename, normalized JSON/hash and workflow schema, cache mode, initial workers, creation/update timestamps, latest outcome/reason |
| `invocations` | `(run_id, invocation_no)`; initial run or resume, workers, environment, start/end UTC timestamps, monotonic duration, outcome |
| `invocation_provenance` | `(run_id, invocation_no)`; Git commit, dirty flag, availability, and observation time for new P07 invocations; absent row means not collected for older history |
| `tasks` | `(run_id, task_id)`; current state, selected/current attempt number (nullable), blocked/failure reason; references original normalized task contract |
| `task_attempts` | `(run_id, task_id, attempt_no)`; invocation number, operation (`execute`/`restore`/`prepare`), state, logical command/resolution descriptor, canonical key record/digest, input/dependency snapshots, timing, nullable child exit code, launch state (`not_started`/`starting`/`started`), relative work/log paths, diagnostic |
| `artifacts` | `(run_id, task_id, attempt_no, relative_path)`; output SHA-256 and size; foreign key to attempt |

The invocations table preserves environment/worker/provenance changes and retained-work attribution across resumes. It is history for one run, not a separate execution service. Missing start/end/exit values are null, never invented zeros. Store UTC timestamps for chronology and measured monotonic durations per invocation/attempt; do not subtract monotonic clocks across processes. JSON fields contain versioned normalized structures. Earlier attempts and artifacts are append-only once terminal; only a stale running attempt is finalized as interrupted on recovery.

Attempt allocation uses one plus the maximum number in that run/task history, never the currently selected result number. Transactions: initialize the run/invocation/tasks together; allocate each attempt and mark running before staging/launch; commit launch intent before spawning and launch confirmation after successful spawning; commit a final attempt outcome and selected task/artifact rows together after publication; commit invocation/run outcome and resolution summary at termination. Failure diagnostics get their own short transaction if necessary. No artifact hashing, subprocess lifetime, or file copying occurs inside a DB transaction. A failure after filesystem publication but before DB commit is deliberately recoverable, not hidden.

Acquire a nonblocking exclusive `fcntl.flock` on `.repro/lock` before any runtime/output mutation other than the minimal creation/opening of `.repro` and the stable lockfile itself. Those bootstrap operations must be race-safe. A second owner exits 3 before touching state/cache/outputs. Keep the descriptor open for the entire mutating invocation, mark it non-inheritable, and launch children with descriptors closed except their intended streams. Do not unlink the lockfile on release: replacing its inode could allow two owners. Parent death releases ownership in the OS; lockfile presence or a stored PID does not establish ownership. Begin this protection in P04 because it already publishes shared outputs; P06 adds persistent recovery and adversarial ownership tests.

Resume protocol:

1. Locate the existing `.repro`, database, and lockfile in the current directory. Acquire the existing lock without recreating it, open the existing database in read/write mode (not create mode), permit any SQLite journal recovery, and check schema/run existence before allocating new records or paths. Missing runtime components produce an error, not fresh initialization. Do not require a read-only precheck that would prevent hot-journal recovery.
2. Load the recorded normalized workflow. Read the recorded basename from this directory, parse/validate it, and compare its normalized contract hash. Missing workflow or changed command/deps/declarations/version rejects resume with code 2 and directs the user to start a new run. Comments, mapping order, and list order for set-like declarations are ignored; command argument order is significant. Do not rewrite history from the edited file. The normalized workflow is frozen for the invocation; later edits are never silently applied to tasks already scheduled.
3. Start a new invocation with inherited cache mode and selected worker count. Finalize prior running attempts/invocations as interrupted due to lost owner. An active owner would have prevented lock acquisition. Reset tasks to pending for reconciliation, preserving earlier result candidates and attempt history.
4. Walk readiness in dependency order. For a retention candidate, hash current inputs in place with type/metadata checks, rebuild the effective key, and select the most recent prior succeeded/cached attempt for this task with that key. Verify its whole published output set against artifact rows, then recheck current input membership/hashes and environment before accepting retention. If all match, re-establish succeeded/cached without creating an attempt or staging inputs, since no child executes; record the source attempt in this invocation. A mismatch makes the task pending for new work.
5. Otherwise, allocate a new attempt, stage fresh copies, and compute its key again from those exact bytes. With cache enabled, perform normal cache lookup and full restoration under the new effective key. If unavailable, execute within that allocated attempt. With cache disabled, never examine cache: retention was decided in step 4, and all remaining work executes within the new attempt. Prior incomplete/failed attempts are never retained from leftover workspace outputs alone.
6. Reconcile dependents only after prerequisites have a verified result for this invocation. Continue independent branches on failure. Finalize run/invocation and reconstruct the manifest from actual records.

Changed external data/script bytes or glob membership do not constitute a workflow-structure edit. They change effective keys during step 4. Rerun/restore the affected producer before deciding descendant reuse; identical new producer outputs can leave descendants reusable. Changed environment likewise changes keys while preserving the original workflow contract. Old output artifacts are not deleted just because a task becomes pending; their presence alone grants no readiness.

Failed and interrupted runs can be resumed with the same rules. A completed run is revalidated and may become failed/interrupted on the new invocation; earlier invocation outcomes remain recorded. No implicit retry loop exists. An unsupported state schema and unknown run ID are explicit errors. An old running row with complete cache content becomes a new restoration attempt; without a valid cache it executes again. Partial workspace restoration is repaired as a whole set. Old live children remain in their earlier attempt directories and are not adopted or signaled by stored PID.

Recovery restarts task commands from the beginning. It promises neither instruction-pointer continuation nor exactly-once execution or exactly-once external side effects.

## L. Manifests and provenance

Manifest schema version 1 is a JSON projection of the run, all invocations, all attempts, and verified artifact records. Include run ID/outcome/reason; normalized workflow/hash; Git revision and dirty flag per invocation (both null outside Git); Python/platform/dependency/lock identity; logical commands and keys; input, configuration, and output hashes; attempt operation, outcomes, UTC timing/duration and exit codes; relative logs; and executed/cache-restored/retained resolutions with source-attempt references.

Seed/config identity is represented by logical arguments and declared configuration path/hash records. The example additionally writes seed and parameters in its generated artifacts. The runner need not parse arbitrary task JSON to infer a seed. Git provenance is captured from the workspace's containing repository at invocation start; the dirty flag uses ordinary Git status honoring ignore rules, without hiding tracked changes; dirty state is an observation, not an archived copy of uncommitted changes. Never make Git revision or dirty status part of a task content key. Store no Git remote/account or absolute executable/workspace path in the manifest.

On orderly success, failure, or interruption, commit the available DB outcome first, write a unique temporary JSON file beside `manifest.json`, close it, then replace the destination atomically. Include the latest invocation number so an older manifest is identifiable. If manifest publication fails, report code 3; the DB remains authoritative and its execution outcome need not be falsified. A crash can leave no manifest or an older invocation's manifest. `status` reads SQLite; successful resume reconstructs the full accurate history and replaces JSON. No separate export subsystem is planned.

The deterministic example should reproduce artifact hashes under its declared input/environment contract. The runner cannot promise identical results for arbitrary commands or every platform, account for unknown external services, or reconstruct uncommitted source bytes from a dirty flag.

## M. Planned synthetic example

The P07 DAG is `generate -> simulate_a / simulate_b / simulate_c -> summarize`. It uses only standard-library numeric code and JSON; no private data, network calls, or large scientific dependency. The [example guide](../examples/simulation/README.md) gives its executable contract.

- `generate.py` reads `config/base.json` containing seed, sample count, and steps. Using a local `random.Random(seed)`, it writes integer initial states and the seed/size parameters to `out/input.json`.
- `simulate.py` reads that input and a branch's `config/a.json`, `config/b.json`, or `config/c.json`. For every initial value, iterate the recurrence `x = (multiplier * x + increment) % modulus` for the configured number of steps, producing a deterministic numeric simulation checksum and aggregate. Each branch writes its own `out/a.json`, `out/b.json`, or `out/c.json`, including branch parameters and aggregate values.
- `summarize.py` consumes all three branch outputs and writes `out/summary.json` containing per-branch aggregates, parameters, and a combined integer total. Serialize deterministic JSON with sorted keys, stable separators, and one trailing newline; omit times, IDs, and host-specific metadata from artifact contents.
- Every script and config is declared as an input of its user. Every command supplies explicit input/config/output arguments. Simulations depend on generate; summarize depends on all three simulations.
- Scale `sample_count * steps` while holding branch parameters fixed for computation comparisons. Tiny fixtures serve correctness tests, larger measured sizes serve benchmarks.
- The change demonstration increases branch b's `increment` by one. Its recorded parameter ensures different output bytes even if a checksum collides; summary includes those parameters so its bytes also change. Generate, a, and c retain their effective keys. Test the numeric fixture also changes b's aggregate so the demonstration reflects changed computation.

No size, speedup, hash, or demonstration result is claimed in P01.

## N. Verification and measurement plan

Use isolated temporary workspaces/caches, synchronized fixtures, timestamped events/barriers, and generous safety timeouts. Assert behavior and artifacts rather than relying only on elapsed-time inequalities. Fixtures must reap/clean their own children even when assertions fail. All rows below are planned gates, not passed tests.

| Contract | Phase | Proposed test/demonstration | Acceptance evidence |
|---|---|---|---|
| Schema, DAG, paths | P03 | Invalid types/keys, duplicate keys, cycles, escapes, symlink components, aliases/prefixes, unknown owners | Rejection identifies task/path; zero child launches |
| Generated inputs/globs | P03/P04 | Absent upstream output; matching unordered producer; empty/overlapping globs | Correct deferral; producer-before-consumer; invalid ambiguity rejected |
| Ordering/concurrency | P04 | Chain, diamond, barrier-coordinated independent children at several worker counts | Event trace proves dependency completion and active-child bound |
| Failures/logs | P04 | Launch failure, nonzero exit after partial output, missing/special output, noisy streams, spaces/empty argv | Failed/blocked states, independent completion, separate complete logs |
| Cancellation | P04/P06 | SIGINT/SIGTERM while several owned groups are active, including TERM-resistant child | Bounded escalation, direct children reaped, accurate partial records; uncertain launch interval reported honestly |
| Identity/invalidation | P05 | Change command, script, data, glob membership, environment; touch timestamps/unrelated file | Changed relevant keys; stable irrelevant keys; identical producer bytes permit descendant reuse |
| Warm/restore | P05 | Unchanged full run; delete published outputs | Zero child launches; matching restored hashes |
| Corruption/incomplete cache | P05 | Bad hash/metadata, missing artifact, symlink, incomplete temporary entry; permission failure | Miss and quarantine for corruption; replacement succeeds; I/O error remains error |
| Input mutation | P04/P05 | Synchronize source/staged input change or glob addition while child waits | Publication rejected; no successful result or completed cache entry |
| Ownership | P04/P06 | Competing invocation and child descriptor inheritance | Second owner rejected; dead parent releases lock; child does not retain it |
| Resume both cache modes | P06 | Valid completed output, deleted output, changed data, changed contract, failed/completed run | Correct retained/restore/execute counts and preserved attempt history |
| Late fixture writes | P06 | Kill parent, resume with a new attempt, then release old writer | New final hashes remain correct; explicit cleanup and orphan boundary reported |
| Publication boundaries | P05/P06 | Inject failure before launch, during child, after staged outputs, between output replacements, after cache rename, before DB commit, during restoration | No premature dependent start; new verified restore/execute; stale running never trusted |
| Provenance | P07 | Seeded DAG cold/warm/changed b, dirty tree, outside Git, missing manifest | Records match events/hashes and reuse; null Git outside repo; resume rebuilds JSON |
| Complete adversarial pass | P08 | Exercise all CLI failure/cache/recovery paths on both OSes | Commands, revision, real results, disclosed skips; no weakened assertions |
| Clean distribution | P10 | Build with pinned tools; install wheel/sdist in empty environments outside source; copy repository example | Help, validation, cold/warm and resume pass; installed lock-unavailable behavior verified |
| Hosted CI | P10 | Linux/macOS Python 3.12 lint, formatting, tests, builds | Hosted jobs green on the intended commit; skips and failures disclosed |

P09 benchmark protocol:

1. Run 24 independent fixed-duration waiting tasks at workers 1, 2, 4, and 8 with `--no-cache`. Label these scheduling measurements. Use one unreported warm-up and at least five measured repetitions per setting, rotating/randomizing setting order. Measure end-to-end CLI wall time with a monotonic clock. Report median/min/max, throughput `24 / wall_time`, and speedup `median_time_1 / median_time_N`.
2. Separately compare the real integer simulation DAG at workers 1 and 4, cache disabled, same fixed size/seed/configuration, one warm-up and at least five measured repetitions per setting. Validate identical outputs. Report slowdowns or negligible improvement honestly.
3. For at least five paired repetitions in fresh isolated application workspaces/caches, run cold, unchanged warm, then changed branch b with fixed worker count/size. Record times, executed tasks, cache hits/misses, per-task decisions, and hashes. Warm time reduction is `100 * (median_cold - median_warm) / median_cold`; cache-hit rate is hits divided by cache-eligible lookups examined, excluding retained/blocked tasks; use null when the denominator is zero. Application-cold does not imply cleared OS/hardware caches.
4. Synchronize interruption after a known completion boundary while another child is running. Measure at multiple boundaries in both cache modes, at least five measured repetitions per selected comparison. Record termination method, old-child cleanup, completed-before counts, retained/restored/reexecuted counts, and hashes against an uninterrupted reference. Correctness is distinct from restart speed.

Retain small raw JSON/CSV, benchmark harness version, commands, seeds/parameters, worker/cache settings, measured revision/dirty flag, Python/dependency identity, OS/architecture, CPU/core count, and memory if available. Use null plus an explanation for unavailable metadata. Keep power/workload/background activity conditions stable and report them. Generate tables from raw data and independently recompute arithmetic. Disclose failed/noisy runs and predeclared exclusion rules. Do not invent numbers, promise a minimum speedup, or use noisy hosted CI timings as machine-to-machine performance evidence.

## O. Distribution and completion

Ship an installable CLI, wheel, source distribution, repository examples, and a reviewed GitHub v0.1.0 release. Examples remain repository-only; users copy `examples/simulation` from the matching source revision into a writable directory when using an installed wheel. Test the wheel and sdist outside the source import path. Do not imply examples automatically ship as package data.

P10 CI targets Linux/macOS Python 3.12 and runs Ruff checks/format checks, unit/integration tests, and package builds with bounded job timeouts and minimum permissions. Select supported action revisions from official sources in that phase. Hosted success must be checked against the intended commit; local checks alone do not close the CI gate.

Completion requires tested advertised behavior, clean installation, hosted CI evidence, real benchmark data and derived summaries, an executable factual README/demo, and no unresolved defect in an advertised feature. Review and authorization precede commits/pushes and release/public-visibility operations. P01 acceptance authorizes none of those publication actions by itself.

## P. P01 scenario review

These are design traces, not executed application tests. Each uses the same staged-input key, private child path, runner-only publication, and DB-before-readiness rule above.

| Scenario | Intended trace and supported result |
|---|---|
| 1. First successful run | Root snapshot/key, child in fresh work directory, frozen outputs -> workspace -> complete cache -> DB success; then dependencies become eligible. |
| 2. Unchanged second run | New run; recreate current snapshots/keys in dependency order, verify/restore every cache entry, commit cached results; zero children. |
| 3. Deleted outputs, valid cache | Producer restores missing whole set before consumer snapshot; consumers restore too; all hashes verified. |
| 4. Changed script, same command | Script is a declared snapshot input; key changes; execute or find a matching new-key cache, then evaluate consumers by output bytes. |
| 5. Changed b input | b config hash changes; b output and summary input change by construction; a/c/generate stay reusable. |
| 6. Producer reruns, same bytes | Producer result has a new input key; dependency identity contains output hashes, not that producer key; consumer reuse can remain valid. |
| 7. Failure after partial output | Child's partial file stays in old work directory; attempt fails, no cache/success, descendants blocked; unrelated work continues. |
| 8. Corrupt cache artifact | Full verification rejects entry, quarantine removes corrupt final name, execute and store replacement; genuine I/O errors abort. |
| 9. Ctrl-C with active tasks | Stop scheduling, terminate owned groups, grace/escalate/reap, record interruption; committed work remains; resume revalidates it. |
| 10. Parent death with old child | Stale running attempt is incomplete. New owner uses different work path. Old relative writes affect only old attempt; tests account for cleanup/resource overlap. |
| 11. Cache rename before DB crash | Preserve interrupted old attempt; new-key validation can restore complete cache in a new attempt; no inferred old success. |
| 12. No-cache resume, valid outputs | Recompute current snapshot/key and verify prior succeeded output hashes; retain without child or cache access. Invalid/missing output reexecutes. |
| 13. Incompatible workflow edit | Compare normalized contract before reconciliation; reject with code 2 and require new run. No silent history rewrite. |
| 14. Different caller directory | Run/validate resolve workflow then use its directory; status/resume require entering that workspace, never search automatically. |
| 15. Installed wheel outside source | Same runtime behavior; copied repository example supplies scripts/configs; null lock digest plus actual installed distributions; Git fields null outside Git. |

All fifteen traces were reviewed for key inputs, child paths, publication ownership, persistence, readiness, reuse, and guarantee limits during P01. Behavioral evidence remains due in the phases above.
