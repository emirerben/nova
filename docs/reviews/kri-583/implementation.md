# KRI-583 implementation and evidence

Ticket: https://linear.app/kria/issue/KRI-583/iphone-record-talking-videos-with-a-steady-scroll-teleprompter
Branch: `codex/kri-583-iphone-teleprompter`
Base: `b33471508f87cdf622ebcdcf301a158a57b1203b`
QA status: `DONE_WITH_CONCERNS` — real capture/recovery passed; the remaining
release checks are listed below.

## Creator flow

Talking edit → Add footage → Record with script → paste/edit script → Open camera.
The front-camera preview has a 144-point script widget below the top safe area.
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

## Automated evidence

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
unit result is `prepare-ui.9ix9lq/unit.xcresult`; the final UI result is
`ui.7Thpnv/ui.xcresult`. UI fixture evidence
is offline replay; the review fixture uses existing bundled footage with test
audio. No model/provider change or live call was made; live-model cost is $0.

## Physical iPhone evidence

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
iPhone. The final pre-ship check passed against fetched `origin/main`
(`4f1dc8c8c`), with no overlapping file changes or release metadata edits.
