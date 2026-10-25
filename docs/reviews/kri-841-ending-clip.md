# KRI-841 — a Talking edit ends on the requested video with its own sound

Parent: KRI-829. Claim: `local_fix` (offline replay; no phone export yet).

## Requests (kit prompts, verbatim parts)

- Café T2 (Turkish): "En sona Atakan'ın ilk ısırık videosunu kendi sesiyle ekle." Footage: Caro
  talking 24.6 s. Visuals: Cotopaxi photo, oblea photo, Atakan's bite video 6.4 s with sound.
- Don Julio T3 (English): "At the very end, add the cheers video with its own sound." Three
  footage clips: Lucas 12.9 s, overhead table 2.0 s (no speech), cheers 3.4 s ("Cheers!" at
  0.9–1.9 s). Needs multi-clip Talking (`TALKING_MULTI_CLIP_ENABLED`).

Expected: the talk plays to its end, then that video plays with its own sound (café 24.6 → 31.0 s,
Don Julio 12.9 → 16.3 s); it appears nowhere else; or the creator sees "Couldn't" before approval.
Source timings come from the kit READMEs (`~/Downloads/kria-ecuador-test/T2_turkish_subtitles_talking`,
`~/Downloads/kria-julio-test/T3_spanish_subtitles_cutaways`); raw media stays outside git.

## Where the ask was lost (main 75a9fff9d / 004f38182)

| Boundary | Before | After |
| --- | --- | --- |
| Brief | Recorded as `last_clip`/`closing`/free text; never "met", often only "Have a look" | Extractor (v21) records `style` + `ending_clip`/`keep_audio`; checked at draft and render-ready |
| Strategy | No field; `closing_media` is images only | Model-hidden `ending_clip {media_id, keep_audio}`; Main Creator v76 teaches it only when the phone can end on a video |
| Repair | — | Kept only when the phone can render it; otherwise a plain EN/TR notice and a held "Couldn't" |
| Worker | Visuals video grounded as a muted mid-talk card/insert; one-word footage clip became a muted cutaway | Appended after the talk at volume 1, excluded from every mid-talk placement; footage ending moved last on the main track |
| Compiler | `_compile_ending_clip` hard-coded `volume=0` (admin lane only) | Uses the lane's `volume` (default 0, so the admin lane stays muted and byte-identical) |
| Receipt | None | `variant["phone_ending_clip"]`, derived from the compiled recipe |

## Executed checks (offline, no paid calls)

- `tests/tasks/test_kri841_talking_ending_clip.py` drives the real repair →
  `_run_generative_job` → `_run_phone_subtitled_job` → `compile_phone_subtitled_plan` →
  `plan_facts_from_phone_variant` receipts with the kit word timings. Only transport is replaced
  (Whisper words, probe/ingest, DB session, pool rows, binding, face sampling).
- `tests/kria/test_kri841_ending_clip_ask.py`: schema/hash stability, repair gates, draft and
  render-ready receipts (EN/TR), montage and one-clip controls, prompt gating.
- Red on the pre-fix parent (the new test files copied onto it): the kit replays fail because the
  video "ends on c0" (café, nothing appended, bite muted mid-talk) and "ends on lucas" (Don Julio,
  cheers a muted cutaway); the rest fail on the missing field/receipt.
- Wrong-content controls: the shared check rejects the recipe main builds today (bite as a muted
  card, nothing appended), a bite appended at volume 0, and cheers as a mid-talk cutaway;
  render-ready is never "met" from the strategy alone, nor when the ending is muted or has no
  audio track.
- Case matrix also covers: every overlay layout, beats/closing naming the ending video, the
  `visualVideos`/`audioMix`/media-lanes gates mirrored at draft, multi-clip off (refused before
  approval; cheers uploaded as a Visual works like the café), several speakers, a talkative ending
  uploaded first, a hand-authored admin lane ending, editor Save, a stated length or "under N s"
  cap (only the ending is dropped, `over_length_limit`; the what-if probe emits no fleet length
  observation), and byte-identical strategies/lanes/recipes without an ending.
- iOS (read from code, no change): `Composition.swift` adds a main-track clip's own audio at
  `originalVolume × clip.volume`; Talking sets no ducking; `visualVideos` + `audioMix` are already
  required and verified, so no protocol or feature bump.

## Gaps (stay open under KRI-829)

- No iPhone export yet: re-send café T2 and Don Julio T3 after deploy (T3 needs multi-clip Talking).
- Live Main Creator emission of `ending_clip` is covered by a hand-authored replay golden only.
- A stated length the ending would break is said at render-ready, not before approval.
- Visuals pool rows carry no audio fact, so `has_audio` is unknown for a Visuals ending.
- If the model omits `ending_clip`, the Visuals video can still be grounded mid-talk; the brief
  check then says "Couldn't" before approval.
- The iPhone editor's live preview does not show a Visuals ending clip (the export does).
