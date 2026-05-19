# Regression fixtures — template_recipe

## regression_24ac3408_malformed.json

Captured from production Langfuse trace `28f24609-492a-4529-b455-c0dce5cd3886`
on 2026-05-15 at 16:35 UTC. Template `24ac3408-993e-4341-a6bb-cfcc4f5ba41c`.

This is the exact recipe Gemini emitted on the second re-analysis of an
agentic template that had previously analyzed cleanly. It contains two
correctness bugs the parser must now reject:

1. **Duplicate adjacent-slot text overlay.** Slot 1 ends with the overlay
   `"being rich in life"` (6.0s → 15.0s). Slot 2 starts with the same text
   `"being rich in life"` (0.0s → 3.0s) at the same screen position. The
   renderer faithfully replays the text on the second slot, making the
   video appear to restart. The `_dedup_overlays_across_slots` guard in
   `app/agents/template_recipe.py` drops the slot-2 duplicate at parse time.

2. **Hallucinated fade-to-black interstitial.** The recipe inserts a
   `fade-black-hold` with `hold_s=2.0` between slots 1 and 2, even though
   the source video had `black_segments=[]` from the FFmpeg detection pass.
   The `_validate_interstitials` grounding guard drops interstitials with
   `hold_s >= 0.5` when no black segment supports them.

The fixture is consumed by `tests/agents/test_agentic_render_regression.py`.

## Next steps

After this PR ships and an agentic template is reanalyzed under the new
`prompt_version=2026-05-17`, capture the resulting recipe trace and save
it under `../prod_snapshots/` as a positive example showing the pct
schema in action. That fixture should have both `start_s/end_s` and
`start_pct/end_pct` on each text overlay and `target_duration_pct` on
each slot.
