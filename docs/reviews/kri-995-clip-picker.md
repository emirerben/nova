# KRI-995: clip picker Next between questions, tap to watch

Run on iPhone Air simulator, iOS 26.5, `xcodebuild ... test-without-building` against `src/apps/ios/Kria.xcodeproj`
(fixture `KRIA_CHAT_CLIP_QUESTION=1`: Dodgeball with clips 1-3, clip 2 suggested; Football with clips 3-4).

## Red control

The two new tests, run against the unchanged picker (tests only, app code from `origin/main` c4f0aa826):

```
CreationUITests.swift:997: testTwoQuestionPickerNextShowsTheSecondQuestionAndDoneSendsBoth :
  XCTAssertEqual failed: ("Done") is not equal to ("Next")
CreationUITests.swift:1035: testTappingAClipOpensItPlayingAndUseThisClipPicksIt :
  XCTAssertTrue failed - a tap opens the clip
```

The first is the reported bug: with the first group answered (its suggestion), the only button already said
Done and sent that one answer.

## Green

| Test | Result |
| --- | --- |
| `testTwoQuestionPickerNextShowsTheSecondQuestionAndDoneSendsBoth` (Next moves on and sends nothing; tabbing back still offers Next; Done sends `dodgeball=fixture-clip-2;football=fixture-clip-4` as one message) | passed |
| `testTappingAClipOpensItPlayingAndUseThisClipPicksIt` (tap opens a playing clip without picking; Use this clip picks it and returns; circle picks without opening; a clip not on the device shows its poster and note) | passed |
| `testClipPickerSlideAcrossTilesSelectsDeselectsAndKeepsCircleAndTapToWatch` (renamed; tap now opens instead of hold) | passed |
| `testClipPickerSlideNearBottomEdgeAutoScrollsAndVerticalDragStillScrolls` | passed |
| `testRealShapeHistoryShowsClipQuestionCardAndOpensTheGridSheet` (48 clips; pager swipe, Use this clip picks the clip shown) | passed |
| `testClipPickerSelectsClipsAndSendsStructuredAnswerWithMissingThumbnailFallback` | passed |
| `testPickerShowsAVisualsPhotoWithItsThumbnailAndAPhotoName` | passed |
| `testTitleCardAndClipPickerInOneReplyGoOutOnOneTurn` / `testClipsFirstThenTheTitleTapSendsBothOnOneTurn` (KRI-802 batch) | passed |

9 tests, 0 failures. `KRIA_IOS_TEST_MODE=unit bash scripts/ios/verify.sh`: 2081 unit tests, 0 failures.

The playing-clip screenshot shows the generated fixture video mid-play (it changes colour each second). The preview now
draws with `SongOrderPlayerSurface` (a bare `AVPlayerLayer`), not SwiftUI `VideoPlayer`, whose iOS 26 controls stall
XCUITest's idle tracking.

## Why the clips "could not be watched"

Pulled from Yasin's iPhone (dev app `com.yasin.kria.dev`, `devicectl ... copy from`): every phone project keeps its
originals under `KriaProjects/<project>/originals/` and binds the chat media id (`analysis-proxy-ios-<UUID>.mp4`) to
that file in `source-assets.json`, so `CreationMediaPreview.localOriginal` resolves and the preview can play. The
blocker was that watching needed press-and-hold or a 26 pt corner icon; a tap selected.

## Not covered

- A device run of the new picker (the dev build with this change is installed on Yasin's iPhone 14; not yet exercised).
- A clip whose original is not on this iPhone still shows only its poster: the server has a copy (analysis proxy or
  cloud original) but sends no playable URL. Needs an API field; left out of this iOS-only change.
- Behaviour change: Done no longer sends a partial answer for multi-group questions. A creator who wants Kria to
  decide one group can still use "Skip, decide for me" for the whole question, or "None of these" per group.
