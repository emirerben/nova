# Kria iOS development

The native app lives in `src/apps/ios`. `project.yml` is the source of truth;
`Kria.xcodeproj` is generated and must not be edited by hand.

## Prerequisites

- Xcode with an iOS 18 simulator runtime
- XcodeGen 2.38 or newer (`brew install xcodegen`)

No Apple team or production bundle identifier is required for simulator builds.
Development defaults come from `Config/Development.xcconfig`; production signing
values belong in an untracked local override or CI secrets.

`Config/Development.xcconfig`'s default bundle identifier is `com.kria.app.dev`,
registered with its own Google iOS OAuth client
(`600393007296-ed1km1cdcteq54iui01rkv7tuuiu3g9m.apps.googleusercontent.com`,
GCP project `TravelX`), so "Continue with Google" works out of the box in plain
Debug/simulator builds. This is separate from the `Kria Live Development` scheme
below, which uses its own dedicated client bound to Yasin's personal bundle
identifier. The API allowlists mobile client IDs via `MOBILE_GOOGLE_CLIENT_IDS`
(`src/apps/api/app/config.py`); both iOS clients must be present in that list for
sign-in to work end to end.

## Real-account build for a collaborator

Use the `Kria Live Development` scheme to run the current source on an iPhone
with a real Kria account. It talks to the production API and uses a dedicated
Google OAuth client registered for Yasin's development bundle, while the app is
signed by his own Apple Personal Team. It does not require access to the Kria
Apple Developer account.

1. Clone the repository and install XcodeGen (`brew install xcodegen`). In Xcode,
   open **Settings > Accounts**, add your Apple ID, and select your Personal Team.
   Copy the 10-character Team ID.
2. Create the ignored signing configuration with the collaborator bundle ID
   registered to the dedicated Google iOS OAuth client:

   ```bash
   scripts/ios/configure-live-development.sh YOUR_TEAM_ID com.yasin.kria.dev
   make ios-generate
   open src/apps/ios/Kria.xcodeproj
   ```

3. In Xcode, select the **Kria Live Development** scheme and the connected
   iPhone, then run. If iOS asks, enable **Settings > Privacy & Security >
   Developer Mode**, restart the phone, and trust the developer identity under
   **Settings > General > VPN & Device Management**.
4. Tap **Continue with Google** and choose the Google address already used by
   the Kria web app. The callback returns to Kria, exchanges the Google identity
   token with `/auth/mobile/exchange`, and loads that account's existing projects.
   Relaunch once to confirm the Keychain session restores.

`com.yasin.kria.dev` is not interchangeable with another bundle identifier:
Google's installed-app OAuth client is bound to that exact value. A different
collaborator needs a separate iOS OAuth client, client ID, callback scheme, and
production API allowlist entry.

The scheme intentionally omits Sign in with Apple and its entitlement because a
free Personal Team cannot sign the production Kria App ID. The standard `Kria`
scheme remains the localhost/fixture workflow. To change the local team, rerun
the setup command with `--force`; do not change the bundle identifier. Never
commit `Config/Local.xcconfig`.

## Commands

```bash
make ios-generate  # regenerate Kria.xcodeproj
make ios-build     # unsigned simulator build
make ios-test      # generate, build, and run unit/UI tests
make ios-verify    # full local build, unit, and UI regression
```

Set `KRIA_SKIP_SIMULATOR_TESTS=1` only when validating compilation on a host
without an installed iPhone simulator. PR CI always compiles and runs unit tests.
The exhaustive native UI suite runs on main and manual dispatch; it is outside
the PR shipping gate. Run `make ios-verify` locally when validating native flows.

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
Gallery follows `/me/jobs` cursors until all pages are loaded, using the API's
60-row page limit and retaining server order while deduplicating job IDs. A
failed refresh preserves the prior library; an initial failure shows recovery
instead of substituting preview videos, including in Debug builds. For an
authenticated, count-only device check, launch Debug with
`-native-library-audit -native-library-inventory-audit` and inspect
`Library/Caches/native-library-audit.json`; this does not open videos or render.
Project actions reuse the authenticated creation-thread PATCH/DELETE contracts,
including revision checks and rename idempotency. Deletion is confirmed and
blocked during rendering or pending uploads. Microphone chat input is deferred.
Native system authentication, Photos, AI consent, and share sheets remain native. Agreement to share media with Kria's AI providers is given once per account (`AIConsentView` gates the workspace); there is no per-upload consent screen, only a short caption in the picker saying what is uploaded.

Native footage controls persist `playback_rate` and normalized `source_crop` in the editor document. Retiming keeps each timeline window fixed: slow motion consumes less source, while footage that ends early holds its last frame. Still images retain their placement duration, and held video tails do not stretch source audio. Crop coordinates use the decoded source with a top-left origin; rendering and selection geometry must agree for rotated footage. The controls participate in document undo and save.

