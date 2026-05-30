# persona_generator rubric

The agent turns an onboarding questionnaire into an editable creator PERSONA
(summary, content_pillars, tone, audience, posting_cadence, sample_topics) plus a
`rationale` shown to the creator as "why this lane". Read the questionnaire in the
input, then the persona. Score each dimension, **integer 1-5**:

1. **lane_focus_and_groundedness** — Does the persona land THIS person in a sharp, recognizable lane built from their actual answers (not a generic creator template)?
   - 5: a focused, ownable lane; summary + pillars clearly trace to specific questionnaire details (their job/hobbies/location); reads like it could only be this person
   - 3: roughly right lane but partly generic; some pillars could apply to anyone in the niche; loosely grounded
   - 1: generic "lifestyle creator" mush, contradicts the answers, or invents a different person than the questionnaire describes

2. **cadence_and_postability** — Is the posting cadence realistic for their stated life, and are pillars/sample_topics specific enough to actually film?
   - 5: cadence fits their real constraints (school/work/travel); 3-5 pillars + 5-8 topics that are concrete, varied, and filmable
   - 3: plausible cadence; topics mostly postable but some vague; pillar count within bounds
   - 1: unrealistic cadence (e.g. "daily" for a full-time student with no time), or topics so vague they couldn't be filmed

3. **reasoning_quality** — Does `rationale` give a concrete, encouraging "why this lane fits you AND why it works on short-form", naming a real lever rather than generic praise?
   - 5: 1-2 sentences, specific to this person, names a genuine TikTok lever (e.g. save-worthy guides, a felt-moment opener, relatable hook) and ties it to their lane; reads like a strategist
   - 3: on-topic but partly generic ("this niche does well on TikTok") or names a lever without connecting it to this person
   - 1: empty, pure generic praise ("you'll do great!"), jargon, or a claim that contradicts the persona

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"lane_focus_and_groundedness": 4, "cadence_and_postability": 4, "reasoning_quality": 4}, "reasoning": "<one sentence>"}
