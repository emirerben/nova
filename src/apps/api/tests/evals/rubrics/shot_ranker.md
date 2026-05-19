# shot_ranker rubric

`shot_ranker` picks top-K moments and orders them. Rank 1 is the hook — getting it wrong wastes the swipe. Score the ranking as a whole, **integer 1-5**:

1. **rank_1_hook_strength** — Is the rank-1 pick genuinely the strongest hook in the candidate set?
   - 5: rank-1 has the highest `hook_score` AND the most concrete, action-driven description
   - 3: rank-1 is one of the top 2-3 candidates; defensible but not the clear winner
   - 1: rank-1 has a lower `hook_score` than other candidates AND a weaker description

2. **set_variety** — Across the top-K, do descriptions and energy levels span distinct beats?
   - 5: no two ranked moments share a near-duplicate description; energy span ≥ 2.0 across the set when the candidate pool allows it
   - 3: mostly varied; one near-duplicate slipped in but it had a high `hook_score`
   - 1: top-K is full of similar-looking moments (same action verb, same energy band) while distinct alternatives sat unranked

3. **description_quality** — Are the ranked moments described with concrete action verbs, not vague labels?
   - 5: every ranked moment names a specific verb-driven action ("DJ slams pad", "wave breaks over rocks", "subject reacts to text")
   - 3: most descriptions are concrete; one or two are scene labels ("crowd shot", "venue interior")
   - 1: ranked moments are dominated by vague labels — the ranking can't be trusted because the inputs are too thin

4. **thematic_fit** — When `copy_tone` / `creative_direction` is provided, does the ranking honor it?
   - 5: ranked picks reinforce the stated tone (formal tone → composed shots ranked high; energetic → high-energy moments ranked high)
   - 3: tone is honored loosely; one pick contradicts it but the rationale explains why
   - 1: ranking contradicts the stated tone (high-energy chaotic moments ranked top for `copy_tone="calm"`)
   - If no tone/direction was provided, score this dimension a 3 (neutral)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"rank_1_hook_strength": 4, "set_variety": 4, "description_quality": 4, "thematic_fit": 4}, "reasoning": "<one sentence>"}