Media direct manipulation freezes the surrounding composed layers while the gesture updates the selected image. Rebuilding the source preview waits until the gesture ends; captions must remain present in the surrounding layers.

Download follows the video currently shown in the editor. A ready source preview is exported locally from the current edit recipe; a matching device-local file is used directly, and a server-rendered result is downloaded only when the source preview is unavailable and the render receipt still matches the current project generation. While a source preview is preparing, the last finished render may remain visible, but canvas editing and download stay disabled until the displayed video is known to be current.

For repeatable visual review, a Debug build accepts `-ui-testing-brand` with
`KRIA_BRAND_STATE` set to `format`, `footage`, `direction`, `rendering`, `ready`,
`editor`, `projects`, `gallery`, `signin`, `account`, or `recovery`.
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
edit capability. Voice recording is available for Narrated and requires
microphone permission (the account's AI consent already covers voices). Legacy upload recovery records
without a role remain primary clips. Pending records block generation, and failed
attachments retry without uploading the original again.

The Debug API URL must escape the second slash (`http:/$()/localhost:8000`) in
xcconfig. `make ios-verify` checks the resolved URL, while allowing a complete
custom URL in the ignored `Config/Local.xcconfig`.

On a physical iPhone, `localhost` addresses the phone, not the development Mac.
An effects-only pilot can pass with this URL because it never calls the API;
the normal editor will then fail with "could not connect to the server". For a
user-facing device test against production, pass
`API_BASE_URL=https://nova-video.fly.dev` to `xcodebuild` and verify the built
app's `KriaAPIBaseURL` in `Info.plist` before installing. Use a reachable Mac URL
instead when testing a local backend. Relaunch without `-device-effects` or
`-ui-testing-*` flags for normal project editing. Preserve the API override when
rebuilding with `SWIFT_OPTIMIZATION_LEVEL=-O` for physical performance testing.

For deterministic UI verification, launch Debug with `-ui-testing-chat` and
`KRIA_CHAT_CREATION_FLOW=v1` or `v2`. `KRIA_CHAT_FIXTURE_MEDIA=1` starts format
selection with a fixture clip already attached so confirmation/render polling can
be tested without an account. These fixtures intercept HTTP and do not prove a
live render. Without the flow variable, every request returns HTTP 503 to
exercise server-error recovery. `CreationFlowTests` covers the native wire
contracts, built-app posters, and recovery-record compatibility.

Chat recovery cards name the cause of a failed request (`RequestFailureCause`).
Only transport failures (offline, timed out, connection lost) show "Connection
interrupted" with Reconnect. An HTTP 5xx means Kria was reached and shows
"Something went wrong" with the server-side explanation; other statuses use
neutral copy. `RequestFailureTests` covers the mapping. In the chat fixture,
`KRIA_CHAT_GENERATE_FAILURE=server_error` or `offline` fails the first "Create
this video" with an HTTP 500 or a dropped connection, and `CreationUITests`
checks both recovery cards.

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

Mobile contract generation checks and verification-script tests run on a parallel
Linux matrix leg. Both legs must pass the stable `build-and-test` gate, so Python
dependency installation does not delay Xcode.

The selected Mac job has separate **Build and unit tests** and **Native UI tests**
steps, sharing one compiled build and simulator. Every selected PR runs the build and
unit-test phase; the exhaustive native UI suite runs on main pushes and manual
dispatch. The selector still classifies native app, design-system, media-engine,
resource, UI-test, and unknown/shared changes as UI-affecting for reporting and
future policy changes. Fonts and type-poster assets under the web public
directory are also native build inputs.

`make ios-verify` still runs the full local gate. When local sessions share a
Mac, set `KRIA_SIMULATOR_ID` to a dedicated available iPhone simulator to avoid
one session reinstalling the app during another session’s tests. Invalid IDs
fail before building; CI retains its automatic device selection. For targeted verification:

```sh
KRIA_IOS_TEST_MODE=unit bash scripts/ios/verify.sh
# Main and manual CI split full coverage without compiling twice:
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
`KRIA_RESTORE_INPUT_TIMES` from the CI build step. To return PRs to build and
unit tests only, make the `ios.yml` build step select `unit` for every
`pull_request` and restrict the UI step to non-PR events; to run the full suite
on PRs instead, pass `full` through. Neither change should skip compilation or
relax `build-and-test`.

Local verification of the timestamp restore on 2026-09-10 reset all 126 native
and shared input mtimes to simulate a fresh checkout, then ran the `unit` mode
against the existing build. It passed in 37.8 seconds; repeating with the cached build-setting queries passed
in 32.1 seconds. Both runs had zero `SwiftCompile`,
`CompileC` or `SwiftEmitModule` actions. This is local incremental-build evidence,
not a promised GitHub-hosted duration; the first cache without a timestamp
manifest still needs to establish one.

### Focused UI coverage (KRI-27, restored for PRs by KRI-117)

The focused-UI policy runs a reviewed native feature's UI checks plus four
cross-app smoke scenarios. The manifest in
`scripts/ios/ui-test-groups.json` lists exact source paths and XCTest identifiers.
The groups are `creation`, `projects`, and `editor`; `smoke` covers workspace
navigation, creation through ready, editor back navigation, and playback.
Shared workspace/app state, services/models, design-system files, media-engine
code, resources, project configuration, test infrastructure, and unknown paths
select `full`. Changes touching more than one feature also select `full`.
The chat-components file also owned project navigation,
the chat transport served project fixtures, and `EditorViews.swift` contained
gallery results; those shared files deliberately had no focused mapping. Project
implementation lived in shared workspace/state files, so those changes ran full;
project-test-only edits could select `smoke,projects`.

All main pushes run full coverage. PRs run build and unit tests, and a PR with
`ios_ui=true` also runs a bounded native UI subset before merge: the selector's
`smoke,<group>` when exactly one feature group changed, or `smoke` alone when the
selector reports `full` (the full suite itself stays post-merge, so PR cost is
bounded by the largest single group). This exists because a PR that was green on
build and unit tests broke creation-to-ready for every later merge (#1088); the
smoke group contains that scenario. `smoke` is an execution-only value: the
selector never emits it and the gate rejects it as a selector output.
The selector retains `ios` and `ios_ui`, adding `ios_ui_groups` (`none`, `full`, or exactly
`smoke,creation`, `smoke,projects`, or `smoke,editor`). The required `build-and-test`
gate still rejects missing or inconsistent group outputs on every run.
Unknown or stale test inventory forces full selection in the selector; the
manifest guard also requires new tests to be explicitly classified before the
PR can pass.

UI tests remain serial. The preparation builds both test bundles, executes all
non-UI tests, and writes the existing one-use receipt. Main and manual UI runs
reuse that build and reject changed native inputs. Selection is validated before
invoking Xcode, and the actual result bundle must contain every expected UI test
with a passing outcome; a test that fails and then passes on retry still counts
as passing (see "Flaky UI tests" below). Zero executed tests, skipped selected
tests, or unexpected UI tests fail the gate. `make ios-verify` runs the complete
unit and UI phases with separate result bundles.

```sh
KRIA_SIMULATOR_ID=<dedicated-iphone-uuid> KRIA_IOS_TEST_MODE=prepare-ui bash scripts/ios/verify.sh
KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS=smoke,creation bash scripts/ios/verify.sh
```

Use a fresh `prepare-ui` before each UI invocation. UI-only mode requires an explicit group;
missing, empty, or malformed selections fail. The full local gate supplies
`full` automatically, and manual CI supplies the full group after preparation.
PR CI invokes the UI phase only when the selector reports `ios_ui=true`, with
the bounded subset described above.

Each invocation writes logs, phase timings, and distinct unit/UI `.xcresult`
bundles under `test-results/ios/`. GitHub uploads these artifacts on success or
failure with seven-day retention. The selection job explains its decision; the
iOS job reports cache restoration and phase durations. Simulator boot overlaps
compilation, so those durations must not be added together.

### Flaky UI tests

The nine red `main` runs investigated in KRI-117 were all timing flakes in the
serial UI suite, not regressions from the merged PRs, so the UI phase in
`run_ui` (`verify.sh`) passes `-retry-tests-on-failure -test-iterations 3`
to `xcodebuild`: a failing test gets up to two more attempts in the same
invocation before it counts as failed. Only the UI phase retries; the unit
phase and the `build-for-testing`/`prepare-ui` compile step never do, so a real
build break still fails fast. A test still failing after all 3 attempts fails
the gate exactly as before.

`run_ui` captures the `xcodebuild` pipeline's exit status without tripping
`set -euo pipefail`, then always runs `ui_tests.py verify` when a result bundle
exists, and only reports success when both `xcodebuild` and `verify` were
green. This is a deliberate control-flow fix: without it, a red `xcodebuild`
exited the script before `verify` ran, so the coverage line and the flaky
report below were never produced on exactly the runs where they mattered.

`ui_tests.py verify` reads the `.xcresult` rollup per test (Passed once a
retry passes; Xcode stops retrying at the first pass) and separately flags any test whose rollup passed but needed
more than one attempt as flaky, via `flaky_tests()`. Each flaky test surfaces
in three places: a `::warning` GitHub annotation on the run, a
`## Flaky UI tests (passed on retry)` section in the job summary, and
`flaky-tests.json` (a JSON list, empty when nothing was flaky) written next to
`ui.xcresult` inside the `ios-native-test-diagnostics` artifact. Retries keep
`main` green; they are a safety net, not the fix. Every flaky warning still
needs its own Linear issue to find and fix the underlying flake.

Local repro, at a much higher iteration count than CI so the failure actually
reproduces:

```sh
xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation \
  -derivedDataPath src/apps/ios/.derived-data CODE_SIGNING_ALLOWED=NO \
  -destination "platform=iOS Simulator,id=<simulator-udid>" \
  -only-testing:KriaUITests/<Class>/<method> \
  -test-iterations 20 -run-tests-until-failure test-without-building
```

`cancelled` iOS runs on `main` are concurrency coalescing, not failures: the
workflow's `ios-${{ github.ref }}` concurrency group keeps one run going plus
one pending per ref, so a burst of pushes to `main` cancels the queued (not
yet started) runs in between and only the newest tip actually executes.

#### Baseline and interpretation

[PR #1007's iOS job](https://github.com/emirerben/nova/actions/runs/34480682289/job/102882556131)
ran for 24m 53s on 2026-09-10. Cache restoration succeeded and restored timestamps
for 111 matching inputs. The build/unit step took 10m 59s, but 167 unit tests
(one intentionally skipped) executed in 4.6 seconds. The unit runner resolved
packages by 13:16:21 UTC, then emitted its first app logs near 13:21:19; this
roughly five-minute gap occurs before test execution, not in slow assertions.
The original console log does not establish why runner startup stalled. Keep
result bundles to diagnose any recurrence rather than claiming a startup fix.

The UI phase took 11m 24s, including 22 tests executing in 9m 53s. Under the
former focused policy, groups selected 9 tests for creation, 6 for projects, and
15 for editor after smoke deduplication. Applying #1007's per-test durations to
those sets estimated test execution of about 4m 29s, 3m 13s, and 6m 29s
respectively. Those were selection-only projections, not measured CI runtimes;
setup, compilation, runner startup, and queueing remained. Broad changes like
#1007 still selected all 22 tests.

The earlier 12–17-minute estimate for focused iOS checks assumed an additional
2–4-minute startup/build improvement that the former change did not claim. A
7–10-minute CI turnaround remains a target, not a measured guarantee.

For a GitHub-hosted comparison on one runner, the workflow could be manually
dispatched with `benchmark_ui_group=creation` (or `projects`/`editor`). That
benchmark ran the full gate first, then a fresh preparation and the selected
focused UI sample; it did not change required coverage or add work to ordinary
PR checks. The relevant comparison was between UI phase timings, not the
combined benchmark-job duration.

Local validation on 2026-09-10 (iPhone 17e simulator, iOS 26.5) passed all 22 UI
tests, confirmed against the result bundle, in a 375-second UI phase. The unit
phase took 10 seconds including runner startup; its 167 tests (one existing skip)
took 0.63 seconds. Build-setting checks took 18 seconds and compilation 38 seconds.
This host did not reproduce the CI startup stall; these numbers do not establish
GitHub-hosted savings.

On the same local simulator, `smoke,creation` passed all nine selected tests in
166 seconds, versus the 375-second full UI phase (209 seconds / 56% less UI-phase
time). Its fresh preparation used 11 seconds for settings, 5 seconds for the
incremental build, and 6 seconds for unit execution. This was a local serial
sample, not a controlled GitHub-runner benchmark. The local `smoke,editor` run
also verified all 15 selected tests passed in 229 seconds (146 seconds / 39%
less UI-phase time than the full run).

## Native editor source assets

`GET /generative-jobs/{job_id}/variants/{variant_id}/timeline` exposes additive
fields for source-based native preview. The existing ownership checks and public
speech-generation projection still apply.

- `base_generation` binds the response to the variant's current render baseline.
- Each clip's `native_source` identifies its original media. Cloud originals
  have a short-lived `source_url`; analysis proxies instead expose a verified
  original descriptor with `local_required: true`, so the phone resolves its own
  original. A missing or ambiguous binding produces `native_source: null`.
- `signed_url` retains the web preview contract, including browser-compatible
  image derivatives. Native clients must use the separate original source.
- `native_assets` lists existing SFX, media-overlay, motion-scene, and visual-block
  resources attached to the public variant. Resources retain their lane IDs;
  image overlays carry `preserve_alpha` from the renderer's current alpha flag.
  A signing failure omits that resource instead of returning a stale URL.

Clients must compare generations before installing a composition and surface
missing resources rather than silently omitting them. This API change does not
open native rendering capability gates or change cloud render behavior.

Validation: `pytest tests/routes/test_native_timeline_sources.py
 tests/routes/test_generative_timeline.py tests/routes/test_generative_jobs.py
 tests/routes/test_editor_commit.py` from `src/apps/api`.
