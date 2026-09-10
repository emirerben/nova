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
