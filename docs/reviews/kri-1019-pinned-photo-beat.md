# KRI-1019: a photo pinned to a spoken beat, reported "Couldn't", then placed as a corner card

Parent incident: KRI-1014 (Plaza de Mayo T2 "maximo 2", job `e46baf42`, 2026-10-28, Fly
v1901 = `685489f07`, client protocol 22). Evidence level: **offline replay** of the job's
recorded transcript, strategy, Visuals pool row and overlay-agent answer through the real
worker chain, plus the after-render review run on the job's stored variant. No device,
live-model or production claim.

## Original request (kit T2 prompt, excerpt)

> When he says "on the other side of the square", show my Casa Rosada photo full screen for
> about two seconds with his voice still playing, then come back to him.

The approved strategy carried one reaction beat: `casa-rosada`, `visual_id`
`asset-012544bd…`, `display_mode: fullscreen`, `hold_s: 2.0`, trigger "on the other side of the
square", occurrence first.

## Reviewed source observations (the expectation)

- The speaker is Maximo (the guide), not the creator, who films.
- Maximo says "On the other side of the square" at 41.00–43.39 s of the source take ("On"
  41.00, "square" 42.82–43.39; local whisper large-v3-turbo `-l en`, word times checked
  against the kit audio).
- The job's stored transcript has no English there: it is a Turkish translation, "…Buenos
  Aires'in… Diğer tarafta… şehir hükümeti vardı" (39.3–45.8 s). That transcript fault is
  KRI-1016, not this ticket. Speech cleanup also cut 41.05–41.52 s ("On the"), KRI-1017.
- What the creator got: the review said "Couldn't: … (I never heard on the other side of the
  square in your voice)", and the video showed the same photo as a corner card in the top bar
  at 39.3–44.3 s source (31.75–36.3 s output), placed by the overlay agent with the reason
  "You say "Buenos Aires'in" …". The review never mentioned the card.

## Cause

1. `phone_reaction_grounding.ground_phone_reaction_beats` found no "on the other side of the
   square" in the stored words, so the beat was `unplaced: never_heard` (honest, given
   KRI-1016).
2. `_run_phone_subtitled_job` (`app/tasks/generative_build.py`) reserves Visuals for the generic
   overlay pass (`ground_phone_subtitled_overlays`) through `used_media_ids`, which unioned
   `beat_media_ids` — the media of PLACED beat cards only. An unplaced beat reserved nothing,
   so its photo was offered to the overlay agent, which put it in a corner. The same gap held
   for an unplaced closing photo or badge, beat grounding that raised, a hand-authored lane
   request, beats switched off, and the KRI-840 full-screen sequence / KRI-661 mixed layouts,
   which go through the same call. No other phone lane places pool Visuals on its own (the
   narrated path has no generic pass; montage writers strip beats; the KRI-841 ending visual
   is already pin-derived).
3. `brief_checks._never_heard_problem` always said "in your voice"; the ask said "when he
   mentions …".

## Change

- `generative_build._creator_pinned_media_keys`: every `visual_id` in the strategy's
  `reaction_beats` and the closing `visual_id` / `badge_visual_id`, both id spellings, read
  off the strategy whatever became of the beat lane, and unioned into `used_media_ids`. A
  pinned photo is now placed by the beat lane as asked, or reported by the beat receipt and
  left out. `beat_media_ids` (which guards later demotions) is unchanged.
- `brief_checks._never_heard_problem(…, other_speaker=)`: when the ask names someone else as
  the speaker (a `speaker` fact that is not the creator/anyone, or "when he/she/they says /
  mentions …"), "I never heard X in the take" / "Videoda X sözünü duymadım". Asks in the
  creator's own voice keep "in your voice".

## Checks

`src/apps/api/tests/tasks/test_kri1019_pinned_photo_beat.py` drives `_run_generative_job` →
`_run_phone_subtitled_job` → beat + overlay grounding → compiler → recipe with the recorded 42
words, pool row and the overlay agent's recorded answer replayed at `OverlayPlacementAgent.run`.

- Incident replay: the photo is in no recipe track, the agent is never offered it, the beat
  receipt says `never_heard`; with the reservation removed the same replay reproduces the
  phone's corner card at 39.3–44.3 s (red control).
- Wrong-content controls: the checker rejects the pre-fix corner card, a full-screen photo at
  the wrong time, a photo shown twice, and a heard photo shown as a corner card.
- Fulfillment control: with Maximo's real English words the photo is full screen at 41.0 s for
  2.0 s, placed once.
- Neighbours: grounding raises, lane request, beats off, `fullscreen` / `fullscreen_images`
  sequence layouts, unplaced closing photo/badge, five id spellings, malformed beats/closings,
  and an unpinned Visual that must still reach the agent and be placed.
- Review copy, EN and TR, through `build_receipts` / `reply_from_receipts`; the creator's own
  cue still says "in your voice". The stored variant of job `e46baf42` now reviews as
  "I never heard on the other side of the square in the take."

## Gaps

- The photo still isn't shown for this take until KRI-1016 keeps Maximo's English; this fix
  makes the miss honest, it does not find the cue.
- The overlay agent's `placed[].reason` still says "You say …" when someone else speaks
  (`prompts/overlay_placement.txt`). It never reaches the iPhone (no Swift reader; render notes
  read only unplaced rows); the web suggestion rail shows it. Web-only follow-up.
- iPhone re-send of T2 after deploy: the edit must show no corner card at 31–36 s and the
  review must say "in the take".
