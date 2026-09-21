# clip_request_resolver rubric

Score each output on these dimensions, **integer 1-5**:

1. **grounding_honesty** — For every `label` assignment, does the record for that clip actually support the value (the words appear in `subject`/`summary`/`setting`/`activity`/`notable_moments`, or the intent has a `creator_text`)? For every membership assignment (`group`/`order`/`include`), does the record genuinely support that clip belonging?
   - 5: every assignment is clearly supported by that clip's record; anything the record can't confirm was routed to `needs_vision` instead of guessed
   - 3: assignments are mostly supported, but one confidence looks inflated relative to how vague the record actually is
   - 1: a value or membership claim is invented — the record says nothing that supports it, and it was not sent to `needs_vision`

2. **needs_vision_targeting** — When a clip's record is too vague to answer, is it correctly routed to `needs_vision` with a short, specific, answerable question (not a vague or leading one)?
   - 5: every genuinely ambiguous clip is flagged with a crisp, factual question; clips whose record already answers the intent are NOT redundantly flagged
   - 3: some ambiguous clips are flagged but the question is generic ("tell me about this clip") rather than targeted
   - 1: an ambiguous clip was guessed instead of flagged, or a clearly-answerable clip was needlessly sent to `needs_vision`

3. **restraint_and_coverage** — Does the output cover every clip that genuinely matches the intent's attribute without forcing clips that don't, and leave an intent's `assignments`/`needs_vision` both empty when nothing in the batch matches?
   - 5: coverage is complete and precise — no relevant clip missed, no irrelevant clip force-fit; an honest empty result when nothing matches
   - 3: one relevant clip missed, or one borderline clip forced in
   - 1: several relevant clips missed, or the intent was force-fit onto clips that clearly don't match rather than returning empty

**No-match fixtures:** when the correct answer for an intent is an empty `assignments` + `needs_vision`, score all three dimensions 5 if the agent returned that, and 1 if it forced any assignment.

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"grounding_honesty": 4, "needs_vision_targeting": 4, "restraint_and_coverage": 4}, "reasoning": "<one sentence>"}
