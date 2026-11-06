# Captions over the corner speaker's face — failure modes (written before the fix)

Qendresa case 1 (kit `BUG-Q2-4`, `~/Downloads/kria-qendresa-test/tests/bugs/test_q2_caption_over_corner_face.py`).

## The bug

On a Talk-to-camera edit with a speaker background and a corner frame (`small_bottom_right`, or a frame
the creator dragged/pinched, #2117), the phone draws the canvas-sized speaker layer scaled by
`width_fraction` and centred at (`x_fraction`, `y_fraction`)
(`RecipeVideoCompositor.framed`, `KriaMediaEngine/RecipeVideoCompositor.swift`). Caption placement
(`services/phone_text_styling.py` `_ensure_generation_environment`) maps sampled face boxes through the
clip transform only, so captions dodge a face at y 0.22-0.40 that is not on screen and land on the
real face at y ~0.65-0.76. Measured 2026-11-06 on origin/main 05d9720d8 with live switches: the kit's
simulated face 7/12 caption blocks overlap; Apple Vision faces from two real stand-in takes
(`kria-green-screen-gt` woman_hair / man_gestures, 2/6/10 s) 7-8/12 in every frame. The receipt
(`caption_look_receipt.over_face`) is computed from the same wrong boxes, so an asked spot that covers
her face reports `over_face: false`.

## Acceptance (what the creator sees)

- Default captions never overlap her face where the phone draws it (corner), on simulated and real faces.
- An asked caption spot stays hers (KRI-1177) and, when it covers her drawn face, the receipt records
  `over_face: true` (the review then says so).
- Inside a full-screen opening (item 16) her face is full-frame there, and captions dodge that.
- Still wrong after this fix (out of scope, named): moving the speaker in the editor after the video was
  made does not re-place the captions (editor path).

## Ways the fix could fail

1. **Full-screen windows (item 16).** Framing every face box would shrink her face during the opening,
   where she fills the canvas, and captions could land on her real face there. Boxes must be split in
   time: unframed inside `full_screen_windows`, framed outside.
2. **Frame not recorded on some backgrounds.** `register_generation_background` returns early for a
   colour fill (`kind == "color"`, KRI-1173) and when `CAPTION_BACKGROUND_CONTRAST_ENABLED=false`. The
   speaker is still in the corner there, so the frame for faces must be recorded before those returns.
3. **Order of calls.** On the phone Talking path the background is registered before the captions are
   styled (`phone_subtitled_plan.py` ~664-673). A compile path that registers it after the environment
   was sampled must re-map the boxes too, from the raw boxes (never map an already-mapped box twice).
4. **Double mapping.** Two compile passes or two registrations would shrink a face twice. Always map
   from the stored raw boxes.
5. **Rotated/tilted take.** A tilted window already protects the whole canvas `(0,0,1,1)`; framed, that
   becomes the corner frame's bounds, which is exactly where the speaker is. Keep that behaviour.
6. **Wrong transform order.** The phone applies the clip transform first (fit/letterbox), then the frame.
   Map clip-transformed canvas boxes through the frame, in that order.
7. **A box mapped fully off-canvas** must be dropped, as today.
8. **No frame** (`full` speaker, matted full-size) or no speaker background: boxes unchanged,
   byte-identical (control).
9. **Kill switch.** The fix moves captions on existing kinds of edits (a re-render of an old job will
   place them differently). Area convention for a bug fix: a kill switch, default true
   (`SPEAKER_FRAME_CAPTION_FACES_ENABLED`); false + worker restart = today's placement, byte-identical.
10. **No face found.** Nothing to map; placement as today.
11. **Asked spot.** KRI-1177 keeps an asked spot; the fix must not start moving asked captions, only
    make `over_face` true when they cover the drawn face.
12. **Other text roles.** Titles/labels read the same `_face_boxes` when they are styled, so rows styled
    after the background is known dodge the drawn face. A title styled in the worker's base pass (before
    the background) keeps its spot: with a bottom corner it sits at the top, clear of her; with a TOP
    corner frame (`small_top_left` / `small_top_right`) it could still meet her face. Not covered here
    (open gap; the captions are re-chosen, titles are not).
14. **A background dropped after it was registered** (Emir's review on #2127). The compiler registers
    the corner background and styles the captions before the background media is bound; a bind failure
    (e.g. a video `source_start` at/past its end) makes the worker drop the lane and compile again with
    no background. `register_generation_background(None)` must then forget the layout: faces back to
    the raw full-frame boxes, Green 5's contrast frames back to the take's own frames, caption look
    chosen again. Guards: `test_a_background_dropped_after_binding_puts_her_face_back_full_frame`,
    `test_after_a_dropped_background_the_review_judges_the_full_frame_face`.
13. **The base pass.** The worker compiles once without lanes (`generative_build.py` ~10665), styling the
    captions, then again with the background (~11472). The caption look cached on the first pass must be
    chosen again once her face moves into the frame (the receipt and colour with it), never kept.

## Checks

- Ported kit tests + new cases in `src/apps/api/tests/tasks/test_caption_corner_face.py`
  (simulated face, six real Vision faces, asked bottom spot, full-screen opening, colour fill,
  contrast switch off, kill switch off control, no-frame control), red before the fix.
- Kit test file with live switch values; KRI-667 guard; ruff; touched-area pytest; `make verify-kria`;
  `bash scripts/preship-check.sh`.
- Native output: iPhone simulator render of a corner-speaker Talking edit, frames scored with the kit's
  `tools/frame_text_faces` (Apple Vision): no caption over the face.
