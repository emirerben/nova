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

Phone protocol 46 (main took 44 and 45 first) and feature `splitScreen` protect the additive recipe. Legacy clients must not save a split timeline after discarding its sections.

## Native session API

Native model and mutation work owns these methods for UI integration:

- `createSplitScreen(panelCount: Int, scope: NativeSplitScope)` creates a section. `.selectedClip` converts the ordinary clip under the playhead (the selection only when the playhead is inside a split) into panel 0 and leaves the other panels empty. `.wholeVideo` turns every ordinary clip into panel 0 members of one section, keeping identities, windows and transitions. The section duration is unchanged. Empty panels display the section background and are filled by `addSplitPanelClip`.
- `selectedSplitScreenSectionID: String?` resolves from the selected clip membership.
- `setSplitScreenPanelCount(_ count: Int)` preserves existing panels and creates or removes the third panel in one undo transaction.
- `setSplitScreenDuration(_ duration: Double)` changes section boundaries and clamps member coverage without silently speeding media.
- `swapSplitPanels(sectionID:_:_:)` swaps complete panel memberships in one undo step, keeping clip properties, offsets and panel sizes.
- `beginSplitDividerDrag` / `updateSplitDividerDrag(deltaFraction:)` / `endSplitDividerDrag` resize two adjacent panels live with one undo step, snapping to equal.
- `addSplitPanelClip(sectionID:panel:fileURL:)` and `(…sourceClipIndex:)` add a clip to a panel's gap or end without lengthening the section.
- `selectSplitPanel(sectionID:panel:)` selects the panel's clip at the playhead.
- `setSelectedSplitScreenOffset(_ offset: Double)` changes only the selected clip's placement within the section.
- `splitSelectedClip()` cuts the selected member at the playhead, preserving source clock and appearance.
- `duplicateSelectedClip()` adds an independent occurrence within the same panel when space exists.
- `replaceSelectedClipSource(clipIndex: Int)` changes the selected occurrence's source, bounded by measured source duration.
- `removeSplitScreen(sectionID:keepPanel:)` converts the selected panel's clips to sequential full-screen clips and removes the other panels in one transaction.

The ordinary inspector continues to own media edits. The split controls expose section layout, panel position and member offset. The timeline displays stable Top, Middle and Bottom rows and retains ordinary clip selection and trim gestures.

## Implementation ownership

- Backend worker owns Python contracts, Save/load, source resolution, compilation and phone gates. It excludes `agents/editor_ops_v2/`, `agents/edit_copilot.py`, `services/kria_editor_ops.py` and prompts, which the AI worker owns.
- Native model worker owns native document, projection, session, compiler and tests outside the engine package. It excludes UI view files.
- Engine worker owns `Packages/KriaMediaEngine` only.
- UI worker owns native view files and UI fixtures/tests only. It does not edit session/model/compiler.
- AI worker owns operation registration/parser/application and AI context preservation.
- Parent owns integration, shared fixtures, documentation, generated contracts and final verification.

## Ownership and compatibility

`speaker_subject` is durable occurrence provenance, distinct from caption ancestry. Copies preserve explicit true/false; legacy speaker IDs are resolved before a new UUID is minted. New independent copies do not inherit a caption-parent link. Recipe `speaker_background.subject_clip_ids` names the exact video occurrences; absent keeps legacy track ownership, while an empty array represents no current subject. `speaker_frame_override` stores independent occurrence framing. Missing or inherit uses the shared default; explicit full-panel differs from inheritance, and custom retains validated frame geometry. Selected-occurrence controls preserve peers. Background focus uses the panel's aspect ratio.

Native chat staging and variant hydration copy slots and section definitions atomically. An explicit section array makes the chat's complete timeline authoritative, including an empty array when unstacking. New canonical UUIDs retain identity and approved source metadata; historical non-UUID temporary IDs retain their existing admission repair. A panel-local edit does not move global canvas layers. Section boundary changes move those layers through serial section units, while explicitly source-owned text/captions follow the movement/hold source clock.

## Collapse mode (`collapse_empty_panels`, phone protocol 47)

A split section may set the optional boolean `collapse_empty_panels`. Absent or false keeps today's behavior: panel rects are fixed for the whole section and a panel with no clip shows the section background.

When true:

