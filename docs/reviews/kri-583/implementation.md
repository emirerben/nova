# KRI-583 implementation and evidence

Ticket: https://linear.app/kria/issue/KRI-583/iphone-record-talking-videos-with-a-steady-scroll-teleprompter
Branch: `codex/kri-583-iphone-teleprompter`
Base: `b33471508f87cdf622ebcdcf301a158a57b1203b`
QA status: `DONE_WITH_CONCERNS` — native window/capture checks passed; current
Kria component alignment and remaining release checks are listed below.

## Creator flow

Talking edit → Add footage → Record with script → paste/edit script → Open camera.
The front-camera preview defaults to a 144-point script window below Close.
Transparent is the default background; Text window settings also offer Glass
and opaque Solid. A top-left grip moves the window and a lower-right grip
resizes it. Tapping either grip opens the accessible settings for background,
width, height, position and Reset. Reset keeps the selected background.
Placement is bounded above the measured capture controls. Dynamic Type keeps
enough height for a reading line whenever screen space permits. Position,
dimensions and appearance save with the edit's script; older drafts safely
default to the new layout. Moving/resizing works before and during capture
without pausing the text; dragging the script body still pauses/repositions it.
Creators can rehearse, set approximate pace from 60–240 words/min, resize text,
pause or reposition text, and record after a cancellable three-second countdown.
Text starts when file output acknowledges capture; reaching the script end does
not stop the camera. VoiceOver uses manual reading, and Reduce Motion defaults
to manual reading with an explicit steady-scroll option.

A completed or playable interrupted movie opens Review take. Retake retains a
previous take until the creator explicitly uses the candidate. Previous take
and Discard are explicit file-removal actions. Script drafts and take manifests
survive dismissal/relaunch, are account/project scoped, and are removed after
confirmed account/project deletion. Full-resolution originals stay on iPhone;
Use take enters the existing proxy intake with one stable recovery identity.
A full Talking slot keeps the new take and explains removing existing footage.

## Baseline automated evidence

- Native build and full unit suite: 1,500 executed, 21 intentionally skipped,
  zero failures. Includes monotonic pace changes/pause/end/reflow, Unicode
  anchors, Dynamic Type layout, account/project isolation, corrupt candidate
  recovery, capture initial state, and emitted-phase transition regressions.
- Focused native UI runs: all 10 selected tests passed without retry in two runs.
  Covers creation/editor smoke checks, three existing voiceover attachment
  journeys, and three teleprompter script/review/failure journeys.
- Final native unit/build run after the hardware audio fix: 1,500 tests,
  21 intentional skips, zero failures.
- Final native UI run after the hardware audio and fixture changes: all seven
  selected tests passed without retry. Large text uses scrollable options and
  error actions, with Record/Stop always outside the options scroll view.
- Offline iOS verification/orchestration suite: all 93 tests passed again.
- Mechanical pre-ship check: passed, no overlapping changes with fetched main,
  no release metadata edits, no API/web code changes.
- Read-only reliability review: fixed start/Stop race, unknown intake receipt
  retry, preview connection timing, and stale `@Published` phase consumption;
  final bounded recheck reported no further material finding.

Result bundles are under `test-results/ios/` in the worktree. The latest full
unit result is `prepare-ui.V9rgzt/unit.xcresult`; the nine-test window UI result
is `ui.2i2jGf/ui.xcresult`. UI fixture evidence
is offline replay; the review fixture uses existing bundled footage with test
audio. No model/provider change or live call was made; live-model cost is $0.

## Baseline physical iPhone evidence

A separate opt-in `Kria Hardware QA` scheme builds `KriaHardwareUITests`, keeping
hardware-dependent cases outside the standard simulator inventory. The test
requires a physical iPhone and `KRIA_TELEPROMPTER_HARDWARE_TEST=1` in its runner;
it uses real camera/microphone capture with the offline Talking workspace.

The final app and hardware runner built successfully for arm64/iPhoneOS 26.5.
The app was installed under a separate `com.kria.teleprompter.qa` identity and
launched directly on the paired iPhone 13 Pro. Production app identity and
entitlements were unchanged. Cached automatic development signing was sufficient;
no Xcode account login or provisioning-profile modification was needed.

The final physical test passed once, with zero failures or skipped tests. It
exercised native script Copy/Paste with granted camera/microphone access, cancelled
the countdown, recorded/stopped a normal take, backgrounded an active retake,
returned to interrupted-take review, and closed/reopened that review. Use take
was enabled; the test deliberately did not submit it to a live server.

