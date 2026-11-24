# Intro scene (KRI-1609 / KRI-1610 / KRI-1611)

`IntroScene` is the closed shape that composes a creator-style intro. KRI-1610 shipped the schema,
the compiler to today's title rows, the five reference teardowns and a render check. KRI-1611 wires
it to Talking edits with no model: the server issues the options and a job-seeded pick chooses.
Flagged off by default: `INTRO_SCENE_ENABLED` plus `INTRO_SCENE_USER_IDS` (JSON list of account
ids, `["*"]` = everyone). Flag off, or any failure, draws today's title rows byte-identically.

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

## The pick (KRI-1611): `app/pipeline/intro_scene_pick.py`

* **Dispatch** (`stamp_intro_scene`, after `stamp_phone_text_style` and `stamp_title_look` in
  `_dispatch_item_render`): admits the job and pins `assembly_plan["intro_scene_ask"]`: a seed, up to
  six footage-free candidates (accent split of her words, a font pair that can draw them, an
  entrance, a zone order) and an ink order. Her last four pinned looks are tried last. Sound is
  always `none` (an automatic sound is an open question on the project). No Skia and no trace event
  (dispatch holds the item lock); a failed history lookup only means no exclusion.
* **Worker** (`_intro_scene_title_rows` in `_phone_talking_title_rows`, so it also serves the
  `_title_clear_of_captions` re-placement): compiles each candidate x zone and places it with the
  REAL `place_title_look`; the first whose real receipt passes `placement_is_clean` wins (at most
  `MAX_ATTEMPTS`). Nothing fits: today's rows. The worker reads the stamp, never the flag.
* **Ink**: `intro_ink_pass` (in `_style_rows_keeping_title_look`, before the title legibility pass)
  takes the first ink of the pinned order that reads (4.5:1) on the sampled take frames; a colour
  she set is never overwritten. Per-footage contrast beyond that stays with KRI-1619.
* **Tiers**: the scene yields to her typed `" / "` stack, a pinned title-look ask, any pinned or
  requested `all_text`/`title` rule (this includes the strategy writer's own font/colour knobs, so
  those jobs keep today's title), a logo above the title, and a job without the informative-text
  stamp for a protocol-55 build. Rows are ordinary rows: her edits and chat ops mark creator fields
  and win; an A1-B1 design she picks styles hook/research rows, which scene rows are not. A plan
  `title_animation` or an asked entrance still replaces the scene's entrance.
* **Approval**: a slashless title drawn as several rows would fail the opening-text check, so
  `stacked_title_matches` also accepts 2-5 rows shown together, top to bottom, that read as exactly
  her words (`_intro_stack_matches`); `brief_checks` and the phone-blocks title read her slashless
  form via `intro_plain_text`.

Check: `tests/pipeline/test_intro_scene_pick.py` and the picks / wiring section of
`make verify-intro-scenes` (EN and TR, light and dark plates, an unreadable-ink control).

Not covered: a lower-third zone, ref 2's folder sticker and refs 3, 4, 5's extras (KRI-1613, 1615,
1616, 1618), phone-build font support and per-footage ink (KRI-1619). The realized pick is not
written back, so "recent looks" are the first stamped candidate of each earlier job.
