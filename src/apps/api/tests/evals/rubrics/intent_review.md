# intent_review rubric

Score each output on these dimensions, **integer 1-5**. The input holds the issued requirements and the frame manifest; the output is the model's typed findings.

1. **verdict_grounding** — Does every `unmet` finding cite frames that plausibly show the problem, and every `met` verdict avoid overreach?
   - 5: each unmet cites 1-3 frames at the time the problem would be visible; nothing about sound, motion or duration is called met
   - 3: evidence is plausible but loose (extra frames cited, or a met on a requirement the stills only partly show)
   - 1: unmet without a visible basis, or a met claim about something stills cannot show

2. **defect_precision** — Is the `defect_kind` the narrowest honest description of what the frames show?
   - 5: wrong content is `wrong_content`, a misspelling is `copy_mismatch`, text missing at an anchored frame is `text_missing`
   - 3: right family, slightly broad (`other` where a named kind fits)
   - 1: a kind that contradicts the requirement (a wording kind on a footage requirement)

3. **calibration** — Is `confidence` honest?
   - 5: `high` only where the cited frames leave no doubt; `unverifiable` where the frames cannot show it
   - 3: one overconfident verdict
   - 1: high confidence on guesses, or every verdict `high`

Pass threshold: avg >= 3.5

Return ONLY:

    {"scores": {"verdict_grounding": 4, "defect_precision": 4, "calibration": 4}, "reasoning": "<one sentence>"}
