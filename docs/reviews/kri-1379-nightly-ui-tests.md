# KRI-1379: why the nightly iPhone UI run stayed red

The nightly iOS run of 2026-11-13 (43415393573) failed on five UI tests. This records which of them fail
again and again, why each one failed, what changed, and the runs before and after.

## Runs read

Every full-suite iOS run with diagnostics from 2026-11-07 to 2026-11-13: nightly runs 42363957464,
42471976543, 42612018448, 42807160881, 43009921809, 43209538327 and 43415393573, plus manual runs
42559502188, 43379789858 and 43392339185. Sources: `gh run view --log` of every native shard, and the
`ui.xcresult` of 43415393573 shards 1 and 5 (screen recordings, synthesized tap points, a spindump).
Each UI test gets three attempts (`-retry-tests-on-failure -test-iterations 3`); a shard fails only when
all three fail or a test runs past its 3 minute allowance.

| Test | Attempts that failed | Where |
| --- | --- | --- |
| `CreationUITests.testLibraryPickerReturnsToChatByDoneAndBySinglePick` | 7, in 4 of 9 runs | always `XCTAssertTrue(done.isEnabled)` right after the pick |
| `CreationUITests.testLivePlanFeedFullSequence` | 4, in 2 runs (all 3 attempts on 2026-11-13) | always the second tap on the title row |
| `TeleprompterUITests`, four tests that open the camera with a script | 7 of 18 attempts, in all 3 runs that reached them | camera opened without the script |
| `NativeEditorInspectorUITests.testSoundEffectsHomeLibraryEditTrimFlow` | 1 timeout in 9 runs | waiting for the app to go idle after tapping a text field |

## 1. Library picker: Add read before the pick arrived

Failure modes (written before editing):

1. The pick is handed over from the PhotosUI process, so `selection` changes a moment after the tap and the
   test reads Add while it is still off.
2. App: the pick never reaches `selection`, so Add stays off.
3. App: Add turns on with the wrong label.

Cause: mode 1. In 43415393573 the tap on the video was sent at 48.37 s and Add was read at 49.74 s, still
off. The label read about 4 s later (`"Add 1 clip"`) passed, the test went on to land on Add overlays, and
the screen recording shows "1 of 1 selected" and Add turning on shortly after the tap. Every failure in the
4 runs is this line.

Change: wait (up to 5 s) for Add to turn on. Modes 2 and 3 still fail the test.

## 2. Live plan feed: the row moved under the second tap

Failure modes (written before editing):

1. While sections are still being decided, each new one grows the feed and the transcript follows the
   newest, so every row above it moves up. A row can move between XCUITest reading its frame and tapping it.
2. Opening the title grows the feed too, which the transcript also follows.
3. App: the second tap does not collapse an open row.
4. Rows tapped after every section is decided may sit above the visible area and need a scroll.
5. Tapping later uses time the test needs before the finished video replaces the feed.

