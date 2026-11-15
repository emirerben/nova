# KRI-1467: split-panel trim extension UI test drags into the edge auto-scroll

`NativeEditorInspectorUITests.testSplitPanelHandleExtensionGrowsSectionAndClampsAtMinimumWithoutMovingPeers`
fails on main for every PR whose iPhone UI shards select the editor group. It failed on PR #2333, run 43639622312,
ui-1: extension 6.9 s against a 6.5 s limit. main push runs skip the iPhone UI legs, so main looks green.

## Bisect

Local, iPhone Air simulator, iOS 26.5, `git bisect run` over `src/apps/ios`:

- dcdd1ef45: pass 2/2
- 741617e17: pass
- fc0aaa58a (#2315, KRI-1399): **first bad**
- 3e063e1f0, 115cd34a0, 71784d4ed, c97850fbb (main): fail

## Cause

Measured with temporary logging (block and handle frames, app width 420 pt; the timeline's edge auto-scroll
zone starts 56 pt from the edge, at x = 364):

| | trailing handle box after the 1.4 s shortening | drag start | drag end | extended |
|---|---|---|---|---|
| before #2315 (dcdd1ef45) | 242.7–286.7 | 264.7 | 372.0 | 5.7 s |
| after #2315 (main) | 264.7–308.7 | 286.7 | 394.0 | 7.8 s |

The test's extension drag was always long enough to end inside the edge zone. Before #2315 it went 8 pt in, and
the held finger's auto-scroll added about 0.3 s. #2315 gave a short block's trailing handle a full 44 pt touch box
that hangs past the block end, so the box's centre moved 22 pt right. The drag now ends 30 pt deep and the
auto-scroll adds about 2.4 s. The app behaves as designed: a finger held at the timeline edge keeps extending.

## Fix (test only)

The extension drag starts at the inner end of the handle box, which lies over the block. It stops 4 pt short of
the edge zone, so no auto-scroll runs. The test now checks that the clip grows by exactly the drag
(shortened duration + distance / points-per-second, ±0.15 s) instead of an upper bound. It also checks there is
room to pass the 4 s section end (expected > 4.6 s).

## Evidence

- Fixed test on main c97850fbb: pass 3/3 (`-test-iterations 3`).
- Unfixed test on main: fail 2/2. On the #2333 head: fail 3/3.

## Two more red UI tests on the CI device (iPhone 17 Pro, 402 × 874 pt)

The ui-3 leg of #2333, and of #2334 (a different diff), also failed
`testSplitPanelTrimCutDuplicateAndUndoKeepPeersExact` and `EditorUITests.testEditorIsReadyBeforeTheTap`. Both pass
on an iPhone Air (420 × 912 pt) and fail on an iPhone 17 Pro simulator, which is the CI destination's model. CI
runs iOS 26.2; this Mac has only 26.5.

### `testSplitPanelTrimCutDuplicateAndUndoKeepPeersExact`

Bisect on iPhone 17 Pro: dcdd1ef45 and 741617e17 good, fc0aaa58a (#2315) first bad. #2315 scrolls a selected
bottom-panel row clear of the clip toolbar (`scrollSelectedClipRowClearOfIsland`). On the shorter screen that scroll
(about 50 pt) takes the ruler and the playhead knob above the lane's visible top, so `playhead.isHittable` is false
(:361). Everything after it cascades: the cut never happens (3 ≠ 4 rows), and the later 3.5× pinch lands on a
smaller element ("Invalid scale 3.50 greater than maximum possible scale 2.96").

A vertical swipe over the blocks does not scroll the lane, because the timeline's scrub gesture takes it.

Fix: when the knob is not hittable, drag the lane down from its title column, then drag the playhead as before.
The app is unchanged.

Product note: on a 6.3-inch phone, selecting a bottom split-panel clip hides the playhead knob until the creator
scrolls the lane back. Seeking by swiping the timeline still works.

### `EditorUITests.testEditorIsReadyBeforeTheTap`

This one has no first-bad commit. It passes at every bisect step, including the last iOS commit on main, and fails
about 1 run in 7 locally on iPhone 17 Pro. On CI it failed on both #2333 and #2334. The test waited a fixed 4 s for
the background editor warm before tapping. When the warm is slower than that, the tap finds it unfinished:
"Preparing preview…" shows and a source is hashed after the tap.

Fix: after the 4 s read, wait (up to 30 s) for the warm's end marker, `t editor.open.done`. It is set after the
editor load, including the preview build. Then sample the counters and tap. The assertions after the tap are
unchanged.