The saved movies were copied from the QA app and inspected independently:

| Take | Duration | Picture | Sound | Saved state |
| --- | --- | --- | --- | --- |
| Normal | 6.033 s | HEVC 1920×1080, 30 fps, portrait rotation | AAC mono 48 kHz, 6.032 s | review, not interrupted |
| Interrupted retake | 1.665 s | HEVC 1920×1080, 30 fps, portrait rotation | AAC mono 48 kHz, 1.665 s | review, interrupted |

Both movies fully decoded without errors and contained nonzero audio samples.
Their display matrices describe portrait rotation without reflection. Inspected
saved frames contain camera pixels only, with no teleprompter widget or text.
Both the previous take and interrupted candidate remained on the iPhone after
closing/reopening. Track timing is technical evidence; no controlled spoken/clap
check was performed to establish perceptual speech sync or microphone quality.

Real hardware exposed an audio setup failure (`-11800` / `-10868`): the capture
graph included microphone input while the manually managed audio session still
used SoloAmbient. The fix activates playAndRecord/videoRecording before starting
the session and deactivates audio only after stopping it. A bounded reliability
review found no further material concern in that change.

Two QA fixture corrections were also needed: use native app Copy/Paste rather
than the background runner pasteboard, and mirror the server's user-role format
selection so abandoned-chat cleanup retains the offline project. The hardware
scenario advertises phone intake with `KRIA_CHAT_DEVICE_RENDER=ready`; it neither
seeds a fake recording nor executes a placeholder render. Production project
deletion behavior was not changed.

Final physical result: `/private/tmp/kri-583-device-ui-recovery.xcresult`.
Test log: `/private/tmp/kri-583-device-ui-recovery.log`.
Build log: `/private/tmp/kri-583-device-capability-build.log`.
Private raw movies and screenshots remain outside the repository.

## Window refinement evidence (Transparent / Glass / Solid)

- Full native unit suite: 1,509 total, 1,488 passed, 21 intentional skips,
  zero failures. Includes nine window decoding, persistence, fractional bounds,
  minimum reading-height and resize/anchor checks. The unchanged model/store/
  geometry results are reused after the final UI-only clipping/gesture edits.
- Nine selected native UI tests passed without retry, including both new window
  journeys, existing teleprompter recovery/large-text checks and creation/editor
  smoke checks. Four smoke checks and the background/persistence journey passed
  again after the visual refinements.
- Final native UI run against the built test bundle: all five teleprompter
  journeys passed without retry, including exact move/resize finger travel,
  uninterrupted rehearsal during handle drags and text-body drag-to-pause.
- The text-drag check initially selected a StaticText that had scrolled above
  the clip. Its final version targets the visible reading area using the window
  frame. An empty method-filter run was discarded; the final class run verified
  five executed tests and zero failures.
- All 93 offline iOS orchestration checks passed after classifying the new UI
  methods and feature source in the test manifest.
- The real iPhone window test passed once: all three backgrounds over genuine
  preview, moving/resizing before capture and during recording, a cancelled
  countdown, normal Stop/Review, interrupted retake and restored review.
- New saved takes: normal 13.500 s and interrupted 1.700 s, HEVC 1920×1080 at
  30 fps with portrait rotation; mono AAC at 48 kHz with nonzero samples. Both
  fully decoded. Inspected frames at 1 s and 8 s contain camera pixels only,
  including the interval when Glass was displayed over live recording.
- Real screenshots prompted a final fix: white grips, rounded window edges and
  an independently clipped reading area below the move header. Handle drags use
  screen coordinates so moving the grip does not reduce finger travel.

Unit result: `prepare-ui.V9rgzt/unit.xcresult`.
Nine-test UI result: `ui.2i2jGf/ui.xcresult`.
Final UI result: `/private/tmp/kri-583-window-final-class-ui.xcresult`.
Passed physical window result: `/private/tmp/kri-583-window-final-device-ui.xcresult`.

That phase's hardware visual recheck did not execute an application test: Xcode
first requested an unlocked phone, then the runner exited with code 74 before
connecting. The final QA build installs successfully under its isolated identity.
Real-camera results above precede only the last visual/clipping and exact-drag
refinements; those have separate final simulator validation. Real-server intake,
spoken sync and additional devices remain unverified.

## Kria component alignment

