# KRI-1450: editor timeline UI tests red on main after the timeline glide

## Incident

#2317 (741617e17, KRI-1410, 2026-11-14 00:01 UTC) made a timeline flick glide on after release
(`startCoast` in `NativeEditorMediaViews.swift`). XCUITest's quick drags (`press(forDuration: 0.05, thenDragTo:)`)
release fast enough to start that glide, so every editor UI test that asserts where a drag lands broke. They
fail on every PR, because the UI shards test the PR merged into main. #2317's own PR ran only the iPhone unit
leg, and main push runs skip the iPhone legs.

PR #2333's CI run 43575876817 failed 8 UI tests, 7 of them distinct:

- `NativeEditorInspectorUITests`: `testTimelineSwipesSeekUnderFixedPlayhead`,
  `testQuickSwipeStartingOnTextScrubsWithoutMovingBlock`, `testTappingClipAlignsItsBeginningUnderPlayhead`,
  `testLongPressMovesTextTimingWithoutOpeningInspector`, `testSplitPanelTrimCutDuplicateAndUndoKeepPeersExact`;
- `SplitScreenAcceptanceUITests`: `testPreviewFollowsTheDividerWhileTheFingerIsStillDown`,
  `testCustomThreePanelLayoutSaveReopenAndFullscreenBoundary`.

## Bisect (iPhone Air simulator, iOS 26.5, arm64 Debug, `-test-iterations 3`)

Test: `testTimelineSwipesSeekUnderFixedPlayhead`.

| Tree | Result |
| --- | --- |
| dcdd1ef45 (no iOS commits between it and 741617e17^) | 3/3 pass |
| 741617e17 (#2317) | 3/3 fail |
| 56ff759ec (main) | 3/3 fail |

The time read 0:01.3–0:05.9 where 0:00.0 was expected.

## Fix

The glide stays off when the app runs with `-ui-testing-editor`, unless a test opts in with
`-ui-testing-editor-timeline-glide`. Real users and Reduce Motion behave as before.

## Evidence (base 63da5ce11)

- **Red, before the fix:**
  - `testTimelineSwipesSeekUnderFixedPlayhead` failed 3 times (0:05.9, 0:05.9, 0:05.1 instead of 0:00.0).
  - The new `testFlickGlidesOnPastTheReleaseOnlyWhenTheGlideIsSwitchedOn` failed at "in editor test mode the
    timeline stops where the finger let go": the time read 0:03.1 at release and 0:05.3 after.
- **Green, with the fix:**
  - Both tests passed 3/3 (`-test-iterations 3`).
  - All 7 distinct UI tests that failed in run 43575876817 passed in one run.
- **Negative control:** with the opt-in ignored (the glide off for every editor test), the new test failed at
  "with the glide switched on, the same flick carries on past where it let go" (2.1 s, not more than 2.4 s).
  The fix was then restored.

## Not covered

- The full UI suite was run only in CI.
- A real-device feel check of the glide is unchanged and still owed under KRI-1410.