Cause: mode 1. In 43415393573 the first tap (title row, y = 364.7) opened the title. By the second tap
(y = 318, the open title's frame when read), Captions had been decided and the feed had moved up: the
recording shows the tap opening "Clips and order" instead, so the title stayed open for the 5 s check.
All 4 failed attempts stop at this check (line 2741 before the file moved, 2883 after).

Change: the two taps move to after "Making your video", when no section is added any more. The other
tests that tap feed rows already do so at that point. Two first tries failed locally and shaped the rest:

- Tapping Title there failed 3 of 3 on mode 4: the title row sits at y = 16.7 to 76.7, under the floating
  header, and the tap went to the header. The test taps Look instead. It is the newest section, so it
  starts open, and it sits just above the panel. The test closes it, then opens it again.
- Tapping Look straight away failed 2 of 3: "Making your video" appears while Look is still springing into
  place, and the recording shows the tap leaving it open. Before each tap the test now waits until the row
  reports the same frame twice in a row (`holdsStill`). In the first final run it read 5 frames over
  0.5 s before the first tap.

Mode 5: the waits and taps add about 1.6 s between "Making your video" and Review. Locally the finished
video still replaced the feed about 7 s after the last check that needs the feed (8 s before the change). On
CI that gap was 6 to 9 s before the change, so the margin is smaller but still several seconds.

## 3. Teleprompter: Use script opened the camera without the script (app bug)

Failure modes (written before editing):

1. The tap meant for Use script lands on Record without a script, because the sheet moves (keyboard,
   caption) between XCUITest reading the frame and tapping.
2. App: Use script is handled, but the camera is built with `startsWithScript` false.
3. App, by design: with the camera unavailable and no script, the camera shows only the access card, so the
   window, Record and Background controls the tests look for do not exist.

Every failed attempt in CI stops right after the camera opens, on a control that exists only with a script
on (`teleprompter-window`, `teleprompter-record`, `teleprompter-background`), and the hierarchy shows only
Close and Try again. That is mode 3, so the camera opened without the script. The recording and tap points
of 43415393573 rule out mode 1: the tap went to (201, 453), the centre of Use script, in a layout that had
not moved for half a second.

Cause: mode 2, a real app bug on the creator's path (+ tray, Add script, Use script). A logging build
(`NSLog` in the sheet's buttons, the sheet's `onDismiss`, the cover's content and the camera's state
object) shows, for every failed attempt locally:

```
KRI1379 finish useScript=true
KRI1379 scriptEntry onDismiss open=true starts=true
KRI1379 cover content starts=false
KRI1379 flow created useScript=false
```

The sheet's `onDismiss` read `cameraStartsWithScript` as true; 6 ms later the full-screen cover built the
camera from a stale read of the same flag, and the camera's `@StateObject` kept that first value. Passing
attempts log `flow created useScript=true`. `openCamera(withScript:)` already deferred
`showsTeleprompter` by one task for the same stale read; the script sheet path had no such deferral. Both
came in with the script-first camera in #2095 (KRI-1193).

Change: the cover is presented with `fullScreenCover(item:)` on a `CameraLaunch` value that carries
`startsWithScript`, so the camera reads the choice from the launch it was opened with. The script sheet
stores its launch and the sheet's `onDismiss` presents it. `showsTeleprompter`, `cameraStartsWithScript`,
`openCameraAfterScript` and the deferral in `openCamera` are gone.

## 4. Sound effects timeout: the app went idle, XCTest did not notice

In 43415393573 the test tapped the sound search field at 18.4 s. XCTest then waited 60 s for the app to
report its animations complete, twice ("App animations complete notification not received"), and the
3 minute allowance ran out. The spindump taken at the timeout shows the app's main thread idle in its run
loop and the keyboard daemon idle. The same two 60 s waits after tapping a text field in the native editor
also timed out `CreationUITests.testEditorAIActivityCompletingOpaqueCapture` (43009921809) and
`CreationUITests.testChangedEditorPreviewRetainsOldPictureAndBlocksExportThenSupportsManualUndo`
(43379789858). The test passed in the other 8 runs, in 28 to 52 s.

No change here: nothing shows the app doing something wrong, and the test does nothing unusual. One fact
for the CI owner: xcodebuild does not retry a test that runs past its allowance (the shard restarts and the
test stays "not passed"), so one such stall turns a nightly red on its own.

## Local runs

Own simulator clone (iPhone 17 Pro, iOS 26.5, Xcode 26.6), own `-derivedDataPath`, no retries:

```bash
xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation \
  -derivedDataPath <own> CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=<own clone>" \
  -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 180 \
  -test-iterations 3 -only-testing:<test> test-without-building
```

Other sessions shared the Mac; the 1-minute load average ran from about 13 to over 900.

| Test | main (with logging) | this branch |
| --- | --- | --- |
| `TeleprompterUITests.testBackgroundCanBePickedKeptAndCleared` | 2 of 3 | 4 of 4 |
| `TeleprompterUITests.testScriptControlsAreReachableAtAccessibilitySize` | 3 of 3 | 4 of 4 |
| `TeleprompterUITests.testScriptIsSavedAndCameraFailureKeepsItEditable` | 0 of 3 | 4 of 4 |
| `TeleprompterUITests.testWindowHandlesDoNotPauseOrScrollTheScript` | 0 of 3 | 4 of 4 |
| `CreationUITests.testLibraryPickerReturnsToChatByDoneAndBySinglePick` | not run (see below) | 6 of 7 |
| `CreationUITests.testLivePlanFeedFullSequence` | 3 of 3 | 6 of 6 |
| `NativeEditorInspectorUITests.testSoundEffectsHomeLibraryEditTrimFlow` | 3 of 3 | unchanged |

"This branch" counts every run of the final code for that test, including one pass on the branch rebased
onto `main`.

The library picker failure is a different, earlier step: the Photos permission alert stayed up after the
test tapped Allow Full Access, so the picker never opened (line 926, before the changed line). It did not
appear in the 9 CI runs. It is noted for the owner of that test, not fixed here.

The two `CreationUITests` races did not reproduce locally on `main`; their evidence is the CI recordings
above. The library picker change was already in the first local build, so it has no local `main` column.

Every camera path the launch value touches also ran on this branch: Record tile, + tray Record, Add
script with and without a script, re-record (`TalkRerecordUITests`, 5 tests), camera refused, camera access
off, and review take (18 tests). In the first run, the 7 that open a saved take through the review fixture
failed: the camera opened (screen recordings), but the fixture's take, which `TeleprompterReviewFixture`
exports with `AVAssetExportSession` on first load, appeared after the tests' 10 s and 20 s waits. All 7
passed when rerun alone, and a second run of all 18 in the first run's order, on the branch rebased onto
`main`, passed 18 of 18.
