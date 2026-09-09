# Web CI runtime and coverage (KRI-16)

## Coverage policy

Every PR targeting main or dev runs the complete Jest suite. Every push to main
also runs the complete suite, preserving detection of merge/main regressions.
There are no path exclusions, scheduled-only tests, or changed-test heuristics.

| Stage | Purpose and coverage | Frequency |
| --- | --- | --- |
| Checkout, Node 20, pnpm 9 | Reproduce the checked-out merge commit and existing test runtime | Every test group |
| pnpm store restore and dependency installation | Supply web and local motion-runtime dependencies | Every test group |
| Deno and offline CanvasKit cache | Exercise actual offline motion rendering, generated contracts and cross-renderer parity | Remaining group on every run |
| Four interaction groups | Chat creation, personalization, library, timeline, inspector, visuals, header, TikTok and Radix interactions | All groups on every run |
| Remaining Jest tests | Other component, editor, reducer, client, fixture and rendering contracts | Every run |
| test-web aggregation | Report success only when the entire matrix succeeds | Every run, including upstream failure or skip |

Lint/API CI, mobile Playwright E2E and deployment triggers remain unchanged.
At investigation time, main's required checks are `lint` and `test-api` (strict),
with review requirements; `test-web` is not required. This change does not modify
branch protection or make deployment contingent on a new check.

## Execution and failure behavior

`scripts/ci/web-tests.mjs` owns interaction membership. Jest discovers the current
suite list; every listed interaction file must exist, and everything else goes
to the remaining group. New tests are included automatically. Each interaction
suite gets its own fresh process, `--runInBand`, and the existing 300000ms per-test
timeout. The remaining group retains Jest's 50% workers and 5000ms default.
No assertions, mocks or production code change.

Four groups are balanced by observed timings, rather than file counts. The
matrix uses `fail-fast: false`; a failure does not cancel sibling groups. Within
an interaction group, subsequent suites still run after an earlier failure.
The stable `test-web` aggregation fails on failure, cancellation, missing needs
or unexpected skips. Each group has a 20-minute job ceiling.

Jest JSON reports include individual suite durations and test counts. Each group
also writes incremental `test-results/web/<group>/timings.json`; artifacts are
uploaded on success or failure and named by group and run attempt. Job summaries
include timings and status. Abrupt runner termination can prevent the current
process from writing a report; prior completed processes retain their reports.

Run runner checks with `node --test scripts/ci/web-tests.test.mjs`, verify real
Jest discovery with `node scripts/ci/web-tests.mjs verify`, and run a group with
`node scripts/ci/web-tests.mjs interaction-1` (or `remaining`). Prime Deno using
the workflow's cache commands before running the remaining group locally.

## Baseline (GitHub-hosted Ubuntu, 2026-09-09)

