# music_matcher rubric

Score each output on these dimensions, **integer 1-5**:

1. **top_pick_fit** — Does the #1 ranked track actually serve this clip set? Read the clip-set summary, then read the #1 track's `labels` (mood, vibe_tags, copy_tone, transition_style, ideal_content_profile) and the agent's `rationale`. If a human editor would pick this song first for these clips, score high.
   - 5: top pick is clearly the strongest fit; rationale names a concrete connection between the song's labels and the clips (mood, energy, subject); predicted_strengths read true
   - 3: top pick is plausible but not obviously the best; rationale is generic ("upbeat, fits the energy") without naming a specific clip-side reason
   - 1: top pick contradicts the clips (sentimental ballad over chaotic comedy reel, or hype track over mellow family b-roll); rationale is hand-waving or contradicts itself

2. **score_calibration** — Do the numeric scores match the apparent fit, and do they spread enough to be useful?
   - 5: scores spread across the available range, top score is high (≥7), poor fits score low (≤4), score gaps reflect real differences in fit
   - 3: scores roughly correlate with fit but cluster tightly (all in 5-7) so the ranking carries the signal but the numbers don't, OR ordering is right but the top score is too low for an obvious winner
   - 1: scores are all near the same value with no relationship to fit, OR a clearly bad pick scores higher than a clearly good one

3. **diversity_in_top_k** — Across the top 3 picks, do the chosen tracks span genuinely different vibes when the library allows it?
   - 5: top 3 represent at least 2 distinct vibes (different `genre` or non-overlapping `vibe_tags`), AND no track is forced into the top-3 just to add diversity — every top-3 pick still scores within ~2 points of the #1
   - 3: top 3 are similar but the library is narrow so this is unavoidable; OR top 3 are clearly diverse but one of them is a stretch fit
   - 1: top 3 are three near-clones of the same song (same genre, overlapping vibe_tags, same copy_tone) when the library genuinely had other plausible options; OR diversity was forced at the cost of obvious fit

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"top_pick_fit": 4, "score_calibration": 4, "diversity_in_top_k": 4}, "reasoning": "<one sentence>"}
