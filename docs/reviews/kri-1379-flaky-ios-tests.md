# KRI-1379 step 2: flaky iOS unit tests, fixed at the cause

Four tests failed on `main` and passed on rerun. This records why each one failed, what changed and the
reproduction before and after. Simulator: a private clone of iPhone 17 Pro (iOS 26, Xcode 26.6), its own
`-derivedDataPath`, on a 15-core Mac. "Load" is `yes > /dev/null` on 13 cores (all but 2). Other sessions'
simulators ran on the same Mac throughout (1-minute load average about 145 during the first run).

Every run used:

```bash
xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation \
  -derivedDataPath <own> CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=<own clone>" \
  -parallel-testing-enabled NO -test-iterations 30 -run-tests-until-failure \
  -only-testing:<test> test-without-building
```

Plain CPU load did not reproduce two of the four. CI shows the reason: the runner stalls the whole test
process, not only the CPU (`BackgroundUploadRetryTests.testARecoveredUploadWhoseStagedFileIsGoneReleasesItsClaim`,
a synchronous test, took 20.95 s in run 43026700447; the failing upload test took 16.9 s in run 42881213617).
So those runs add a stall model on top of the load: the test host is sent `SIGSTOP` for S seconds after
every R seconds of running (`kill -STOP` / `kill -CONT` on the `Kria.app/Kria` process of that simulator).

## 1. `LiveBackgroundPreviewTests.testTheLoopedBackgroundWrapsOnlyAtTheEndOfAPass`

CI: runs 42465245478, 42482060682, 42988132881, 43209538327.

Failure modes (written before editing):

1. The sampler is held back for longer than a frame, so neighbouring samples skip frames. A skip across the
   loop point lands mid-pass (`209, 60, 65`) and fails "after a wrap the next frames are the start of the pass".
2. The sampler is held back for about a second, so the sample before it (mid-pass, `129`) and the one after
   it (start of the next pass, `28`) read as "a wrap from the middle" of a pass.
3. The test counted 11 s of wall clock, not passes, so a slow runner sees fewer wraps.
4. App side, the regression it pins: the looper preloads the next copy, whose output reports frame 0 as new
   about 3 frames before the end of the pass; drawing it shows the first frame early, then resumes the
   pass, then holds a stale frame at the real loop point.
5. App side: an output attached late misses a pass's first frames (covered by
   `testEveryLoopedItemHasAnOutputWaitingAndFramesAreDecodedAtTheDrawnSize`).

Cause: modes 1 to 3. Holding the test process back for 1 s at a time reproduces both CI messages at once,
while the player keeps real time. Not proven: a timestamped copy of the old test ran 120 more times under the same
load without failing, so no failure was caught with sample times. A player that skipped to the next copy
mid-pass would read the same way, and the live-looper tests that remain would not catch such a skip.

Change: frame picking moved into `LoopedFramePicker` (same code, now taking the item on screen, the outputs
and the host time as inputs; `AVPlayerItemVideoOutput` conforms to a three-method protocol). The test drives
it through three looper copies on a scripted clock, twice a frame for two full rotations, with each copy's
output behaving as measured in mode 4. It asserts the exact frame due at every tick. It runs in 0.5 s
instead of 11 to 14 s. The two live-player tests above it still cover the real looper.

| Run | Condition | Result |
| --- | --- | --- |
| before | load | failed on iteration 20 (19 passed): `... 132, 136, 139, 27, 28 ...` "every wrap leaves from the last frames of a pass" |
| before (diagnostic build) | load + stall 1 s every 2.5 s | failed on iteration 1: `... 41, 46, 60, 136, 168 ...` "after a wrap the next frames are the start of the pass" |
| after | load | 30/30 |
| after | load + stall 1 s every 0.2 s | 30/30 (27 stalls) |
| red control | load, pre-fix picking (first output with a new frame) | fails: "at tick 115 (pass 0) frame 0 was drawn where frame 57 was due" |

## 2. `NativeEditorRenderCompilerTests.testPhotoAndVideoOverlayPlaybackAdvancesAfterScrubbing`

CI: runs 41632135036, 41721151965 (line 1618), 42915872402 (line 1773): `XCTAssertNotNil(session.scrubPreviewFrame)`.

Failure modes (written before editing):

1. The drag slept 60 ms per step. A composed still that takes longer than a step finishes after a newer
   request, and the session drops it on purpose (only the newest position is shown), so no still lands
   during the drag.
2. After the last seek the test waited 2.5 s of wall clock for a still; on a loaded runner one still (or two,
   with the one in flight) takes longer.
3. The newest still can still be rendering when Play is tapped, and must not cover playback when it lands.
4. Play must play on from the scrub position into the photo or video overlay.
5. A new scrub must pause playback.

