# audio_template rubric

Score each output on these dimensions, **integer 1-5**:

1. **structural_fit** — Do `shot_count`, `slots`, and `total_duration_s` match the musical phrasing of the track?
   - 5: slot count and durations follow the music's bar/phrase structure; hook section feels like the actual hook of the track
   - 3: structure is plausible but generic (e.g., uniform 1-second slots with no relationship to phrasing)
   - 1: arbitrary count, durations don't add up, or no relationship to detected beats

2. **style_metadata** — Are `copy_tone`, `creative_direction`, `transition_style`, `pacing_style`, `subject_niche` specific and consistent with each other?
   - 5: each is a concrete, descriptive phrase; they all describe the same musical mood and editing style
   - 3: most are specific; one or two are generic ("energetic", "fast")
   - 1: empty strings, contradictions, or one-word descriptors that don't compose

3. **beat_recipe_quality** — Do `beat_timestamps_s` and slot boundaries actually line up with audible beats?
   - 5: beats are accurate; slot boundaries land on beats; hook section timing makes sense for cut-on-beat editing
   - 3: beats roughly correct but spaced slightly off; slot boundaries fall between beats more often than on them
   - 1: beats are sparse, missing, or placed at arbitrary intervals

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"structural_fit": 4, "style_metadata": 4, "beat_recipe_quality": 4}, "reasoning": "<one sentence>"}
