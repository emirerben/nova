# Editable split screen

The creator can compose two or three horizontal panels from ordinary image and video clips. A split section has its own duration. Each panel has independent media, source timing and crop. Full-screen clips can precede or follow it. Manual editing and AI editing must persist the same structure.

## Workflow

- [x] Read the Principles section of poteto-mode.
- [x] Phase A: Frame.
- [x] Phase B: Design the workflow.
- [x] Phase C: Run the loop.
- [x] Phase D: Keep the audit trail.
- [ ] Phase E: Verify and hand back.

Architecture steps are Ground, Sketch, Agree, Implement and Scrap. Agreement proceeds without a human checkpoint because the user authorized implementation. Read-only native, backend and editor explorations supply independent alternatives before integration.

## Done predicate

An iPhone editor fixture can create a two-panel section, change it to three panels, use images and videos together, edit each clip with its applicable existing controls, save, reopen, edit through the real AI operation boundary, then edit manually again. Preview and an inspected native export show the same source frames, panel crops, audio and full-screen boundaries. The stored document retains individual clips and their identities. No flattened intermediate substitutes for that document.

The acceptance matrix records implementation, offline verification and native verification separately. Any missing operation or unrun journey remains an open acceptance gap.

## Scope and throughput checkpoint

This crosses the persistent timeline, API validation, AI operation parsing and application, native document and session, timeline UI, recipe compilers and native compositor. At least six independently testable units are required. The risky unknown is simultaneous media on a timeline whose editor currently adds every clip duration in sequence.

Independent exploration uses three Astra agents, as requested by the user. Implementation will divide only across disjoint files after the shared contract is settled. Native builds remain serial and deferred until shared Mac load is checked. The worktree starts at `31477c8e9` on `codex/editable-split-screen`. No publication or deployment is authorized.

## Timeline decisions

1. The shared section projection owns clip windows, section windows and total duration. UI, inverse mapping, preview and server compilation consume that projection.
2. A panel-local trim or move preserves its neighbors. Changing a section's duration moves later sequential entries. Source-anchored content follows its clip. Canvas-global timed content follows the existing timeline rebase policy. Continuous music does not acquire per-panel shifts.
3. Scrubbing, playback, ruler ticks and selection geometry consume the same projected total duration.
4. Panel edges trim source playback and preserve rate. Source media bounds and the existing minimum clip duration apply. A gap displays the section background. Source clocks never restart merely because another panel begins or ends.
5. A gesture or compound section edit creates one document undo snapshot. Intermediate drag updates do not add snapshots.
6. Shared Python and Swift fixtures cover two and three panels, unequal durations, mixed media, gaps, exact boundaries, independent crops and clocks, full-screen neighbors and legacy timelines. Native render artifacts establish visual fidelity separately.

## Failure cases to encode before implementation

- A panel is silently decoded as an ordinary sequential clip, lengthening the video.
- Save, AI context projection or a recipe whitelist drops membership or section metadata.
- A panel inherits overlay restrictions that mute its video or discard its transform.
- Two clips borrow the same source clock or source crop.
- A gap exposes another panel or leaks the preceding full-screen image.
- A boundary draws two sections for one frame or drops the first full-screen frame.
- Trimming, splitting, moving or extending one panel shifts another panel's media.
- A section edit changes global duration but leaves captions, overlays or ruler geometry stale.
- A stale AI draft resurrects deleted media or restores the old layout after a manual save.
- An old client accepts a recipe and silently renders it without panels.
- A source label is projected onto the wrong simultaneous clip.
- An export path and preview path use different placement or audio rules.

## Existing behavior constraints

The current native editor has singular selection. General multiselection is not an existing capability to preserve. Existing format and renderer capability restrictions still apply and must be visible. Their absence must not be reported as split-screen support. Captions, overlays and drawings retain the existing canvas-wide placement controls. Source-owned timing follows the selected occurrence; it is distinct from spatial clipping. This change does not introduce a new panel-local overlay anchor.

Related code includes speaker full-screen windows from #2111, source-time label projection from #1306 and manual save precedence from #1655. They are regression neighbors, not implementations of this feature.
