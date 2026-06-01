# content_plan_generator rubric

The agent turns a creator PERSONA (+ optional life events) into a day-by-day
content plan: each item has day_index, theme, idea, filming_suggestion, and a
`rationale` shown to the creator as "why this works". Read the persona + events in
the input, then the plan. Score each dimension, **integer 1-5**:

1. **persona_grounded_variety** — Are the ideas grounded in the persona (and events, if any), rotating across the creator's pillars rather than repeating one note?
   - 5: every idea is specific to this creator and their pillars; the plan rotates across pillars and leans into the events; no generic filler; no duplicate ideas
   - 3: mostly grounded and varied, but a few generic or near-duplicate ideas, or one pillar dominates
   - 1: generic short-form filler that ignores the persona, repeats the same idea, or never reflects the stated events

2. **activation_frontloading** — Are the strongest, most-postable ideas front-loaded into the first 7 days, with concrete themes/ideas and practical filming tips throughout?
   - 5: days 1-7 are the most postable, lowest-friction, highest-appeal ideas; themes + ideas are concrete; filming tips are practical
   - 3: a reasonable spread but the strongest ideas aren't clearly front-loaded, or filming tips are thin
   - 1: weak/abstract ideas up front, or themes/ideas too vague to film

3. **reasoning_quality** — Do the per-item `rationale`s name a real lever (a proven success factor) and a concrete fit, rather than generic praise? Judge across the items that carry one.
   - 5: most items' rationales name a genuine lever (hook in the first 2s, save-worthy, relatable one-liner, felt-moment opener…) AND tie it to this idea/creator; reads like a strategist
   - 3: rationales are on-topic but often generic ("this will perform well") or name a lever without connecting it to the specific idea
   - 1: rationales are missing, pure generic praise, or contradict the idea

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"persona_grounded_variety": 4, "activation_frontloading": 4, "reasoning_quality": 4}, "reasoning": "<one sentence>"}
