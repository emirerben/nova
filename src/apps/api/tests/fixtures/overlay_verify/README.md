# overlay-verify fixtures

Recipe-overlay scenarios spanning the documented renderer failure modes. Each
file is `{"overlays": [...]}` (the shape inside `recipe.slots[i].text_overlays`).
A correct Skia renderer must render every overlay here **un-clipped, full text,
at the declared anchor**. They double as the regression set for
`make verify-overlays ARGS="--fixtures"` and `tests/pipeline/test_overlay_verify.py`.

Seeded from the #296 class (prod jobs `ff0d2e1c` / `89cde014` — `text_anchor="left"`
rendered "It's not just luck" as "s not just luck"). When you add a new
anchor/position/effect field to the burn dict, add a fixture exercising it.
