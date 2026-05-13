# clip_router rubric

`clip_router` assigns one candidate clip to each slot. The wrong assignment is invisible to "looks sane" eyeballing — a swap between two roughly-similar candidates can quietly degrade the whole narrative. Score the assignment as a whole, **integer 1-5**:

1. **slot_type_fit** — Does each assignment match the slot's role?
   - 5: hook slots get the highest-hook-score candidates; broll/content slots get visual variety, not the peaks; punchline/outro slots get the peak energy
   - 3: most assignments fit; one or two are defensible-but-not-optimal
   - 1: hook slot got a low-hook-score candidate, or a high-hook-score candidate was wasted on a broll slot

2. **energy_match** — Does each slot's `energy` target line up with its assigned candidate's `energy`?
   - 5: every assignment is within ±1.5 energy, OR the rationale explicitly justifies the gap
   - 3: most are aligned; one assignment has a >1.5 energy gap without rationale
   - 1: multiple assignments invert the expected energy curve (low-energy candidates on high-energy slots)

3. **sequence_variety** — Across adjacent slots, do descriptions / actions span distinct beats?
   - 5: no two adjacent slots use near-duplicate descriptions when alternatives existed
   - 3: one adjacent-pair near-duplicate, but the alternatives were also weak
   - 1: two or more adjacent slots use near-identical candidates while distinct alternatives sat idle

4. **rationale_quality** — Does each `rationale` give a concrete, code-grounded reason?
   - 5: every rationale names the dimension that drove the pick ("hook_score 9 vs 4", "energy 7.5 matches slot target 7", "only candidate with `description` mentioning ball-strike")
   - 3: most rationales are specific; one or two read as "best fit" / "good match"
   - 1: rationales are boilerplate ("best clip", "matches well", "good fit") — no auditable reason

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"slot_type_fit": 4, "energy_match": 4, "sequence_variety": 3, "rationale_quality": 4}, "reasoning": "<one sentence>"}
