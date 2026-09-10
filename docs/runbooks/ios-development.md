# Kria iOS development

The native app lives in `src/apps/ios`. `project.yml` is the source of truth;
`Kria.xcodeproj` is generated and must not be edited by hand.

## Prerequisites

- Xcode with an iOS 18 simulator runtime
- XcodeGen 2.38 or newer (`brew install xcodegen`)

No Apple team or production bundle identifier is required for simulator builds.
Development defaults come from `Config/Development.xcconfig`; production signing
values belong in an untracked local override or CI secrets.

## Commands

```bash
make ios-generate  # regenerate Kria.xcodeproj
make ios-build     # unsigned simulator build
make ios-test      # generate, build, and run unit/UI tests
make ios-verify    # same gate used by CI
```

Set `KRIA_SKIP_SIMULATOR_TESTS=1` only when validating compilation on a host
without an installed iPhone simulator. CI must run the complete gate.

Verification boots the selected simulator while `build-for-testing` compiles the
app and test bundles, then runs `test-without-building` on that same destination.
UI tests run serially for deterministic navigation checks.
CI caches the actual `src/apps/ios/.derived-data` directory, including Swift
packages, with keys scoped to the Xcode version, architecture and package/project
configuration. Source changes still go through Xcode's incremental build checks.
Changing Xcode or dependency configuration starts a fresh cache.

To check the verification script’s build, boot, and failure handling without
launching Xcode or a simulator, run the offline orchestration tests from the repo
root (CI also runs these before the full gate):

```bash
python3 -m unittest discover -s scripts/ios/tests -v
```

## Architecture boundaries

- Creation threads, runtime-v2 drafts, approvals, editor commits, and jobs remain
  server-authoritative. SwiftData is a recoverable device cache, not a second
  product authority.
- The checked mobile API contract is generated from the API's Pydantic models.
  Do not hand-add a field to Swift without updating the server contract and its
  drift fixture.
- Native preview and export consume the versioned edit recipe. They must never
  decode a job's renderer-internal `assembly_plan`.
- Full-resolution media remains on device during analysis. Uploading originals
  for cloud rendering requires explicit user consent and a temporary-media
  retention receipt.
- Access and refresh tokens belong in Keychain. The native app never contains
  `INTERNAL_API_KEY` and never sends `X-User-Id`.

## Performance gate

Simulator results prove correctness only. Before enabling a local-render
capability, run the instrumented fixture on an iPhone 13 and a current iPhone,
then record preview FPS, seek-to-visible-frame p95, export duration, peak memory,
thermal changes, and preview/export parity. Local export stays behind the server
capability flag until those measurements pass the rollout thresholds.

## Native brand and navigation

