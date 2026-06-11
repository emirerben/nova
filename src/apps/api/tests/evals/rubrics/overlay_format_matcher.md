# overlay_format_matcher rubric

Score each output on these dimensions, **integer 1-5**:

1. **form_fits_content** — Does the chosen `effect` suit the hero clip and clip-set summary? Energetic / surprising / punchy content wants a kinetic effect (`karaoke-line`, `pop-in`, `scale-up`); calm / scenic / sentimental content wants `fade-in` or `static`.
   - 5: effect clearly matches the content's energy; a human editor would pick the same
   - 3: effect is defensible but not obviously right (e.g. `pop-in` on calm b-roll)
   - 1: effect fights the content (karaoke sweep on a slow sunset, or static on a hype reveal)

2. **legibility** — Will the text read on real footage? White (or high-contrast) primary, a highlight color that pops, a size appropriate to the position.
   - 5: colors read against typical footage; size/position sensible for a hero intro
   - 3: workable but slightly off (low-contrast highlight, oversized for the position)
   - 1: low-contrast or clashing colors that would be hard to read

3. **exemplar_grounding** — Do the `matched_example_ids` actually resemble this content, and does the chosen form echo them?
   - 5: matched exemplars genuinely match the content profile; the form mirrors theirs
   - 3: matched exemplars are loosely related
   - 1: matched exemplars are unrelated, or the form contradicts the exemplars it cites

4. **layout_fits_content** — Is `layout` right? `cluster` (editorial word-cluster) belongs ONLY on calm / scenic / aesthetic content (travel, nature, slow lifestyle) where a short 3-6 word hook works; everything else — energetic, punchy, wordy, karaoke-driven — wants `linear`. `cluster` paired with `karaoke-line` is always wrong.
   - 5: layout matches the content's mood and pacing; cluster only where editorial calm suits it
   - 3: defensible but not obviously right (linear on a calm scenic set that could carry a cluster)
   - 1: cluster on energetic/punchy content, or cluster + karaoke-line

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"form_fits_content": 4, "legibility": 4, "exemplar_grounding": 4, "layout_fits_content": 4}, "reasoning": "<one sentence>"}
