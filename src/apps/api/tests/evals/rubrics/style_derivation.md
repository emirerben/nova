# style_derivation rubric

The agent receives a creator's persona (summary, content pillars, tone, audience),
optional TikTok analysis summary, available curated style sets (id/label/tags), and
font vibe descriptors. It must pick a style set and optionally override specific knobs
(font, size, position, colors) when the persona clearly calls for it.

Read the input persona + TikTok summary and the output UserStyle. Score each
dimension, **integer 1-5**:

1. **persona_alignment** — Does the chosen style set and any knob overrides match the
   creator's personality, content type, and tone?
   - 5: set tags and font vibe clearly match the persona (e.g. a city-aesthetic creator
     gets editorial serif, not a bold sans; a fitness creator gets dynamic not subtle);
     rationale names a concrete persona signal that drove the pick
   - 3: plausible pick but rationale is generic ("this looks professional"); knob
     overrides unjustified or absent when persona clearly warranted them
   - 1: set/font contradicts the persona; or rationale absent; or invented set/font name

2. **parity_safety** — Does the output only use fields within the parity-safe knob set?
   No `effect` field, no invented keys.
   - 5: output knobs contain only allowed fields; no `effect`; all values in valid ranges
   - 3: all keys valid but one value slightly out of range (e.g. text_size_px=85)
   - 1: `effect` present in knobs; or unknown field key; or style_set_id not in the
     provided catalog list

3. **calibration** — Are knob overrides proportional? Only override knobs when the
   persona gives clear evidence. Editorial taste: smaller sizes (≤62px) and serifs over
   loud sans.
   - 5: ≤3 knob overrides, each with a named persona justification; or zero overrides
     with a rationale that the set already matches perfectly
   - 3: too many overrides (>4) for a subtle persona; or one override unjustified
   - 1: overrides every field regardless of persona; or overrides nothing for a creator
     whose persona clearly differs from the default set

4. **instruction_level_correctness** — Is `instruction_level` set correctly?
   - 5: "none" only when persona/TikTok data explicitly signals the creator wants
     minimal guidance; "full" for new/unclear creators; "light" for intermediate
   - 3: slightly conservative (full when light would do); rationale mentions guidance
   - 1: "none" with no persona signal; or "full" when TikTok shows an established creator
     who explicitly dislikes generic advice

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"persona_alignment": 4, "parity_safety": 5, "calibration": 4, "instruction_level_correctness": 4}, "reasoning": "<one sentence>"}
