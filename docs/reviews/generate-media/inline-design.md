# Inline Visuals adaptation

Emir selected option 1 and clarified that Generate media must live inside the existing Visuals tab. Approval must add an ordinary image block that can move and use all existing visual controls. This supersedes the separate Generate media sheet design.

The selected references are the local option-1-revised-generation.png and option-1-revised-timeline.png mockups from the design exploration. They are illustrative references, not app evidence. Paper remains blocked and was not retried.

## Implementation

NativeOverlayGenerationPanel replaces OverlayGenerationSheet inside NativeVisualPanel's existing Add visual content. It has no NavigationStack, navigation destination or generator sheet. The original video preview and native panel shell remain in place. Prompt mode and Let Kria choose share the same compact panel.

Generate preview plans the moment and prepares one image preview. The image remains pending until Add to timeline. The existing GeneratedMediaSheet request/recovery logic supports an embedded presentation; its default attachment-sheet presentation remains available.

Approval uses NativeVisualAuthoring.plannedOverlay and NativeEditorSession.applyOverlayProposal to insert kind media, media_kind image, origin user. Selection then opens the ordinary photo inspector. Timing, transform, layer order and Animation use the same functions as imported images. No generated-only block type or edit lock is added.

Existing proposals show Edit in timeline. The action selects the existing block before constructing a generation draft. Automatic mode omits a previous prompt so hidden prompt text cannot influence it.

## Acceptance checks

OverlayGenerationUITests exercises the real native Visuals panel with offline HTTP/provider responses. It verifies an image preview before insertion, explicit Add, a single ordinary visual block after repeated Add, normal Edit and Animation controls, one-step Undo and Redo, reopening an applied proposal without generation, Save acknowledgment and saved reopen. The full source-pool fixture uses real local original resolution and compositor preparation. This is client integration evidence, not a live image-model quality evaluation or a production server persistence claim.

OverlayGenerationTests.testGeneratedImageUsesOrdinaryVisualCustomizationAndSnapshotPersistence checks the normal image record, later position and size changes beyond the planner defaults, timing, zoom, rotation, layer movement, Undo and snapshot reopen.

The existing generation UI tests cover narration approval, unavailable capabilities, image acceptance, process-relaunch recovery and explicit resubmission after rejection. A legacy test once failed during format-picker setup before reaching generation; its isolated rerun passed. Earlier inline fixture runs also exposed missing source metadata and were not accepted as render proof. The current fixture supplies complete source and recipe receipts instead of suppressing preview failures.

## Delivery scope

This adaptation is local only. The earlier branch-publication and Linear-project denials remain in effect. No publication retry, Paper retry, production edit or provider-spend activation occurred.

## Final local result

The completed overlay-inline-delivery.xcresult reports 3 passed, 0 failed: both real native OverlayGenerationUITests and the ordinary-image customization/persistence unit test. The process exited 0 with TEST SUCCEEDED. The image UI run includes approval, duplicate Add, ordinary controls, Undo/Redo, Save acknowledgement and saved reopen with a working source preview. The automatic/text run verifies an empty automatic prompt after previously typing in prompt mode. Earlier adaptation runs passed 35 selected native unit tests, including transparent PNG compositor export; those are earlier-run evidence, not a repeated full-suite claim.

Final app screenshots use offline image/provider fixtures, not generated-art quality evidence:

| Artifact | Confirmed Library ID |
| --- | --- |
| Inline prompt | libfile_1c764cae9fd08191b60860cfd146b638 |
| Inline preview | libfile_21f6e8eef3b481919bca5436fe3c7387 |
| Ordinary image controls | libfile_84a0c83437e08191a9c8ffb5bcdfb78f |
| Saved image timeline | libfile_011b05c9e8408191ab99b6658a39b58f |

Local PNGs are in /Users/emirerben/Documents/Codex/2026-10-30/task/overlay-inline-delivery/. The result bundle and log are in the task root. Independent review found a repeated-spend risk when reopening an applied proposal; the action now selects its existing timeline block before creating a generation request, and the reviewer verified the fix.

Model the Domain shaped the ordinary-media-block representation. Prove It Works shaped the complete-source fixture and rejection of earlier missing-source screenshots as render evidence. Physical-device behavior, live-model taste and production server persistence remain outside this local acceptance run.
