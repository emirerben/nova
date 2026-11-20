# iPhone split timeline gesture checks

The creator reported a gap immediately before the Kria outro. A read-only inspection on 20 November 2026 found a fixed three-panel section lasting 13.566667 seconds. The top and bottom clips each ran from 0 to 10 seconds. The middle clips met at 4⅔ seconds and ended at 10 seconds. All four slots contained footage. These timings supplied the expected result for the simulator replay.

On an iPhone 17 Pro simulator running iOS 26.5, removing the empty ending shortened the section to 10 seconds. The UI test compared all four slot records before and after, then used Undo to restore the original section. The replay used fixture media.

On the original editor, holding a clip and dragging it into an empty panel left it in panel 0. The regression assertion expected panel 1 and failed. On the updated editor the same gesture moved it into panel 1. Dropping onto occupied footage exchanged both slot identities between panels. An ordinary clip still reordered with one undo step, and a quick horizontal swipe still sought through the timeline.

The vertical-scroll check started over the footage row, moved the row up, then swiped down from inside the timeline and recovered its original screen position. During investigation a return swipe opened the text inspector. Recording vertical travel as a pan prevented the release from selecting the block.

The divider UI check sampled pixels on both sides of the moved divider while the gesture was held. The opt-in simulator benchmark completed all 80 layout updates without a timeout. That check observes the composition's layout. It does not measure physical display latency.

Run the editor checks from the generated Kria Xcode project with the Kria scheme:

- `KriaUITests/SplitScreenAcceptanceUITests`
- `KriaUITests/NativeEditorInspectorUITests/testLongPressDraggingClipReordersKeepsLengthAndUndoesInOneStep`
- `KriaUITests/NativeEditorInspectorUITests/testQuickSwipeAcrossAClipSeeksInsteadOfReordering`
- `KriaTests/NativeSplitScreenFlowTests`
