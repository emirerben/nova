# song_classifier rubric

Score each output on these dimensions, **integer 1-5**:

1. **label_consistency** — Do `genre`, `vibe_tags`, `energy`, `pacing`, and `mood` describe the same song? Do they contradict each other?
   - 5: every field reinforces the same musical identity; an editor could picture the track from the labels alone
   - 3: most fields agree; one or two are generic or mildly off (e.g. `energy=high` on a clearly mellow track)
   - 1: contradictory (e.g. `genre=cinematic`, `energy=peaks_high`, `mood="quiet melancholy"`, `pacing=frantic`) or boilerplate that ignores the track

2. **content_profile_specificity** — Is `ideal_content_profile` concrete enough that the matcher (Phase 2) can actually use it to filter clip sets?
   - 5: names subjects, lighting, motion style, energy curve — "close-up couple shots at golden hour, slow motion candid laughs" beats "romantic content"
   - 3: directionally right but generic — "uplifting montage clips" without specifics
   - 1: empty, single-word, or so vague it could apply to any song

3. **downstream_fit** — Do `copy_tone`, `transition_style`, and `color_grade` match what a real editor would pick for this song?
   - 5: copy_tone matches the song's voice; transition_style matches the rhythmic feel; color_grade is plausible and specific
   - 3: most match; one is generic or slightly off
   - 1: copy/transition/color contradict the song's mood (e.g. `transition_style=whip_pan` + `copy_tone=sentimental` on a slow piano ballad)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"label_consistency": 4, "content_profile_specificity": 4, "downstream_fit": 4}, "reasoning": "<one sentence>"}
