# Freehand drawing (KRI-589)

Creators draw over their video with a finger in the Kria iOS editor: a brush
(pen, marker, highlighter, neon, chalk, spray), a colour and a size. The drawing
sits on the timeline like text or a Visual, can draw itself on or fade in and out,
and is moved, scaled and rotated with the usual gestures. Device-rendered
variants only; slides get static drawings in a follow-up (PR2).

## Contract (frozen first; the server and the app code against it)

| Piece | Where |
|---|---|
| Editor lane `drawings` (`DrawingElement` / `DrawingStroke`) | `app/agents/_schemas/drawing.py` |
| Recipe layer `EditRecipeV2.drawing_layers` (`PortableDrawingLayer`) | `app/kria/portable_drawing.py`, `app/kria/recipes_v2.py` |
| Server compile (`compile_drawing_layers`, wrapper `with_drawings`) | `app/kria/portable_drawing.py`, `app/pipeline/phone_recipe_shared.py` |
| Swift twins | `KriaMediaEngine` `PortableDrawing.swift`, `NativeDrawingPainter.swift` |
| Shared fixtures | `tests/fixtures/drawing-parity/{v1,compile_v1}.json` (Python and Swift assert the same files) |

Caps: at most 12 drawings per variant, 60 strokes and 3000 points per drawing, 1500
points per stroke, 8000 points per variant (about 128 KB worst case; under 10 KB
after the app simplifies strokes). Coordinates are normalised to the authoring
canvas (`canvas_aspect`); renderers aspect-fit it, never stretch. Scale and
rotation pivot on the strokes' bounding-box centre. Drawings are always the topmost
layer: there is no z field.

`drawing_layers` is left out of the recipe while empty (existing recipes and digests
are byte-identical) and a non-empty list requires the `freehandDrawing` capability.

## Compile rules

`compile_drawing_layers` is the reference; the Swift `NativeEditorRenderCompiler`
follows the same rules, pinned by `compile_v1.json`: drop a drawing that starts at or
after the timeline end, clamp `end` to the duration (dropped if under 0.1 s remains),
clamp `draw_duration` to the window, keep order, copy the rest.

`with_drawings(recipe, variant["drawings"])` runs on **every** recipe the phone editor
builds or patches in `services/phone_editor.py` (authored, guided, subtitled,
narrated, voiceover montage), whatever sections the Save carried, so a text edit or a
shorter cut re-clamps the stored lane and a longer cut brings the window back. The
stored lane is never rewritten by a cut. Patching compilers (`narrated`, `voiceover`)
strip the previous layers first, since the recipe validator rejects a layer past the
new end. A window shorter than the 0.1 s minimum after the clamp is dropped from the recipe; the
lane keeps it. `draw_duration` clamps to the window.

A guided Save that touches only `text_elements` and/or `drawings` keeps the approved
timing program (`render_sections - {"text_elements", "drawings"}`); it never re-clocks.
For guided variants the canonical lane is `GuidedEditorRevision.drawings`, which is
omitted from the dump, `lane_hashes` and `state_hash` while empty (guard:
`tests/schemas/test_guided_edit_revision.py`).

Speech and silence cuts retime drawings like text (`speech_cut_state._fit_drawings`):
shift, clamp, drop a drawing the cut removed, shorten its draw-on to the new window.

## Save, capabilities, errors

- `POST .../editor-commit` takes `drawings` (full lane) and `deletions[{kind:"drawing"}]`.
  `validate_drawings` names the cap it breaks (`422 drawings_invalid`).
- `editor_capabilities.drawings.{add,edit,trim,delete}` is `{editable, reason}`. Cloud
  variants: closed, reason `drawing_requires_phone_render` (a Save is refused the same
  way). Device variants: `add/edit/trim` open only with the flag on, `freehandDrawing`
  verified and a build declaring `X-Kria-Client-Protocol >= 6`; `delete` follows only
  the variant shape.
- **Kill switch `FREEHAND_DRAWING_ENABLED=false`**: new or changed drawings get
  `422 drawings_disabled`; stored drawings still compile and play, and removal stays
  open. Apply with `fly secrets set FREEHAND_DRAWING_ENABLED=false` (api + worker) and
  restart.
- **Old-build protection (426).** `GET /me/jobs/{id}/device-render` and `editor-commit`
  answer `426 {"detail": {"code": "client_update_required", "feature": "drawing"}}`
  when the pinned recipe (or the stored lane) has drawings and the build declares a
  protocol under `FREEHAND_DRAWING_MIN_CLIENT_PROTOCOL` (default 6; protocol 5 is the slides-size build, which cannot decode drawings): such a build
  rejects `drawingLayers` as an unknown key and would fail the job. Workers and chat
  edits have no app build and are not gated.

## Rollout

Code default is on. Before the first creator sees the Draw tool, prod needs
`freehandDrawing` in `PHONE_RENDER_VERIFIED_FEATURES`:

```bash
fly secrets set -a nova-video PHONE_RENDER_VERIFIED_FEATURES="<current list>,freehandDrawing"
```

then restart api and worker (the list is read at start). Without it the capability
stays closed (`drawing_unavailable`) and `validate_phone_pilot_recipe` refuses
`drawing_layers`. Verify with the editor capability payload of a device variant
(`drawings.add.editable == true`). Not in scope: web authoring, cloud variants,
AI-authored drawings, Apple Pencil pressure, z-order, animated slides.

## Rollback and known limits

- **Code rollback is not safe once creators have saved drawings.** `GuidedEditorRevision`
  and the recipe models forbid unknown keys, so an older api/worker image cannot read a
  guided revision that stores `drawings`, nor a pinned recipe with `drawing_layers`
  (`device_status` fails). Use the kill switch (it stops new drawings and keeps playback)
  instead of reverting the deploy.
- Un-verifying `freehandDrawing` (rolling the `PHONE_RENDER_VERIFIED_FEATURES` secret back)
  makes every Save of a variant that still has drawings fail in `validate_phone_pilot_recipe`
  until the drawings are deleted; deletion keeps working.
- A guided or authored **re-cut keeps drawings on their absolute output times** (text and
  overlays are re-projected, `project_lane_times` does not know `drawings`); the recipe
  clamps them, so it is safe but a drawing can drift off its moment.
- Size: the lane is stored three times (variant, revision, pinned recipe). The caps allow
  roughly 220 KB for one lane at the extreme, close to the 256 KB `EDITOR_STATE_MAX_BYTES`
  shared by every lane in a chat turn.
- Chat/copilot edits cannot change drawings (no op exists), so no receipts or plan cards
  mention them.
