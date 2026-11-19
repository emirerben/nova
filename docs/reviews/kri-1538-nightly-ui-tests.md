# KRI-1538: six nightly iPhone UI tests, and why no PR caught them

The nightly `iOS` run was not green once from 2026-11-05 to 2026-11-19, and the nightly `CI` run was green 4
times in 14. This records what failed, why, what changed, and the runs before and after. It follows
`docs/reviews/kri-1379-nightly-ui-tests.md`, which covers the nights before 2026-11-13.

No app code changes here. None of the six failures is an app regression.

## Runs read

Nightly `iOS` runs of 2026-11-13, 11-15, 11-16, 11-17, 11-18 (44299097913) and 11-19 (44518781625), and the
nightly `CI` runs 44284283982 and 44068416242 (`iPhone validation (screens)`). Source: the log of every
failed native shard. Each UI test gets three attempts in the nightly (`-retry-tests-on-failure
-test-iterations 3`), so a shard fails only when all three fail or a test runs past its 3 minute allowance.

| Test | 11-13 | 11-16 | 11-18 | 11-19 |
| --- | --- | --- | --- | --- |
| `CreationUITests.testConfirmScreenLandscapeChoiceIsSentWithoutAFit` | pass | fail 3/3 | fail 3/3 | fail 3/3 |
| `CreationUITests.testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView` | pass | fail 3/3 | fail 3/3 | fail 3/3 |
| `AccountPrivacyUITests.testFinishedEditLearningConsentAcceptAndDisableKeepsApprovedPreference` | pass | fail 3/3 | fail 3/3 | fail 3/3 |
| `CreationUITests.testReviewPlanFlagsUpdatesAndUndoesASectionFromTheFeedCTA` | pass | pass | fail 3/3 | fail 2, pass 1 |
| `NativeEditorInspectorUITests.testLongPressMovesTextTimingWithoutOpeningInspector` | pass | pass | fail 3/3 | pass |
| `NativeCaptionVisualUITests.testCaptionAndVisualPaperScreens` | pass | pass | fail 3/3 | pass |

## Why no PR went red

None of the six is in the `smoke` UI group. A pull request whose diff selects the `full` UI group runs
only smoke (`ci.yml`, "Run selected iPhone UI scenarios"), and the `creation` and `screens` journey legs
run nightly and on manual dispatch only (`PR_NATIVE_LEGS = ("unit",)`). Both pull requests that outdated
these tests selected `ios_ui_groups=full` and `native_legs=["unit"]`: #2346 (run 43712079132) and #2347
(run 43830210594). A chat or localisation change can therefore outdate a `creation` or `editor` test with a
green pull request, and only the nightly shows it.

The fix pull request itself selects `full` too, so its own CI does not run these tests. They were run
on the CI Xcode by a manual dispatch of a throwaway branch (below).

## 1. Shape picker touch target: 43.99999999999994 is not below 44

`CreationUITests.swift:1124` (the checks are in `createWithVideoShape`): `XCTAssertGreaterThanOrEqual
failed: ("43.99999999999994") is less than ("44.0") - 44pt touch target`. Only the runtime-v2 pass fails.

Failure modes (written before editing):

1. The picker moved to a fractional y and a 44 pt row reads one floating-point step under 44.
2. App: the row really is shorter than 44 pt.

The app still asks for `minHeight: 44` (`VideoShapePicker.swift`). #2346 (KRI-1403, "the approval card fits
one screen") rebuilt the content above the picker inside the approval card. Mode 1. The assertions compare
against 43.9, as `NativeEditorInspectorUITests` (43.9) and the chip test in this file (43.5) already do.
Mode 2 would still fail: a short row is far below 43.9.

## 2. Clean up speech: the approval card now fits one screen

`CreationUITests.swift:1419` and `:1430`: the `chat-jump-to-latest` pill never appears after scrolling up.

Failure modes:

1. The card fits the screen, so there is nothing to scroll past and the reader is never 80 pt from the end.
2. App: the pill is broken and never shows for a reader who is above the end.
3. The feed takes over between two checks of the "render stage stays" loop (line 1448, 2 of 6 attempts).

#2346 made the card fit on purpose (2,229 pt to 606 pt on a 656 pt chat) and pins it in
`LongApprovalCardUITests`. Mode 1. The pill precondition now applies only when the pill appears after the
scroll, so a card that is taller than the screen again gets the old check back. For mode 3 the loop accepts
the render stage or the plan feed. What this test no longer proves on today's layout: that a "Latest" pill
left up before approval is gone afterwards (the original KRI-1008 case). The end state is still checked.

