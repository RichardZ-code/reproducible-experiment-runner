# Integer simulation example

This five-task workflow compares three parameter sets applied to the same seeded initial states:

```text
generate -> simulate_a / simulate_b / simulate_c -> summarize
```

`generate.py` uses a local `random.Random(1729)` to draw `sample_count` integers in `[0, 9999]`. Its command states the seed, and `config/base.json` must contain the same seed. For each initial state, a simulation applies `x = (multiplier * x + increment) % modulus` exactly `steps` times. Each branch reports the sum of final states, the sum of `(index + 1) * final_state`, and the minimum and maximum. `summarize.py` reads a, b, c in that order and adds their aggregates and weighted checksums. Each branch has its own config. The default is 128 samples and 120 steps, for 15,360 recurrence updates per branch. Increase `sample_count` or `steps` in `config/base.json` to increase real computation, subject to the scripts' 20 million update bound per branch.

From the repository root, with the documented editable installation:

```sh
.venv/bin/runner validate examples/simulation/workflow.yaml
.venv/bin/runner run examples/simulation/workflow.yaml --workers 4
.venv/bin/runner run examples/simulation/workflow.yaml --workers 4
.venv/bin/runner run examples/simulation/workflow.yaml --workers 4 --no-cache
```

The workflow directory is the workspace. The artifacts are `examples/simulation/out/input.json`, `out/a.json`, `out/b.json`, `out/c.json`, and `out/summary.json`. The CLI prints a run ID and the relative manifest path `.repro/runs/<run-id>/manifest.json`; attempt logs are under the same run's `attempts/<task-id>/<attempt-number>/` directory. For example, from the workflow directory:

```sh
../../.venv/bin/runner status RUN_ID
../../.venv/bin/runner resume RUN_ID
../../.venv/bin/python -m json.tool .repro/runs/RUN_ID/manifest.json
```

Replace `RUN_ID` with the printed value. `status` reads stored facts and does not rewrite JSON. `resume` checks the current workflow and inputs, records a new invocation, and rewrites the manifest from committed state. To see selective cache reuse, change `increment` in `config/b.json` from 5 to 6 and run the same workflow again. `simulate_b` and `summarize` execute; matching generate, a, and c results restore from cache. A new run with `--no-cache` executes all five tasks without cache access. A no-cache resume can still retain complete verified outputs.

The manifest's `tasks.generate.attempts[0].command` contains the seed; `tasks.simulate_b.attempts[0].inputs` contains the recorded `config/b.json` hash. `invocations[].git` records an observation at invocation start, with an explicit unavailable value outside Git. A cache restoration records verification of cached bytes, but the cache format does not identify its original producer's Git revision. A dirty flag records that source differs from the commit, not the changed bytes. The artifact JSON is deterministic for the declared inputs and recorded environment; manifest IDs, timings, and reuse history legitimately vary. The example scripts and configs are repository-only and were copied into a separate workspace for the P08 clean-wheel smoke check; they are not shipped in the wheel.
