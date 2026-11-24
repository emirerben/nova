# Intro scene (KRI-1609 / KRI-1610)

Dark and uncalled. `IntroScene` is the closed shape an AI will fill in to compose a creator-style
intro; this step ships the schema, the compiler to today's title rows, the five reference
teardowns as data, and a render check. No wire change, no flag, nothing calls it yet.

## Data shape

`app/pipeline/intro_scene.py`: one `solid`/`accent` voice per word of the approved title, plus an
option id each for font pair, ink, title zone, entrance and sound, or an explicit decline. No field
reaches the screen as text and none holds a coordinate, size or time; a structural test walks the
JSON schema and fails on any free string, integer or number (it follows refs, combinators,
properties, dict values, `items` and `prefixItems`, and treats an untyped node as free). `issue_menu(title)` lists the ids the
server offers (a pair whose face cannot draw the title is not offered; `pen` only with a Patrick
Hand accent, because the handwriting effect always draws Patrick Hand strokes).

## Compiler

`compile_intro_scene` returns `IntroCompile(rows, reason, typed_title, sound, position)`. Rows are
the `phone_title_look.title_look_rows` shape, one per voice run, so the phone renderer, the editor
and `place_title_look` need no change. Every failure is `rows=None` plus a reason: use today's title.
Before returning rows, `preflight_rows` places them on a typical speaker (`TYPICAL_FACE`, the
same `place_title_look`), draws them and measures the ink; a scene that cannot sit clear of the
head, the captions and the title safe zone is declined (`placement_unclean`, `ink_off_zone`): a long
accent run at 250px in `grotesk_loop`, a long caps row in `grotesk_hand`/`grotesk_pen`, a top-left
row over the head. The typical face is a stand-in, so the wiring step MUST also gate on
`placement_is_clean(receipt)` for its real `place_title_look` receipt (and decline to today's title
when it is not clean). Two caveats for the wiring step: `stacked_title_matches` needs ` / ` between lines, so approve
against `typed_title`, not the creator's slashless title; and a scene with one run has no stacked
form (it falls to the plain title path).

## Teardowns and the render check

`src/apps/api/prompts/intro_teardowns.json` holds the five references: each with the footage it
suits, why it works, an EN and a TR scene, and the moves today's engine lacks (`missing_moves`, and
`engine_limits` for gaps shared by all). `make verify-intro-scenes` (prod image) or
`cd src/apps/api && python -m app.cli.verify_intro_scenes` (host) renders them through Skia on a
synthetic plate and writes `.intro-verify/{report.json,montage.png}`; four negative controls must
fail, and it sweeps every issued font pair and zone over stress titles (EN, TR with dotted capitals,
a long first line): each scene the compiler lets through must PASS, the rest must be declined with
a reason. Tests: `tests/pipeline/test_intro_scene_schema.py` (skia-free) and `test_intro_scene.py`.
