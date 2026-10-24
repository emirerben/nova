# Protected text regions (KRI-827)

"No text on her face or on the plate": every text on a phone Talking edit (opening title,
closing text, text on a spoken word, captions) stays off what the creator named, for each
text's whole time on screen.

## Contract

- **What** to keep clear: the brief writer records `facts.keep_text_off =
  [{"kind": "face"}, {"kind": "object", "label": "plate"}]` (Main Creator v77 / extractor
  v22). Server code reads the field, never the words (KRI-667).
- **Where** an object is: only the creator's mark, a box on one frame of one of their clips
  (`SubmitTurnBody.protected_region`, normalized upright-source coordinates). The server
  checks the clip belongs to the thread's item (404 otherwise), stamps its storage
  generation, stores it on the user event, and the approval pins it on
  `BriefBinding.protected_regions` (in the digest only when non-empty). A mark alone is an
  ask; an object named without a mark is `no_selection` and never called clear. Model output
  and Gemini metadata never place a region.
- A face target protects every detected face, not only the dominant one.

## Asking for the mark before approval (KRI-901)

- An act turn on the mark route (`protected_targets.marks_route`: an app that shows the card
  AND a draft the phone Talking writer really renders, i.e. phone clips, a phone-rendering
  account, a Talking or self-narrated format, no recorded voiceover: the item's
  `voiceover_gcs_path` sends it to the Voiceover writer, which ignores marks) whose live
  brief keeps text off an object with no current mark on a clip the writer may put the
  text on does not offer approval: the runtime (`tasks/kria_runtime.
  _protected_region_gate_sync`) rolls back the speculative draft and replies "Mark the plate
  on one frame so I can keep the text off it. Or tell me the text can go anywhere." with
  `protected_region_question {question_id, label, requirement_ids, media_ids}` (one object
  per question, brief order). `media_ids` are the clips still missing a mark among
  `protected_targets.speaker_candidates`: the only clip, else every clip the clip analysis
  did not find silent (no audio / `understanding.speech` heard nothing), plus the clip a
  past render of this item spoke from (`rendered_speaker_media_id`, from the
  `protected_text_regions.speaker_media_id` / multi-clip receipts). The speaker is chosen at
  render, so a mark on a clip outside that set never satisfies the ask, and a render that
  spoke from an unmarked clip asks again for it. Approval readiness uses the same
  candidates (without the past render). A mark is current while its clip is still in the item with the same storage
  generation; a re-upload asks again. Faces are never asked for.
- Only app builds that show the card are asked: submit stamps `marks_protected_regions`
  on the user event when `X-Kria-Client-Protocol >= PROTECTED_REGION_MARK_MIN_CLIENT_PROTOCOL`
  (default 17; kill switch `999`, api + worker). Older builds and the web approve as before
  and the receipt reads `no_selection`.
- The answer turn (`protected_region` on the turn) forces a re-plan, reaches the brief
  extractor as a neutral "I marked the plate on a frame." (never a new instruction) and is
  a structured answer for duplicate-send detection.
- Approval readiness (flag-gated) agrees, on the same route only: an unmarked object is
  `needs_choice` with the same sentence; once marked, a requirement whose only unchecked ask
  is `keep_text_off` is `executable` with `evidence_owner="protected_text_render"` (the
  render receipt must still read `clear`). Off the route (web, older builds, the cloud
  render, other formats) readiness keeps its pre-KRI-901 verdicts and other checkers' verdicts
  are never overwritten. The report saves `protected_marks`; approval, Retry and plan review
  re-check it against today's route, so a draft whose account left the phone cohort is
  prepared again rather than approved with a promise the cloud render cannot keep.
- iOS: `ObjectMarkCard` (clip chips, the upright frame from this iPhone's original via
  `KriaCropFrameLoader`, a moment slider, a free-shape box with the crop editor's
  move/resize math). `ProtectedRegionAnswer.box(from:)` is the one (x, y, w, h) to
  `[left, top, right, bottom]` conversion, clamped with a 2% floor. Verified by
  `ObjectMarkUITests`, `tests/kria/test_kri901_mark_before_approval_postgres.py` and the
  `app_mark_rotated_take` scenario of `make verify-protected-text` (a portrait take stored
  rotated, marked the way the card converts it).

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
   no_selection | invalidated | other_source | stale`, with the mark's provenance and the cut/crop it
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
occluded, decoy, full frame, cleanup cut, landscape crop, tracker timeout, the app's mark on a
rotated portrait take, swapped source),
real tracker + real Skia ink measured every 0.1 s; writes `.protected-verify/report.json` +
`montage.png`; exits 1 on any row claimed clear while covering the plate.

## Not yet

- On-device re-placement after an editor crop / trim / speed change.
- A web mark card (the web never gets the question; its object asks read `no_selection`).
- Cloud (non-phone) caption placement and guided-story per-moment text still protect faces
  only.
- Automatic object detection (no mark) is not attempted.
- After the render the text is not re-placed: an edit only makes the receipt `stale` (see
  below), and the review says the text was not re-checked. Tracked boxes are not stored, so
  re-measuring needs a re-render.
- Only the first speaker clip of a multi-speaker Talking edit is protected and measured.
- A mark is never removed by a later "text on the plate is fine" instruction.

## Edits after the render

The receipt carries `measured_fingerprint` (text rows, caption geometry and appearance,
caption cues, bars). `brief_checks` recomputes it from the variant; a mismatch (a dragged
title, moved captions, retyped caption, bars switch from any write path) turns every
measured verdict into `stale`, never `clear`. A phone editor Save
(`phone_editor._restale_protected_text`) also compares the cut, crop (`position_x`) and bars
it recompiles with against the receipt's provenance and marks it `stale` when they differ.

Captions are measured in the look the render draws: the phone text-style policy look
(`generation_caption_look`) when one applies, and a move changes only its y. Faces are a
hard limit for captions only when the creator named a face.
