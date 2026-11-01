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

## Authorized publication and CI follow-up

After the earlier denials, Emir explicitly authorized publishing tested commit afc6fde084 to emirerben/nova on codex/generate-media-overlays, opening a draft PR and creating an implementation ticket in the existing Kria project. Readbacks confirmed PR #1956 and KRI-1064 in iOS editor polish. No new project was created. Current main merged without conflicts as fb6e0a8a48. All preship gates passed again.

At fb6e0a8a48 the full focused native run passed 37 tests, zero failed: 35 units including actual transparent PNG compositor export and both inline UI journeys. Persistent Library evidence: summary libfile_be0456c75c1c8191b4f5163360ea6f43 and log libfile_0ffc83636e088191a793a6289fbb3c7d.

CI API shard 1 identified two integration omissions: missing capability parity classifications for the new editor methods and absent creator-language input to OverlayGenerationAgent. The fix classifies explicit overlay approval as agent-unsupported (requires human native preview acceptance), classifies planning/clock/identity methods as read-only, and carries the prompt/device language into the agent with prompt_version 2026-10-30.3. Local failing guards plus overlay tests and all free replay evals then passed: 2378 passed, 27 skipped, 7 expected failures. No paid model requests. CI is still required at the new head; broader acceptance/rollout gaps remain on KRI-1064.

## Replacement preview after partial source registration

Independent current-main review found that source registration used the proposal ID alone. After accepted registration followed by a lost response or local failure, generating a different image for that proposal reused an ID already bound to the first asset. The server correctly rejects changed payloads with import_id_reused.

The native recovery regression first reproduced this failure: the original preview stayed recoverable, but the replacement could not enter the timeline. Registration now uses a deterministic UUID per proposal/asset pair; the ordinary timeline block still uses the stable proposal ID. The fixture retains the accepted first registration before losing its response and rejects changed sources under reused IDs. It does not suppress the server contract. Independent review verified the repair.

The complete focused native run then passed 38 tests, zero failed: 35 units including transparent-overlay export and all three real UI journeys. Result bundle: /Users/emirerben/Documents/Codex/2026-10-30/task/overlay-registration-green.xcresult; red control beside it as overlay-registration-red.xcresult. Remote CI must be verified again for the repair; the previous exact head had all Linux checks green with three native legs still queued.

## Reference-order grounding control

A second boundary review reproduced order-dependent grounding: an early explicit-time or visual-window check could pass before a later speech citation moved the start elsewhere. Six conflicting-reference permutation cases and two valid mixed-reference cases were added before repair; the original implementation failed five of them.

Speech anchors now resolve first and must agree. Every visual window and explicit timestamp is then checked against the final start, followed by the existing bounds, Carousel and collision guards. Independent review ran the eight cases and original single-speech case: nine passed. The full selected overlay/language/parity/free replay run passed 2386 tests, with 27 skipped and seven expected failures. Native source remains the 38-pass recovery-repair source. No paid calls.

## Cross-deploy generated-request replay

The optional transparency field initially changed model_dump output even for older opaque requests, changing their stored idempotency digest. Independent executable review confirmed same-key POST retries would return 409 across deployment; GET-by-event recovery was unaffected. A real isolated PostgreSQL regression reproduced the conflict before repair.

False transparency is now omitted only from the digest payload, retaining the prior serialization and every other field. True remains hashed so changing an opaque request into a transparent request under the same event ID is still rejected. The regression verifies replay returns the same operation with one offline provider call, and that a transparency change does not cause another call. Generated service/admission/transport/routes, credit contracts and overlay controls passed: 68 passed, one skipped, 24 subtests. Independent review found no new issue. The database was newly created empty for this test, with offline transport and storage; no live request or credential change occurred.
