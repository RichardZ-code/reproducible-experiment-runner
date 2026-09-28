# Five-task example: verified three-run demonstration

This is a real local CLI demonstration of cold execution, unchanged cache restoration, and selective invalidation after one numeric input change. It uses a disposable copy of the [source-only example](../examples/simulation/README.md), seed 1729, and four workers. The three `run` commands each create a distinct logical run. The [README](../README.md#quick-start) explains installation first.

## Reproduce

Run this block with Bash or zsh from the repository root after installing the package in a Python 3.12 `.venv`. It uses that installed entry point and interpreter, then copies only the example's tracked workflow, scripts, and configs into a new temporary workspace. The workspace and its `.repro` state remain in the temporary directory for inspection.

```sh
(
  set -euo pipefail
  RUNNER="$PWD/.venv/bin/runner"
  PYTHON="$PWD/.venv/bin/python"
  DEMO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/repro-demo.XXXXXX")
  mkdir -p "$DEMO_DIR/config"
  cp examples/simulation/workflow.yaml examples/simulation/*.py "$DEMO_DIR/"
  cp examples/simulation/config/*.json "$DEMO_DIR/config/"
  cd "$DEMO_DIR"

  "$RUNNER" validate workflow.yaml
  "$RUNNER" run workflow.yaml --workers 4 | tee cold.txt
  COLD_ID=$(sed -n 's/^Run ID: //p' cold.txt)
  "$RUNNER" run workflow.yaml --workers 4 | tee warm.txt
  WARM_ID=$(sed -n 's/^Run ID: //p' warm.txt)
  export COLD_ID WARM_ID

  "$PYTHON" - <<'PY'
import json
from pathlib import Path

print("cold b aggregate", json.loads(Path("out/b.json").read_text())["aggregate"])
print("cold combined total", json.loads(Path("out/summary.json").read_text())["combined_total"])
PY

  "$PYTHON" - <<'PY'
import json
from pathlib import Path

path = Path("config/b.json")
config = json.loads(path.read_text())
assert config["increment"] == 5
config["increment"] = 6
path.write_text(json.dumps(config, separators=(",", ":")) + "\n")
print("Changed only config/b.json increment: 5 -> 6")
PY

  "$RUNNER" run workflow.yaml --workers 4 | tee changed.txt
  CHANGED_ID=$(sed -n 's/^Run ID: //p' changed.txt)
  export CHANGED_ID
  "$PYTHON" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(".")
ids = [os.environ[name] for name in ("COLD_ID", "WARM_ID", "CHANGED_ID")]
assert len(set(ids)) == 3
manifests = [
    json.loads((root / ".repro" / "runs" / run_id / "manifest.json").read_text())
    for run_id in ids
]
expected = [
    (5, 0, 0),
    (0, 5, 0),
    (2, 3, 0),
]
for label, manifest, (executed, cached, retained) in zip(
    ("cold", "warm", "changed"), manifests, expected
):
    resolutions = manifest["invocations"][-1]["resolutions"]
    counts = tuple(
        sum(item["disposition"] == kind for item in resolutions.values())
        for kind in ("executed", "cache_restored", "retained")
    )
    assert counts == (executed, cached, retained), (label, counts)
    print(label, manifest["run"]["id"], "executed/cached/retained", counts)

old, changed = manifests[0], manifests[2]
task_for_file = {
    "input": "generate", "a": "simulate_a", "b": "simulate_b",
    "c": "simulate_c", "summary": "summarize",
}
for name, task in task_for_file.items():
    path = root / "out" / (name + ".json")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    prior = old["tasks"][task]["attempts"][-1]["artifacts"][0]["sha256"]
    recorded = changed["tasks"][task]["attempts"][-1]["artifacts"][0]["sha256"]
    assert actual == recorded
    assert (prior != actual) == (name in {"b", "summary"})
    print(name, "changed", prior != actual, "sha256", actual)

old_b = old["tasks"]["simulate_b"]["attempts"][-1]["artifacts"][0]
old_summary = old["tasks"]["summarize"]["attempts"][-1]["artifacts"][0]
print("old b and summary hashes", old_b["sha256"], old_summary["sha256"])
print("new b aggregate", json.loads((root / "out/b.json").read_text())["aggregate"])
print("new combined total", json.loads((root / "out/summary.json").read_text())["combined_total"])
print("Git provenance", changed["invocations"][-1]["git"]["availability"])
PY
)
```

The graph is `generate → {simulate_a, simulate_b, simulate_c} → summarize`. Dependency edges permit the middle three tasks to become ready together; the four-worker setting and this fast CLI transcript alone do not prove overlapping child execution. The [P09 scheduling measurements](../benchmarks/results/mac-arm64-20260928-p09-scoped/summary.md#scheduling-benchmark-24-fixed-duration-waiting-tasks) independently observed child-lifetime overlap for a controlled waiting-task workload.

## Observed transcript

These are the actual CLI output and verification lines from this P11 run, with only the repetitive `Workflow`, `Logs`, `Duration`, and `Manifest` lines omitted from each run. All commands exited 0. Times from this short demo are not benchmark measurements.

```text
Workflow valid: 5 tasks. Input availability and contents are deferred until task readiness.
Dependency lock identity unavailable; using installed distribution inventory.
Run ID: de8ff27faaf94118824d36e19ffce136
Invocation: 1
generate: succeeded
simulate_a: succeeded
simulate_b: succeeded
simulate_c: succeeded
summarize: succeeded
Outcome: succeeded; executed=5, retained=0, cached=0, cache_misses=5, failed=0, blocked=0, interrupted=0, uncertain_launches=0
Dependency lock identity unavailable; using installed distribution inventory.
Run ID: 8f03964fa59f4b61a9e884a37b2fbc7e
Invocation: 1
generate: cached
simulate_a: cached
simulate_b: cached
simulate_c: cached
summarize: cached
Outcome: succeeded; executed=0, retained=0, cached=5, cache_misses=0, failed=0, blocked=0, interrupted=0, uncertain_launches=0
cold b aggregate 621615
cold combined total 1879288
Changed only config/b.json increment: 5 -> 6
Dependency lock identity unavailable; using installed distribution inventory.
Run ID: 16a081d6ae1a4329a74ae79abb335182
Invocation: 1
generate: cached
simulate_a: cached
simulate_b: succeeded
simulate_c: cached
summarize: succeeded
Outcome: succeeded; executed=2, retained=0, cached=3, cache_misses=2, failed=0, blocked=0, interrupted=0, uncertain_launches=0
cold de8ff27faaf94118824d36e19ffce136 executed/cached/retained (5, 0, 0)
warm 8f03964fa59f4b61a9e884a37b2fbc7e executed/cached/retained (0, 5, 0)
changed 16a081d6ae1a4329a74ae79abb335182 executed/cached/retained (2, 3, 0)
input changed False sha256 79b3d6933c2aa8f600b34725f32097125925f481f93c54a82e57f0fdd04deeee
a changed False sha256 b1a6effce39ac753bd5c71289bfb3493b08c70e3270e4b9a92a50f3d1d749966
b changed True sha256 48cb8603c594c827932730e2d9fa4d7f07594e28323c21f237d7f7844c785619
c changed False sha256 a349e90dfcbc4da252f1a26074880131e2bd3d4f4746d59cc126f783f71fb868
summary changed True sha256 febfc2a4be0ed445c403db7d07761c128f8a2ef072c6b0935de9bb63da68926b
old b and summary hashes 7973fe6ab56dc1a5eb08f4d22c0599d06320ab3e0f320e4d9a35271450d86737 b516995b00c51c0c2caccca6f7740a0c6e4d062d92e449b6051d8b37df53e0bb
new b aggregate 658934
new combined total 1916607
Git provenance outside_git
```

The cold published JSON outputs had branch b aggregate **621615** and combined total **1879288**. After the numeric change they are **658934** and **1916607**. Hashes for `input`, `a`, and `c` stayed equal. The printed `out/b.json` hash was independently computed from its bytes and matched the changed run's `tasks.simulate_b.attempts[0].artifacts[0].sha256`; the script checks all five. Each manifest records its own run ID and selected attempts.

## Source and environment

This run used a fresh Python 3.12.14 virtual environment installed by normal `python -m pip install .` from an authenticated GitHub Desktop clone of repository commit `917382552cdce8d665945fccd14d14cee3fcdf9d`, with this P11 README and demo overlaid. The installed module resolved under that temporary environment's `site-packages`, not the original development checkout. The copied example workspace was outside Git, so the manifest truthfully records `git.availability: outside_git` and a null commit. The source commit here identifies the archived runner source, not provenance collected inside the workflow. The normal source install has no development lock, so the CLI reported that it used installed distribution inventory for environment identity.

The temporary directory's private absolute path is omitted from this shareable transcript; no CLI results or hashes were changed. A direct HTTPS clone in the restricted Codex shell reached GitHub but had no terminal credential route. The existing authenticated GitHub Desktop account cloned the exact private repository into a new temporary directory. The normal installation, README quick start, and this demo then succeeded from that clone. No account or Git authentication settings were changed.
