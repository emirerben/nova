# Protected text regions (KRI-827)

"No text on her face or on the plate": every text on a phone Talking edit (opening title,
closing text, text on a spoken word, captions) stays off what the creator named, for each
text's whole time on screen.

## Contract

- **What** to keep clear: the brief writer records `facts.keep_text_off =
  [{"kind": "face"}, {"kind": "object", "label": "plate"}]` (Main Creator v76 / extractor
  v21). Server code reads the field, never the words (KRI-667).
- **Where** an object is: only the creator's mark, a box on one frame of one of their clips
  (`SubmitTurnBody.protected_region`, normalized upright-source coordinates). The server
  checks the clip belongs to the thread's item (404 otherwise), stamps its storage
  generation, stores it on the user event, and the approval pins it on
  `BriefBinding.protected_regions` (in the digest only when non-empty). A mark alone is an
  ask; an object named without a mark is `no_selection` and never called clear. Model output
  and Gemini metadata never place a region.
- A face target protects every detected face, not only the dominant one.

## Render (`app/pipeline/phone_protected_text.py`)

1. `resolve_protected_plan`: targets + marks; a mark on another clip is `other_source`, on
   other bytes of the clip (generation mismatch) is `invalidated`; else the object is tracked
   over the speaker's kept span (`object_track.track_object`: CSRT in a killable subprocess,
   0.1 s anchors in SOURCE time, each proposal re-checked against the seed patch; a lost
   anchor protects the nearest seen boxes grown by 8%, nothing seen protects the mark itself).
2. Captions: `choose_caption_look` moves the caption block off the object (and faces); the
   moved position is saved on `caption_editor_style.position_y_frac` so editor recompiles keep
   it.
3. Title / closing / spoken text: `place_protected_title`; faces and objects are hard limits
   over every 0.1 s sample of the row's window (swept between samples), mapped through the
   current cut and crop (`object_canvas_boxes`). Ladder: preset, fallback rows, any row in the
   band, under the face, then shrink (0.85 / 0.72 / 0.6). No branch trades the object for the
   face. A face-only ask ("no text on her face") takes the same path and protects every
   detected face. When the captions moved (possibly up into the title band) the title and
   closing rows keep clear of the moved caption lines at every size before any spot that
   touches them (`text_overlaps` in the receipt).
   A later text-style pass (`generation_style_rows`) is re-measured
   (`keep_styled_rows_clear`): a restyle that grows or moves a row onto a face or the object
   is undone, so the placement receipt stays true.
4. Receipt `variant.protected_text_regions`: per target `clear | collision | unverified |
   no_selection | invalidated | other_source`, with the mark's provenance and the cut/crop it
   was projected through. `brief_checks` judges a `keep_text_off` requirement from it: `met`
   only when every text was measured clear of an object the tracker actually saw. A face
   target is `clear` only where the face sampler detected the faces (fallback box, sampler
   failure/timeout or a placement error is `unverified`). Any other ask on the same
   requirement ("brighten the clips and ...") keeps its own checker; the verdicts combine.
   A mark with no stamped generation is `invalidated` once the clip has one; the latest mark
   on the rendered clip wins.

Without a `keep_text_off` fact or a mark nothing runs and the render is unchanged.

## Verify

`make verify-protected-text` (also `tests/pipeline/test_protected_object_text_e2e.py`):
synthetic clips with a known plate path (static, moving into the title band, fast pass,
occluded, decoy, full frame, cleanup cut, landscape crop, tracker timeout, swapped source),
real tracker + real Skia ink measured every 0.1 s; writes `.protected-verify/report.json` +
`montage.png`; exits 1 on any row claimed clear while covering the plate.

## Not yet

- The iOS mark card (draw a box on a source frame) and on-device re-placement after an
  editor crop / trim / speed change: the API, contract and render are ready; until the app
  sends `protected_region`, an object ask honestly reads `no_selection`.
- Cloud (non-phone) caption placement and guided-story per-moment text still protect faces
  only.
- Automatic object detection (no mark) is not attempted.
- Approval readiness does not yet ask the creator to mark a named object before approving.
- Editor Save does not yet compare the receipt's provenance (keep_segments, crop,
  position_x) with the saved edit: a trim / crop / text move after the render keeps the
  render-time `protected_text_regions` verdict.
