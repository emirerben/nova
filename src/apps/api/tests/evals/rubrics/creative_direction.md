# creative_direction rubric

The agent returns one freeform paragraph describing how an editor should recreate the template's style with new footage. Score on:

1. **technical_specificity** — Does it name concrete editing techniques (transition types, speed ramps, color choices)?
   - 5: names 3+ techniques with enough detail to reproduce
   - 3: gestures at techniques without committing to specifics
   - 1: only vibes ("energetic, fun") with no concrete instructions

2. **coverage** — Does it touch the dimensions the prompt asks for: pacing, transitions, color grading, speed ramps, audio sync, on-camera presence, letterbox, niche?
   - 5: covers ≥6 of 8
   - 3: covers 4-5
   - 1: covers ≤3 — too narrow to guide a recreation

3. **actionability** — Could a video editor reading this paragraph reproduce the style with new footage?
   - 5: yes, the paragraph is a real instruction
   - 3: directionally yes; editor would need to fill gaps
   - 1: no, the paragraph is descriptive prose with no instructions

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"technical_specificity": 4, "coverage": 5, "actionability": 4}, "reasoning": "<one sentence>"}