Cause: modes 1 and 2. Under load the test failed on both kinds (image and video) in its first iteration.
After the change, iterations took 3.0 to 14.2 s under the same load, which no fixed 2.5 s budget covers.

Change: a DEBUG-only seam, `NativeEditorSession.finishScrubFramesForTesting()`, returns when the scrub
stills already asked for have been rendered and shown (or dropped). The slow drag awaits each still; the
fast drag and Play run without waiting, so mode 3 is still exercised. The playback wait (mode 4) is
unchanged: it counts 160 poll turns rather than a deadline, and it never failed in CI.

| Run | Condition | Result |
| --- | --- | --- |
| before | load | failed on iteration 1, both kinds, line 1773 (the CI failure) |
| after | load | 30/30 (3.0 to 14.2 s each) |

## 3. `BackgroundUploadRetryTests` (selection tests)

CI: run 42881213617 `testChoosingTheSameAssetAgainUploadsItOnlyOnce` (line 924, 16.9 s), run 43026700447
`testChoosingAnAssetWhoseClipWasRemovedElsewhereUploadsItAgain` (line 945, 8.2 s).

Failure modes (written before editing):

1. The tests polled for the selection's effects with a 5 s wall-clock deadline. A stall longer than what
   is left of it, with the selection still pending, fails the test although the work completes moments later
   (CI: the next assertions, 200 ms later, passed).
2. The negative checks slept 200 ms and then asserted nothing more was sent. A duplicate upload that had not
   run yet would pass them.
3. App side: a second choice of the same asset starts a second upload (guarded twice: `selectionTasks` and
   the ledger).
4. App side: a stale ledger entry blocks choosing an asset again.

Cause: mode 1. The CI durations above match it exactly: the deadline expired during a stall.

Change: `BackgroundUploadCoordinator.select` returns the selection work it started (`@discardableResult`,
nil when the choice started nothing; the picker ignores it). The harness keeps those tasks and the tests
await them, so a pass or a failure no longer depends on the runner's speed, and "nothing more was sent" is
checked after everything that was started has finished. The same change applies to the sibling tests that
polled selection work the same way (`testChoosingAnAssetStartsUploadingBeforeTheUserTapsDone`,
`testUnchoosingWhileUploadingCancelsItAndCleansUp`, `testAnAssetJustAttachedIsNotUploadedAgainBeforeTheAttachedListCatchesUp`
and the five KRI-180 failed-load tests). Tests that hold a reservation open still poll (`waitUntil`): their
selection work cannot finish until the test answers it, and none of them failed in CI.

| Run | Condition | Result |
| --- | --- | --- |
| before | load | 60/60 (not reproduced; slowest 0.5 s) |
| before | load + stall 6 s every 1.3 s | 60/60 (not reproduced) |
| before | load + stall 6 s every 0.05 s | failed after 36 passes: line 924, the CI failure |
| after | load + stall 6 s every 0.05 s | 60/60 (30 iterations of both tests) |
| red control | ledger dedupe removed (both guards) | 4 failures in 3 tests, including the two former sleep checks |

## Regression

Under load, 2 iterations: `BackgroundUploadRetryTests`, `RerecordUploadTests`, `LiveBackgroundAsyncSegmentationTests`,
`LiveBackgroundCompositingTests`, `LiveBackgroundPreviewTests`, `LiveBackgroundTuningTests`,
`NativeEditorRenderCompilerTests`, `TalkBenchVideoFixtureTests`, `TeleprompterBackgroundTests`,
`UploadFileSharingTests`: 454 passed, 0 failed.

## 4. `NativeEditorSessionTests.testEditorResume*`: left unchanged

CI: runs 41700098824 (2026-11-03) and 42412737860 (2026-11-07), both
`testEditorResumeWithPersistedAndServerStopTakesTwoTapsAndSurvivesRestoration`, both before #2270 merged
(2026-11-10). #2270 found the cause (the first tap queued behind the decline's status read, then discarded by
`stopAll()` in the test's teardown) and reproduced it by forcing that order (5/5 before, 20/20 after).

| Run | Condition | Result |
| --- | --- | --- |
| current `main`, all three `testEditorResume*` | load | 90/90 |
| current `main`, all three | load + stall 6 s every 0.05 s | 90/90 |
| test body before #2270 | load | 30/30 |
| test body before #2270 | load + stall 6 s every 0.05 s | 30/30 |

Neither model reproduces it, before or after #2270, and no `main` run has failed on it since. Its remaining
waits are a 5 s `waitUntil` added by #2270 and two 5 s poll loops. Making them event-driven needs a way to
await `DeviceRenderSessions`' queued re-reads, a change to a model-checked state machine
(`verify-phone-render`) that this flake does not justify without a reproduction.
