# KRI-1550: four intermittent nightly UI tests, the exact-44 checks, and the caption switch

Follows `docs/reviews/kri-1538-nightly-ui-tests.md` ("What this does not fix"). Two of the four tests were
failing because of app bugs, and both are fixed in the app. Two failures were the test giving up on a
system prompt too early. The two slide-post failures are not explained; they were each seen once.

## Runs read

The job logs of every native shard of the nightly `iOS` runs on 11-07, 11-08, 11-12, 11-13, 11-14, 11-15,
11-16, 11-17, 11-18 (44299097913) and 11-19 (44518781625). Each UI test gets three attempts per night.
Screen recordings and accessibility trees of the failed attempts came from the xcresult in
`ios-native-test-diagnostics-shard-4` of runs 43707825608 (11-15) and 44299097913 (11-18).

| Test | 11-12 to 11-17 | 11-18 | 11-19 |
| --- | --- | --- | --- |
| `CreationUITests.testSendingALongPromptWithTheKeyboardUpAfterFifteenUploadsKeepsTheChatOnScreen` | pass | fail, fail | fail 3/3 |
| `CreationUITests.testGalleryPickIsReadFromPhotosAndReachesUpload` | 11-15: fail 2, pass 1; other nights pass | fail 1, pass 1 | pass |
| `SlidePostUITests.testDraggingToTheEdgeAutoScrollsTheStrip` | pass | fail 1, pass 1 | pass |
| `SlidePostUITests.testNewlyReadyAssetBecomesASlideWithoutTapping` | pass | fail 1, pass 1 | pass |

## 1. Long prompt after fifteen uploads: the chat stopped scrolling over media rows (app)

`CreationUITests.swift` "scrolled away from the end, the Latest pill is offered": after fourteen drags up the
footage card was never on screen and the pill never appeared. Every attempt since 11-18, so not intermittent.

Failure modes, written before the fix:

1. App: a vertical drag that starts on a chat media row no longer scrolls the chat.
2. App: the pill is no longer offered for a reader above the end.
3. The chat fits the screen, so there is nothing to scroll (as in KRI-1538 section 2).
4. The drags start outside the transcript.

The first red nightly (11-18) is the first one after #2351 (KRI-1454), which gave every chat media row a
swipe-to-delete `DragGesture` through `.simultaneousGesture`. With fifteen receipts the drag start
(0.3 of the screen height) is on a media row. `DragSelectScrollView` already records the same trap: a
SwiftUI `DragGesture` on scroll content swallowed the scroll. Mode 1. Reproduced locally on the unchanged
app (iPhone 17, iOS 26.5): the test fails at the same line.

Fix (`KriaMediaRow.swift`): the row swipe is a UIKit pan that fails as soon as the finger's travel is more
vertical than sideways, so the scroll view keeps vertical drags. The direction has to be read from the
touches: under SwiftUI, `gestureRecognizerShouldBegin` was asked with a translation of (0, 0) after a
0.1 s press, so a translation check there refused the slow sideways swipe too (seen with temporary logging).
A flick's carry is projected like a scroll view's deceleration, standing in for `predictedEndTranslation`.
After: the long-prompt test passes, and the three swipe-to-delete tests from #2351 still pass.

The first 11-18 attempt failed earlier, on the Photos prompt (section 2a).

## 2. Gallery pick: two different failures

### 2a. The Photos prompt came after the test stopped watching for it (test)

11-15, two of three attempts: no gallery tile within 40 s, and the screen showed Add media with nothing
picked. Failure modes:

1. The Photos tap did not register.
2. App: the Photos choice was lost between the "Upload from" sheet closing and the gallery opening.
3. The permission prompt came late and covered the gallery.
4. App: the gallery opened without tiles (Photos library not seeded).

The recording shows the "Upload from" sheet closing at 24.0 s (so the tap registered, ruling out 1),
nothing until 41.5 s, then the system's "would like full access to your Photo Library" prompt, which
stays until the test fails. The springboard tree captured when the test's 10 s wait for the prompt ended
(34.2 s) has no alert. The prompt came about 18 s after the tap, then covered the gallery for the rest of
the 40 s tile wait; XCTest's interruption monitor only runs on an interaction, not on a wait. Mode 3. The
app asks only once, when Photos is chosen; the wait is the system prompt on the CI simulator after
`resetAuthorizationStatus`. The long-prompt test's first 11-18 attempt failed the same way.

Fix: `waitAnsweringPhotosAccess(for:timeout:)` in `CreationUITests` answers the prompt whenever it shows
while it waits for the gallery, in one 50 s budget (the old 10 s + 40 s). Used by the six tests that open
Photos. Mode 4 still fails: no tile within 50 s.

### 2b. A slide that ended one tile away tapped its first tile (app)

11-18, one attempt: "2 of 10 selected" where 1 was expected after the deselecting slide from tile 1 to
tile 2. In the recording the slide works (27.04 s tile 1 off, 27.20 s tile 2 off: "1 of 10"), then at
27.50 s tile 1 is selected again. Failure modes:

1. The slide started on the wrong tile.
2. App: the tile under the slide's start fired a tap when the finger lifted.
3. The selection was re-read from Photos and restored.

