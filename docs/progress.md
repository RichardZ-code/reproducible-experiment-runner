# Phase progress

P01 documentation is prepared for review. The runner is not implemented. Prepared work, passed checks, user acceptance, and publication are distinct gates.

| Phase | Status | Acceptance gate | Evidence | Review/publication |
|---|---|---|---|---|
| P00: Setup | Completed per supplied report, with GitHub Desktop fallback | Correct checkout, Python and folder access, identified repository route | Supplied P00 report: clean checkout, compatible bundled Python 3.12, stdlib/SQLite checks, Desktop account and repository verified | Report carried forward; no push demonstrated |
| P01: Design and rules | Prepared for review | Consistent design, working rules, scope, scenario review, clean documentation checks | Five documents read back; 15 scenario traces reviewed; inline documentation checks passed; tracked diff whitespace check passed | Awaiting user review; unstaged, uncommitted, not pushed |
| P02: Project scaffold | Not started | Project environment, installable package, honest CLI help, smoke tests | Pending | Not reviewed or published |
| P03: Workflow validation | Not started | Schema/DAG/path rejection before child execution | Pending | Not reviewed or published |
| P04: Concurrent execution | Not started | Ordering/concurrency, isolation, logs, failure propagation and cancellation | Pending | Not reviewed or published |
| P05: Content cache | Not started | Stable identity, verified storage/restore, invalidation and corruption checks | Pending | Not reviewed or published |
| P06: State and recovery | Not started | Durable attempts, ownership tests, safe resume with/without cache | Pending | Not reviewed or published |
| P07: Example and manifests | Not started | Seeded DAG and accurate versioned provenance | Pending | Not reviewed or published |
| P08: Test the complete runner | Not started | Adversarial behavior matrix, demonstrations and scoped fixes | Pending | Not reviewed or published |
| P09: Run real benchmarks | Not started | Reproducible harness, at least five repetitions, raw data and recomputed summaries | Pending | Not reviewed or published |
| P10: GitHub Actions and installation | Not started | Linux/macOS hosted CI plus clean wheel/sdist installation | Pending | Not reviewed or published |
| P11: README and demo | Not started | Tested quick start, real demo and evidence-backed claims | Pending | Not reviewed or published |
| P12: Final audit | Not started | Read-only audit and resolved advertised-feature defects | Pending | Not reviewed or published |
| P13: Release | Not started | Reviewed artifacts/revision, CI, authorized release and public access checks | Pending | Not reviewed or published |

## Setup carried forward

Python 3.12 is the baseline. P00 verified a compatible bundled interpreter, required standard-library imports, SQLite, pip, and ensurepip. The older system Python is not the baseline. No project environment or package exists yet. Interpreter location is session context, not portable project configuration.

GitHub Desktop remains the reviewed commit/push route. Direct CLI push is not configured, and successful Desktop push has not yet been demonstrated. These are access limitations, not evidence of an application defect. No authentication/install work is part of P01.

## Design clarifications to review

- Workflow-directory workspace; current-directory `.repro` lookup for status/resume.
- Strict ASCII relative file paths, portable alias checks, and limited basename globs.
- Private input copies, reserved Python executable resolution, and runner-only output publication.
- Content keys include script bytes, direct dependency output hashes, and an explicit conservative environment record; installed wheels can have a null source-lock digest.
- Filesystem/cache/SQLite publication is ordered, not jointly atomic; completed work needs current verification before readiness.
- Resume inherits cache mode, preserves history, and can retain verified outputs with cache disabled.
- Ordinary cancellation owns live child groups; abrupt-death orphan work and arbitrary external side effects have explicit limits.

See [decisions](design-decisions.md) and the [acceptance matrix](design.md#n-verification-and-measurement-plan). No material design blocker has been identified. No application tests or benchmarks have run.

## P01 evidence

- Read back the new repository instructions and applied them during the remaining documentation work.
- Reviewed all 15 scenarios in [the design review](design.md#p-p01-scenario-review). Clarified retention versus attempt allocation, uncertain child-launch records, final input checks, temporary-file exclusion, and SQLite journal recovery.
- Inline Python documentation check: exit 0. All five files checked for balanced fences, whitespace, final newlines, personal paths/account details, opaque citation markers, and em dashes; 46 local links/anchors, all 14 phase labels, six CLI forms, and 15 scenario rows passed.
- `git diff --check`: exit 0 for tracked changes. New untracked Markdown files were inspected and checked separately; they are not covered by ordinary `git diff`.
- Dependency-resolution commands were checked against official pip-tools documentation only. No dependency installation, application tests, benchmarks, or CI execution occurred.

## Next phase

Review and accept P01 before requesting P02. P02 creates packaging, the project-local environment, dependency constraints, the CLI surface/help, and smoke tests. It does not authorize implementing the later execution/cache/recovery engine or publishing changes. Verify the session's compatible interpreter again before environment creation; never substitute the older system Python silently.
