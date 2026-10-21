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

`generic_context_labels.json` compiles short and long combined location/activity
labels through the same right-aligned burn-dict contract used by guided edits.

`narrated_title_fit.json` (KRI-455 follow-up) is the narrated opening title as the
cloud burns it: one short title on the `top`/`large` preset and three long ones
fitted into the top band by `narrated_title.narrated_title_placement` (smaller
font, block top on the 6% safe margin). On the preset alone the long ones wrapped
to 4-6 lines and ran off the top of the frame.

`legacy_font_aliases.json` burns the four legacy file-root names TextElement
still accepts ("Inter-Bold" guided narration captions + v<3 titles,
"Inter-Regular"/"PlayfairDisplay-Regular" web presets, "PlayfairDisplay-Bold"
visual-block cards). Each must resolve to its registry face, not fall back to
the `display` default — a fallback is a FAIL here.

`turkish_glyphs.json` (KRI-785) burns Turkish text (`ğĞşŞıİçÇöÖüÜ` + punctuation) in
every registry font. Faces that lack a letter (Alte Haas Grotesk, Permanent Marker,
Satisfy, Big Curls, Rascal, and ZT Bros Oskon 90s, whose dotless ı still has its dot)
must switch the whole run to their `assets/fonts/glyph-fallbacks.json` partner. The
verifier FAILs any overlay whose resolved face still lacks a glyph.
The last three lines cover the review gaps: Big Curls with only `ç ö ü` (its mapped
glyphs there are blank outlines, so no `ş` is needed to trigger the fallback), a `₺`
price in the display default, and a locale-free lower-cased `İ` (`i` + U+0307).

`shaped_rtl_fallback.json` (KRI-785) is Arabic smooth-type in Inter. No registry face
draws Arabic, but the shaped path fills each codepoint from the system font manager, so
the glyph check must not FAIL it (it only FAILs codepoints no system face can draw).
