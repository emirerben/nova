# template_text rubric

The eval harness pre-computes objective scores against OCR-derived ground truth
(when a `ground_truth/<slug>.json` fixture exists) and passes them to you as
the `scoring` field in the agent_output payload. Use those numbers as the
anchor when assigning rubric scores — do not estimate from the raw overlay
list when scoring numbers are present.

Score each output on these dimensions, **integer 1-5**:

1. **completeness** — Fraction of ground-truth overlays the agent recovered.
   Anchor on `scoring.completeness` if present.
   - 5: ≥ 0.95 (essentially every text was found, including small watermarks and brief labels)
   - 4: 0.85 – 0.94
   - 3: 0.70 – 0.84 (clear gaps — typically the small text the prompt explicitly warned about)
   - 2: 0.50 – 0.69
   - 1: < 0.50 (large fraction of the on-screen text was missed)

2. **timing_accuracy** — Mean temporal IoU between matched predicted/truth overlays.
   Anchor on `scoring.mean_temporal_iou` if present.
   - 5: ≥ 0.85 (timings line up to within ~10% of overlay duration)
   - 4: 0.70 – 0.84
   - 3: 0.55 – 0.69 (visible drift, manageable with `start_s_override` / `end_s_override`)
   - 2: 0.35 – 0.54
   - 1: < 0.35 (timings are essentially uncorrelated with truth)

3. **position_accuracy** — Mean spatial IoU between matched predicted/truth bboxes.
   Anchor on `scoring.mean_spatial_iou` if present.
   - 5: ≥ 0.70 (boxes overlap by more than two-thirds of their area)
   - 4: 0.55 – 0.69
   - 3: 0.40 – 0.54 (right region of frame but loose padding)
   - 2: 0.25 – 0.39
   - 1: < 0.25 (boxes are in the wrong half of the frame)

4. **font_color_accuracy** — Fraction of matched pairs whose font_color_hex is within
   CIE76 ΔE ≤ 10 of ground truth. Anchor on `scoring.color_match_fraction` if present.
   - 5: ≥ 0.90 (colors agree on essentially every overlay)
   - 4: 0.75 – 0.89
   - 3: 0.60 – 0.74 (right color family, occasional hue swap)
   - 2: 0.40 – 0.59
   - 1: < 0.40 (colors are wrong more often than not)

5. **effect_label_accuracy** — Fraction of matched pairs with identical `effect`
   value vs human-labeled truth. Anchor on `scoring.effect_label_accuracy` if present.
   - 5: ≥ 0.90 (font-cycle, typewriter, slide-in etc. correctly identified)
   - 4: 0.75 – 0.89
   - 3: 0.60 – 0.74 (defaults to "static" / "none" too often)
   - 2: 0.40 – 0.59
   - 1: < 0.40 (effect labels are essentially random)

When `ground_truth` is missing for a fixture (i.e. `scoring` is `null` in the
agent_output payload), fall back to qualitative inspection of the overlay
list: does it look plausible for a TikTok template? Does it include
watermark-style overlays as well as hooks? Are bboxes inside the frame? Is the
color hex format right? Score conservatively in this fallback mode — 3 for
"looks plausible," 4 only if the breadth of overlays clearly covers everything
the rubric asks for.

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"completeness": 4, "timing_accuracy": 3, "position_accuracy": 4, "font_color_accuracy": 4, "effect_label_accuracy": 3}, "reasoning": "<one sentence>"}
