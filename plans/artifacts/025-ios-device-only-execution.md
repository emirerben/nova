# Plan 025 execution record — 2026-10-02

Status: **Automated qualification passed; signed-device and production gates pending.**
Plan 025 remains **IN PROGRESS**; KRI-256 is not complete. Its Linear status
was left unchanged. No production rollout was performed.

## Scope and baseline

Executed KRI-256 in the user-selected existing worktree
`/Users/emirerben/Projects/nova-ios-device-rollout-test-plan`, branch
`codex/ios-device-rollout-test-plan`. Preserved the pre-existing plan/index edits.
Fetched main and fast-forwarded `b93328d88` → `9d7f5fe71`; only mobile CI/release
metadata changed across that interval, with no drift in the referenced runtime.
Read KRI-256, its implementation children, and KRI-241 with linked dependencies.
No Linear records, production flags, deployments, accounts or user media changed.

The API effective-settings read confirmed protocol floor 2, admission still
hybrid, cloud execution enabled, all phone format/narration gates enabled, and
30 verified capabilities. Multi-clip Talking and caption editing are both on;
the plan's default-off assumptions are not the current production profile.
Only flag/capability names were retained in test code. Worker profile parity is
still an operational verification gate.

## Implemented automated gaps

- A1/A2: video-embedded source audio now has a regression covering caption,
  title, style, original/clip volume, saved export and a full-rebuild trim.
  Visual-only updates preserve the running audio mix. Gain updates bind mix
  parameters to the actual composition tracks.
- A3: a server-compiled `talking_head` native fixture tests cutaway pixels,
  distinct speaker/cutaway tones, caption on/off windows, and capability
  negotiation. Required execution is separate from the default unit run.
- A4: a Debug-only transport delivers a real typed 426 through the production
  API parser/state; XCUITests cover all root routes, accessibility scrolling,
  and App Store/support actions through a deterministic URL seam.
- A5: the production-profile matrix tests picker discovery, media-shape
  availability, real dispatch and real worker recipe publication, including
  editor/Talking toggles, guided voiceover and runtime-v2 unified montage.
- Runbook: format-specific signed-device canaries are hard hold points;
  Release 1's 24-hour acceptance precedes Release 2 topology reduction.

## Automated evidence before ship integration

| Check | Result | Local artifact |
|---|---|---|
| Baseline admission / cloud fences / rollout / format matrix | 176 passed | `/tmp/plan025-backend-baseline.log` |
| Compiler / speech / completion / runtime suites | 289 passed | `/tmp/plan025-backend-formats.log` |
| Dispatch helper regression selection | 27 passed, 107 deselected | `/tmp/plan025-dispatch.log` |
| Production-profile matrix and final capability/dispatch regressions | 261 passed, including all 61 profile cases | `/tmp/plan025-backend-focused-final.log` |
| Generated contracts / mobile OpenAPI | Match checked-in outputs | `/tmp/plan025-contracts.log` |
| Final KriaMediaEngine full package | 249 executed, 4 external-reference skips, 0 failures; audio parity included | `/tmp/plan025-media-engine-final.log` |
| Embedded-audio regression | Red: three mix-identity assertions; green: 6 focused audio tests | `/tmp/kri241-red.log`, `/tmp/kri241-final.log` |
| Audio parity | 3 synthetic speech/mix renders; native/cloud loudness differences +0.1, -0.2, +0.4 LU; comparison passed | `/private/tmp/plan025-audio-parity/report.json` |
| Final iOS unit phase | 858 executed, 16 explained skips, 0 failures | Worktree `test-results/ios/full.ACqJW6/unit.xcresult` |
| Final full backend suite | **18,442 passed, 30 skipped, 3 xfailed; exit 0** | `/tmp/plan025-backend-qualified.log` |
| Final full iOS UI suite, dedicated iPhone 17 Pro Max | **110 passed, 0 failed, 0 skipped, no retries; complete gate exit 0** | Worktree `test-results/ios/full.ACqJW6/ui.xcresult`, `/tmp/plan025-ios-qualified.log` |
| Earlier full iOS UI suite before final fixes | 108 passed, 2 failed; 1 pass required retry | Worktree `test-results/ios/full.lCkbrF/ui.xcresult` |
| Final-build UI follow-up, second simulator, no retries | 13 passed, 3 failed; both new blocking-update cases passed | `/private/tmp/plan025-ui-followup.xcresult` |
| Final UI test corrections | Panel expansion passed; chat visible-tap correction passed all 3 explicit repetitions | `/private/tmp/plan025-panel-fixed.xcresult`, `/private/tmp/plan025-chat-visible-tap.xcresult` |
| Text creation / animation rerun | Passed with simulator stable | `/private/tmp/plan025-text-animation-final.xcresult` |
| Native montage/speech renders | All 5 required active cases passed after fixes; 3 KRI-220 retired cases skipped | `/private/tmp/plan025-montage-fixed.xcresult` |
| Guided voiceover / Visuals native renders | All 7 passed after exporter fix | `/private/tmp/plan025-photo-fixed.xcresult` |
| Final preship, documentation size and diff checks | Passed | `/tmp/plan025-preship-complete.log` |

