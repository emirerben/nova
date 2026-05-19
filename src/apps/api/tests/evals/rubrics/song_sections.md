# song_sections rubric

Score each output on these dimensions, **integer 1-5**:

1. **section_edit_worthiness** — Would a human editor actually cut to these sections? Are they hooks, drops, choruses, or memorable musical events — not arbitrary 30-second windows?
   - 5: every section is a moment an editor would reach for first (the obvious chorus, the climactic drop, the standout bridge)
   - 3: rank 1 is a strong choice; ranks 2/3 are usable but less obvious
   - 1: picks feel mechanical (every-30-seconds-from-the-start) or land on dead air (filler, fade-out, intro silence)

2. **boundary_quality** — Do `start_s`/`end_s` land on musical phrase boundaries, or do they cut mid-bar/mid-word? Slot-aligned cuts (from the audio_template recipe context) score highest.
   - 5: every boundary lands on a phrase start/end; cuts feel clean and intentional
   - 3: most boundaries are good; one section starts or ends a beat early/late
   - 1: boundaries are arbitrary — sections cut mid-vocal-line or mid-instrumental-phrase

3. **rank_calibration** — Is rank=1 actually the strongest section? Is rank=2 stronger than rank=3? The matcher uses rank 1 for the default edit and rank 2/3 for variant diversity, so the ordering must be defensible.
   - 5: ordering is clearly correct — rank 1 is the obvious winner, rank 2 is the next-best meaningfully different section, rank 3 is third
   - 3: rank 1 is right; ranks 2 and 3 could plausibly be swapped
   - 1: ordering looks arbitrary or rank 1 is worse than rank 2/3

Pass threshold: avg ≥ 3.5

Return ONLY:

    {"scores": {"section_edit_worthiness": 4, "boundary_quality": 4, "rank_calibration": 4}, "reasoning": "<one sentence>"}
