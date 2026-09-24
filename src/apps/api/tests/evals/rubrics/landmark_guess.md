# landmark_guess rubric

Score each output on these dimensions, **integer 1-5**:

1. **specific_name** — Is `name` one specific, locally-spelled landmark or venue (not a generic scene like "a street" or "the sea")?
   - 5: a proper name people actually use, native spelling preserved
   - 3: a real place but a broader area than the landmark, or an anglicised spelling
   - 1: a generic scene description, or a full sentence

2. **grounded_in_frames_and_place** — Does the guess follow from what is visible plus the place/coordinates, with `evidence` naming the visible cue?
   - 5: evidence names a visible cue and the name is consistent with the place given
   - 3: consistent with the place but the evidence is generic
   - 1: contradicts the place, or a named landmark has empty evidence

3. **honest_uncertainty** — Does it say "unknown" when nothing identifiable is visible, and does confidence track how sure it really is?
   - 5: "unknown" exactly when warranted, confidence low on weak guesses and high on unmistakable ones
   - 3: a borderline case named instead of unknown, but confidence is middling
   - 1: a fabricated name for a generic scene, or "unknown" with high confidence

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"specific_name": 4, "grounded_in_frames_and_place": 4, "honest_uncertainty": 4}, "reasoning": "<one sentence>"}