The initial Swift app compile caught an async expression inside `XCTUnwrap` in
the new audio helper. It was corrected before the successful build/unit phase.

### Skip accounting

The final package run includes audio parity. Its four remaining skips are one
moving-transition reference test and three golden-hour production-reference
tests. They need external production-rendered reference fixtures and are not
evidence for Plan 025's speech cases.

The default app unit run skipped fifteen render-fixture cases because they are
run separately with `KRIA_E2E_DIR`, and one Keychain-entitlement test because the
simulator host is unsigned. Required native render cases must pass explicitly;
these default skips do not waive them. Three old voiceover-less montage cases
were retired by KRI-220; their replacement is the unified montage planner/worker
contract plus the guided native Visuals/crossfade render path. Signed Keychain
behavior remains part of TestFlight qualification.

## Failures discovered during qualification

1. Native Talking derives `alphaOverlay` for the cutaway overlay track, while
   the server initially omitted it from the recipe and Talking gate. Production
   already verifies the feature, but incomplete server negotiation is unsafe.
   The compiler declaration and admission gate now both require `alphaOverlay`;
   focused backend and native negotiation checks pass.
2. The narrated slowed-footage case exports 10.129292 seconds for a 10-second
   recipe. Video is exactly 10 seconds; audio beyond the end is audible, so this
   is not merely a tolerance issue. The reader and writer session now end at recipe duration. Native rerun passes:
   video 10.000000 seconds, audio 9.998938 seconds; no audible overrun remains.
   A retimed-footage/narration package regression also pins the endpoint.

## Broader baseline limits

Full backend Ruff lint passes. Full formatting currently reports 106 files;
every one is byte-identical to `HEAD` (verified against Git), so none was
introduced by this change. Scoped formatting/preship passes. Reformatting the
entire unrelated backend was intentionally left out of Plan 025.

The full backend run had eight failures: four slide-post tests required
`drawtext`, absent from `/opt/homebrew/bin/ffmpeg`; all four pass with the
already-installed `/opt/homebrew/Cellar/ffmpeg-full/8.1.1/bin` first in `PATH`.
The other four involve draft retention, development budget accounting, and two
maintenance tests reaching cloud credential discovery from retained test rows.
All four pass against a freshly migrated, isolated local database
(`nova_plan025_qualification_test`). The affected production/test files are
unchanged from `HEAD`. The first isolated-database full rerun had 18,440 passes and two setup errors:
Apple revocation tests allow only `nova_test`/`kri55_test`. Those two passed on
the approved shared test database. A final complete run used a temporary
localhost-only PostgreSQL instance containing a fresh `nova_test`, plus the
full FFmpeg build: **18,442 passed, 30 skipped, 3 xfailed, exit 0**. The temporary
server was stopped afterward. No shared database was reset.

The initial full UI gate was **red**. Its completed result reports 97 passed and 11
failed among 108 executed cases; three passes required retries. Nine failures
were reported as tests killed by a signal. The running build predated the two
new update tests; inventory validation correctly rejected those missing cases.

A no-retry follow-up ran all 11 failed, 3 flaky, and 2 missing cases on the
second simulator against the final build: **13 passed, 3 failed**. Both new 426
journeys passed: all seven root routes are blocked, and accessibility scrolling
plus App Store/support actions work. The preview-resize, timeline, quick-add,
and drawer cases that were killed or flaky in the broad run mostly passed.

The three follow-up failures were investigated:

- `CreationUITests.testIncomingResponseDoesNotPullReaderFromScrolledHistory`:
  an enabled/hittable wait alone still failed 2/3 repetitions. Captured tap
  coordinates and screenshots established that XCTest tapped the card center
  beneath the persistent composer, even though the upper card was visible.
  The test now computes the card's visible intersection above the composer and
  taps that area. All response and scroll assertions remain intact. Three-repeat
  final verification **passed all three iterations**, without retry-on-failure. Evidence: `/tmp/plan025-chat-failed-tap.png`.
