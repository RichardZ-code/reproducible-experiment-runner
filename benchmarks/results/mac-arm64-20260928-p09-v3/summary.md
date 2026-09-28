# P09 measured results

Raw source: `samples.jsonl`. Times are external CLI wall seconds; all listed samples passed state, manifest, task, and hash checks.

Protocol 1; source fingerprint `7ea2bd0c2cb1dcfd7a12da86969b48797bed4b2fb794a43d892ee554d77a11bb`; Git revision `7f50175bd7056a5fa6780c0eb91487badad33da1`; dirty=True.

## Scheduling benchmark: 24 fixed-duration waiting tasks

Requested wait: 0.25 s per task. Throughput is 24 / median wall seconds. Observed peak uses task-log start/end events.

| Workers | Valid n | Median s | Min s | Max s | Tasks/s | Speedup vs 1 | Peak active range | Verified |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 5 | 7.7141 | 7.5081 | 7.7532 | 3.1112 | 1.0000 | 1-1 | 24 launches/results; hashes match |
| 2 | 5 | 4.0561 | 3.9342 | 4.1100 | 5.9171 | 1.9019 | 2-2 | 24 launches/results; hashes match |
| 4 | 5 | 2.1314 | 2.0685 | 2.1411 | 11.2602 | 3.6193 | 4-4 | 24 launches/results; hashes match |
| 8 | 5 | 1.2734 | 1.2636 | 1.3566 | 18.8477 | 6.0580 | 8-8 | 24 launches/results; hashes match |

## Five-task seeded integer computation

Seed 1729; sample_count=5000, steps=1000; 5000000 recurrence updates per branch. No application cache.

| Workers | Valid n | Median s | Min s | Max s | 1-worker / setting ratio | Verified |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 5 | 0.7854 | 0.7763 | 0.8282 | 1.0000 | 5 launches/results; hashes equal |
| 4 | 5 | 0.4055 | 0.4015 | 0.4504 | 1.9366 | 5 launches/results; hashes equal |

## Paired cache runs

Cold means empty application cache in a new workspace; operating-system caches were not cleared. Each pair then runs unchanged warm and branch-b increment 5 to 6.

| Stage | Paired n | Median s | Min s | Max s | Executed range | Restored range | Hits/eligible | Verified |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| cold | 5 | 0.4483 | 0.4044 | 0.4567 | 5-5 | 0-0 | 0/25 (0.0000%) | hashes/summary match |
| warm | 5 | 0.1828 | 0.1821 | 0.1871 | 0-0 | 5-5 | 25/25 (100.0000%) | hashes/summary match |
| changed | 5 | 0.3999 | 0.3925 | 0.4032 | 2-2 | 3-3 | 15/25 (60.0000%) | hashes/summary match |

Warm reduction from medians: 100 × (0.4483 - 0.1828) / 0.4483 = 59.2241%. Pair reductions, computed from each pair's unrounded duration:

| Pair | Cold s | Warm s | Pair reduction % |
| --- | ---: | ---: | ---: |
| C-pair-1 | 0.4044 | 0.1828 | 54.7936 |
| C-pair-2 | 0.4557 | 0.1871 | 58.9318 |
| C-pair-3 | 0.4483 | 0.1821 | 59.3785 |
| C-pair-4 | 0.4567 | 0.1823 | 60.0775 |
| C-pair-5 | 0.4483 | 0.1856 | 58.5931 |

Changed-input task dispositions (identical in each verified pair):

| Task | Disposition |
| --- | --- |
| generate | cache_restored |
| simulate_a | cache_restored |
| simulate_b | executed |
| simulate_c | cache_restored |
| summarize | executed |

## Controlled interruption and resume

Only installed-CLI resume is timed. The initial after-publication run uses a benchmark wrapper; active-child uses the installed CLI. Resume durations describe remaining work after a verified boundary, not full-run speedup.

| Boundary | Stop | Cache | Valid n | Committed before | Retained | Cached | Executed | Unknown launches | Resume median/min/max s | Verified |
| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| active_child | SIGINT | cache | 5 | 1-1 | 1-1 | 0-0 | 2-2 | 0-0 | 0.1846 / 0.1823 / 0.1872 | final hashes match reference |
| active_child | SIGINT | no-cache | 5 | 1-1 | 1-1 | 0-0 | 2-2 | 0-0 | 0.1844 / 0.1823 / 0.1871 | final hashes match reference |
| after_publication | SIGKILL | cache | 5 | 1-1 | 1-1 | 1-1 | 1-1 | 0-0 | 0.1824 / 0.1793 / 0.1861 | final hashes match reference |
| after_publication | SIGKILL | no-cache | 5 | 1-1 | 1-1 | 0-0 | 2-2 | 0-0 | 0.1833 / 0.1811 / 0.1870 | final hashes match reference |

Five valid measured trials per condition are required. Warmups, pilots, references, and smoke runs do not enter these tables. Valid slow observations are retained. No p95 or statistical confidence claim is made.
