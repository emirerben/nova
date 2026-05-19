# text_designer rubric

`text_designer` makes the per-element typographic decisions that turn a slot's placeholder into the visual anchor of the moment. The agent runs in **shadow mode** against the static `_LABEL_CONFIG` dict — its picks don't ship today, but its drift from a good baseline does. Score the output as a whole, **integer 1-5**:

1. **hierarchy_fit** — Does the styling match the `placeholder_kind`?
   - 5: subject gets bold treatment (xlarge/xxlarge, gold/brand color, attention effect); prefix is small + serif + neutral + quiet effect; other lands at medium with no shouting
   - 3: roughly right but one dimension feels off (subject sized correctly but with the wrong effect, or prefix with the right size but a too-loud color)
   - 1: hierarchy inverted (prefix shouts louder than subject, or subject treated as a footnote)

2. **slot_position_awareness** — Does the styling honor the slot's position in the template?
   - 5: slot 1 gets the signature treatment (xxlarge subject with `font-cycle` + `accel_at_s` timed to a beat ≈ 8s); mid-slots drop one size and use lighter effects; final slot is bolder than the middle but doesn't copy slot 1 verbatim
   - 3: slot 1 is correctly bold but a mid-slot is also slot-1-bold (signature feels diluted), OR slot 1 is correctly bold but the effect choice misses the established hook treatment
   - 1: every slot styled identically with no position awareness; or slot 1 is lighter than mid-slots

3. **timing_accuracy** — Are `start_s` and `accel_at_s` values inside the legal envelope for the slot's role?
   - 5: `start_s` lands in the right band per kind (prefix 0.5-2.0s, subject 2.0-3.0s on slot 1 / 0.0-1.0s on later slots, other 0.5-1.5s); `accel_at_s` set IFF effect is `font-cycle`, scaled to slot 1's ≈8s pattern for the hook
   - 3: timing is plausible but the prefix → subject ordering is tight (prefix lands too late, subject overlaps); OR `accel_at_s` is set but the effect isn't `font-cycle` (or vice versa)
   - 1: text starts in the slot's last 0.5s with no reading time, OR `accel_at_s` is set on a non-font-cycle effect (the renderer ignores it; signals confused output)

4. **tone_typography_alignment** — When `copy_tone` / `creative_direction` is set, does the styling honor it?
   - 5: tone maps cleanly to font_style + color + effect (casual→sans+gold/white+fade-in or pop-in; formal→serif+white/gold/deep-red+scale-up; energetic→bold sans+bounce/font-cycle; calm→serif light+off-white/pink+fade-in); creative direction's explicit cues are followed
   - 3: tone is honored loosely; one dimension (font OR color OR effect) doesn't match the tone but the others do
   - 1: tone is ignored or contradicted (calm tone → bounce effect; formal tone → bouncy bright magenta)
   - If no tone/direction provided, score this dimension a 3 (neutral)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"hierarchy_fit": 4, "slot_position_awareness": 4, "timing_accuracy": 4, "tone_typography_alignment": 4}, "reasoning": "<one sentence>"}