- `NativeEditorInspectorUITests.testSourcePreviewTextCreationAndAnimations`:
  the focused result reported signal TERM. Exported test-daemon and simulator
  logs show the entire simulator shut down at 12:34:01 local time, terminating
  its system services and daemon; this is not an isolated app crash. The cause of
  the simulator shutdown is unknown. The single-test stable rerun **passed**.
  Evidence: `/tmp/plan025-simulator-shutdown.log`,
  `/tmp/plan025-runner-termination.log`.
- `NativeEditorInspectorUITests.testVisiblePanelHandleExpandsAndRestoresEveryTool`:
  the panel did expand, but a hard-coded 160-point drag did not reach the preview
  on the taller iPhone 17 Pro Max. The test now derives drag distance from the
  measured panel/preview gap and keeps all original layout assertions. Its
  no-retry final rerun **passed** for all four tools.

Those corrections changed tests only. At that point the complete gate was still
red, so qualification continued through the final clean full run recorded below.
No full-suite waiver was used.

## Earlier UI gate — resolved by the continued remediation below

A complete second `make ios-verify` used a newly created, dedicated iPhone 17 Pro
Max simulator. Its unit phase executed 857 tests with 16 explained skips and no failures. UI
inventory is complete: **108 passed, 2 failed, 0 skipped**, including both new
update-screen cases. One of the passes required a retry. There were no simulator
shutdowns in this run. That gate was red:

- `testLibraryPickerReturnsToChatByDoneAndBySinglePick` failed all three attempts.
  The old predicate checked that the host button was absent, which is already
  true while its Photos picker overlays it. The test now waits for both the
  picker toolbar and host sheet to disappear within the same 10-second budget;
  that focused verification still **failed**: the toolbar remained after the
  full wait. Subsequent coordinate evidence established that the test had
  missed the video tile; the continued remediation below resolves it. No production behavior changed. Evidence:
  `/private/tmp/plan025-picker-final.xcresult`, `/tmp/plan025-picker-final.log`.
- `testSongReferenceBarKeepsTimelineAndToolsOnScreen` failed all three attempts:
  preview height is **236 points**, below the **239-point** assertion on the
  larger phone. The subsequent production layout fix reserves the required
  quarter-screen floor; the original UI assertion is unchanged and now passes.
- `testLongPressCopiesPromptsAndRepliesWithoutBlockingOptionTaps` passed only
  on retry. Its pasted-text assertion failed on the first
  attempt (the Copy menu did close). The captured values exactly match the
  separate `ChatMessageCopyTests` unit fixture: `End on the sunset.` and
  `Should the edit end on the sunset?`. Concurrent simulator runs and enabled
  Simulator pasteboard synchronization were confirmed. Five isolated explicit
  repetitions then passed without code changes (`/private/tmp/plan025-copy-red.xcresult`).
  The user authorized temporarily disabling automatic pasteboard synchronization
  for the final qualification run. The Copy test passed its first attempt
  with synchronization off. The original enabled setting was restored and
  verified after the complete run.

The earlier corrected chat tap, corrected panel gesture, text-animation test,
and all other cases passed within this earlier complete run. The final complete
run below is clean and supersedes these failures.

## Evidence limits and manual gates

The KRI-241 regression uses a measurable tone inside a video. Offline decoding
was audible even before the fix; the failing assertions demonstrate unnecessary
live-mix replacement, not reproduction of the physical iPhone silence. KRI-241
remains open until real speech survives unsaved/saved edits on a physical phone.

Exploratory audio checks found pre-existing partial-mute leakage in both fast
and full composition paths. Main fixed that separately in PR #1318, included
in the ship integration below; its envelope regression passes in the integrated
engine suite. Source review also found cached ducking after narration/SFX gain
edits, which was not introduced by this change and still needs qualification
for admitted journeys promising that behavior. Whole-clip muted cutaways are
tested independently. No waiver is implied.

Do not proceed to TestFlight or production from this implementation session.
The complete backend and iOS gates now pass. Signed-device and operational
qualification remain the hold points.

Remaining operational gates: signed builds on oldest/current phones; D0–D12;
D2 on all four active accounts; physical KRI-241 evidence (or an explicitly
accepted disabled-editor limitation); worker/API profile agreement; admission
receipts; broker/database drain; post-disable D3/D7; 24-hour acceptance with no
STOP condition. Plan 025 and KRI-256 must not be marked DONE from automation alone.

## Continued remediation

The user requested continued fixes after the initial NO GO report. The song-bar
layout now reserves the required quarter-window preview height, with a focused
metrics regression and unchanged no-song behavior. All 16 layout unit tests
and the existing song-reference UI test pass in
`/private/tmp/plan025-picker-translated.xcresult`.

