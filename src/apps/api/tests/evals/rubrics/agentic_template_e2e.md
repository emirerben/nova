# agentic_template_e2e rubric

Score the **assembled recipe** that the agentic build orchestrator produces (creative_direction → template_recipe → text_designer baked per overlay). This is end-to-end coherence, not per-agent quality. Per-agent rubrics already cover that.

Score each dimension, **integer 1-5**:

1. **overlay_styling_coherence** — Does every label-like overlay have all six text_designer fields filled (text_size, font_style, text_color, effect, start_s, accel_at_s when font-cycle)? Are subject overlays visually distinct from prefix overlays (subjects bigger, branded color; prefixes quieter)?
   - 5: every label has full styling; subjects clearly dominate prefixes
   - 3: most have full styling; one or two missing accel_at_s on font-cycle subjects
   - 1: styling fields missing on labels; subject vs prefix indistinguishable

2. **transition_pacing_fit** — Do `transition_in` choices flow given the slot's energy and the template's `pacing_style`? Hard-cut on hook is fine; a dissolve out of a high-energy slot is suspicious.
   - 5: every transition justified by adjacent slot energy and global pacing
   - 3: most transitions reasonable, one or two feel generic
   - 1: transitions ignore slot energy or contradict pacing_style

3. **beat_snap_realism** — If `beat_timestamps_s` is non-empty, do slot boundaries land near beats? If empty, is the total_duration_s consistent with the sum of slot durations + interstitial holds?
   - 5: durations sum correctly AND (if beats present) every slot boundary is within 0.3s of a beat
   - 3: durations consistent, beats roughly aligned with drift
   - 1: durations don't sum, or beats and boundaries are independent

4. **first_slot_hook_design** — Is the hook (slot 0 / position 0) typographically the heaviest moment of the template — large subject overlay, font-cycle effect, accel timed before the interstitial fires?
   - 5: hook has the biggest subject, font-cycle with accel synced to first interstitial
   - 3: hook has a subject overlay but accel timing is generic
   - 1: hook is indistinguishable from any other slot

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"overlay_styling_coherence": 4, "transition_pacing_fit": 4, "beat_snap_realism": 5, "first_slot_hook_design": 4}, "reasoning": "<one sentence>"}