- **Active panels.** At time `t` inside the section a panel is active when one of its members has a half-open window `[start, end)` containing `t`. Two overlapping members of the same panel (a transition) count as one active panel. A panel with no members at all is never active.
- **Layout.** Only active panels are shown, top to bottom in panel-index order. With `n` active panels, `available = 1 - gutter * (n - 1)` of the canvas height and each panel gets `available * weight_i / sum(weights of active panels)`. Gutters sit only between active panels. One active panel fills the whole canvas. Zero active panels show the section background across the canvas.
- **Hard cut.** The layout changes on the frame a member starts or ends. There is no animation. Preview and export must change on the same frame.
- **Panel identity.** Panel indexes, weights and member `split_panel_index` values are unchanged. Collapse only changes which panels are drawn and how tall they are.
- **Section duration.** The section lasts until its longest member ends: `duration_s = max(split_offset_s + duration_s)` over its members. Trimming or extending a member changes it, later sections shift, and a section always keeps at least one member.
- **Weights.** Dragging a divider in the preview changes the weights of the two adjacent ACTIVE panels and keeps their sum.
- **Defaults.** New sections created by a protocol 47 client set `collapse_empty_panels: true`. Existing sections and sections made by older clients stay false. The Split panel offers "When a panel ends: Others fill the screen | Leave it empty".
- **Recipe.** V2 recipe sections carry `collapse_empty_panels` next to `panel_weights`. A recipe with the field set to true requires the `splitCollapse` capability. Members stay ordinary video clips on separate panel tracks.
- **Gating.** `SPLIT_COLLAPSE_PROTOCOL = 47`. A client below 47 must not save a timeline that has `collapse_empty_panels: true`, and the server must not offer or accept the field below 47. The server advertises collapse only when `splitCollapse` is in `PHONE_RENDER_VERIFIED_FEATURES`, so the rollout stays dark until that setting is changed.
- **Engine API.** `SplitScreenSection` gains `collapseEmptyPanels: Bool = false` (strict decode key `collapse_empty_panels`). `panelRect(index:canvas:activePanels:)` takes an optional ascending list of active panel indexes (nil = all panels, today's behavior).

## Section length in fixed-layout sections (grow-only)

Collapse sections derive their length from the longest member (shrink and grow). A section WITHOUT `collapse_empty_panels` keeps the length the creator gave it, but it GROWS when a member's trailing edge is dragged past the section end: `duration_s = max(duration_s, max(split_offset_s + duration_s))`. It never shrinks on its own. Trailing trim of a member is bounded by its source footage (still images by the 1800 s timeline cap), the next member in the same panel, and the 1800 s section cap. Later sections shift through the same section-timing rebase as collapse sections.

## Gap-filling and whole-video operations (native session)

- `extendSplitToWholeVideo(sectionID:)` absorbs every ordinary (non-split) clip, before and after the section, into panel 0 in timeline order. It requires exactly one section. Windows, identities and transitions are preserved.
- `loopFillSplitPanel(sectionID:panel:)` repeats the panel's existing clips (in order) after its last member until the section end, clipping the last copy. Each copy gets a new slot identity.
- `canAddToSplitPanel(sectionID:panel:)` is true when the panel has free time inside the section (a gap under the playhead, or time after its last member) or can still grow the section.
- `makeSplitFullScreen(sectionID:side:)` cuts the section at the playhead. `.fromPlayhead` keeps the section before the playhead and turns the rest into ordinary clips from the chosen panel (the selected panel, else the first panel with footage). `.untilPlayhead` is the mirror.
- `removeSplitScreen(sectionID:keepPanel:)` turns the section back into ordinary clips from `keepPanel` only.

- `canAddToSplitPanel(sectionID:panel:)` is true only when an add has room: a gap under the playhead, or free time after the panel's last member. A panel that is full to the section end cannot take another clip; shorten one of its clips first. `addSplitPanelClip` clamps to that free time, so the affordance never offers an add that would be refused.
- `wholeVideoUnavailableReason` explains in one short sentence why "Whole video" is unavailable; `defaultSplitScope` is `.wholeVideo` when there are at least two ordinary clips and no section.

## Blank block (`media_kind: "blank"`, phone protocol 51)

A split panel can hold a flat-colour block instead of footage. It is a timeline slot with `media_kind: "blank"`, `color` (`#RRGGBB`), `duration_s`, the usual `split_section_id`, `split_panel_index` and `split_offset_s`, `in_s: 0` and NO `clip_index`. The editor value is the same atomic timeline, so it trims, moves, cuts and deletes like a photo (unlimited length, the 1800 s cap, a cut makes two independent blocks that keep the colour).

- **Where.** Only inside a split section, and a section keeps at least one real clip (a section of only blanks has nothing to play, so the editor drops the blanks with the last clip). A blank never overlaps another member of its panel and has no transition, no source adjustments and no audio. Other slots still require a `clip_index`; slot rows of any other `media_kind` are unchanged.
- **Colour.** Starts as the section background. Any `#RRGGBB` is valid; the server stores it uppercase.
- **Collapse.** A blank counts as a member: its panel stays drawn while it plays and a collapsing section lasts until its longest member, a blank included.
- **Recipe.** The recipe lowers it to `split_screen_sections[].blanks: [{id, panel_index, start, end, color}]` (absolute seconds, omitted when empty). It adds no track clip, asset or audio. The compositor paints one flat sRGB layer in the panel viewport for each layout the window spans, above the section background and under text, the same solid-colour drawing as the speaker background fill. Requires the `splitBlankPanel` capability.
- **Gating.** `SPLIT_BLANK_PANEL_PROTOCOL = 51`. The server offers `editor_capabilities.split_blank_panel` only to protocol 51 and with `splitBlankPanel` in `PHONE_RENDER_VERIFIED_FEATURES`. A build below 51 gets 426 `client_update_required` (`split_blank_panel`) when it saves, polls the timeline of, or renders an edit that holds a blank, because it cannot decode a slot with no `clip_index` and its Save would erase the block.
- **Agent.** No chat op places a blank yet (the split ops own that). An unrelated op over a timeline that holds one keeps it untouched.
