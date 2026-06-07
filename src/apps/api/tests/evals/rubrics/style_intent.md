# style_intent rubric

Score each output on these dimensions, **integer 1-5**:

1. **intent_accuracy** — Does the chosen intent correctly classify what the creator wants?
   - 5: intent is unambiguous — "make my font bigger" → style_edit, "I want more travel content" → persona_preference, "stop showing gym stuff" → scope_reduction
   - 3: intent is plausible but another would also fit (borderline utterance); the fields are still correct for the chosen intent
   - 1: wrong intent chosen (e.g. persona_preference when the creator clearly asked for a visual change; style_edit returned for a content-strategy question)

2. **fields_precision** — Are the returned fields correct and minimal?
   - 5: only the correct fields are populated with correct values; no extra/invented keys; knob keys are all parity-safe (font_family, text_size_px, position, position_x_frac, position_y_frac, text_anchor, text_color, highlight_color, stroke_width, cycle_fonts)
   - 3: fields are mostly correct but one value is imprecise or a borderline extra field appears
   - 1: wrong fields for the intent, hallucinated knob keys (e.g. "effect"), or no fields when clear fields were implied

3. **reply_quality** — Is the reply appropriate for the intent and confidence level?
   - 5: confirmation reply for high-confidence applied intents; focused clarifying question for clarify/low-confidence; helpful "I can X" for unknown — all concise and actionable
   - 3: reply is correct in type but generic or wordy
   - 1: reply is wrong type (confirmation when clarification needed, or vice versa), or empty

4. **confidence_calibration** — Is the confidence score honest?
   - 5: high confidence (0.8+) only when intent+fields are unambiguous; low confidence (< 0.55) when utterance is genuinely vague; needs_clarification=true when appropriate
   - 3: confidence slightly off (e.g. 0.9 for a borderline utterance, or 0.5 for an obvious one)
   - 1: confidence is uncalibrated (always 1.0, always 0.5, or inverted — high for vague, low for clear)

**Hallucination checks (automatic fail → score 1 for fields_precision):**
- Any knob key outside the 10 parity-safe names
- style_set_id that is not in the catalog (coercion missed)
- instruction_level outside "full" | "light" | "none"

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"intent_accuracy": 4, "fields_precision": 4, "reply_quality": 4, "confidence_calibration": 4}, "reasoning": "<one sentence>"}