A scratch UI test slid from tile 1 to tile 2 at both XCUITest velocities with presses of 0.05 to 1.0 s:
with a 0.05 s press the first tile was always toggled back (4 of 4); from 0.1 s it never was. The nightly
test pressed for 0.1 s, at the edge. Mode 2: `DragSelectRecognizer` let every recognizer on the touch run
alongside the slide, so the tile button still fired at touch-up when the finger ended within its reach.
A finger that starts moving at once, as a real one does, hits it on any slide across two neighbours.

Fix (`DragSelectScrollView.swift`, used by the Photos gallery, saved uploads and the clip picker): only
the scroll view's recognizers run alongside a slide, so the tile's tap fails once the slide begins. After:
12 of 12 slides correct. The gallery test's deselecting slide now presses for 0.05 s: it fails on the old
code at the same assertion as on 11-18 and passes with the fix. The clip picker's two slide tests, one of
which checks that a vertical drag still scrolls, pass.

## 3 and 4. Slide-post setup: seen once each, not explained

Both failed on 11-18 in the same shard, two minutes apart, in `openRichWorkspace`, the setup every
`SlidePostUITests` test shares, not in what the tests check:

- `testDraggingToTheEdgeAutoScrollsTheStrip`: Save was tapped (the press shows in the recording) but no
  save started (no progress spinner) and the subtitle stayed "Unsaved changes" for 10 s.
- `testNewlyReadyAssetBecomesASlideWithoutTapping`: New chat was tapped; the button dimmed while the chat
  was being created and came back, but no conversation opened and the drawer stayed open.

The red "Kria couldn't complete that request." under "Start your first video" in both recordings is not a
cause: it shows on a passing local launch too. Silent early returns in `SlidePostSession.save` (busy, AI
chat running, conflict pending) would match the first; nothing shows which one. Both passed on their next
attempt and on every other night. Stability is below.

## Exact-44 touch targets

`TouchTargetAssertions.swift` holds `assertTouchTarget(_:)`: a frame height or width must be at least
44 - 0.1, the tolerance `NativeEditorInspectorUITests` already used (43.9). All 24 exact `>= 44` checks
(11 in `CreationUITests.swift`) use it, and so do the five copies that already compared against 43.9 or
43.5. A failing check now says which size it read.

## Caption switch (KRI-1538 section 6, mode 5)

No nightly has run since the waits (#2404) merged on 11-20, so there is no new nightly evidence. The CI
simulator is iOS 26.2 (Xcode 26.3); local runs here are iOS 26.5 (Xcode 26.6). In ten CI attempts with the
waits in place it lost one tap (below), the rate KRI-1538 measured. No retry was added.

## Verification

Local, iPhone 17 simulator set to English, iOS 26.5, Xcode 26.6, no retry:

| Check | Before | After |
| --- | --- | --- |
| Long prompt after fifteen uploads | fails at the "Latest" pill | passes |
| Gallery pick, deselecting slide pressed 0.05 s | fails at "1 of" (as on 11-18) | passes |
| Slide experiment, 12 press/velocity cases | 4 of 12 wrong | 12 of 12 right |
| Swipe-to-delete: reveal, full swipe, refused delete | (pass on #2351) | pass |
| Other tests that open Photos (cancel, composer tray, Apple picker, three videos) | | pass |
| Clip picker slides, incl. "vertical drag still scrolls" | | pass |
| `testDraggingToTheEdgeAutoScrollsTheStrip`, `testNewlyReadyAssetBecomesASlideWithoutTapping` | | 10 of 10 each |

Self-review found that UIKit keeps the last movement's speed after the finger stops, so a row swipe held
still before release could still count as a flick and delete; the flick now counts only if the finger moved
in the last 0.1 s, and the swipe tests pass again after that change.

After the snapshot change below, the long-prompt test passed 2 of 3 locally; the second attempt stopped in
XCTest's own event synthesis ("Timed out while synthesizing event", 57 s, during a scroll-up drag) while the
Mac's load average was 21 from other builds, and the next attempt took 992 s. Not counted either way.

CI, one `macos-15` runner, Xcode 26.3, iOS 26.2 simulator, `-test-iterations 10`, no retry-on-failure,
from a throwaway branch with a one-job workflow (run 44764112232, this branch before the review fix):

| Test | Passed |
| --- | --- |
| `testGalleryPickIsReadFromPhotosAndReachesUpload` | 10 of 10 |
| `testSendingALongPromptWithTheKeyboardUpAfterFifteenUploadsKeepsTheChatOnScreen` | 9 of 10 |
| `testDraggingToTheEdgeAutoScrollsTheStrip` | 10 of 10 |
| `testNewlyReadyAssetBecomesASlideWithoutTapping` | 10 of 10 |
| `NativeCaptionVisualUITests.testCaptionAndVisualPaperScreens` (unchanged) | 9 of 10 |

The long-prompt miss (iteration 2) was the "sent prompt is in the visible chat" check right after Send:
the app was busy for 3.3 s after the tap, then the first check, five separate queries for the sent text, the
header and the composer, took 4.4 s while the chat was still scrolling to the new turn. Its frames came from
different moments and it outlasted its 3 s wait; the frames read just after put the prompt 62 pt inside the
visible chat. Each check now reads one snapshot of the screen and the wait is 5 s. A second run of that test
alone is in the PR.

The caption miss (iteration 5) is KRI-1538 section 6 mode 5 again: the second tap on the highlight switch, the
one that turns it off, and the colour row was still there 10 s later. One in ten with the waits in place, the
rate KRI-1538 measured. No retry was added; the next step stays as written there.
