# template_recipe rubric

Score each output on these dimensions, **integer 1-5**:

1. **slot_design** — Do slots reflect the actual structure of a TikTok-style template?
   - 5: slot count and durations match a recognizable template arc (hook → body → outro)
   - 3: structure is plausible but generic
   - 1: arbitrary count, durations don't add up to a believable short

2. **transition_appropriateness** — Are `transition_in` choices justified by the slot's role?
   - 5: hook gets attention-grabbing transition, body cuts feel paced, outro lands
   - 3: defaults everywhere ("hard-cut") but not wrong
   - 1: jarring or contradictory transitions for the slot's energy

3. **interstitials_fidelity** — Do interstitials match black-segment hints when provided?
   - 5: every detected curtain-close / fade-to-black has a matching interstitial entry with sensible animate_s/hold_s
   - 3: most match; minor over- or under-counting
   - 1: ignores the hints entirely or invents interstitials with no signal

4. **style_metadata** — Are `copy_tone`, `caption_style`, `color_grade`, `pacing_style`, `subject_niche` specific and consistent with each other?
   - 5: each is a concrete, descriptive phrase; they describe the same template
   - 3: at least 3 of 5 are specific
   - 1: empty strings, generic words ("nice"), or contradictions

5. **text_bbox_plausibility** — When the template has visible burned-in text, does each overlay's `text_bbox` plausibly identify the text region? Score `null` (skip this dimension) if no overlay in the recipe has burned-in text or no overlay emits a bbox. When scoring:
   - 5: bbox is present for every visible-text overlay, coordinates look tight around the glyphs (not full frame, not zero area), `sample_frame_t` falls within the overlay's `[start_s, end_s]` window, and bbox is absent (`null`) for overlays whose `sample_text` was clearly inferred from vibe rather than read off the screen
   - 3: most bboxes look plausible; one or two are loose, off-position, or missing
   - 1: bboxes are wildly wrong (covering whole frame, zero area, far from text), or emitted on overlays with no visible burned-in text

Pass threshold: avg ≥ 3.5 across the dimensions that were scored (skip dimensions return `null` and are excluded from the average).

Return ONLY:

    {"scores": {"slot_design": 4, "transition_appropriateness": 3, "interstitials_fidelity": 5, "style_metadata": 4, "text_bbox_plausibility": null}, "reasoning": "<one sentence>"}
