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

On the initial Codex macOS checkout, files inside the ignored project `.venv` received the macOS `hidden` flag. Python skips a hidden editable-install `.pth` file, so the install could report success while `runner` failed to import `repro_runner`. A fresh temporary macOS environment installed and imported the package normally without a link. If this specific issue recurs in a project-local `.venv`, confirm it with `ls -lO .venv/lib/python3.12/site-packages/*.pth` and `.venv/bin/python -v -c 'import repro_runner'`. Only when Python reports `Skipping hidden .pth file`, run this from the repository root:

```sh
ln -s ../../../../src/repro_runner .venv/lib/python3.12/site-packages/repro_runner
.venv/bin/runner --help
```

The link stays inside the ignored `.venv` and points to the editable source. Do not add it when the ordinary editable install already works.

Check the current scaffold:

```sh
.venv/bin/runner --help
.venv/bin/runner validate --help
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

The registered workflow commands currently return an explicit unavailable response. See the [design](design.md) for their intended behavior and [progress](progress.md) for phase evidence.

Regenerate the macOS lock only when intentionally reviewing dependency changes. In a Python 3.12 environment with pip-tools installed, run:

```sh
.venv/bin/python -m piptools compile --all-build-deps --extra dev --strip-extras --allow-unsafe --output-file requirements/dev-macos.lock pyproject.toml
.venv/bin/python -m pip install -r requirements/dev-macos.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e '.[dev]'
.venv/bin/python -m pip check
```

For a first lock generation, install `pip-tools` in `.venv` before compiling; later runs use its recorded pin. Re-run tests and Ruff after a refresh, review the lock diff and generated provenance, and record the resolver and Python versions. Generate the Linux lock with the same command and `requirements/dev-linux.lock` on Linux. Do not copy or rename the macOS resolution as Linux evidence. The lock includes build requirements; development package builds use `.venv/bin/python -m build --no-isolation` once distribution verification is in scope.

Locally verified in P02: macOS Python 3.12 environment, macOS lock resolution and installation, editable package/entry point, CLI help and unavailable behavior, smoke tests, and Ruff. A second fresh temporary macOS environment passed the documented install and check sequence without an import link. Linux resolution, Linux behavior, hosted CI, and clean wheel/source-distribution installation remain future gates.
