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
| Native app, media engine, resources, UI tests, `scripts/ios/**`, iOS workflow | iOS build/unit and affected UI scenarios on PRs; full iOS coverage nightly and on manual dispatch; shell tests and mobile contracts |
| Native unit tests/fixture or `Kria/Generated/**` only | iOS build, unit tests and mobile contracts; no PR UI execution |
| Web public fonts and type-posters bundled in Xcode | iOS build/unit on PRs; full iOS coverage nightly/manual |
| `src/apps/ios/Kria/Core/NativeEditorDocument.swift`, `src/apps/ios/Kria/Core/SlidePost.swift` (Swift constants read by API drift guards) | API tests and API lint, plus iOS build/unit on PRs; full iOS coverage nightly/manual |
| API internal implementation, prompts, tests | API tests and API lint |
| API routes/schemas, Kria contracts, models/config/main/worker, mobile identity, API dependency definitions | API and native build/unit contracts; fixture-driven UI tests are not selected |
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

API tests run as two deterministic round-robin shards over the
complete non-quality test-file set; both shards feed the same stable `test-api`
gate, so sharding changes wall-clock scheduling without reducing coverage.
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
PR gets native render evidence. The `unit` leg also skips the export/OCR unit
proofs listed in `scripts/ios/render_evidence.py` (real MP4 exports read back
with Vision) unless the PR touches the render, export or text code in that
file's trigger list: after they landed on 2026-10-30 the leg's unit execution
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

Portable checks and synthetic fixture generation finish before a Mac queues.
API-only journeys keep Linux evidence without requiring a native fixture bundle.
Native fixtures use an artifact name containing the workflow run, attempt, and commit.
The transfer manifest verifies that identity and every relative file hash; the
existing creation proof is checked again before native rendering. Native unit
journeys verify the existing unit result where available; otherwise they run
once as a selected batch. Fixture-backed exporter scenarios remain separate.

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
