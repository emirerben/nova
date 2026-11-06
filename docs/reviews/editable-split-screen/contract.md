# Shared split-screen contract

The timeline remains one atomic editable value. Its `slots` retain ordinary media fields and identities. Its optional `split_screen_sections` array owns simultaneous layout and duration. Old timelines omit the array.

```json
{
  "slots": [
    {"slot_id":"a", "clip_index":0, "in_s":2, "duration_s":4,
     "split_section_id":"section-a", "split_panel_index":0, "split_offset_s":0},
    {"slot_id":"b", "clip_index":1, "in_s":7, "duration_s":3,
     "split_section_id":"section-a", "split_panel_index":1, "split_offset_s":0.5}
  ],
  "split_screen_sections": [
    {"id":"section-a", "duration_s":4, "panel_count":2,
     "panel_weights":[1,1], "gutter":0, "background_color":"#000000"}
  ]
}
```

Section members occupy one contiguous block of the slot array. At the first member, the serial cursor advances by the section duration exactly once. Every member starts at that section's start plus its own `split_offset_s`. Panel indexes are zero-based and ordered top to bottom. A panel may contain multiple ordinary clips and gaps. An empty panel displays the section background. A section must retain at least one member so it has a serial position.

Membership is all-or-none. Unknown sections, nonfinite timing, negative offsets, invalid panel indexes, noncontiguous section members and clips outside their section fail validation. Two or three positive panel weights must match `panel_count`. Gutter is the normalized fraction of canvas height between panels, bounded to 0...0.1. The remaining height is distributed according to weights. Background colors use `#RRGGBB`.

Source crop remains normalized upright source space. Ordinary transforms, backdrops, looks, source audio and timing retain their ordinary meanings inside the panel viewport. Canvas lanes remain canvas-wide unless they carry an explicit spatial owner. Source-time label ownership remains separate from spatial placement.

## Recipe lowering

V2 recipes add optional `split_screen_sections`. Each entry has `id`, `start`, `end`, `panel_weights`, `gutter` and `background_color`. Recipe `TimelineClip` adds optional `split_screen` with `section_id` and `panel_index`. Members remain ordinary video clips on separate panel video tracks. This keeps audio and transforms on the ordinary pipeline. The compositor renders within a panel-sized local canvas, clips all resulting pixels to that viewport and places the result in the output canvas. Section background fills gaps. Half-open section windows restore full-screen media exactly at the end.

Phone protocol 43 and feature `splitScreen` protect the additive recipe. Legacy clients must not save a split timeline after discarding its sections.

## Native session API

Native model and mutation work owns these methods for UI integration:

- `createSplitScreen(panelCount: Int)` converts the selected ordinary clip to a section and duplicates it into independently editable panels. Each new slot has a new identity. The original section duration remains unchanged. Replacement uses the existing available source pool.
- `selectedSplitScreenSectionID: String?` resolves from the selected clip membership.
- `setSplitScreenPanelCount(_ count: Int)` preserves existing panels and creates or removes the third panel in one undo transaction.
- `setSplitScreenDuration(_ duration: Double)` changes section boundaries and clamps member coverage without silently speeding media.
- `moveSelectedSplitScreenPanel(to index: Int)` swaps complete panel memberships, keeping clip properties and offsets.
- `setSelectedSplitScreenOffset(_ offset: Double)` changes only the selected clip's placement within the section.
- `splitSelectedClip()` cuts the selected member at the playhead, preserving source clock and appearance.
- `duplicateSelectedClip()` adds an independent occurrence within the same panel when space exists.
- `replaceSelectedClipSource(clipIndex: Int)` changes the selected occurrence's source, bounded by measured source duration.
- `removeSelectedSplitScreen()` converts the selected panel's clips to sequential full-screen clips and removes the other panels in one transaction.

The ordinary inspector continues to own media edits. The split controls expose section layout, panel position and member offset. The timeline displays stable Top, Middle and Bottom rows and retains ordinary clip selection and trim gestures.

## Implementation ownership

- Backend worker owns Python contracts, Save/load, source resolution, compilation and phone gates. It excludes `agents/editor_ops_v2/`, `agents/edit_copilot.py`, `services/kria_editor_ops.py` and prompts, which the AI worker owns.
- Native model worker owns native document, projection, session, compiler and tests outside the engine package. It excludes UI view files.
- Engine worker owns `Packages/KriaMediaEngine` only.
- UI worker owns native view files and UI fixtures/tests only. It does not edit session/model/compiler.
- AI worker owns operation registration/parser/application and AI context preservation.
- Parent owns integration, shared fixtures, documentation, generated contracts and final verification.
