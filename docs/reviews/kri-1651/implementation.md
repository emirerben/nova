# KRI-1651 verification record

## Root causes observed

The scoped incident diagnostic found that the original chat treated the photo duration as the total video length. It found no durable editor Save receipt or rejection code. Historical route logs could not be accessed. No original project was mutated, and the generic Save failure remains open.

The local red control used a guided revision with photos at source indexes 1 and 3 and a two-entry legacy video path list. Three photo-only requests incorrectly established the entire edit's target duration. The guided bulk operation selected photos in the parser but wrote neither photo's requested duration in the compiler. On the base checkout, four tests failed and the whole-video control passed.

The fix classifies the scoped duration separately, supplies the existing kind selector for a standalone photo request, reads timeline media kind from slot metadata instead of legacy video paths, and verifies photo timings separately from whole-edit duration. The native order sheet uses occurrence identities, not media IDs, and shares the chat sheet's `ClipOrderDraft` order model. The new montage upload control reuses the existing phone Visuals pool without sending a new upload role.

## Evidence boundaries

Offline checks exercise the actual copilot parser, editor compiler, guided segment Save builder, and receipt checks against synthetic sources. The tests retain video lengths and assert both photo lengths at exactly 0.2 seconds. A wrong-photo-duration control retains the video lengths but cannot pass the photo timing receipt. An explicit overlay request with no overlay must clarify instead of changing timeline photos.

Native UI tests are separate from the offline check. The inspected synthetic native export is evidence for the timing boundary, not the original project or a production rollout. No live model call was made and no original incident project was re-rendered. The generic Save rejection and device export remain open in KRI-1651 until those paths have evidence.

## Native simulator and replay evidence

- `bash scripts/check-api.sh test tests/services/test_photo_timeline_duration.py tests/services/test_kria_editor_ops.py tests/services/test_kria_editor_guided_timeline.py tests/services/test_length_policy.py -q`: 122 passed.
- `bash scripts/check-api.sh test tests/evals/test_edit_copilot_evals.py tests/evals/test_brief_extractor_evals.py tests/evals/test_main_creator_evals.py -q`: 380 replay tests passed. Two unrelated caption-scope fixture pins were updated because the added photo-only prompt guidance cannot change their recorded answers. No live call or paid judge was made.
- iPhone 17 Pro simulator, iOS 26.5: `NativeEditorInspectorUITests/testReorderSheetAppliesTheWholeOrderInOneUndoStep` and `NativeSplitScreenRowsTests/testSerialReorderMovesClipLocalLanesButKeepsSongAndSpanningOverlay` passed. The UI test changed the finished editor's order and undid it; the mixed lane test moved a clip-local caption and overlay without moving the song caption or spanning overlay.
- `RenderedOriginalClockTests/testMixedMontagePhotoQuickCutReordersReopensAndExportsSixFrames` added a native export red control. Before fixing the compositor boundary, FFprobe showed a 2.1-second, 63-frame 30fps export; FFmpeg decoded frame 57 as black and frames 58–62 as red. The compositor tiled instruction ranges on its media clock but re-tested floating-point layer membership at the frame boundary. The fix uses the same 60,000-timescale clock for both tests.
- Rerun after the compositor fix passed on iPhone 17 Pro simulator. The test applied the photo duration through `NativeEditorSession`, confirmed the reversed order, rehydrated the encoded editor snapshot, compiled the native preview and exported a real MP4 through `AVFoundationLocalExporter`. Separate FFprobe/FFmpeg inspection confirmed 57 blue video frames then exactly six red photo frames, with no black frame, for 63 frames at 30 fps and 2.1 s total. See `native-frame-evidence.json`. This tests a synthetic saved-snapshot hydration, not the affected account's live Save API.

- The guided-phone Save-core integration test starts from a server-admitted photo outside the legacy video paths, saves it on the timeline, parses the photo request even when the recorded model answer says total length, compiles the retime/reorder, executes `prepare_editor_commit`, and reads the new native device recipe plus reopened editor snapshot. The result is a 0.2-second photo followed by the unchanged 1.0-second video.
- Rebased on `5c11326681` (CA-8). Regenerated the Xcode project because main added Swift files; the final seven native tests passed, and the independent frame evidence matched the earlier export byte-for-byte in the reported measurements. The 902 selected reliability checks and the request evidence gate passed.
