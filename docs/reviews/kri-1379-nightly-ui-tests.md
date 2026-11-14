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
| Tests that ran past the 3 minute allowance (section 4) | 5 shards in 10 runs | 3 waiting for the app to go idle after an editor text field tap, 2 a caption test too long for the limit |

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

Coverage given up: this was the only live-feed test without reduced motion, and the only one that tapped a
row while sections were still arriving. A creator can do that, and no test covers it now. Opening and
closing a row is checked only once every section is decided.

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

## 4. Tests that ran past the 3 minute allowance

In the 10 runs read, 5 shards turned red because one test ran past its 180 s allowance. xcodebuild restarts
the runner and never retries such a test: it gets one attempt where every other failure gets three, so each
of these alone turned its shard red.

| Test | Run, shard (job) | Where the time went |
| --- | --- | --- |
| `NativeEditorInspectorUITests.testSoundEffectsHomeLibraryEditTrimFlow` | 43415393573, 5/5 | two 60 s idle waits after tapping the sound search field |
| `CreationUITests.testEditorAIActivityCompletingOpaqueCapture` | 43009921809, 2/3 | two 60 s idle waits after tapping the editor chat composer |
| `CreationUITests.testChangedEditorPreviewRetainsOldPictureAndBlocksExportThenSupportsManualUndo` | 43379789858, 2/5 | the same |
| `NativeCaptionAnimationUITests.testWordDisplayKeepsTheChosenAnimation` | 42471976543, 2/3 (128704776476) | no stall: a long test, still looking for "In animation Silk Reveal" at 178 s |
| the same | 42612018448, 3/3 (129142822546) | the same, at 179 s |

There are two mechanisms.

### 4a. A test too long for the allowance (already fixed on `main`)

The caption test checked cue and lyric captions in one test, with idle waits of 5 to 18 s after each tab
change. It passed once in 122.7 s and ran over twice. #2243 split it into
`testWordDisplayKeepsTheChosenAnimationOnCueCaptions` and `...OnLyricCaptions`, merged 2026-11-09 15:37,
after both overruns. Each half ran in 6 of the runs read since, and all 12 runs passed, in 46 to 131 s. No
change here.

### 4b. The editor's first text-field focus leaves XCTest's animation count above zero (CI only)

How XCTest decides the app is idle, read from `XCTAutomationSupport` in Xcode 26.6: it swizzles UIKit's
`UIViewAnimationState` `animationDidStart:` and `animationDidStop:finished:` into one atomic counter, +1
and -1 whatever the animation, and answers "animations idle" only when the counter is 0. A tracked
animation that starts and never stops makes every later action wait 60 s.

Evidence from CI. Each `ui.xcresult` carries the simulator's unified log (`xcrun xcresulttool export
diagnostics`), which includes XCTest's own messages inside the app:

- In passing first focuses (pids 64356 and 95289 in 43415393573 shard 5) XCTest's idle request arrives
  after UIKit has focused the field ("Reloading input views") and is answered at once. The next one is
  answered 10 ms after the keyboard's `didShow`.
- In both stalls with a readable log (43415393573 shard 5, 43379789858 shard 2) the request arrives 30 to
  50 ms before "Reloading input views", while the main thread is still building the first keyboard of
  that launch. It is never answered, not even after the keyboard's `didShow` 1.6 s later. XCTest never
  logged "animationDidStop without a matching animationDidStart", so the counter stayed above zero rather
  than going negative.
- The spindump at the timeout shows the main thread and UIKit's in-process animation thread idle: nothing
  was animating. A start was never matched by a stop.
- 3 of 102 taps on editor text fields stalled (2 of 93 on the editor chat composer, 1 of 9 on the sound
  search). The same composer outside the editor stalled 0 of 255 times.

Not reproduced locally. On iOS 26.5 (Xcode 26.6), about 50 focuses of the same two fields never left the
counter above zero. That includes 24 with focus slowed by 400 ms to mimic CI's slow first keyboard,
watched by a DEBUG probe that mirrored XCTest's counter per animation state. CI runs Xcode 26.3 with the
iOS 26.2 simulator, and Xcode 26.6 cannot download that runtime. Which UIKit animation never stops is
still open, so no app change is made on a guess. It is tracked in KRI-1411.

Change: `verify.sh` reruns a test that ran past its allowance once, with the same settings, into
`ui-rerun.xcresult`. Only a test whose sole failure is the time allowance is rerun. A test that also failed
an assertion, or runs past the allowance again, stays red, and `verify` still requires every other selected
test to pass. A rerun that passes is listed in `flaky-tests.json` and the job summary, like any
pass-on-retry. On the real CI bundles, `ui_tests.py overruns` picks out exactly the sound-effects test
(43415393573 shard 5) and the changed-preview test (43379789858 shard 2), and nothing in 43415393573
shard 1, whose three failures were assertions.

End to end on the real gate. A smoke test was made, temporarily, to sleep 200 s on its first run only.
`KRIA_IOS_TEST_MODE=prepare-ui` then `KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS=smoke bash
scripts/ios/verify.sh` on the simulator clone gave:

```
UI execution (smoke): 271s (exit 65)
Rerunning tests that ran past the time allowance:
-only-testing:KriaUITests/CreationUITests/testLaunchEntersChatFirstWorkspace
UI rerun past the time allowance (smoke): 35s (exit 0)
Flaky (passed on retry): KriaUITests/CreationUITests/testLaunchEntersChatFirstWorkspace (passed after 2 attempts)
Verified 5 selected UI tests passed.
```

`verify.sh` exited 0, and `flaky-tests.json` names the test with "Test exceeded execution time allowance of
3 minutes". The cases that must stay red (runs past the allowance again, an assertion failure next to an
overrun, a red run with no overrun) run against the real script with a stubbed `xcodebuild` in
`scripts/ios/tests/test_verify.py`.

## 5. The same stale sheet read at four other sites

Section 3's bug was a `@State` value set together with a presentation flag and read only inside the
presented content. A scan of every `sheet(isPresented:)` and `fullScreenCover(isPresented:)` in the app for
a value set next to its flag found four more. None was reproduced, but each would fail the same way:

- `SlidePostWorkspaceView`: the AI sheet's chat-edit choice (`aiChatEnabled` with `showsConversation`). A
  stale read opens the sheet with chat edit off: "Describe the post…", and no way to send when no proposal
  is allowed.
- `CameraRollStoriesView`: the moment preview's player (`player` with `showsPreview`). A stale read opens an
  empty sheet.
- `CameraRollStoriesView`: the idea's progress (`use` with `showsUse`, and the flag was set before the
  value). A stale read opens an empty sheet.
- `ResultsView`: the downloaded file to share (`shareFileURL` with `showShare`). A stale read opens an empty
  share sheet.

Each now presents with `sheet(item:)` carrying its value, as `CameraLaunch` does and as
`CameraRollSurfaceView` already presents the idea's progress (`.sheet(item: $model.shownUse)`). The shared
file is deleted when its sheet disappears, because `onDismiss` runs after the item is already cleared.
On the simulator clone, 3 iterations each without retries: the four slide post tests that open the AI sheet
passed 12 of 12. `testRichLayoutFromTheDrawerWhenCapabilitiesFailToLoad` checks "Describe the post…"
(chat edit off) and `testAIButtonOpensSheetAndChatEditStagesOnThePage` checks chat edit on.
`CameraRollStoriesUITests` passed 15 of 15. No UI test opens the stories preview, the idea progress from
the stories review, or the finished video's share sheet. Those three build and follow the same pattern,
but no test exercises them.

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
