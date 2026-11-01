# KRI-1101: a pre-ticked clip question is shown before Done

Incident: Leverkusen M2 run 2, 2026-10-31 (thread `e81aff13-…`, clip question `c6d69c72-…`). Two questions: "the TOR on
the big screen" (no pre-tick) and "the moments where Taha is celebrating in both goal clips" (`suggested_media_ids` =
one clip). After a tick on question 1 the main button already said Done, and Done sent question 2's pre-tick unseen.

Cause: `ClipSelectionState.isAnswered` is true for any tick, and suggestions start ticked, so a pre-ticked question
was "answered" before it was opened. `nextUnanswered(after:)` found nothing, and the button read Done. KRI-995 (#1877)
built Next between questions but only tested a pre-tick on the FIRST question, which is on screen when the picker opens.

Fix: the state records which questions the picker has shown (`markShown`, from the sheet's `onAppear` and
`onChange(of: categoryKey)`, so Next and the tabs both count). `isDone` = shown and answered; `nextPending(after:)`
walks to the first question after the visible one that is not done. Pre-ticks stay as the starting selection.

## Failure modes listed before the code

1. A pre-ticked later question counts as answered before it is shown (the incident).
2. A pre-ticked first question is on screen at open, so Next must still appear while question 2 is pending.
3. Visiting a question by its tab counts as seeing it.
4. A seen question the creator emptied still needs an answer: Next goes there, never Done.
5. One question: unchanged (a tick or "None of these" gives Done at once).
6. Three or more questions: the walk wraps and never skips a pre-ticked unseen one.
7. No state write inside `body`: marking happens in `onAppear`/`onChange`; the button treats the visible question as seen.
8. Closing and reopening the sheet keeps what was seen (it lives in the card-owned state).
9. Slide-to-select rebuilds state from a snapshot and must keep the seen set.
10. Keeping a seen pre-tick unchanged and pressing Done sends it, as before.
11. `clip-done`, `clip-none-<key>` and the "Shows <label>" hint are unchanged.

## Red control

New fixture `KRIA_CHAT_CLIP_QUESTION=later-suggested` mirrors the incident: Dodgeball (clips 1-3) without a suggestion,
Football (clips 3-4) with clip 4 suggested. The new UI test, run against the unchanged picker (app code at 7f9cc18dc,
iPhone 17 Pro Max simulator, iOS 26.5):

```
CreationUITests.swift:1224: testPreTickedSecondQuestionIsShownBeforeDoneAndCanBeCorrected :
  XCTAssertEqual failed: ("Done") is not equal to ("Next") - the pre-ticked second question has not been shown yet
CreationUITests.swift:1227: ... No matches found for Elements matching predicate '"clip-sheet-title" IN identifiers'
```

The second line is the incident itself: the tap that should have shown Football sent the answer and closed the picker.

## Green (same simulator, with the fix)

| Test | Result |
| --- | --- |
| `testPreTickedSecondQuestionIsShownBeforeDoneAndCanBeCorrected` (tick Dodgeball -> Next; Football shown with clip 4 ticked; Done; add clip 3; one turn sends `dodgeball=fixture-clip;football=fixture-clip-3+fixture-clip-4`) | passed |
| `testTwoQuestionPickerNextShowsTheSecondQuestionAndDoneSendsBoth` (pre-tick on the first question) | passed |
| `testClipPickerSelectsClipsAndSendsStructuredAnswerWithMissingThumbnailFallback` (second question reached by its tab, then Done) | passed |
| `testTappingAClipOpensItPlayingAndUseThisClipPicksIt` | passed on rerun (first run failed in `createFreshChat` before the picker: "What are we making?" not shown in 20 s) |
| `testClipPickerSlideAcrossTilesSelectsDeselectsAndKeepsCircleAndTapToWatch` | passed |
| `testClipPickerSlideNearBottomEdgeAutoScrollsAndVerticalDragStillScrolls` | passed |
| `testRealShapeHistoryShowsClipQuestionCardAndOpensTheGridSheet` (one question, 48 clips) | passed |
| `testPickerShowsAVisualsPhotoWithItsThumbnailAndAPhotoName` | passed |
| `testTitleCardAndClipPickerInOneReplyGoOutOnOneTurn` / `testClipsFirstThenTheTitleTapSendsBothOnOneTurn` (KRI-802 batch, one question) | passed |
| `KriaTests/ClipSelectionTests` (incl. new `testPreTickedGroupsAreNotDoneUntilShown`, three questions) + `KriaTests/DragSelectTests` | 45 passed |

Screenshots (`pre-ticked-second-next`, `pre-ticked-second-corrected`, attached in the test's result bundle) show Next
on Dodgeball with clip 1 ticked, then Football with the "Suggested" clip 4 ticked, clip 3 added and Done.

## CI coverage

The fixture lives in the shared `ChatUITestTransport.swift`, which `scripts/ios/ui_tests.py` does not map to a group, so
the PR's selector picks `full`, and PR CI turns `full` into the smoke set (`ci.yml`). The picker tests above therefore
run locally only on the PR, and in the full suite after merge.

## Not covered

- A device run on Yasin's iPhone with the Leverkusen M2 footage.
- The server's wrong pre-tick itself (one of the two goal clips) is KRI-1089; this change only makes it visible and
  correctable before sending.
