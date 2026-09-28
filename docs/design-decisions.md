# Design decisions

Status: proposed in P01, awaiting user review. These records explain the [implementation contract](design.md); they do not claim implemented behavior.

## A. Requirements carried forward from the guide

| Decision | Why it fits | Main tradeoff or limitation | Contract |
|---|---|---|---|
| Local CLI with Python/asyncio and SQLite | Small dependency-aware subprocess tool with inspectable local records | One local workspace; no distributed coordination | [Scope](design.md#a-purpose-and-scope), [architecture](design.md#b-architecture-and-ownership) |
| One OS-backed workspace owner | Prevent interleaved state/output publication; dead owner releases lock | Other execution invocations fail rather than queue | [Ownership](design.md#k-sqlite-ownership-and-resume) |
| Argument lists and no implicit shell | Preserve argument boundaries and avoid quoting/expansion ambiguity | Scripts must accept explicit paths; commands remain trusted | [Attempts](design.md#g-attempts-and-command-execution) |
| Regular files and explicit ownership/dependencies | Verify and restore bounded artifacts; prevent unordered read/write races | No arbitrary output directories or inferred dependencies | [Schema](design.md#c-workflow-schema-and-validation), [paths](design.md#d-workspace-and-paths) |
| Private attempts with ordinary input copies | Old partial/late writes do not become new shared results | Extra disk I/O; no OS sandbox or arbitrary external-side-effect protection | [Attempts](design.md#g-attempts-and-command-execution), [interruption](design.md#h-logs-failures-and-interruption) |
| Content-based dependency invalidation | Reuse stays correct when a producer recomputes identical bytes | Undeclared inputs cannot be inferred | [Identity](design.md#i-cache-identity-and-environment) |
| Explicit environment and script identity | Unchanged command text is insufficient to identify execution | Fingerprint is conservative and not hermetic | [Identity](design.md#i-cache-identity-and-environment) |
| Verified workspace and cache publication before DB success | Descendants must see complete verified artifacts | Filesystem and SQLite have separate crash boundaries | [Publication](design.md#j-cache-restoration-and-publication) |
| Continue independent branches; block failed descendants | Preserve useful independent results | Workflow can keep using resources after one branch fails | [Outcomes](design.md#f-scheduling-and-outcomes) |
| Original workflow identity and no-cache recovery | Resume preserves meaning and does not depend solely on cache | Changed workflow requires a new run; interrupted work starts over | [Resume](design.md#k-sqlite-ownership-and-resume) |
| Wheel/sdist and GitHub distribution | Enough to install and review a small CLI | Repository examples need an explicit copy step; PyPI/Docker deferred | [Distribution](design.md#o-distribution-and-completion) |

Source: build guide P01 and pages 10-17, 20-23, 28; checklist P00-P13 sequence. The older idea's command-string examples are superseded by the guide's argument-list contract. No original essay, private paths, or chat citation identifiers are reproduced here.

## B. P01 implementation clarifications

These are proposed choices made in P01, not quotations from the guide.

| Decision | Rationale | Main tradeoff or limitation | Contract |
|---|---|---|---|
| Workflow-directory workspace; status/resume use current directory only | Makes location predictable without global discovery or another CLI flag | User must enter the workflow directory first | [CLI](design.md#e-cli-contract) |
| Default four workers; explicit exit-code categories | Small stable interface and measurable overrides | No automatic CPU sizing; infrastructure failure differs from task failure | [CLI](design.md#e-cli-contract) |
| Non-empty outputs, optional empty inputs/deps; reject YAML aliases/merges | Small schema with artifact-based success and unambiguous parsing | Output-free tasks and YAML shorthand unsupported | [Schema](design.md#c-workflow-schema-and-validation) |
| Final-basename `*`/`?` globs, no recursion; match declared producers before they exist | Defines ownership independently of execution order | Some useful broad patterns are intentionally rejected | [Schema](design.md#c-workflow-schema-and-validation) |
| ASCII canonical paths and portable case-folded collision checks | Avoid normalization/alias surprises on macOS | Rejects Unicode and some layouts valid on Linux | [Paths](design.md#d-workspace-and-paths) |
| Bare executable names; Python aliases map to runner interpreter | Avoid accidentally invoking the old system Python | Native PATH tools are ambient dependencies; no absolute executable declarations | [Attempts](design.md#g-attempts-and-command-execution) |
| Frozen output copies and source/snapshot recheck before publication | Key describes accepted bytes; late work-file writes cannot alter copies | Additional copying; external concurrent writers are not transaction participants | [Attempts](design.md#g-attempts-and-command-execution) |
| Full installed distribution name/version inventory plus optional source lock digest | Standard-library discovery works in editable and wheel environments | Development package changes can invalidate; wheel lock may be null; modified installed bytes are not detected | [Environment](design.md#i-cache-identity-and-environment) |
| All direct dependency output hashes enter the key, not producer keys | Precise conservative dependency rule with content-based downstream reuse | Unconsumed outputs can cause extra misses | [Identity](design.md#i-cache-identity-and-environment) |
| Quarantine corrupt entries, then miss; genuine I/O errors abort | Safe replacement and visible diagnostics | Corrupt/temp entries use disk until deliberate maintenance; no GC | [Cache](design.md#j-cache-restoration-and-publication) |
| Workspace output replacements, complete cache rename, DB commit, then readiness | Fixes one explicit order for crash tests | Mixed workspace files can exist before success; recovery must reconcile | [Publication](design.md#j-cache-restoration-and-publication) |
| Five-second TERM grace, then KILL; no stored-PID cleanup | Bounded ordinary cancellation without unsafe ownership guesses | Abrupt-death orphans may survive and consume extra resources | [Interruption](design.md#h-logs-failures-and-interruption) |
| Start ownership/isolation in P04; add durable resume in P06 | Shared-output safety is required as soon as execution exists | Some small primitives precede the phase that completes their tests | [Ownership](design.md#k-sqlite-ownership-and-resume) |
| Derived ready condition, separate invocation reuse attribution, explicit uncertain launch interval | Distinguishes fresh execution, cache restoration, and retained work without inventing crash-window counts | Summary counters need clear denominators; retained work creates no new attempt | [Outcomes](design.md#f-scheduling-and-outcomes) |
| SQLite runs/invocations/tasks/attempts/artifacts; rollback journaling | Preserve resume history and allow a simple read-only status path | More records than a single mutable task row; no automatic schema migrations | [State](design.md#k-sqlite-ownership-and-resume) |
| Resume inherits cache mode and last worker count; completed runs revalidate | One recovery path covers valid work, data changes, and previous failures | Revalidation can change the latest outcome; historical invocations remain intact | [Resume](design.md#k-sqlite-ownership-and-resume) |
| Setuptools, bounded runtime requirements, per-OS pip-tools development locks | Familiar packaging with reproducible development resolution | Pins require target-OS regeneration; builds use the pinned environment without isolation | [Packaging](design.md#a-purpose-and-scope) |
| Integer recurrence simulation and repository-only examples | Small deterministic computation with independently configurable branches | Synthetic workload does not stand in for every scientific workload | [Example](design.md#m-planned-synthetic-example), [distribution](design.md#o-distribution-and-completion) |

## P02 packaging clarification

The scaffold uses `setuptools>=77` and `wheel` as build requirements. Setuptools 77 or later supports the SPDX `license = "MIT"` and `license-files = ["LICENSE"]` metadata matching the existing license. The macOS lock includes build requirements through pip-tools' `--all-build-deps`; the editable installation uses those installed pins with `--no-build-isolation --no-deps`. This is a packaging detail within the P01 strategy, not a change to the runner's behavior. See [packaging](design.md#a-purpose-and-scope) and the [developer setup](development.md).

## C. Proposed departures and open decisions

No material departure from the guide is proposed. The refinements above choose behavior where the guide leaves details open. There are no unresolved material design decisions; the whole contract still awaits user review. In particular, review the deliberately narrow paths/globs, conservative environment invalidation, current-directory status/resume convention, and the orphan/external-writer limitations before implementation.

Dependency resolution and current supported build/CI tool revisions remain implementation-phase checks, not fabricated P01 results. If those checks expose an actual semantic conflict, stop and resolve the specific departure before changing the accepted contract.
