# KRI-583 implementation and evidence

Ticket: https://linear.app/kria/issue/KRI-583/iphone-record-talking-videos-with-a-steady-scroll-teleprompter
Branch: `codex/kri-583-iphone-teleprompter`
Base: `b33471508f87cdf622ebcdcf301a158a57b1203b`

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
- Final native UI run after presentation/accessibility changes: all seven
  selected tests passed without retry. Large text uses scrollable options and
  error actions, with Record/Stop always outside the options scroll view.
- Offline iOS verification/orchestration suite: all 93 tests passed.
- Mechanical pre-ship check: passed, no overlapping changes with fetched main,
  no release metadata edits, no API/web code changes.
- Read-only reliability review: fixed start/Stop race, unknown intake receipt
  retry, preview connection timing, and stale `@Published` phase consumption;
  final bounded recheck reported no further material finding.

Result bundles are under `test-results/ios/` in the worktree. The latest full
unit result is `prepare-ui.JDu7VH/unit.xcresult`; the final UI result is
`ui.5qUK0a/ui.xcresult`. UI fixture evidence
is offline replay; the review fixture uses existing bundled footage with test
audio. No model/provider change or live call was made; live-model cost is $0.

## Physical iPhone release check still required

1. Record spoken words with a visible clap; check saved picture/audio sync.
2. Check portrait framing, mirrored selfie preview and nonmirrored saved movie.
3. Confirm the face remains visible and no widget/text appears in saved pixels.
4. Exercise backgrounding, lock/audio interruption, route changes and dismissal;
   playable partial takes must reopen, and camera/microphone must stop.
5. Use the recorded take in a real Talking edit; confirm original remains on
   device, analysis proxy attaches once, and editor playback/export uses it.

Hardware capture and real-server attachment/export remain unverified. This
change has not been released or distributed through TestFlight.
