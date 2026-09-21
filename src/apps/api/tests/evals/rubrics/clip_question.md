# clip_question rubric

Score each output on these dimensions, **integer 1-5**:

1. **answer_grounding** — Is the answer something that could genuinely be confirmed from the single clip being asked about (not general-knowledge guessing), and does the `evidence` describe what was actually seen/heard?
   - 5: the answer is a short, concrete noun phrase directly tied to visible/audible content, with evidence that names the specific cue
   - 3: the answer is plausible but the evidence is generic or only weakly ties back to the clip
   - 1: the answer looks guessed from world knowledge rather than the clip, or evidence is empty for a non-"unknown" answer

2. **honest_uncertainty** — Does the model say "unknown" (rather than fabricate) when the question genuinely cannot be answered from this clip, and does confidence track how sure it really is?
   - 5: "unknown" used exactly when warranted, confidence low on uncertain answers and high on clearly-visible ones
   - 3: a borderline case answered instead of flagged unknown, but confidence is at least appropriately middling
   - 1: a fabricated answer on a genuinely unanswerable question, or an "unknown" answer with high confidence

3. **format_discipline** — Is `answer` a short noun phrase (≤3 words), never a sentence or hedge like "it looks like maybe a..."?
   - 5: answer is a clean 1-3 word phrase
   - 3: answer is borderline wordy but still short
   - 1: answer is a full sentence or contains hedging language

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"answer_grounding": 4, "honest_uncertainty": 4, "format_discipline": 4}, "reasoning": "<one sentence>"}
