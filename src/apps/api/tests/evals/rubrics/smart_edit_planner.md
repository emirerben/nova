# smart_edit_planner rubric

`smart_edit_planner` decorates deterministic transcript candidates with a bounded
editorial treatment. Score the decision as a whole, **integer 1-5**:

1. **transcript_grounding** — Every proposal must use an exact candidate span and
   preserve transcript-derived sequence numbers and word anchors.
2. **visual_alignment** — Selected assets should match the spoken entity and enter
   on the entity's grounded word, without invented asset IDs.
3. **editorial_semantics** — Scene, text, boundary, and SFX tokens should support
   the candidate's role without overwhelming the talking head.
4. **safety_discipline** — The output must stay inside closed vocabularies and must
   not introduce voice-bearing SFX, raw paths, coordinates, font choices, gains,
   or absolute timing values.

Pass threshold: avg >= 3.5

Return ONLY:

    {"scores": {"transcript_grounding": 4, "visual_alignment": 4, "editorial_semantics": 4, "safety_discipline": 4}, "reasoning": "<one sentence>"}
