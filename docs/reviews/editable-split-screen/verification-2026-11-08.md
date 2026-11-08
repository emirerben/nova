# Split-screen verification on November 8

Tracker: [KRI-1231](https://linear.app/kria/issue/KRI-1231/build-editable-split-screen-videos). The branch was integrated with `origin/main` through the AI-halo merge (#2145) and the protocol collision was resolved. Earlier results in [the November 7 record](verification-2026-11-07.md) describe older heads; this record covers the integrated head.

## Integration changes

- Main took protocols 44 (KRI-1264) and 45 (KRI-1253). The split-screen floor is now **46** (server 426 floor, editor refusal, `splitScreen` admission, Swift contract, tests). The formal split-protocol verifier cases moved to 45/46 and its function pin was recomputed. The pinned function differs from the old pin only by the two protocol literals and main's unrelated card-style gate, and the verifier then passed with its mutant still caught.
- Merge conflicts kept both sides: the halo's retained-preview guards and main's merge-review guards, main's rewritten chat-draft merge (which replaced the old deletion-honouring helper), the speaker-occurrence selection with main's pinned-text, drawing and centre-snap gestures, and the compositor's `speakerSliceClipID` together with `playbackRevision`.
- Two defects surfaced by the integration were fixed. Main's "selected item wins a drag" path called `speakerIsDrawnNow()` without the occurrence selection (compile error). The engine accepted any device original as a supporting-card photo, which broke main's `testStillLayoutValidatesOnlyOnMainTrackVisualPhotos`; the allowance is now limited to clips with split-screen membership.
- Unstacking with `keep_panel 0` dropped a title that sat over both panels, because the time projector anchored overlapping parallel segments to the last one, which was the removed panel. The projector now prefers an overlapping segment that survives. A red-first regression covers both kept panels with and without an empty section tail.

## Results

| Check | Head | Result |
| --- | --- | --- |
| iOS unit suite (`KRIA_IOS_TEST_MODE=unit make ios-verify`) | `a6882ab4d` | 3,111 run, 35 skipped, **0 failures** |
| Targeted native batch: protocol/Save-error/split units plus 15 split UI selections | `7b86babee` | 145 passed, 0 failed, 1 skipped. `testSplitSectionLongPressDragMovesEveryPanelAndUndoesOnce` failed attempt 1 on a geometry assertion and passed on retry |
| Media engine package (`swift test`) | `a6882ab4d` | 554 run, 10 skipped, **0 failures** |
| Request-to-native-output replay (2- and 3-panel, positive and wrong-content controls, preview and export) | `97c2dd507` | 4/4 passed; frames inspected |
| Live provider check of the split edit prompts (prompt v119, `gemini-3.1-pro-preview`) | `13aad28fa` | 5/5 passed, $0.39 on the development ledger |
| Affected API suites (kria, split ops, wire contract, editor ops, routes) | `099340762` + fixes | 10,509 passed; 7 failures in render-review admission tests need real GCS storage (fake bucket in this environment) and are not split code |

The replay's first execution failed three of four cases: saturated primaries read back shifted (red 255/43/0, blue 3/57/255, magenta 255/73/255), identically in preview and export and at every panel position, with correct panel content. The shared `SolidVideoFixture` writes untagged H.264. The oracle tolerance, authored as 45 before the harness first ran, is now 80 (worst measured channel 73). Wrong-content samples differ by at least 200 on some channel and every control is still rejected. The fixture's colour tagging was not changed.

The live check covered create, a three-panel request with a reorder, a resize plus swap on an existing split, unstack and a Turkish request. The model replies parsed and compiled. The raw replies are not kept in the repository.

## Still open

- Aggregate formal alignment (36 recorded drifts) was not rerun; only the scoped protocol verifier was.
- Physical-device acceptance and broader shared recovery on a device are not claimed.
- Live provider behaviour is covered only by the five prompts above.
