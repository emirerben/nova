# clip_plan_matcher rubric

Score each output on these dimensions, **integer 1-5**:

1. **assignment_fit** — For each returned assignment, does the chosen clip actually serve the plan item's `theme` / `idea`? Read the clip's `subject` / `hook` / `transcript`, then read the item's theme/idea and the agent's `rationale`. If a creator would genuinely be happy to publish that clip as that day's video, score high.
   - 5: every assignment pairs a clip whose content clearly depicts the item's theme; rationale names a concrete subject-level connection
   - 3: assignments are plausible but at least one is a stretch (the clip is tangentially related to the item, or the rationale is generic)
   - 1: an assignment pairs a clip with an unrelated item (gym clip on a travel day), or the rationale contradicts the clip/item content

2. **score_calibration** — Do the numeric scores reflect real fit, and is the ordering honest (highest-first)?
   - 5: strong fits score high (≥7), the ordering is non-increasing, and any pairing that was a weak fit either scores low or was correctly omitted
   - 3: scores roughly track fit but cluster tightly, or the top score is too low for an obvious match
   - 1: scores are uncorrelated with fit, or a weak pairing outscores an obvious one

3. **restraint_and_spread** — Did the matcher avoid forcing weak matches, and (when it returned >1) spread across distinct items rather than dumping clips on one day?
   - 5: only genuinely good pairings returned (≤ max_assignments), each activating a different plan item when the clips allowed it; an honest empty list when nothing fit
   - 3: one redundant or borderline assignment, or two clips on the same item when a second item was a reasonable fit
   - 1: forced weak matches to fill the quota, or assigned multiple clips to one item while obviously-better items went unused

**No-match fixtures:** when the correct answer is an empty `assignments` list, score all three dimensions 5 if the agent returned `[]`, and 1 if it forced any assignment.

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"assignment_fit": 4, "score_calibration": 4, "restraint_and_spread": 4}, "reasoning": "<one sentence>"}
