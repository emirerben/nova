# transition_picker rubric

`transition_picker` picks the punctuation between two specific clips. Wrong picks read as glitches (whip-pan on static shots) or break the template's pacing (dissolve on energetic content). Score the decision as a whole, **integer 1-5**:

1. **default_fidelity** — Does the pick honor Rule 0 (return the template default unchanged unless the pair clearly contradicts it)?
   - 5: when the default fits (camera-movement compatible, energy delta in band), the pick IS the default. Overrides happen only when the rationale names a clear contradiction.
   - 3: pick matches the default but the rationale doesn't explain why; OR an override happens with a plausible reason that's slightly stronger than needed
   - 1: pick deviates from the default with no justification, OR pick matches a clearly-contradicted default without flagging the mismatch

2. **camera_movement_compatibility** — Is the picked transition physically compatible with the camera state of both clips?
   - 5: whip-pan only when both clips have compatible lateral movement; zoom-in only when incoming has subject visible from frame 0; dissolve picked with breathing room (duration ≥ 0.4s); curtain-close at section breaks regardless of camera; none used for narrative scene splits (vs. hard-cut's "the transition IS a hard cut")
   - 3: pick is physically plausible but slightly off (e.g., whip-pan with directions correct but the dest subject isn't framed for the entry yet)
   - 1: whip-pan between two static shots ("reads as a glitch"); zoom-in landing on an empty wide frame; dissolve at 0.2s

3. **pacing_style_modulation** — When `pacing_style` is set, does the pick honor the bias?
   - 5: high-energy → leans toward hard-cut/whip-pan/zoom-in with durations at the SHORT end; slow-cinematic → leans toward dissolve/curtain-close with LONG durations; mid-tempo → balanced
   - 3: pick is consistent with pacing but the duration drifts to the wrong end of the range
   - 1: pick contradicts the pacing (slow-cinematic → whip-pan at 0.2s; high-energy → dissolve at 0.8s)
   - If `pacing_style` is empty, score this dimension a 3 (neutral)

4. **duration_envelope** — Does `duration_s` land inside the canonical range for the picked transition?
   - 5: hard-cut / none = 0.0; whip-pan ∈ [0.20, 0.40]; zoom-in ∈ [0.30, 0.50]; dissolve ∈ [0.40, 0.80]; curtain-close ∈ [0.60, 1.00]
   - 3: duration is in the right ballpark but slightly outside the canonical range (e.g., dissolve at 0.35s, whip-pan at 0.45s)
   - 1: duration is clearly wrong for the transition (hard-cut with duration 0.8; dissolve at 0.1s)

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"default_fidelity": 4, "camera_movement_compatibility": 4, "pacing_style_modulation": 4, "duration_envelope": 4}, "reasoning": "<one sentence>"}
