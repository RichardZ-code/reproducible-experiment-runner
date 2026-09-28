# P09 measured results

Raw source: `samples.jsonl`. Times are external CLI wall seconds; all listed samples passed state, manifest, task, and hash checks.

Protocol 1; source fingerprint `e153d4bdcfd881d7682cc039aa280b660671b52bca2b260e81931b8ccd57c7b8`; Git revision `7f50175bd7056a5fa6780c0eb91487badad33da1`; dirty=True.

## Scheduling benchmark: 24 fixed-duration waiting tasks

Requested wait: 0.25 s per task. Throughput is 24 / median wall seconds. Observed peak uses task-log start/end events.

| Workers | Valid n | Median s | Min s | Max s | Tasks/s | Speedup vs 1 | Peak active range | Verified |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 5 | 7.7373 | 7.6317 | 8.3961 | 3.1019 | 1.0000 | 1-1 | 24 launches/results; hashes match |
| 2 | 5 | 3.9746 | 3.9724 | 4.3652 | 6.0383 | 1.9467 | 2-2 | 24 launches/results; hashes match |
| 4 | 5 | 2.1383 | 2.0387 | 2.1748 | 11.2239 | 3.6184 | 4-4 | 24 launches/results; hashes match |
| 8 | 5 | 1.2752 | 1.2071 | 1.3537 | 18.8213 | 6.0677 | 8-8 | 24 launches/results; hashes match |

## Five-task seeded integer computation

Seed 1729; sample_count=5000, steps=1000; 5000000 recurrence updates per branch. No application cache.

| Workers | Valid n | Median s | Min s | Max s | 1-worker / setting ratio | Verified |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 5 | 0.7831 | 0.7784 | 0.7861 | 1.0000 | 5 launches/results; hashes equal |
| 4 | 5 | 0.3991 | 0.3971 | 0.4561 | 1.9621 | 5 launches/results; hashes equal |

## Paired cache runs

Cold means empty application cache in a new workspace; operating-system caches were not cleared. Each pair then runs unchanged warm and branch-b increment 5 to 6.

| Stage | Paired n | Median s | Min s | Max s | Executed range | Restored range | Hits/eligible | Verified |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| cold | 5 | 0.4490 | 0.4029 | 0.5080 | 5-5 | 0-0 | 0/25 (0.0000%) | hashes/summary match |
| warm | 5 | 0.1838 | 0.1804 | 0.1871 | 0-0 | 5-5 | 25/25 (100.0000%) | hashes/summary match |
| changed | 5 | 0.3927 | 0.3499 | 0.4025 | 2-2 | 3-3 | 15/25 (60.0000%) | hashes/summary match |

Warm reduction from medians: 100 × (0.4490 - 0.1838) / 0.4490 = 59.0603%. Pair reductions, computed from each pair's unrounded duration:

| Pair | Cold s | Warm s | Pair reduction % |
| --- | ---: | ---: | ---: |
| C-pair-1 | 0.4029 | 0.1838 | 54.3730 |
| C-pair-2 | 0.4556 | 0.1866 | 59.0463 |
| C-pair-3 | 0.4490 | 0.1838 | 59.0712 |
| C-pair-4 | 0.4032 | 0.1871 | 53.6073 |
| C-pair-5 | 0.5080 | 0.1804 | 64.4809 |

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
| active_child | SIGINT | cache | 5 | 1-1 | 1-1 | 0-0 | 2-2 | 0-0 | 0.1869 / 0.1815 / 0.2370 | final hashes match reference |
| active_child | SIGINT | no-cache | 5 | 1-1 | 1-1 | 0-0 | 2-2 | 0-0 | 0.1852 / 0.1783 / 0.1871 | final hashes match reference |
| after_publication | SIGKILL | cache | 5 | 1-1 | 1-1 | 1-1 | 1-1 | 0-0 | 0.1832 / 0.1808 / 0.1869 | final hashes match reference |
| after_publication | SIGKILL | no-cache | 5 | 1-1 | 1-1 | 0-0 | 2-2 | 0-0 | 0.1826 / 0.1789 / 0.1830 | final hashes match reference |

Five valid measured trials per condition are required. Warmups, pilots, references, and smoke runs do not enter these tables. Valid slow observations are retained. No p95 or statistical confidence claim is made.