The picker failure was a remote-view coordinate mismatch: the Photos host begins
at y=146, but its video image reports local y=82. The old test tapped the local
center at y=154; translating through the host taps the actual tile at y=300.
The original automatic-dismissal assertions now pass. The selected asset is
also verified by name in the offline fixture’s upload-failure list; its Dismiss
action works and Photos remains available to choose again. This fixture does
not implement upload reservations, so successful upload is covered elsewhere.
The final focused picker run passed (`/private/tmp/plan025-picker-retention-green.xcresult`).
All temporary `[DEBUG-picker256]` diagnostics were removed.

The complete final `make ios-verify` passed on the dedicated iPhone 17 Pro Max:
**858 unit tests executed (16 explained skips), 110 UI tests passed, no failures,
no UI skips or retries, exit 0**. The inventory verifier confirmed all 110 cases.
Both blocking-update tests, the corrected picker, Copy, and song-reference
layout passed on their first attempts.

Evidence: `test-results/ios/full.ACqJW6/{unit,ui}.xcresult` and
`/tmp/plan025-ios-qualified.log`. The iOS source fingerprint stayed unchanged
throughout the full run:
`623203d977bf494496fc0481d7ff28c483c307797ea949b08de5ef6f6bbef4a1`.
Simulator pasteboard synchronization was temporarily disabled with user approval
and restored afterward. The dedicated simulator was shut down after testing.
The existing backend, engine, native-render, contract, and preship evidence still
applies; backend/render code did not change during this continued remediation.


## Ship integration and review

The ship pass fast-forwarded the preserved work to `origin/main@1c3415df1`.
This includes the newer editor deletion, keyboard-panel, and audio-envelope
fixes. The earlier evidence above describes the pre-integration build; final
integrated checks are being recorded separately here.

Six specialist lenses covered testing, maintainability, security, performance,
API contracts, and simplification. The two testing gaps were closed with
focused music-backed audio-mix preservation/rejection and keyboard-visible
song-reference layout regressions. A fresh adversarial review retracted an
initial music-topology concern after confirming the existing equality guard
rejects that change before entering the fast path. No production-code findings
remain. Adversarial fixture review was summary-only; the testing audit read
the tests. Coverage is a manual path assessment, not instrumented coverage.

The repository's post-merge workflow owns release metadata; this PR does not
edit `VERSION`, `CHANGELOG.md`, or root package versions. This remains an
automated qualification PR, without signed-device or production acceptance.

### Integrated verification results

| Check | Result | Local artifact |
|---|---|---|
| Full backend | **18,606 passed, 30 skipped, 3 xfailed; exit 0** | `/tmp/plan025-ship-backend-final.log` |
| iOS full unit phase | **891 executed, 16 explained skips, 0 failures** | `test-results/ios/full.ThYyxH/unit.xcresult` |
| Full iOS UI inventory | **111 passed, 0 failed, no skips or retries; exit 0** | `test-results/ios/full.ThYyxH/ui.xcresult`, `/tmp/plan025-ship-ios.log` |
| Full media-engine package | **250 executed, 4 external-reference skips, 0 failures** | `/tmp/plan025-ship-engine.log` |
| Review-added music regression and original-audio tests | **4 passed** | `/tmp/plan025-ship-audio-focused.log` |
| Final layout metrics, including review-added keyboard case | **17 passed** | `/private/tmp/plan025-ship-layout.xcresult` |
| Fresh server-compiled montage/speech native cases | **5 required cases passed; 3 explicitly retired cases skipped** | `/private/tmp/plan025-ship-montage.xcresult` |
| Fresh guided voiceover / Visuals native cases | **7 passed** | `/private/tmp/plan025-ship-photo.xcresult` |
| iOS selection/build scripts | **93 passed** | `/tmp/plan025-ship-scripts.log` |
| Generated contracts, scoped lint and preship | Passed | `/tmp/plan025-ship-contracts.log`, `/tmp/plan025-ship-preship.log` |

The first ship backend attempt had 17 connection failures and extra database
skips because the restarted disposable PostgreSQL cluster used its default
port while the tests targeted its isolated port. After correcting the launch
options and verifying `nova_test` on port 55397, the entire suite passed with
the expected skip count. The disposable cluster was stopped afterward.

No production source changed during the integrated runs. Review added only
two unit regressions, checked separately above; documentation edits caused the
evidence wrapper to omit its whole-tree fingerprint. The direct test results
and result bundles are the verification evidence. Simulator clipboard sync
was temporarily disabled under the existing user approval for the UI gate and
was restored after that gate.