| Run | Setup/cleanup | Serial interactions | Remaining | Web job total |
| --- | ---: | ---: | ---: | ---: |
| [PR 34351865462](https://github.com/emirerben/nova/actions/runs/34351865462) | 42s | 19m06s | 3m48s | 23m36s |
| [main 34350956227](https://github.com/emirerben/nova/actions/runs/34350956227) | 39s | 19m24s | 3m54s | 23m57s |

The PR baseline discovered 303 suites (13 interaction + 290 remaining), all
passing: 298 interaction tests and 3436 remaining tests = 3734 total.
Installation took seven seconds. Serial interaction execution consumed 81% of
web job time; dependency installation and caching are not the bottleneck.
ChatCreationWorkspace alone took 302 seconds. The planned interaction groups
sum to roughly 316, 287, 270 and 260 seconds before setup overhead.

## Local validation

All five groups passed on macOS: 303 suites and 3734 tests, matching the baseline.
Interaction groups selected 2/3/4/4 suites (100/103/68/27 tests); the remaining
job selected 290 suites (3436 tests). These local runtimes are not used as hosted
performance evidence. Six runner guard tests cover exact partition membership,
new/colocated test inclusion, missing/duplicate discovery, and actual CLI exit
codes for successful, failed, cancelled and skipped dependencies. A subprocess regression guard also requires pnpm-based discovery. Workflow YAML
parsing and the repository pre-PR checks also passed.

## Hosted validation

The first three hosted attempts ([34359477982](https://github.com/emirerben/nova/actions/runs/34359477982),
[34359592194](https://github.com/emirerben/nova/actions/runs/34359592194),
[34359671694](https://github.com/emirerben/nova/actions/runs/34359671694)) exposed
an invocation regression: launching Jest's JS entrypoint directly bypassed
pnpm's executable wrapper and its NODE_PATH, so styled-jsx imports failed.
Their remaining groups failed, and the aggregate check correctly failed rather
than accepting partial coverage. These attempts are excluded from successful
performance samples. The runner now uses `pnpm exec jest`, preserving pnpm's
module resolution without changing dependencies, mocks or assertions. With an
isolated pnpm 9 installation, the direct invocation reproduced the failure and
the wrapper passed; the corrected remaining suite passed all 3436 tests locally.


### Successful measurements (2026-09-09)

All three samples below passed **303 unique suites / 3734 tests**, with zero
skipped tests. Downloaded Jest reports were checked for duplicate/missing suites,
pass status and counts. The workflow and runner implementation are identical
across these commits; only an invocation regression guard and documentation
were added between samples.

| Run | Observed web feedback | Execution path excluding queues | Initial queue | Group start spread | Gate queue | Summed runner time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [34360334606](https://github.com/emirerben/nova/actions/runs/34360334606) | 7m54s | 7m51s | 3s | 12s | 3s | 29m40s |
| [34360526158](https://github.com/emirerben/nova/actions/runs/34360526158) | 8m17s | 7m56s | 2s | 32s | 3s | 33m24s |
| [34360586094](https://github.com/emirerben/nova/actions/runs/34360586094) | 7m45s | 7m16s | 42s | 104s | 2s | 28m53s |

Observed feedback is earliest matrix start through aggregate completion; initial
queue time is reported separately. The execution-only critical path is the
longest matrix job duration plus the aggregation job duration, excluding delayed
job starts. Group start spread is a scheduling diagnostic, not a value to subtract
wholesale from feedback (groups run concurrently).

**Median observed feedback: 7m54s; median execution-only path: 7m51s.** Both
meet the eight-minute median target, so further rebalancing is unnecessary.
Compared with the two-run baseline median of 23m46.5s, observed feedback fell
66.8%. Median summed runner time increased from 23m46.5s to 29m40s (+24.8%).
These are elapsed runner durations, not billing-rounded minutes. All web jobs,
including the aggregate job, are included; lint/API jobs are excluded from this
web-only measurement and can still determine overall PR readiness.

Per-group step timings from GitHub (seconds; setup includes checkout, runtimes,
dependency install, offline cache where applicable, and runner guards):

| Run | Group | Setup | Tests | Cleanup | Total |
| --- | --- | ---: | ---: | ---: | ---: |
| 34360334606 | interaction-1 | 25s | 429s | 3s | 7m37s |
| 34360334606 | interaction-2 | 33s | 398s | 5s | 7m16s |
| 34360334606 | interaction-3 | 27s | 207s | 4s | 3m58s |
| 34360334606 | interaction-4 | 29s | 362s | 6s | 6m37s |
| 34360334606 | remaining | 31s | 203s | 4s | 3m58s |
| 34360526158 | interaction-1 | 35s | 419s | 5s | 7m39s |
| 34360526158 | interaction-2 | 27s | 389s | 5s | 7m01s |
| 34360526158 | interaction-3 | 29s | 362s | 5s | 6m36s |
| 34360526158 | interaction-4 | 27s | 339s | 5s | 6m11s |
| 34360526158 | remaining | 34s | 300s | 6s | 5m40s |
| 34360586094 | interaction-1 | 26s | 256s | 4s | 4m46s |
| 34360586094 | interaction-2 | 31s | 387s | 6s | 7m04s |
| 34360586094 | interaction-3 | 27s | 343s | 5s | 6m15s |
| 34360586094 | interaction-4 | 27s | 347s | 4s | 6m18s |
| 34360586094 | remaining | 25s | 229s | 4s | 4m18s |

Aggregation took 14s, 17s and 12s respectively. Detailed individual-suite timing
and assertion counts are in each run's `web-tests-<group>-1` artifacts. To inspect
or reproduce the record, use `gh api repos/emirerben/nova/actions/runs/RUN_ID/jobs`
for job/step timestamps and `gh run download RUN_ID --pattern 'web-tests-*'` for
Jest JSON and incremental timing reports. This is a three-run sample, not a
latency guarantee; repeat the measurements if the suite or runner changes.

## Tradeoffs and rollback

Parallel execution preserves all pre-merge coverage and fresh-process isolation,
but uses five concurrent runners and repeats setup five times. Runner minutes
may increase even as feedback improves; queue availability can limit the gain.
Deno is installed only where needed. Existing dependency resolution and caches
are deliberately unchanged; lockfile/package-manager work is separate.

If isolation regresses or runner cost is unacceptable, revert the workflow and
runner changes together to restore serial execution. Do not disable tests or
relax branch protection as a performance workaround.