The teleprompter now uses Kria's existing native components throughout the script,
camera, settings and review flow. WorkspaceTopRow and KriaGlassCloseButton provide
shared chrome; AttachmentHeading, KriaSectionLabel and Inter use the existing
hierarchy. Preparation/review actions use the shared primary and secondary capsule
styles, and Record/Stop use KriaInkCTAButtonStyle. Countdown, saving and camera
recovery use Kria glass panels and the existing spinner.

The movable reading window uses Kria's floating material for Glass and opaque
Paper with Ink text for Solid, with Paper as the reduced-transparency fallback.
Transparent remains clear, with Paper reading text and an Ink shadow. Shared
44-point floating icon buttons and the Kria outline icon set supply move/resize
handles. Material is kept below identified controls to preserve accessibility
frames. Drag behavior, script timing, geometry, capture and persistence are unchanged.

Camera options are more compact. Text window settings sit beside Close at the
top, while pace, rehearsal/line controls and Record/Stop stay in the bottom glass
panel. Text size and Start of script are in the scrollable Text window sheet.
Background/position choices stack at accessibility text sizes, Record/Stop stays
outside the options scroller, and font changes are disabled while capture is busy.
The settings sheet explicitly inherits the presenting Dynamic Type size so its
typography and layout match the script/camera, including accessibility overrides.

- Simulator and signed physical-device builds passed. The final restyle installed
  successfully under the separate `com.kria.teleprompter.qa` identity on the
  paired iPhone; production/dev app identities were not replaced.
- The first native UI run verified nine selected tests passed without retry.
  All five teleprompter journeys then passed against the final review-button
  styling, including exact drag travel, saved window preferences and recovery.
  Four creation/editor smoke checks passed on that app build; their unchanged
  results are reused after the final sheet-only accessibility correction.
- Expanded settings checks identified a test helper that dismissed the medium
  sheet by swiping down at the top. It now scrolls toward offscreen controls and
  handles empty clipped frames. Screenshot inspection also found and fixed the
  sheet's missing Dynamic Type override. The normal-size settings persistence
  journey passed again after that fix. At the largest text size, verification
  targets the native Stepper's Increment button and checks a real font-size
  change; the Stepper container itself is not an interactive touch target.
  The final focused accessibility test passed without retry, including an
  enabled, hittable Increment button, changed font label and visible camera
  Close/Record controls after dismissing settings. Its screenshot confirms
  enlarged Inter labels and vertically stacked Kria actions in the sheet.
- All 93 offline iOS orchestration checks passed. The unchanged model/store/
  geometry code reuses the prior 1,509-test unit result rather than claiming a
  new full-unit run for this presentation-only change.
- Inspected screenshots show Kria's shared glass window, Paper window, settings,
  script and review surfaces. The phone run reached genuine camera preview and
  changed all three backgrounds before a system low-battery shutdown warning
  blocked move/Record interactions. That interrupted run is not a passing camera
  result and did not expose an app capture failure.

Initial native UI result: `/private/tmp/kri-583-kria-components-ui.xcresult`.
Five-journey UI result: `/private/tmp/kri-583-kria-components-final-class-ui.xcresult`.
Normal-size settings recheck: `/private/tmp/kri-583-kria-components-scroll-ui.xcresult`.
Final accessibility button check: `/private/tmp/kri-583-kria-components-stepper-ui.xcresult`.
Low-battery-interrupted physical result:
`/private/tmp/kri-583-kria-components-device-ui.xcresult`.
Private camera screenshots and test screen recordings remain outside the repository.

## Physical iPhone release check still required

1. Record spoken words with a visible clap; check saved picture/audio sync.
2. Check reading comfort, face visibility and matched preview/saved framing on
   smaller and larger iPhones while a creator holds the phone naturally.
3. Exercise lock/audio interruption, route changes and app relaunch; playable
   partial takes must reopen, and camera/microphone must stop. Backgrounding
   and dismissal/reopen passed the physical test above.
4. Use the recorded take in a real Talking edit; confirm original remains on
   device, analysis proxy attaches once, and editor playback/export uses it.

Real-server attachment/export, spoken sync and the remaining hardware scenarios
remain unverified. This change has not been released or distributed through
TestFlight.

## Local handoff

Implementation and hardware fixes are committed on the branch above in
`/private/tmp/nova-kri-583-iphone-teleprompter`. No branch push, PR, merge or
release was performed. The dedicated QA app remains installed on the paired
iPhone. The latest pre-ship check passed its mechanical gates against fetched
`origin/main` (`900b94070`). It reports newer overlapping changes in intake,
attachments, QA transport and the UI test manifest. Integrate and revalidate
those changes before opening/merging a PR. No release metadata was edited.