## 3. Text preferences: "Top left", not "Top Left"

`AccountPrivacyUITests.swift:391`: `placement.label.contains("Top Left")`.

`TextRoleSpec.positionName` returned the capitalised key until #2347 (German and Turkish screens), which
made it the catalogue string `String(localized: "Top left")`, the same string the speaker and background
panels use. The assertion is case-sensitive. It now expects "Top left" and prints the label on failure.

## 4. Review sheet: reading a field the sheet removes while it updates

`CreationUITests.swift:3152`: `Failed to get matching snapshot: No matches found for …
"review-edit-caption-c2"`, at the first poll of an `eventually(timeout: 60)`.

Failure modes:

1. While a section updates, the sheet shows plain text with no identifier instead of the text field
   (`ReviewPlanModel.canChange` needs `!isUpdating`). Reading `.value` of a missing element records a test
   failure, so the poll fails the test before `eventually` can retry.
2. App: the update never finishes.

The fixture finishes an update after 8 reads of the plan (`ticksToReady`). The test passed only when the update landed
before XCUITest's second snapshot retry: +4.0 s on 2026-11-13 (pass), +4.7 to +6.9 s on the failing
attempts. Mode 1. `fieldText` returns `""` for a missing element; mode 2 still fails at the 60 s timeout.
The sibling `testReviewPlanUndoAllPutsEverythingBack` uses the same helper and failed 1 of 2 attempts on
two nights.

## 5. Long-press text timing: a 5 second wait for two 2-3 second polls

`NativeEditorInspectorUITests.swift:2836`, in `waitForStableRowRelation`: the wait needs two consecutive
equal polls. Each poll reads two frames and costs about 2 s on a normal night and about 3 s on
2026-11-18, when one wait got a single poll. The default timeout is 15 s. On that night's first attempt
the move-back drag also did not register; no screenshot survives, so that part is unexplained.

## 6. Caption switches: a tap that sometimes does nothing (not fixed here)

`NativeCaptionVisualUITests.swift:330`-`338` on 2026-11-18, and 1 failure in 3 on the verification run.

The test reads each switch right after tapping it, which is a race of its own. But with every read
replaced by a 10 s wait, the test still failed 2 of 6 attempts on the CI runner (run 44621577976): the
first tap on "Highlight spoken word" was synthesized and the switch never changed, 4.2 to 4.6 s after the
panel opened in passing and failing attempts alike. A second tap later in the same attempt worked. It does
not reproduce locally (20 of 20 pass, Xcode 27.2). Whether the switch was disabled, not hittable, or the
app refused the edit is not known yet, so this change leaves the test alone; a wait would only make the
failure slower.

## Before and after

CI Xcode 26.3, one `macos-15` runner, `-test-iterations 3`, no retry-on-failure, so a pass is 3 of 3.
Run 44612415554 had fixes 1 to 5; the caption test is unchanged.

| Test | Nightly 11-18 and 11-19 | Run 44612415554 |
| --- | --- | --- |
| Shape picker (1) | fail 3/3, fail 3/3 | 3 of 3 pass |
| Clean up speech (2) | fail 3/3, fail 3/3 | 3 of 3 pass |
| Text preferences (3) | fail 3/3, fail 3/3 | 3 of 3 pass |
| Review sheet (4) | fail 3/3, fail 2 of 3 | 3 of 3 pass |
| Review sheet sibling, undo all | fail 1 of 2 on two nights | 3 of 3 pass |
| Long-press text timing (5) | fail 3/3, pass | 3 of 3 pass |
| Caption switches (6), unchanged | fail 3/3, pass | 2 of 3 pass |

Local, Xcode 27.2 on an iPhone 17 Pro simulator: tests 1 to 5 and the sibling passed once each.

## What this does not fix

- The caption switch test (section 6). It fails about one attempt in three on the CI runner, so with the
  nightly's three attempts it will still turn a shard red now and then.
- The selection gap above. The tests will go stale again the same way unless a red nightly reaches
  someone, or pull requests that select `full` run more than smoke.
- 24 other UI assertions still compare a frame with exactly 44, 11 of them in `CreationUITests.swift`. Any
  layout shift can trip them as it did test 1.
- Seen failing on some attempts and not investigated: `testGalleryPickIsReadFromPhotosAndReachesUpload`,
  `testSendingALongPromptWithTheKeyboardUpAfterFifteenUploadsKeepsTheChatOnScreen`,
  `SlidePostUITests.testDraggingToTheEdgeAutoScrollsTheStrip` and
  `SlidePostUITests.testNewlyReadyAssetBecomesASlideWithoutTapping`.
