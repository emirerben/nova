# Change-based CI

PRs targeting `main` or `dev` select whole suites from their merge-base diff.
Every push to `main` runs full API coverage and an iOS build/unit/smoke release check.
PRs run the affected iOS build/unit phase and a focused UI subset when selected;
the exhaustive iOS UI phase runs nightly and on manual dispatch. Web application
tests and lint are retired; public web assets bundled into iOS remain covered
by native checks.
Other specialized workflows and deployment triggers retain their existing rules.

## Selection policy

| Changed files | PR coverage |
| --- | --- |
| `src/apps/web/**` | No web suite; bundled public assets still select iOS coverage |
| Native app, media engine, resources, UI tests under `src/apps/ios/**` | API tests and API lint (API parity and fixture guards read these files), plus iOS build/unit and affected UI scenarios on PRs; full iOS coverage nightly and on manual dispatch; shell tests and mobile contracts |
| `scripts/ios/**`, iOS workflow | iOS build/unit and affected UI scenarios on PRs; full iOS coverage nightly and on manual dispatch; shell tests and mobile contracts. `scripts/ios/kri-524-creation-e2e.py` also runs the API tests (an API test executes it) |
| Native unit tests/fixture or `Kria/Generated/**` only | API tests and API lint, iOS build, unit tests and mobile contracts; no PR UI execution |
| Web public fonts and type-posters bundled in Xcode | iOS build/unit on PRs; full iOS coverage nightly/manual |
| API internal implementation, prompts, tests | API tests and API lint |
| API fonts, and fixtures Swift tests open by path (files directly in `src/apps/api/tests/fixtures/`) | API tests and API lint, iOS build and unit tests |
| API routes/schemas, Kria contracts, models/config/main/worker, mobile identity, API dependency definitions | API tests and the Linux mobile-contract job; no Mac unless an iPhone input also changed (see "When a PR gets a Mac") |
| `scripts/ci/journey-manifest.json`, when the edit only adds or changes journey rows | API tests, plus the journeys whose rows changed; iOS build and unit tests (and a Mac) only when a changed row is native. Any other manifest edit selects all suites and every journey |
| `.env.example` | API tests and API lint (API tests read it) |
| Shared packages, assets, root dependencies/build config, selector, general CI workflows or unknown paths | All suites |
| `docs/**`, `plans/**`, `agents/**`, listed root documentation, `VERSION` | No heavyweight suites |
| Root `package.json` / `package-lock.json` changing only release versions | No heavyweight suites |

