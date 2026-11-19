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
full history, runs the selector's offline regression tests, and compares the PR
head with the merge base of its event base/head SHAs. Tests still execute on the
normal GitHub merge checkout. The diff uses NUL delimiters, has no API file-count
limit and disables rename detection so both old and new paths are considered.
No fork tokens or PR-supplied shell interpolation are needed.

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

API tests run as four deterministic round-robin shards over the
complete non-quality test-file set; every shard feeds the same stable `test-api`
gate, so sharding changes wall-clock scheduling without reducing coverage. The
count lives in `scripts/ci/api-tests.py` (`SHARD_COUNT`) and the workflow matrix
must list exactly those shards. Two shards took about 22 minutes of pytest each;
the shards are the last required check on a PR that starts no Mac.
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
pass. Full iOS regression runs at 03:23 UTC nightly or on manual dispatch; full
native journeys run at 02:23 UTC nightly or on manual CI dispatch. Neither
regression path dispatches TestFlight.

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
length, and wait at least three working days after a change lands.

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
(Settings, Variables, or `gh variable set CI_MAC_SELECTION --body full`). Every
selected PR gets its Mac again on its next run, with no code change. Delete the
variable to return to the rule.

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
