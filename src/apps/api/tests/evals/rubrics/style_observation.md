# style_observation rubric

The agent watches one of a creator's own TikTok videos and observes how they style
their on-screen text overlays. It must report what is VISUALLY PRESENT, using only
the constrained vocabulary defined in the agent schema.

Read the video description / fixture context and the output ``VideoStyleObservation``.
Score each dimension, **integer 1–5**:

1. **accuracy** — Does the observation match what was actually in the video?
   - 5: all reported fields match the video's actual text style; ``has_on_screen_text``
     correctly identifies whether text is present; no hallucinated attributes
   - 3: most fields correct but one minor mismatch (e.g. called "center" when text is
     slightly "center-above"); or ``confidence`` inflated vs actual video clarity
   - 1: ``has_on_screen_text`` wrong; or major field mismatch (wrong font_feel,
     wrong color family); or fields present when video has no on-screen text

2. **vocabulary_discipline** — Does the output use only the allowed Literal values?
   No raw font names (e.g. "Playfair Display"), no pixel values, no invented keys.
   - 5: every field uses only the defined Literal vocabulary; no raw font names; no
     extra keys; ``has_on_screen_text=False`` videos return ONLY that field + confidence
   - 3: one field slightly outside vocabulary but parse()-correctable (e.g. "Middle"
     instead of "center" — would be coerced to null by parse())
   - 1: raw font name present; ``effect`` field present; invented vocabulary; or
     a no-text video returns text-style fields

3. **completeness** — For videos WITH on-screen text, are all observable fields reported?
   - 5: reports every visually determinable field; only uses null for genuinely
     ambiguous/invisible attributes (not as a shortcut)
   - 3: one or two fields left null that were determinable from the footage
   - 1: most fields null despite clear visible text; or only ``font_feel`` reported
     when colors, position, and size are plainly visible
   Note: this dimension scores N/A (treat as 5) for ``has_on_screen_text=False`` videos.

4. **confidence_calibration** — Is the confidence value appropriate?
   - 5: confidence ≥ 0.8 for crystal-clear on-screen text; ≤ 0.5 for fast-moving,
     small, or partially-visible text; uses full 0–1 range appropriately
   - 3: slightly overconfident (0.9 for a barely-visible overlay) or underconfident
     (0.4 for a clean, large title card)
   - 1: confidence always 0.7 regardless of actual video clarity; or wrong end of range

Pass threshold: avg ≥ 3.5; vocabulary_discipline must be ≥ 4 (non-negotiable parity guard)

Return ONLY:

    {"scores": {"accuracy": 4, "vocabulary_discipline": 5, "completeness": 4, "confidence_calibration": 4}, "reasoning": "<one sentence>"}