The `ios_ui` output identifies UI-affecting changes and always implies
`ios=true`; PRs run smoke and the affected UI group or changed test methods.
When selection is broad, PRs run the smoke tripwire. Main pushes run one build/unit/smoke lane; the complete UI suite runs nightly
and on manual dispatch. See the
[iOS runbook](ios-development.md#change-based-ci) for phase commands and cache reuse.

Mixed changes select the union. Runtime-tree Markdown (including prompts) is
classified as code before documentation rules. New/unrecognized paths select
all suites. Dependency versions inside a lockfile are not release metadata.
The exact policy lives in `scripts/ci/select-tests.py`; extend it with regression
cases when adding a new subsystem or cross-subsystem dependency.

## Detection and required checks

`.github/workflows/ci-changes.yml` is reused by CI and iOS. Each call checks out
full history and compares the PR head with the merge base of its event base/head
SHAs. Tests still execute on the
normal GitHub merge checkout. The diff uses NUL delimiters, has no API file-count
limit and disables rename detection so both old and new paths are considered.
No fork tokens or PR-supplied shell interpolation are needed.

### Where the CI scripts' own tests run (KRI-1543)

`python3 -m unittest discover -s scripts/ci -p 'test_*.py'` covers selection,
the gates, the registries and PR evidence. It takes about 5 minutes on a hosted
runner (17 to 20 s on 2026-11-12; `test_pr_evidence.py` alone is now 135 s
locally, `test_select_tests.py` 49 s).

- `ci.yml` calls the selector with `selftest: false` and runs the tests in the
  `ci-selftests` job, beside the suites. That job has no `needs` and no `if`.
  The required `lint` gate needs it and fails unless its result is `success`
  (`select-tests.py gate lint lint-suites`), on every event. Suites therefore
  start about 5 minutes earlier, and a red self-test still blocks the merge.
- `ios.yml` (main pushes, nightly, manual) keeps the default: the tests run
  inside the selector job, before selection.
- A new caller of `ci-changes.yml` gets the default. Passing `selftest: false`
  without a `ci-selftests` job that `lint` needs fails
  `SelfTestWiringTests` in `scripts/ci/test_select_tests.py`.

What changed in behaviour: with a broken selector, `ci.yml` suites now start and
`lint` goes red afterwards; before, nothing started. To undo, revert the pull
request that made the change.

Before selecting suites for a PR to `main`, the selector job runs the existing
release-metadata ownership guard. Manual release version or changelog edits stop
selection before API or native jobs can queue. The guard fetches the current base
and compares its merge base with the event's exact PR head, so metadata inherited
from main remains allowed. Dependency changes remain allowed. The separate
release-ownership check keeps its existing name and scope.

To recover, restore the automation-owned fields and push the corrected commit.
If fetching the base fails, retry the failed run after resolving the fetch error.
PR-body evidence validation and its body-edit workflow are unchanged.

An unavailable or empty diff selects all suites. A broken selector or failing
selector test blocks the final gates instead of claiming a successful skip.
The selection and reason appear in the workflow summary.

Stable checks remain `lint`, `test-api`, and iOS `build-and-test`.
The heavy jobs are conditional; the small final checks use `always()` and verify:

- Selection completed successfully and provided a literal `true` or `false`.
- Selected API and native suites completed successfully.
- Unselected suites were actually skipped.
- Missing, failed, cancelled or unexpectedly skipped jobs fail the check.

No branch-protection changes are needed for the existing required `lint` and
`test-api` names. Unrelated PRs still use small Ubuntu selector/gate jobs but
avoid dependency installation, PostgreSQL/Redis services, five web workers and
macOS runners. Superseded PR and portable main CI runs are cancelled; a running main iOS
release lane is never cancelled. Nightly/manual regressions have separate concurrency groups.

API tests run as six shards over the complete non-quality test-file set
(KRI-1544); every shard feeds the same stable `test-api` gate, so sharding
changes wall-clock scheduling without reducing coverage. The count lives in
`scripts/ci/api-tests.py` (`SHARD_COUNT`) and the workflow matrix must list
exactly those shards.

Files are packed by measured time, longest first into the lightest shard, from
`scripts/ci/api-test-timings.json` (files of 5 s and more; any other file weighs
0.5 s). A missing or malformed timings file only makes the shards uneven; every
file still runs once. Each shard hands pytest its files in path order, never in
packing order: xdist's `load` mode gives each worker a run of consecutive tests,
which tends to keep CPU-heavy neighbours in `tests/pipeline/` on the same
worker, one after another, without guaranteeing it.

To refresh the timings (a few runs' worth is enough; each shard attempts to
upload its junit report as `api-junit-<run-id>-<shard>-<attempt>` for 3 days).
These uploads are diagnostic and do not affect the required test result.
Keep the artifact directories: when merging reports from retries, the timing
tool takes the highest available attempt separately for each run and shard.

```sh
gh run download <run id> -p 'api-junit-*' -D /tmp/api-junit
python3 scripts/ci/api-test-timings.py /tmp/api-junit "run <id>, <date>"
```

Measured on 2026-11-20 on throwaway branches, API job only, five repeats per
arm (runs 44651481740, 44658565495, 44664494076):

| Layout | Slowest shard job, median | Tests above 45 s (of 60 s) | Other failures |
| --- | --- | --- | --- |
| two shards by file position (before) | 24.6 min | 2 samples | none |
| three packed, `load` mode | 16.2 min | 1 | slide-post delivery tests in 3 of 5 repeats (KRI-1545) |
| three packed, `--dist loadfile` | not adopted | 5, one test at a median of 53 s | none |
| three packed, `load` mode, KRI-1545 lock (adopted 2026-11-20) | 18.3 min | 2 | none |

Measured on 2026-11-21 on a throwaway branch, API job only, five repeats per
arm on one commit (run 44913979019, 45 jobs, all green), with the locked
install and timings refreshed from five runs:

| Layout | Slowest shard job, median | Tests near their timeout | Other differences |
| --- | --- | --- | --- |
| three packed | 1,012 s (16.9 min) | none | none |
| six packed (adopted) | 642 s (10.7 min) | none | none: 52,354 tests, identical outcomes |

Tests with a timeout of their own did get slower at six shards, with room to
spare: the three carousel focus-mode renders reached 47 s of their 180 s, and
one route replay 59 s of its 300 s. No test on the default 60 s timeout went
above 45 s in either layout. An earlier run of the same experiment (run
44903511580) had one test at 45-46 s, `test_lane_targets_only_move_toward_green`;
it was split per fixture first (#2443).

What that taught:

- A layout change moves where xdist's chunks end, so tests of one file that
  share database rows can start running beside each other. The slide-post
  delivery tests did; they now hold a cross-worker advisory lock
  (`exclusive_slide_post_delivery` in `tests/conftest.py`). A test that sweeps a
  whole table needs such a lock.
- `--dist loadfile` removes that race but spreads the CPU-heavy files over all
  four workers at once, and slow tests get slower. It failed the bar below.
- Four shards by file position were tried on 2026-11-19 (#2389) and reverted
  the same day: a 24-41 second test ran for 53-60 seconds against the
  60-second per-test timeout.

Before changing the count, the packing or the xdist mode, run the old and the
new layout side by side on a throwaway branch (the API job alone, at least five
repeats per arm) and compare the per-test rows:

```sh
python3 scripts/ci/api-times.py compare OLD_DIR NEW_DIR
```

It requires:

1. no test that stopped running and no outcome that differs;
2. no test newly at its timeout, and no more samples per run above three
   quarters of their timeout than the old layout (45 s of the default 60 s;
   a test with its own timeout is judged against that).

A throwaway branch for this adds one workflow triggered by pushes to that
branch, holding the API job with a `layout` and a `repeat` matrix dimension,
`max-parallel: 10` so real pull requests keep runners, and an environment
override for the shard count. Do not reduce `ci.yml` itself: a test reads it.

Order the matrix so the arms take turns (repeat 1 of every arm, then repeat 2),
never one whole arm after the other. Runner speed drifts over an hour, and a
run on 2026-11-21 that listed all "disk" jobs before all "memory" jobs showed a
7% gain for an in-memory test database that was not real: tests that never touch
the database were 10% faster in that arm too. Read a difference under about 10%
at five repeats as noise unless it shows up only in the tests the change
should affect. That experiment is neither adopted nor disproven; all time spent
off the CPU is about 1,100 s per run across six shards, so the most a faster
database could save is about 45 s per shard.

`journey-offline` runs its two slowest steps (the KRI-492 prerequisites and the
camera-roll tests) on four xdist workers. Both spend about 215 s in
`tests/integration/test_actual_export_verification.py` when run serially.
Measured on 2026-11-21 on a throwaway branch, six repeats per arm, arms taking
turns, same commit:

| Workers | KRI-492 step, median | Camera-roll step, median | Outcomes against serial |
| --- | --- | --- | --- |
| serial | 230 s | 232 s | |
| two | 179 s | 163 s | identical, 262 and 180 tests |
| four | 153 s | 151 s | identical, 262 and 180 tests |

Four workers only gain a third because the tests already use every core
(FFmpeg): summed test time doubles. The slowest test used 26% of its timeout.
The same file runs in both steps and in an API shard; running it once per job
would save more and is not done yet.

The `iPhone validation` matrix has a `unit` leg (build, unit tests, selected UI
subset, named-unit gate) plus `creation` and `screens` legs that compile from
the same cache and split the fixture-driven journeys by measured duration. The
selector's `native_legs` output starts only the legs whose journeys were
selected (`LEG_JOURNEYS` in `scripts/ci/select-tests.py`; `test_ci_throughput.py`
keeps that map and the workflow's step conditions in agreement). One serial
Mac took 40-55 min per PR (2026-10-23); each leg targets about 20 min.
On PRs the selector starts only the `unit` leg, alongside a Linux
mobile-contract job: three Macs per iOS PR saturated the account's five
concurrent macOS runners (2026-10-27..30: median Mac wait 10-19 min, median PR
67-87 min). The fixture-driven journeys still replay offline on Linux for every
PR; their iPhone renders run nightly and on manual CI dispatch, which is how a
PR gets native render evidence. A PR's selected UI scenarios run on the
`unit` leg when they fit one shard (`ui_tests.PR_SHARD_TARGET_SECONDS`, 35 min
of recorded time). A longer selection, such as an editor PR's group (88.6
recorded min; 87 min serially on #2225 after build and unit tests), gets one
`ui-<i>` leg per duration-balanced shard, up to three
(`ui_tests.PR_MAX_SHARDS`; the larger creation group, ~158 recorded min, also
stops at three, since nightly/manual alone may use `MAX_SHARDS` = 5), and the unit leg only
builds and runs unit tests. Each UI leg verifies exactly its part of the
partition, and the `journey` aggregate recomputes the shard count and leg list
so a missing or extra leg fails. A changed UI test file selects the group that
owns its tests (`ui_tests.file_group`); a file with shared file-scope helpers
needs an explicit `sources` mapping or selects `full`. Refresh the durations
with `scripts/ios/refresh-ui-durations.py`, which reads both `iOS` and `CI`
native legs. The `unit` leg also skips the export/OCR unit
proofs listed in `scripts/ios/render_evidence.py` (real MP4 exports read back
with Vision) unless the PR touches the render, export or text code in that
file's trigger list, or a selected journey names them (`journey-gate.py
native-units` needs every named test in the unit results): after they landed on 2026-10-30 the leg's unit execution
went from a median of ~12 to ~22 min. `ios.yml` runs the whole unit suite on
every main push and nightly, so they still gate each merge's follow-up. The legs start as soon as selection finishes and compile while
`journey-offline` runs; `scripts/ci/wait-for-jobs.py` blocks only before the
fixture bundle is downloaded and fails the leg unless `journey-offline` (and
`journey-library`, when selected) succeeded, so the Linux journeys are no
longer in front of the Mac queue. The stable `build-and-test` gate requires every leg and the contract
job to pass when iOS is selected. Main uses one iOS build/unit/smoke lane and dispatches TestFlight after those checks
pass. Full iOS regression runs nightly or on manual dispatch; full native
journeys run nightly or on manual CI dispatch. Neither regression path
dispatches TestFlight.

A full iOS regression runs at most three of its six legs at a time
(`max-parallel: 3` in `ios.yml`), so it never holds more than three of the
account's five hosted Macs. Uncapped, the nightly of 2026-11-21 held all five
for 65 minutes and two PRs waited 43 and 53 minutes for their iPhone job
(results after 86 and 88 minutes). The price is a second round: legs 1 to 3
start together, legs 4 and 5 and the Linux contracts leg start as slots free
up. First capped run (45007886433, manual): never more than three Mac legs at
once, Mac legs 58 to 74 minutes each. Size the cap from Macs left for PRs, not
from nightly duration; `test_full_regression_never_holds_more_than_three_macs`
fails if it is removed or raised past three.

The nightlies are meant to run in the UK night. GitHub starts this repo's
scheduled runs hours after the time in the workflow, and the delay moves: about
5 hours in mid-October 2026, 7 to 9 hours in mid-November. The schedules are
therefore written early (KRI-1552): `ci.yml` at 17:23 UTC and `ios.yml` at
18:23 UTC, which today start at about 00:00 to 02:00 and 01:30 to 03:00 UTC. The
iOS nightly then runs for about two and a half hours. To see when they really start:

```sh
gh api "repos/{owner}/{repo}/actions/workflows/ios.yml/runs?event=schedule&per_page=7" \
  --jq '.workflow_runs[] | .created_at'
```

Re-tune both times together, keeping CI one hour ahead of iOS, when a week of
iOS nightlies starts before 22:00 or after 05:00 UTC. The guard is
`test_the_two_nightlies_are_scheduled_an_hour_apart_ci_first` in
`scripts/ios/tests/test_workflow_policy.py`.

Test registries are derived, not listed (KRI-1380). A journey API test declares
`pytestmark = pytest.mark.journey("<journey-id>")` at module level (add
`step="<name>"` when one journey has several `ci.yml` steps; the list, annotated
and `mark.journey` spellings all work, and a decorator or class attribute is
refused by name). `ci.yml` steps call `scripts/ci/journey_registry.py step <name>`
instead of listing test paths, and `journey-manifest.json` rows gain the marked
files as `api_tests` at load time (`journey_registry.journeys()`), so a new or
renamed test needs no registry edit. CI self-tests assert properties of a step
(non-empty, files exist), never a fixed path. Trigger `paths`, evidence tests
without a marker and `xctests` stay in the manifest. A journey that `ci.yml`
runs through `journey_registry.py tests <id>` (clip speed, camera-roll
recommendations, the plaza title, recovery) takes a marker the same way. A
trigger path is one file or a directory ending in `/`, the rule
`select-tests.py` applies (`journey_registry.matches_journey_path`); `check`
fails a path that selects nothing, so a new test file is added to `paths` by
hand to trigger its journey.

`make check-test-registries` (also a `scripts/ci` unit test and
`preship-check.sh`) fails on a manifest path, test or `xctests` entry that does
not exist, a Makefile, a script under `scripts/` or `formal/kria_turns`, the
KRI-577 ledger `tests/gates/reliability-gates.json` or a non-`ci.yml` workflow
that names a test that does not exist, a hand-kept file that still names a path git
renamed, a marker whose step `ci.yml` never runs, a step with no marked test, an
unknown journey id, a hand-listed test path in `ci.yml` and a native test named
in `ci.yml` (`-only-testing:Target/Class/test`, `--xctest Class/test`) that does
not exist in that target; a problem `sync` cannot fix names its own fix, the rest
end with `make sync-test-registries`. After a rename, `make sync-test-registries` rewrites the renamed paths
in those files and follows Swift class and test-method renames into
`ui-test-groups.json`, the manifest's `xctests`, the `ci.yml` native steps,
`scripts/ci/journey-gate.py`, `scripts/ios/render_evidence.py` and the
`scripts/ios/tests` self-tests, including a renamed UI test file path, class
name and `func` name they quote (stage a file rename first); `check` names a file
that still holds the old name or a `src/apps/ios/Tests/...swift` path that is gone.
When sync cannot follow an entry (a method renamed in the same edit that adds or
removes another), the check after sync says to edit or delete that entry by hand.
The `scripts/ios/tests` self-tests borrow their UI test file, class and methods
from the tree (`ui_fixture.py`) or use synthetic names, and a self-test asserts the
recorded xcresult fixtures name no real UI test (sync rewrites `.py` files, not
fixtures).
Not derived yet: `api_tests` entries for the manifest's journeys that no marker
declares (a new test for those still needs a manifest line or a marker), the
native test names in `ci.yml` and the manifest's `xctests` (followed through
renames, not generated), the manifest's `paths`, the Makefile, `verify-kria.sh`,
`reliability-gates.json`, `request-following-regressions.py` and workflow test
lists (validated and rename-followed, not generated), and
`ui-test-durations.json` / `ui-test-measured-durations.json` (an unrecorded test
weighs the median; refresh with `scripts/ios/refresh-ui-durations.py`).

The stable `journey` job now aggregates `journey-mapping`, `journey-offline`,
the database-backed `journey-library` (Linux), and every `journey-native` leg
(Mac). Linux jobs install FFmpeg and friends through
`.github/actions/apt-install`, which caches the downloaded `.deb` files so a
crawling Ubuntu mirror (25-35 min in 4 of 22 PR runs on 2026-10-23) no longer
holds the Mac legs back. The
selector's `native_validation` output is
false on main pushes, and otherwise follows selected iOS/native journeys. The
aggregate independently checks that decision against the event and manifest,
then requires success or an explicit skip for each dependency.
The heavy native job uses `!cancelled()` so optional skipped preparation does
not block it, while superseding a PR commit stops its obsolete native execution.
Only the small aggregate checks use `always()` to report failed dependencies.

The Mac queues as soon as selection finishes. It does not wait for the `journey-mapping` job:
the selector emits `journey_mapped` from the same check, so an unmapped selection still
never allocates a Mac, and the `journey` aggregate rejects an inconsistent `journey_mapped`.
Portable checks and synthetic fixture generation run while the Mac compiles.
API-only journeys keep Linux evidence without requiring a native fixture bundle.
Native fixtures use an artifact name containing the workflow run, attempt, and commit.
The transfer manifest verifies that identity and every relative file hash; the
existing creation proof is checked again before native rendering. Native unit
journeys verify the existing unit result where available; otherwise they run
once as a selected batch. Fixture-backed exporter scenarios remain separate.

## Baseline

The repository is public, so hosted runner minutes cost nothing. What CI spends
is the account's five concurrent macOS runners and the time people and agents
wait for the required checks. `scripts/ci/ci-baseline.py` measures both from
GitHub (read-only) so a CI change has a before and an after:

```sh
python3 scripts/ci/ci-baseline.py --days 14 --until 2026-11-19T00:00:00Z
```

It needs about 2,200 GitHub API calls for 14 days and stops before the per-run
queries if the hourly allowance cannot cover them. Compare windows of the same
length that lie wholly on one side of the change: a 14-day window that
straddles the merge mostly measures the old behaviour. The KRI-1534 selection
changes landed on 2026-11-19, so the first clean 14-day "after" window ends
2026-12-03. For an earlier read, compare a shorter window after the change
with a "before" window of the same length.

Measured 2026-11-05 to 2026-11-19 (UTC), before KRI-1534:

| Measure | Value |
| --- | --- |
| PRs merged to `main` | 277; a Mac ran on 256 (92%) |
| Minutes from first check to last required check, all PRs | median 49.8, p90 120.5 (267 measured) |
| ... when a Mac ran | median 51.5, p90 121.2 (250) |
| ... when no Mac ran | median 26.5, p90 47.6 (17) |
| Mac-minutes, all workflows | 50,393 |
| ... on jobs that ended cancelled / failed | 8,721 (17%) / 7,991 (16%) |
| ... on re-run attempts | 5,009 |
| ... failed on PRs while `main`'s own iOS run was already red | 1,769 of 5,057 |
| ... `CI` on pull requests / `iOS` on push / `iOS` nightly / TestFlight | 33,991 / 5,736 / 4,412 / 3,396 |
| Cancelled Mac jobs | 291; median 17.0 min before the cancel, 59 within 5 min |
| Nightly `CI` / `iOS` | 4 of 14 green / 0 of 14 green |

How to read it:

- The wait is measured on the merged head commit only: from the first check
  that started (any attempt) to the latest attempt of the last of `lint`,
  `test-api` and `build-and-test` to finish. Earlier pushes to the same PR are
  not counted, and 10 PRs merged before a required check finished are left out.
- Mac-minutes count every attempt of every macOS job, with part minutes rounded
  up. Linux minutes are not reported: they are free and not scarce.
- Cancelled Mac jobs had usually run for a long time, so starting the Mac a few
  minutes later would recover little of that time.
- A PR run tests the merge with `main`, so a red `main` fails every PR's Mac
  leg. About a third of the failed PR Mac-minutes fell in such periods, judged
  by the last `iOS` run on `main` that had started before the PR run.

### Waiting, setup and running per job

`--phases` splits every job of the newest finished runs into three buckets, so
a speed change is aimed at the biggest one:

```sh
python3 scripts/ci/ci-baseline.py --phases 150 --days 1 --until 2026-11-21T06:00:00Z
python3 scripts/ci/ci-baseline.py --phases 150 --workflow ios.yml --event push
```

- **Waiting** is job creation to runner pickup, plus any step named "Wait for
  ..." (a job waiting on another job). A job behind `needs` is created when its
  dependencies finish, so their runtime is not counted as its wait.
- **Setup** is checkout, toolchain installs, caches, service containers,
  uploads and the runner's own bookkeeping. The step names it covers are listed
  in `SETUP_PREFIXES` at the top of the script. A step that is not listed counts
  as running, so a new step can never make setup look cheaper.
- **Running** is everything else: the checks themselves.

It costs one API call per run. Skipped, cancelled and never-started jobs are
left out; matrix legs are reported separately. The report also lists every step
with a median of 8 s or more and the bucket it was filed under.

Pull-request runs of `ci.yml`. Job columns are seconds, p50 / p95, for
`api (0)` and the iPhone unit leg:

| Window | Runs | Minutes to last required check | API setup | API running | iPhone running | iPhone waiting |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-14 to 11-19, before KRI-1534 | 126 | median 50.0, p95 134.6 | 162 / 193 | 1,140 / 1,461 | 1,809 / 2,816 | 305 / 5,859 |
| 2026-11-20, three API shards | 22 | median 32.5, p95 44.0 | 166 / 186 | 852 / 974 | 1,867 / 2,264 | 5 / 369 |

Here the wait is run start to the last of `lint`, `test-api` and
`build-and-test`, for every finished PR run, not only merged heads. Setup is
about 2.5 minutes on every Linux job that installs the API (`pip install` 86 s,
`setup-python` 32 s, service containers 22 s, apt 15 s). Runner-minutes per PR
run on 2026-11-20: 75 Linux, 32 Mac.

### The API's locked install

Every CI job that needs the API's Python packages uses
`.github/actions/api-env`: `uv sync --frozen` from `src/apps/api/uv.lock`, with
torch from the CPU-only index. Measured side by side on one hosted runner
(#2434): 10 s and 2.3 GB locked, against 114 s and 7.0 GB for the unlocked
`pip install -e ".[dev]"` it replaced. There is no cache on purpose: the
download is faster than a cache restore of that size.

- After any change to the dependencies in `src/apps/api/pyproject.toml`, run
  `cd src/apps/api && uv lock` and commit `uv.lock`. `lint-suites` fails when
  the two disagree.
- The lock resolves every extra together, so CI runs versions the production
  image (which installs the `observability` extra) can also install: celery
  5.6.x and redis 6.4.x, not the 5.7.0 and 8.1.0 the unlocked install picked.
  The production Dockerfile is not built from the lock yet.
- Before changing how the suite is installed, sharded or run, download the
  `api-times-*` artifacts of runs before and after and compare them:
  `python3 scripts/ci/api-times.py compare OLD_DIR NEW_DIR`. It fails when a
  test stopped running, an outcome changed, a test newly reaches the 60 s
  timeout, or more samples per run sit above 45 s.

### API lint and format

`python3 scripts/ci/api-lint.py` runs `ruff check` and `ruff format --check` on
`src/apps/api`. `lint-suites` runs it on the whole tree; `scripts/preship-check.sh`
runs it on the changed files. Both checks always run, so one push shows every
problem. First measured 2026-11-21 on a laptop: 0.06 s lint, 0.03 s format,
3,452 files.

- ruff is pinned exactly (`ruff==` in `pyproject.toml`, the same version in
  `uv.lock`). The command refuses to run with any other version and prints the
  install line, because two ruff versions format differently. Before the pin,
  CI ran 0.17 and laptops 0.15, and formatting was not checked in CI at all:
  92 files had drifted.
- To move to a new ruff: change the pin, run `cd src/apps/api && uv lock`,
  run `ruff format .` with the new version and commit the three together.
- Format one file, never a directory you did not change, and with the pinned
  ruff (a bare `ruff` on PATH may be another version):
  `cd src/apps/api && "$(python3 ../../../scripts/ci/api-lint.py --ruff-path)" format path/to/file.py`.

### Local git hooks

`hk.pkl` defines two git hooks, run by [hk](https://hk.jdx.dev). Install once
per clone: `brew install hk && hk install` (`scripts/worktree-setup.sh` does the
second part). The install is in the repository's git config, so every worktree
has it; a branch without `hk.pkl` runs no hk checks. They repeat checks CI
runs anyway, to save the CI round trip.

| Hook | Checks | Measured 2026-11-21 |
| --- | --- | --- |
| commit | `api-lint.py` on the staged API Python files (check only, nothing is rewritten) | 0.08 s |
| push | `api-lint.py` on the whole API tree, the CLAUDE.md size limit, the journey and UI test registries, and unmapped journey paths against main | 2.0-2.3 s |

The budget for both together is 9 s. Time them with
`HK_TIMING_JSON=/tmp/hk.json hk run pre-push --from-ref origin/main --to-ref HEAD </dev/null`.
The editor's format-on-save hook (`scripts/hooks/format-on-save.sh`) formats
only with the pinned ruff; with any other version it does nothing.

### Skips in the API shards

A skipped test is a check that did not run. After pytest, each API shard reads
its own per-test rows and fails if a test skipped for a reason that is not on
`scripts/ci/api-expected-skips.json` (`check_skips` in `scripts/ci/api-tests.py`).
Each entry names a file, a pattern the skip reason must match, and why the skip
is acceptable. An entry whose `why` starts with `GAP` is a check that should run
and does not yet.

- A new skip in CI: make the test run, or add an entry with the reason. Do not
  widen an existing pattern to cover it.
- Tests that need the repository's history are marked
  `@pytest.mark.needs_git_history` (`tests/git_history_required.py`). They skip
  in the shards, which clone one commit, and run in `creator-contract-replay`
  with `NOVA_REQUIRE_GIT_HISTORY=1`, where a missing commit or merge base is a
  failure. That job runs only the marked tests.
- First measured 2026-11-21: 115 skips per run in 26 files. 26 of them were
  checks that never ran on pull requests (25 reliability-gate fix-commit tests
  and the copy-catalog codemod test) and one needed an uninstalled package.

## When a PR gets a Mac (KRI-1534)

A pull request starts a Mac only when it changes an iPhone input
(`IOS_NATIVE_INPUTS` in `scripts/ci/select-tests.py`), a CI policy path or a path
the selector cannot classify. The selector emits this as `ios_native`, and the
selection summary says "iPhone inputs: unchanged (no Mac ...)" when it applies.

Before this rule a PR got a Mac whenever it selected `ios` or a journey flagged
`needs_native`: 92% of merged PRs, most of which changed no file the iPhone
build or its tests can read. For those the Mac rebuilt and retested the same
bytes as `main`. Nothing the PR's Python produced reaches a test on that Mac:
the offline fixture bundle is handed to tests through `TEST_RUNNER_*` variables,
and only the `creation` and `screens` legs set them, which run nightly and on
manual dispatch. `test_no_pr_mac_step_hands_python_output_to_the_iphone_tests`
fails if a PR leg starts receiving it.

What still runs for such a PR, unchanged:

- `ios` is still selected by API contract paths, so the Linux `ios-contracts`
  job runs and `build-and-test` requires it. It is what catches a stale mobile
  contract; regenerating the contract changes `src/apps/ios/Kria/Generated/`,
  an iPhone input, so that PR gets its Mac.
- Linux journeys (`journey-offline`, `journey-library`) and the API shards.
- The `journey` gate recomputes Mac selection from `ios_native`, so a Mac that
  is skipped when an input changed, or runs when none did, fails the gate.

Nightly and manual `CI` runs and every push to `main` are not affected.

**Rollback:** set the repository variable `CI_MAC_SELECTION` to `full`
(Settings, Variables, or `gh variable set CI_MAC_SELECTION --body full`). On
the next run every pull request selects what it did before KRI-1534: a Mac
whenever `ios` or a native journey is selected, everything for a
journey-manifest edit, and everything for `.env.example`. No code change is
needed. Delete the variable to return to the rules.

The list is exact where it can be: the app tree, the scripts that drive it,
bundled web resources, API fonts, the shared parity fixtures, and the files
directly inside `src/apps/api/tests/fixtures/` (Swift tests open those by name,
some built at run time; the folders below it are API-only). A PR that changes
one of those API files now also runs the Swift unit suite, which it did not
before: both sides read the file.

Two checks keep the input list honest:

- `test_every_repo_path_the_iphone_tree_names_is_a_declared_native_input` scans
  Swift string literals and project config for repo paths outside
  `src/apps/ios/`. A new fixture or font a Swift test opens by path fails that
  test until it is declared. The scan cannot see a path assembled at run time
  from pieces, which is why unknown paths still select a Mac.
- `scripts/ci/native-inputs-backtest.py` replays the rule over real PR runs and
  lists every Mac failure on a run the rule would have skipped, with whether
  `main`'s own iOS run was already red.

Replay of 2026-11-05 to 2026-11-19 (897 pull-request `CI` runs):

| | Runs |
| --- | --- |
| Rule would start no Mac | 216 (24%) |
| ... a Mac really ran to an end | 151 |
| ... passed / green on re-run | 123 / 2 |
| ... failed and stayed red | 26 |
| Changed an API font or fixture Swift tests open | 56 |

None of the 26 was caused by its PR; every log was read, and `main`'s own iOS
run was red just before each. 24 are one Swift test-build error (test doubles
no longer conforming to `KriaAPIClient`) on unrelated branches between
2026-11-06 22:12 and 2026-11-07 12:19. One is a unit test that `main`'s iOS run
failed the same evening (run 42412737860), on a PR that changed two Python
files. One is the Mac's wait step reporting a failed Linux journey job, which
the rule does not skip. Under the rule those PRs would not have gone red for a
break they did not cause; `main`'s own iOS run still reports it.

### Journey manifest edits (KRI-1541)

`scripts/ci/journey-manifest.json` is CI policy and was in 330 of those 897 PR
runs (37%). Until KRI-1541 any edit of it selected every suite, a Mac and all
179 journeys. Now the selector compares the manifest at the merge base and at
the PR head, row by row:

- Added or changed rows select those journeys, plus the API suite (API tests
  read the manifest). Every row whose definition changed still executes.
- A changed row that is native on either side (`needs_native`, `xctests` or
  `xctest`) also selects the iPhone unit suite and a Mac.
- Unchanged rows are identical on both sides and are not selected by the edit.
  Reordering or reformatting changes no row.
- A removed row, a change outside `journeys`, duplicate or missing ids, a
  manifest that does not parse on either side, or a changed id the checkout's
  registry does not know: everything is selected, as before.

`.env.example` (in 116 runs) selects the API suite, whose tests read it.

Narrowing which journeys run drops only a test's second execution: every
journey API test file is also in the API shard set, which every PR that selects
`api` runs in full (`test_every_journey_api_test_also_runs_in_the_api_shards`).
What `journey-offline` adds for a selected journey is its fixture generation
and evidence steps.

The Linux side was replayed too. Of the 328 runs with a plain row edit, 4 had a
Linux job (`journey-offline`, `journey-library`, `journey-mapping`,
`ios-contracts`) fail and stay red. Three of those jobs still run under the
row rule. The fourth is `journey-offline`'s camera-roll step, which the row
rule would not have selected on that PR: its failing test,
`tests/integration/test_actual_export_verification.py::test_frame_profile_never_accepts_a_shifted_or_wrong_picture`,
passed in the API shards on the same commit (run 43607819671), so it was a
flaky comparison, not a regression the PR caused.

Replay of the same 897 runs with these rules: 189 manifest edits change only
Linux rows, 139 change a native row, 2 select everything. The rule starts no
Mac on 305 runs (34%), 89 more than without the manifest rules. A Mac ran to an
end on 219 of the 305: 179 passed, 7 went green on re-run, 33 failed. The 33
are the 26 above plus 7, each read:

- 3 are the same inherited test-build break on `main`.
- 2 are `SegmentationInputScalerTests.testAChangeOfFrameSizeRendersAtTheNewSizeAndBuffersAreRecycled`
  on two unrelated PRs; each PR's next run passed with no iPhone file changed.
- 1 is the Mac's wait step reporting a failed Linux journey job.
- 1 is `NativeEditorPlaybackAdmissionUITests`: an in-app playback fixture that
  never admitted its first frame on that runner. The same test passed on ten
  other Mac jobs within four hours, including the same branch.

## When a nightly goes red (KRI-1549)

The nightlies are the only runs of the `creation` and `screens` journey legs
and of the full iPhone UI suite; a PR that selects the `full` UI group runs only
smoke. From 2026-11-05 to 11-19 the `iOS` nightly was red 14 nights in a row and
the `CI` nightly 10 of 14, and nobody was told.

Each scheduled run of `ci.yml` and `ios.yml` now ends with a `nightly-alert`
job (`scripts/ci/nightly-alert.py`, built-in `GITHUB_TOKEN`, no secret):

- If any job of the run failed, was cancelled (a timeout is a cancel) or had not
  finished, it opens one GitHub issue, **Nightly CI run is red** or **Nightly
  iOS run is red**, labelled `nightly-red` and assigned to the repository owner,
  or comments on that issue if it is already open. The text names the run's start
  time (UTC) and commit, never "last night": scheduled runs start hours late.
- It lists each red job with its failed step and the failed tests it reads from
  the job log: the UI suite's `UI coverage mismatch` line, XCTest
  `Test Case ... failed` lines with no later pass (a test that passed on retry
  is flaky, not failing) and pytest `FAILED`/`ERROR` lines. A log it cannot read
  is named as such; the jobs are still listed.
- The next green scheduled run of the same workflow closes the issue with a
  comment naming that run. Only the newest scheduled run speaks: re-running an
  older night's jobs (which keeps the `schedule` event) changes nothing.
- It runs on `schedule` only. PR, push and manual runs never open, update or
  close the issue. A run of any branch but `main` uses its own title ("... (branch
  <name>)") and no assignee, so a drill cannot touch main's issue.

To change who is assigned, set the repository variable
`NIGHTLY_ALERT_ASSIGNEES` (comma-separated GitHub logins). With the issue open:

1. Open the failing tests' job from the issue and read the diagnostics artifact
   (`ios-native-test-diagnostics-*`). File a Linear issue per failing test, or
   fix it, as for any red `main` check.
2. Merging the fix does not close the issue; the next green scheduled run does.
   A manual `workflow_dispatch` run is a useful check but does not close it.

To see what the alert would write for any finished run, without changing
anything:

```sh
GITHUB_REPOSITORY=emirerben/nova python3 scripts/ci/nightly-alert.py --dry-run --run-id <run id>
```

The guard is `scripts/ci/test_nightly_alert.py`: it lists the ways the alert can
fail and holds the job's `needs` (every other job, so it runs last), its
schedule-only condition and its permissions. A new job in either workflow must
be added to `nightly-alert`'s `needs`.

## Verification and rollback

```sh
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
actionlint .github/workflows/ci.yml .github/workflows/ci-changes.yml .github/workflows/ios.yml
```

Regression cases cover real git histories, renamed/deleted/mixed files, unusual
filenames, moving base branches, release metadata versus dependencies, missing
diffs, and gate success/failure/cancellation combinations.

To inspect a PR locally, set `CI_EVENT=pull_request`, `CI_BASE=<base SHA>` and
`CI_HEAD=<head SHA>` when running `python3 scripts/ci/select-tests.py`. Without a
PR event the selector runs all suites, making manual diagnosis conservative.

To disable selection without changing required-check names, make `selection()`
return all suites, then update the selector tests. Do not turn failed detection
into an intentional skip or remove the final gates.
