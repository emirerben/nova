# KRI-1467: editor UI tests red on the CI iPhone 17 Pro

On 2026-11-14 three editor UI tests failed on every PR whose iPhone UI legs selected them (#2333, #2334). CI runs
them on an iPhone 17 Pro simulator (402 × 874 pt, iOS 26.2).

## Split-panel tests: fixed on main by #2361

`testSplitPanelHandleExtensionGrowsSectionAndClampsAtMinimumWithoutMovingPeers` and
`testSplitPanelTrimCutDuplicateAndUndoKeepPeersExact` bisect to fc0aaa58a (#2315, KRI-1399). In the first test,
the trailing trim handle hangs past the block end, so the extension drag ends in the timeline's 56 pt edge
auto-scroll zone. In the second, selecting a bottom-panel row scrolls the playhead knob out of reach.
Emir's #2361 (KRI-1379) fixes both. This branch takes main's version of that file.

## `EditorUITests.testEditorIsReadyBeforeTheTap`

There is no first-bad commit. On iPhone 17 Pro it passes at every bisect step from dcdd1ef45 to main. It failed
1 run in 7 locally and on CI for both #2333 and #2334. The test gave the background editor warm a fixed 4 s
before tapping Open editor. On a slower machine the warm is not finished yet, so the tap finds "Preparing preview…"
and a source is hashed after the tap.

Fix: after the 4 s read, wait up to 30 s for the warm's end marker before sampling the counters and tapping.
The marker is `t editor.open.done`, set at the end of the editor load, after the preview build. The assertions
after the tap are unchanged, so a warm that never finishes, or a tap that still does heavy work, fails as before.
