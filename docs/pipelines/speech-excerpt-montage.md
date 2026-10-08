# Spoken-excerpt montage (KRI-282)

A phone montage whose footage includes a talking-to-camera clip can play chosen
excerpts of that speech over the other footage, cut to the speaker, return to
fast cuts, and repeat. It is phone-render only (see "Cloud fallback").

Kill switch: `SPEECH_EXCERPT_MONTAGE_ENABLED` (default `true`). `false` makes the
entry point return immediately, so every montage takes the ordinary unified path
byte-identically, and nothing is transcribed or stored. Apply:
`fly secrets set SPEECH_EXCERPT_MONTAGE_ENABLED=false --app nova-video` + restart api + worker.

## Flow

1. **Timed transcript.** `ClipSpeech.segments` (`schemas/clip_understanding.py`) holds timed
   sentence segments. `analyze_kria_clips` adds them after vision analysis for a clip with speech
   (`_with_speech_segments`, cached whisper via `services/speech_segments.transcribe_stored_clip`).
   `prompt_view(include_segments=True)` shows a compact list to the chat agent. Best effort: a
   failure never fails the analysis.
2. **Entry.** `_run_generative_job_impl` calls `services/phone_speech_montage_job.run_phone_speech_montage_job`
   before `_run_phone_unified_montage_job`. It returns `False` (fall through) unless some clip has
   speech, or the creator's words mention speech and no clip has any.
3. **Plan.** `agents/speech_excerpt_planner.py` (`nova.plan.speech_excerpt_planner`, prompt
   `prompts/speech_excerpt_planner.txt`) reads the request and the timed segments and returns
   ordered sections (`schemas/speech_montage.py`): `speech` (clip ref + quoted phrase +
   `visual: speaker | cutaways`) and `montage` (duration + optional cut length). It names excerpts
   by quoted phrase and clips by short refs (`c1`, `v1`), never by timestamp or media id.
4. **Ground.** `services/speech_montage_planning.plan_speech_montage` grounds every quote to word
   timings with `services/speech_segments.ground_excerpt` (the KRI-178 reaction-beat token folding;
   exact, `"head ... tail"` spans, or a >=80% fuzzy window). Windows are padded into the silent gap
   on each side (never more than 45% of it), so excerpts start and end on silence and two adjacent
   excerpts never overlap.
5. **Compile.** `pipeline/phone_speech_montage_plan.compile_phone_speech_montage_plan` (see below).
6. **Pin.** Variant `speech_montage` (`resolved_archetype` the same), `render_destination: device`,
   record in `assembly_plan["speech_montage"]` (sections, adjustments, requirement receipts).

## Recipe shape (plain schema v2, no version bump)

- One main video track `speech-montage`, back to back, timeline = sum of sections.
- `speech`/`speaker`: the speaker clip on the main track at the excerpt window, `volume=1`, with
  `audio_fade_in/out` (0.05s / 0.18s, shortened for short excerpts).
- `speech`/`cutaways`: the same excerpt on audio track `speech-audio` (source = the speaker's
  video asset, `source_start` = excerpt start, `timeline_start` = section start, same fades), while
  muted (`volume=0`) b-roll cuts of about 2.4s cover the picture on the main track.
- `montage`: muted fast cuts (default 0.8s, 0.4-1.5s requested) round-robin over the non-speaker
  clips; a clip met again advances its source window. A global factor lengthens cuts so the main
  track stays under 92 clips.
- Optional `music` track (compiler supports it; the worker passes none today): split clips that
  sound only during montage runs, the song position resuming after each speech section.
- Capabilities: `basicComposition`, `local1080Export`, plus `audioMix` when an audio track exists.
- **Pinned corner text (KRI-527):** the montage draws no text of its own, so the creator's
  `pinned_texts` are the only text layers (`title-N`, via `with_pinned_text_layers`); a pin adds
  `positionedText` (and `authoredText` for a variable font) to the capabilities. A clip scope is the
  n-th video clip on `speech-montage`. See [`pinned-text`](pinned-text.md).
- **Speaker orientation:** any. A landscape or square speaker clip (e.g. a 16:9 announcement) is
  accepted and shown with the engine's plain centre cover-fit, exactly like every other phone
  montage clip; the receipt gets an adjustment ("sides are cropped ... centred"). No `source_crop`
  is emitted on purpose: without face tracking the only nameable window is the centred one (pixel
  identical to the cover-fit), and a crop would add the `sourceCrop` capability, which
  `validate_phone_pilot_recipe` refuses unless it is in `PHONE_RENDER_VERIFIED_FEATURES`. Portrait
  recipes are unchanged. Known limit: a speaker standing off-centre in a wide frame can be cut off;
  a future face-tracked `source_crop` (needs `sourceCrop` verified) would fix that. Only a clip
  with no video picture or over 5 minutes is refused.

## Asking instead of failing

`needs_creator` becomes `SpeechMontageClarification` (an `UnsupportedPhonePlan`); the dispatcher
persists its message as the job's `error_detail` with `failure_reason=phone_plan_unsupported`.
Questions are specific: the quote that was not found, whose speech when two clips talk, that no
clip has speech, or what extra footage is needed. A quote that cannot be grounded is dropped and
reported (`planned.dropped`, `adjustments`) while the remaining excerpts render; only when none
ground does the job ask.

## Request following

`kria/brief_checks.py`: `plan_facts_from_speech_montage` + `_check_speech_excerpts` (excerpts
grounded, speech over other footage where asked, back to the speaker, fast cuts between).
The record carries `pinned_texts` (only the lines actually drawn) and `plan_facts_from_speech_montage`
exposes them as `texts`, so a brief text requirement is met by a drawn pin.
`tests/evals/request_following/checkers.py`: `speech_excerpts` (plan-level `FinalPlan.speech`).

## Cloud fallback

None, by design: the cloud montage renderer cannot lay one clip's speech under another clip's
picture, and a phone-proxy job has no cloud sources (`require_cloud_source_paths`).

## Pending

Live evals for the planner (`pytest tests/evals/test_speech_excerpt_planner_evals.py` with
`NOVA_EVAL_MODE=live`, Gemini spend) and an on-device check of the recipe (speech audio-track
clip sourced from a video asset; back-to-back same-source excerpts need the iOS same-source
audio crossfade of the matching iOS lane).
