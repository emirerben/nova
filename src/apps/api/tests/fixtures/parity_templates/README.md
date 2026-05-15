# Parity template fixtures

Minimal template definitions used by `tests/quality/single_pass_parity.py` to
compare single-pass and multi-pass encode output on real pixels.

These are NOT the production templates. They're stripped-down recipes that
exercise specific pipeline features (hard-cut, color-hold, xfade, curtain-close,
absolute overlays). Each fixture corresponds to a single-pass milestone:

- `impressing-myself.json` — M2 (2 slots, hard-cut, no overlays)
- `just-fine.json` — M2 (2 slots, hard-cut, no overlays)
- `morocco.json` — M2 (24 slots, hard-cut — stresses the scaffold)
- *(future)* `dimples-passport.json` — M3 (slots + dissolve transition)
- *(future)* `football-face-hook.json` — M2 with fade-black-hold interstitial
- *(future)* `rule-of-thirds.json` — M6 (absolute-timestamp grid overlays)

## Schema

```json
{
  "name": "string",
  "slots": [
    {
      "position": 1,
      "target_duration_s": 3.0,
      "transition_in": "none",
      "text_overlays": []
    }
  ],
  "interstitials": []
}
```

Required slot fields: `position` (1-indexed), `target_duration_s`,
`transition_in` ("none" for M2). Optional: `text_overlays`.

Optional `interstitials`: list of `{after_slot, type, hold_s, hold_color}`.
M2 supports `type ∈ {"fade-black-hold", "flash-white"}`. `"curtain-close"`
and `"barn-door-open"` require milestones 4+ and will fail the parity gate
until those land.

## Adding a milestone fixture

1. Find the closest production template (see `src/apps/api/scripts/seed_*.py`).
2. Strip it to the minimum that exercises the milestone feature.
3. Drop the JSON in this directory.
4. Append the name to `PARITY_TEMPLATE_FIXTURES` in `tests/quality/single_pass_parity.py`.
5. Re-run the parity workflow_dispatch.
