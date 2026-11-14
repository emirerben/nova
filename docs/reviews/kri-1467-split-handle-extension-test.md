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
