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

## Hosted validation

Target: median web feedback below eight minutes across at least three successful
hosted runs. This is a target, not a measured result. Results will be recorded
here before KRI-16 is considered complete.

Measure from the earliest matrix job start through aggregation completion.
Record both that elapsed time and queue gaps; also record every group duration,
setup overhead, test counts and summed runner minutes (including aggregation).
Use Actions job/step timestamps plus the uploaded Jest reports. Report individual
runs and the median; preserve failures rather than selecting only fast results.

## Tradeoffs and rollback

Parallel execution preserves all pre-merge coverage and fresh-process isolation,
but uses five concurrent runners and repeats setup five times. Runner minutes
may increase even as feedback improves; queue availability can limit the gain.
Deno is installed only where needed. Existing dependency resolution and caches
are deliberately unchanged; lockfile/package-manager work is separate.

If isolation regresses or runner cost is unacceptable, revert the workflow and
runner changes together to restore serial execution. Do not disable tests or
relax branch protection as a performance workaround.
