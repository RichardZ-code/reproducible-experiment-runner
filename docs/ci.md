# P10 continuous verification

The final `.github/workflows/ci.yml` runs on pushes to `main`, pull requests targeting `main`, and explicit manual dispatch. Its required matrix is `ubuntu-24.04` x64 and `macos-15` arm64 with Python 3.12. Each job has a 20-minute limit, read-only repository permission, no persisted checkout credentials, and independent failure reporting. A missing Linux lock fails setup. The matrix and artifact-verified Linux lock were published by the user at `5869b55`.

The actions are pinned to the following verified official release commits:

| Action | Release | Commit |
|---|---|---|
| `actions/checkout` | `v7.0.1` | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/setup-python` | `v7.0.0` | `5fda3b95a4ea91299a34e894583c3862153e4b97` |
| `actions/upload-artifact` | `v7.0.1` | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |

The tags and peeled references were checked against the three official GitHub repositories. Runner labels and architectures were checked against [GitHub-hosted runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). Workflow permissions and expression behavior follow [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) and [secure use](https://docs.github.com/en/actions/reference/security/secure-use). Hosted execution has begun, with a test failure described below.

Each final job creates its own checkout `.venv`, installs the platform lock, then installs the local package editable with the already installed build backend (`--no-build-isolation --no-deps`). It runs `pip check`, Ruff lint and format, the complete default-order pytest suite, and the reduced compute benchmark harness smoke. The smoke checks harness correctness and classification, with no timing threshold or formal P09 measurement comparison.

The job builds from the checked-out source with `python -m build --no-isolation`, using the prepared locked build tools. Build's normal source-archive-then-wheel sequence is retained. Two separate temporary consumer runtimes then install the exact new wheel and source archive with normal pip dependency handling. The source archive runtime uses pip's ordinary isolated build. The helper checks installed import origin, distribution metadata, CLI entry point, declared dependencies, pip consistency, and a copied repository-only five-task example from outside Git. It checks cold execution, warm restoration without task launches, output hashes, SQLite and manifest history, status, completed resume, manifest reconstruction, and no-cache run/resume. Consumer dependency versions can resolve independently of the development lock and are recorded by the helper.

The wheel contains runtime modules, metadata, entry point, and license. The example remains in Git source and is copied into each temporary probe workspace. It is not promised as wheel content. The current source archive includes the package source, build metadata, README, license, and automatically selected tests; it does not include the repository-only example or installation checker. The archive is verified as an install source, not as a standalone test checkout. Neither distribution should include `.venv`, `.repro`, generated example outputs, or benchmark workspaces.

Each job uploads only its environment record, JUnit result, build log and SHA-256 list, wheel and source archive, small clean-install logs/reports, and reduced benchmark records. Names include platform, architecture, Python, run ID, and attempt. Retention is seven days. A diagnostic upload may occur after a failure, but the failed required step still fails the job. Open the intended Actions run and the platform job to inspect the failed step; download that job's named artifact for bounded logs and distribution checksums. A green generator is not a green final matrix.

## Linux lock bootstrap

No approved Linux executor was available in the local checkout environment. The temporary `generate-linux-lock.yml` workflow was published and manually dispatched by the user. [Run 36393182066](https://github.com/RichardZ-code/reproducible-experiment-runner/actions/runs/36393182066) succeeded for source commit `1984b08f382890678c8f3f25a71a62308a62f181` on Linux x86_64 with Python 3.12.14 and pip-tools 7.6.1. Its artifact ZIP SHA-256 was `5638af4f81ce7be57c47c0d4f1ac29597610f80d292d3c2c6265de0e06b5b5a6`; the candidate lock SHA-256 was `1d7bd350d65ade6846486df46fef7a763a248e67f42258817bed009b9350cc86`. The ZIP contained only the candidate and provenance. The provenance source commit and pyproject SHA-256 matched the local checkout, and all 20 resolved pins matched the macOS lock. The generated header contains `--no-index`; the workflow invocation and separate provenance command do not. The generated file was preserved without editing. The user subsequently published that lock and deleted the temporary generator in commit `5869b55`.

The bootstrap run proves generation on Linux, not installation or tests from the Linux lock. The final matrix uses that checked-in lock and must pass both platform jobs on the intended reviewed commit.

## First final-matrix run

[Push run 36394330222](https://github.com/RichardZ-code/reproducible-experiment-runner/actions/runs/36394330222) tested clean commit `5869b55cc6442ac85cb8e37458a8cb129ca4f600`. Both jobs installed their locks and passed Ruff, then failed the same `test_help_exposes_command_surface` assertion during the complete test suite. Each JUnit artifact recorded 253 tests, one failure, zero errors, and zero skips. macOS 15 arm64 used Python 3.12.10 and SQLite 3.49.1; Ubuntu 24.04 x86_64 used Python 3.12.14 and SQLite 3.45.1. Neither job reached distribution builds, clean installs, or benchmark smoke, so no hosted distribution hashes are available.

Rich inserted ANSI color sequences inside `--help` in the CI output. The test searched the raw output for the contiguous flag text. A local CI-style color reproduction failed identically. The prepared fix removes ANSI sequences from test output before checking the same required command and option names. The focused test passed after the fix, followed by 253/253 default-order tests in 30.81 seconds under `env -u NO_COLOR TERM=xterm-256color FORCE_COLOR=1 GITHUB_ACTIONS=true .venv/bin/python -m pytest -q`; repository-wide Ruff lint and format checks passed. The fix and these evidence notes are local only. P10 acceptance requires a new hosted run with both jobs green on the reviewed corrective commit.
