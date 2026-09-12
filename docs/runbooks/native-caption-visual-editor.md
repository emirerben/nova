# Native captions and visuals (KRI-43)

The native editor uses Paper screens 11–19 for caption and visual inspectors.
Captions have Edit captions, Style, and Settings tabs. Language, regeneration,
Sounds, and the separate Overlays/Styles tools are outside this change.

## State and controls

`NativeEditorLanePanel.swift` provides the shared inspector shell and caption
controls. `NativeVisualPanel.swift` browses the authenticated visual library,
opens the existing Photos/Files upload flow, and authors media, text cards,
Card stack/Film strip compositions, and footage-level Zoom pulse effects.
Video thumbnails are decoded with AVFoundation; signed playback URLs are not
sent to an image decoder.

All mutations use `NativeEditorSession` transactions. A text card consists of a
visual block and an ordinary text element linked by `visual_block_id`; moving,
trimming, removing, and undoing the card keep that text attached. Media keeps
its existing representation: legacy `media_overlays` remain overlays, and new
imports use `visual_blocks` with `kind=media`. Fullscreen display preserves the
stored overlay position and size. Fit/fill operates on fullscreen media; z orders
peer media in the same renderer pass. Text/caption and legacy-overlay pass order
remains intact. Layer controls never request subject segmentation.

New media has separate entrance, exit, loop, and speed controls. Text cards use
the existing authored text animation controls. Motion compositions retain their
runtime choreography and expose speed for v2 presets; older presets are never
silently upgraded. Camera effects change the footage, not a floating visual.

## Additive wire fields

- `caption_meta.appearance` commits alignment, outline/shadow colors, shadow
  opacity, and explicit spoken-word highlighting. It persists as
  `caption_editor_style` on the variant and returns through the public editor
  snapshot. Sentence/word display and highlighting are independent once authored.
- Media overlays and media blocks accept optional `editor_style` version 1:
  rotation, fit mode, zoom, and an animation object. The animation has version,
  entrance, exit, loop, and speed. Absent fields retain the legacy rendering path.
- The visual-library response includes `source_url` for original media, separate
  from display/preview derivatives. Existing ownership checks still apply.

The new client requires `caption_editor_style` or `visual_editor_style` in the
existing capability response before authoring those fields. Existing lane gates
still apply. No database migration is required.

## Timeline decisions

1. **Duration source:** `NativeEditorSession.timelineProjection` owns ordered
   output entries, overlaps, inserted blocks, and total duration. Inspectors use
   that projection and the session's canonical duration.
2. **Ripple:** adding a visual never moves clips, captions, text, SFX, other visual
   lanes, motion, camera effects, or the continuous music bed. Existing inserted
   block projection rules still apply at output boundaries.
3. **Scrubbing:** playback, ruler, gestures, and numeric inspector timing use the
   same session duration. Inspector values map back through
   `unprojectOutputTime`; stored lane timestamps stay in base time.
4. **Resizing:** left/right numeric controls trim their corresponding boundary,
   cannot invert the interval, and clamp to the edit. Still/card insertion prefers
   three seconds of remaining output time; videos also respect source duration.
   Media has a 0.1-second minimum; cards cap at 10 seconds. Motion uses 30-fps
   frame boundaries and an eight-second cap. Camera pulses use 0.4–2 seconds.
   Animating faster does not retime a layer. Visual additions do not create clip
   transition overlaps.
5. **Undo:** a slider, corner transform, or timing drag shares one document
   snapshot. A card and its linked text are one transaction. Cancelled imports
   create no timeline records; failed source resolution leaves the edit intact.
6. **Parity:** `VisualEditorStyleTests` and `test_visual_editor.py` pin paired
   animation samples; `test_visual_blocks_smoke.py` exercises rotated media and
   phase windows through FFmpeg, including retained focal placement after rotation
   and zoom. Native placement tests preserve existing fades when an editor style
   is added. `NativeEditorRenderCompilerTests` pins caption
   alignment to the final ASS margins. Existing timeline tests cover inserted
   boundaries, removed entries, overlaps, inverse mapping, and music exclusion.

## Verification and rollout

Run `make ios-verify`, the KriaMediaEngine package tests, affected backend tests,
`make verify-editor-timeline`, `make verify-overlays`, and
`scripts/preship-check.sh`. `NativeCaptionVisualUITests` records all nine inspector
states; the existing editor UI suite covers small-screen scrolling, timing
handles, VoiceOver-sized text, Reduce Motion, and one-gesture undo. Source import,
cache recovery, save conflicts, and edits during rendering retain their existing
transport and session regression coverage.

Deploy the API and render workers before distributing the iOS client. Confirm the
new operation capabilities on an eligible edit. Keep existing visual/motion and
camera feature gates aligned with their render support. A rollback to an older
client does not rewrite saved JSON; use the same backend render support for edits
that already contain the new fields.
