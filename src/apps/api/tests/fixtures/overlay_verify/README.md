# overlay-verify fixtures

Recipe-overlay scenarios spanning the documented renderer failure modes. Each
file is `{"overlays": [...]}` (the shape inside `recipe.slots[i].text_overlays`).
A correct Skia renderer must render every overlay here **un-clipped, full text,
at the declared anchor**. They double as the regression set for
`make verify-overlays ARGS="--fixtures"` and `tests/pipeline/test_overlay_verify.py`.

Seeded from the #296 class (prod jobs `ff0d2e1c` / `89cde014` — `text_anchor="left"`
rendered "It's not just luck" as "s not just luck"). When you add a new
anchor/position/effect field to the burn dict, add a fixture exercising it.

`smooth_type.json` exercises the Text Motion v2 burn payload and settled
shaped-run layout through the real Skia/FFmpeg path. The verifier samples at
95% of the overlay window, so active reveal, blur, and hold timing are covered
by the dedicated renderer/parity suites rather than this settled-frame fixture.

`high_visibility_shadow.json` explicitly selects `shadow_style="high_visibility"`
and pins the opt-in dual-shadow profile in both white and yellow at every reference
calibration size (48/60/88/128px).

`guided_title_face_avoid_band.json` (KRI-116) is the guided-story title burn dict
(`vertical_anchor="center"`, 104px Fraunces) parked at `position_y_frac=0.34` — one
of the candidate bands `choose_guided_text_y_frac` moves a title to when the
authored default (`y_frac=0.16`) collides with a face. Confirms the move itself
never clips text; face-avoidance placement logic is unit-tested separately in
`tests/pipeline/test_render_geometry.py` and `tests/pipeline/test_guided_story.py`.
