# clip_metadata rubric

Score each output on these dimensions, **integer 1-5**:

1. **hook_quality** — Is `hook_text` a compelling opening that creates a question?
   - 5: hooks immediately, references something specific in the clip
   - 3: on-topic but generic ("watch this!")
   - 1: empty, vague, or off-topic

2. **moments_specificity** — Are `best_moments[].description` concrete actions, not scene labels?
   - 5: every description names a specific verb-driven action ("through-ball pass to striker")
   - 3: mostly action; 1-2 vague entries slip through
   - 1: scene descriptions ("player on field"), wide-shot labels, or "moment 1/2/3"

3. **moments_coverage** — Do moments span the clip with varied energy?
   - 5: timestamps spread across the clip, energy varies meaningfully (some 9s, some 5s)
   - 3: spread but flat energy, OR varied energy but clustered timestamps
   - 1: all clustered AND identical energy values

4. **score_calibration** — Does `hook_score` reflect what the moments and hook_text actually contain?
   - 5: high score iff hook is strong AND moments are specific; low score iff content is weak
   - 3: score is in the right ballpark
   - 1: score contradicts the rest of the output (e.g., 9 with empty moments)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"hook_quality": 4, "moments_specificity": 3, "moments_coverage": 4, "score_calibration": 4}, "reasoning": "<one sentence>"}