KRI-23 aligns the entire native app with Paper’s **Mobile Flow** page in
[Kria — Chat-First Creation](https://app.paper.design/file/01M166BHQ2QZ6EMMBB1D40GV9W/3-0).
`DesignSystem/DesignTokens.swift` defines semantic Sunlit roles: Sky selection,
Butter actions, Sage audio/direction, and Lilac/Plum text tools. `BrandComponents.swift`
contains the approved DynaPuff wordmark and shared outline navigation icons.
The in-app wordmark is a transparent, tightly cropped vector PDF in `KriaWordmark.imageset`, exported from the approved Paper icon artwork (Main Brand Assets, ETC-0 / ETD-0). It preserves the corrected letter spacing and blue `#9BCAFF` without SwiftUI font metrics or per-letter offsets. The opaque 1024px app icon uses the same artwork on white `#FFFFFF`; iOS supplies the rounded mask. Paper PDF export includes a gray canvas rectangle: remove that export-only background before producing the transparent wordmark. The original DynaPuff font and OFL license remain bundled.

The chat workspace owns the project drawer, Gallery, and account presentation.
Project actions reuse the authenticated creation-thread PATCH/DELETE contracts,
including revision checks and rename idempotency. Deletion is confirmed and
blocked during rendering or pending uploads. Microphone chat input is deferred.
Native system authentication, Photos, upload consent, and share sheets remain native.

For repeatable visual review, a Debug build accepts `-ui-testing-brand` with
`KRIA_BRAND_STATE` set to `format`, `footage`, `direction`, `rendering`, `ready`,
`editor`, `projects`, `gallery`, `signin`, `account`, `consent`, or `recovery`.
These fixtures compose the real components; they never enter Release navigation.
Use `-ui-testing-chat` for interactive navigation and the existing editor fixtures
for edit/save/export verification.

See the [KRI-23 native design review](../reviews/kri-23/README.md) for screenshot
comparisons, behavior coverage, and the remaining live-device validation limits.

## Native chat creation (KRI-24)

The capabilities endpoint advertises `formats`, per-role `media` limits,
`runtime_versions`, and `visuals_enabled`. New native chats select runtime v2
only when advertised; older responses default to v1. Existing conversations keep
their runtime: v1 uses messages/actions, v2 uses turns/deltas/approvals. Deploy
the additive capabilities API before the native release. No new feature flags
or migrations are required.

Creation attachments keep three roles separate: primary footage uses thread
media uploads, narration uses the same contract with `kind=audio`, and supporting
visuals use the existing PlanItem asset-pool reservation/registration routes.
Visuals are offered when the server advertises the existing autoplace or guided
edit capability. Voice recording is available for Narrated and requires both
microphone permission and cloud-upload consent. Legacy upload recovery records
without a role remain primary clips. Pending records block generation, and failed
attachments retry without uploading the original again.

The Debug API URL must escape the second slash (`http:/$()/localhost:8000`) in
xcconfig. `make ios-verify` checks the resolved URL, while allowing a complete
custom URL in the ignored `Config/Local.xcconfig`.

For deterministic UI verification, launch Debug with `-ui-testing-chat` and
`KRIA_CHAT_CREATION_FLOW=v1` or `v2`. `KRIA_CHAT_FIXTURE_MEDIA=1` starts format
selection with a fixture clip already attached so confirmation/render polling can
be tested without an account. These fixtures intercept HTTP and do not prove a
live render. Without the flow variable, the transport remains unavailable to
exercise connection recovery. `CreationFlowTests` covers the native wire
contracts, built-app posters, and recovery-record compatibility.

Native projections must treat `creator_agent.status` and `state.generation` as
authoritative before `active_job_id` exists: `executing` stays in preparation
and `failed` retains the direction and retry. The current Creator phase wins
over a previous generation receipt. Poll responses use request ordering as
well as revisions because reconciliation can change a projection without
appending an event. `testSlowDirectionAndPreJobFailureNeverReturnToUploading`
exercises the delayed response, pre-job failure, retry and ready transition.

### Change-based CI

The iOS workflow always reports `build-and-test`. The shared
[CI selector](change-based-ci.md) schedules the macOS build only for affected PRs;
every push to main runs the full iOS gate. Unrelated PRs report an explicit
not-applicable result through a small Ubuntu gate without allocating a Mac.

The selected Mac job has separate **Build and unit tests** and **Native UI tests**
steps, sharing one compiled build and simulator. PR changes confined to unit
fixtures/tests or the generated OpenAPI client run the fast phase only; backend
contract changes also compile/test the native client without rerunning the
fixture-driven UI suite. Native app, design-system, media-engine, resource,
UI-test and unknown/shared changes retain UI coverage. Fonts and type-poster
assets under the web public directory are also native build inputs.

`make ios-verify` still runs the full local gate. When local sessions share a
Mac, set `KRIA_SIMULATOR_ID` to a dedicated available iPhone simulator to avoid
one session reinstalling the app during another session’s tests. Invalid IDs
fail before building; CI retains its automatic device selection. For targeted verification:

```sh
KRIA_IOS_TEST_MODE=unit bash scripts/ios/verify.sh
# CI splits full coverage without compiling twice:
KRIA_IOS_TEST_MODE=prepare-ui bash scripts/ios/verify.sh
KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS=full bash scripts/ios/verify.sh
```

`prepare-ui` builds both test bundles and runs unit tests. The unit phase
excludes `KriaUITests` rather than whitelisting a single unit target, so future
non-UI targets remain covered. Xcode still controls the build dependency graph. `ui` runs only the UI
bundle and requires a one-use receipt from the successful preparation, matching
all current native/shared input contents and generated project files. Changed
inputs or a failed preparation require a fresh build. Receipts are not cached.

### Xcode cache reuse

GitHub caches from PR runs belong to `refs/pull/<number>/merge`; other PRs cannot
restore them. PRs can restore the default/base branch cache. The first main cache
from PR #1004 was saved at 10:42 UTC on 2026-09-10, after PR #1005 started at
10:25, explaining its cold build despite a prior PR cache. See GitHub's
[cache access restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).

Both API-URL build-setting queries use the same cached DerivedData directory as
compilation. Previously these queries resolved packages through Xcode's default,
uncached directory before the actual cached build began.

The main push gate seeds the shared cache. CI now saves the compiled build as
soon as unit tests pass, before UI execution, so this seed is available earlier.
Cache hit/miss and the restored key are shown in the job summary. The key still
isolates Xcode, runner architecture and project/package configuration; a cache
hit never bypasses compilation or selected tests.

Fresh checkouts reset source modification times. `cache-inputs.py` stores hashes
and nanosecond timestamps inside the existing cached `Build/` directory. After
project generation, `KRIA_RESTORE_INPUT_TIMES=1` restores timestamps only for
byte-identical current files, including shared resources and generated project
files. Changed, added or deleted inputs retain normal Xcode invalidation. Missing
or corrupt manifests act as cache misses; paths come from the current checkout,
not the cached manifest. Standalone developer runs do not restore times by default.

Run cache/phase regression tests with `python3 -m unittest discover -s
scripts/ios/tests -v`. To disable timestamp restoration, remove
`KRIA_RESTORE_INPUT_TIMES` from the CI build step; to restore full PR UI coverage,
select `ios_ui=true` whenever `ios=true`. Neither rollback should skip compilation
or relax `build-and-test`.

Local verification of the timestamp restore on 2026-09-10 reset all 126 native
and shared input mtimes to simulate a fresh checkout, then ran the `unit` mode
against the existing build. It passed in 37.8 seconds; repeating with the cached build-setting queries passed
in 32.1 seconds. Both runs had zero `SwiftCompile`,
`CompileC` or `SwiftEmitModule` actions. This is local incremental-build evidence,
not a promised GitHub-hosted duration; the first cache without a timestamp
manifest still needs to establish one.

### Focused UI coverage (KRI-27)

PRs with changes confined to one reviewed native feature run that feature's UI
checks plus four cross-app smoke scenarios. The manifest in
`scripts/ios/ui-test-groups.json` lists exact source paths and XCTest identifiers.
The groups are `creation`, `projects`, and `editor`; `smoke` covers workspace
navigation, creation through ready, editor back navigation, and playback.
Shared workspace/app state, services/models, design-system files, media-engine
code, resources, project configuration, test infrastructure, and unknown paths
require the full suite. Changes touching more than one feature also run full.
Project implementation currently lives in shared workspace/state files, so those
changes run full; project-test-only edits can select `smoke,projects`.

All main pushes run full coverage. Existing unit-only rules for generated API
clients, backend contracts, and unit-test-only edits are unchanged. The selector
retains `ios` and `ios_ui`, adding `ios_ui_groups` (`none`, `full`, or exactly
`smoke,creation`, `smoke,projects`, or `smoke,editor`). The required `build-and-test`
gate rejects missing or inconsistent group outputs. Unknown or stale test
inventory forces full selection; the manifest guard also requires new tests to
be explicitly classified before the PR can pass.

UI tests remain serial. The preparation builds both test bundles, executes all
non-UI tests, and writes the existing one-use receipt. Focused runs reuse that
build and reject changed native inputs. Selection is validated before invoking
Xcode, and the actual result bundle must contain every expected UI test with a
passing outcome. Zero executed tests, skipped selected tests, or unexpected UI
tests fail the gate. `make ios-verify` runs the complete unit and UI phases with
separate result bundles.

```sh
KRIA_SIMULATOR_ID=<dedicated-iphone-uuid> KRIA_IOS_TEST_MODE=prepare-ui bash scripts/ios/verify.sh
KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS=smoke,creation bash scripts/ios/verify.sh
```

Use a fresh `prepare-ui` before each UI invocation. UI-only mode requires an explicit group;
missing, empty, or malformed selections fail. The full local gate supplies
`full` automatically, and CI supplies the selector output. To roll back selection without weakening required checks,
return `full` whenever `ios_ui=true` (and retain `none` when UI is omitted).

Each invocation writes logs, phase timings, and distinct unit/UI `.xcresult`
bundles under `test-results/ios/`. GitHub uploads these artifacts on success or
failure with seven-day retention. The selection job explains its decision; the
iOS job reports cache restoration and phase durations. Simulator boot overlaps
compilation, so those durations must not be added together.

#### Baseline and interpretation

[PR #1007's iOS job](https://github.com/emirerben/nova/actions/runs/34480682289/job/102882556131)
ran for 24m 53s on 2026-09-10. Cache restoration succeeded and restored timestamps
for 111 matching inputs. The build/unit step took 10m 59s, but 167 unit tests
(one intentionally skipped) executed in 4.6 seconds. The unit runner resolved
packages by 13:16:21 UTC, then emitted its first app logs near 13:21:19; this
roughly five-minute gap occurs before test execution, not in slow assertions.
The original console log does not establish why runner startup stalled. Keep
result bundles to diagnose any recurrence rather than claiming a startup fix.

The UI phase took 11m 24s, including 22 tests executing in 9m 53s. Focused groups
select 9 tests for creation, 6 for projects, and 15 for editor after smoke
deduplication. Applying #1007's per-test durations to those sets estimates test
execution of about 4m 29s, 3m 13s, and 6m 29s respectively. These are selection-only
projections, not measured CI runtimes; setup, compilation, runner startup, and
queueing remain. Broad changes like #1007 still select all 22 tests.

The earlier 12–17-minute estimate for focused iOS checks assumed an additional
2–4-minute startup/build improvement that this change does not claim.

For a GitHub-hosted comparison on one runner, manually dispatch the iOS workflow
with `benchmark_ui_group=creation` (or `projects`/`editor`). This always runs the
full gate first, then a fresh preparation and the selected focused UI sample;
it does not change required coverage or add work to ordinary PR checks. Compare
the two UI phase timings, not the combined benchmark-job duration.

Local validation on 2026-09-10 (iPhone 17e simulator, iOS 26.5) passed all 22 UI
tests, confirmed against the result bundle, in a 375-second UI phase. The unit
phase took 10 seconds including runner startup; its 167 tests (one existing skip)
took 0.63 seconds. Build-setting checks took 18 seconds and compilation 38 seconds.
This host did not reproduce the CI startup stall; these numbers do not establish
GitHub-hosted savings.

On the same local simulator, `smoke,creation` passed all nine selected tests in
166 seconds, versus the 375-second full UI phase (209 seconds / 56% less UI-phase
time). Its fresh preparation used 11 seconds for settings, 5 seconds for the
incremental build, and 6 seconds for unit execution. This is a local serial sample,
not a controlled GitHub-runner benchmark.
