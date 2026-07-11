# retake_detector rubric

Context: the input is the verbatim word-level transcript of a raw talk-to-camera clip. The agent flags ABANDONED takes (an earlier, discarded delivery the speaker then re-delivers) as inclusive word-index spans; every flagged span gets CUT from the video. The final kept take must never be flagged. Cutting real content is far worse than missing a retake.

Score each output on these dimensions, **integer 1-5**:

1. **span_correctness** — Do the flagged spans cover exactly the abandoned delivery (including its restart-marker words like "wait, let me start over" / "dur baştan alayım") and nothing more? Cross-check the indices against the word list: the words INSIDE each span should be the discarded attempt; the words AFTER it should be the kept re-delivery of the same content.
   - 5: every span starts at the first abandoned word and ends at the last marker/flub word; the kept take is fully preserved; no span bleeds into kept content
   - 3: spans identify the right region but are off by a word or two at a boundary (e.g. missing the trailing marker word, or starting one word late)
   - 1: a span covers the KEPT take instead of the abandoned one, covers unrelated content, or the indices don't correspond to any restart in the transcript

2. **false_positive_discipline** — Does the agent flag ONLY genuine retakes? Rhetorical repetition ("Every day. Every day."), anaphora, list enumerations, recaps, and stumbles without a re-delivery must NOT be flagged. An empty list on a clean transcript is a perfect answer.
   - 5: nothing but genuine abandoned takes is flagged; negatives return an empty list
   - 3: one borderline span where the repetition could plausibly be read either way, clearly reasoned in the `reason`
   - 1: deliberate repetition, emphasis, or list structure flagged as a retake — real content would be cut

3. **reason_quality** — Does each `reason` name the concrete restart evidence (the marker phrase and/or the re-delivered content) in one short line, so an admin reading the cut-plan viewer can verify the cut without re-reading the whole transcript?
   - 5: reason quotes or names the marker/flub and points at the re-delivery ("re-delivered from word 8")
   - 3: reason is correct but generic ("speaker restarts here")
   - 1: reason is empty boilerplate, wrong, or contradicts the span

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"span_correctness": 4, "false_positive_discipline": 4, "reason_quality": 4}, "reasoning": "<one sentence>"}
