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

## Review round 2 (KRI-1611)

* **Caps pairs and approval**: `grotesk_hand` and `grotesk_pen` set `text_case: upper`, so the
  compiled layer reads "BU YILIN". `_intro_stack_matches` compares case-blind (dotted and dotless i
  fold to i); spelling, order and spacing still must match.
* **Unmeasured ink**: with no sampled frames or no reading, `intro_ink_pass` takes the first light
  ink of the pinned order (as today's white title), never a dark first pick.
* **Title edits**: a manual Review Plan title edit addresses every line (`_title_ids`), and
  `op_rewrite_text` turns a rewrite of all lines of a stack into one plain row. This also fixes the
  same shape for her typed " / " stacks.
* **Evidence**: `make verify-intro-scenes` (prod image) now reads the compiled text layers and
  checks every issued pair in EN and TR.

## Taste intake and the variety report (KRI-1614)

Two dev-time tools keep the taste growing and keep the picker honest. Neither runs at request time.

**Reference intake**: `python -m app.cli.intro_intake` (`draft`, `review`, `approve`), modelled on
the `overlay_examples` library. A person gives a screenshot or short video and the two example
titles; a model DRAFTS a teardown under the closed schema (option ids and one voice per word, plus
short prose notes); the engine reviews it; a person approves it into
`prompts/intro_teardowns.json`. Details live in the module docstring. Rules worth knowing:

* `draft` without `--live` or `--replay` is a dry run. `--live` is one paid vision call (a few
  frames, Gemini Flash); run it only under an approved development spend. It does not go through the
  cost ledger. Tests use `tests/fixtures/intro_intake/sixth_reference_reply.json`, a hand-written
  reply, not a recorded one.
* `review` recomputes the checks: schema (`app/pipeline/intro_teardowns.py`), then per language the
  compile, the Skia render measured like `make verify-intro-scenes`, and the approval check. It
  draws `preview.png` (reference frame, EN, TR). `approve` re-runs it, asks y/N and records
  `approved_by` / `approved_on`. The file keeps its exact format, so an approval is one new entry and
  a new `version` (`<date>-intake-<id>`).
* A move the contract lacks is never added to a prompt. `review` lists the moves no teardown or
  engine limit names yet; `approve` writes them to `engine-tickets.json` in `save_issue` shape
  (project, owner, labels). `--file-tickets` files them through Linear's API with `LINEAR_API_KEY`
  (skips a title already in the project); that path has not run against a real workspace.
* The intake files land in `.intro-intake/` (gitignored).

**Variety report**: `cd src/apps/api && python -m app.cli.intro_variety_report` runs the real picker
over many seeds, five hand-written titles per language (EN, TR) and the light and dark plates, and
fails when one option dominates. Two layers: `pool` (the footage-free pick, 1000 seeds a language, no
Skia: dominance and starvation) and `realized` (fitted with the real placement and inked on the
plate: dominance, top-3 looks, decline rate). The rules, with their reasons, are in the module
docstring (V1-V10). Four things to know: ink is judged per plate (on the light wall only oxblood
reads, so it is listed as an exemption with the contrast it was computed from, not skipped); a
dominance verdict uses the 95 percent lower bound of the share, so a small sample's luck does not
fail it; seven negative controls (a picker that ignores its seed, never picks one entrance, mostly
declines, collapses in Turkish only, or has the placement put every scene above the head) must FAIL, and a control that passes exits 1; nothing asserts
an exact count. Outputs `.intro-variety/{report.json,montage.png}`; the montage is the first seeds
of one title per language, drawn through the real renderer and measured.

**In CI** it is `tests/pipeline/test_intro_variety.py` and `test_intro_intake.py`, inside the
existing `test-api` shards on the free runners (they install libegl1/libgl1 for skia, and the tests
import skia, so a missing library fails instead of skipping). Journey `kri-1614-intro-variety-intake`
maps the paths. The variety run is about 25 s on a laptop and projected at 70-110 s on a runner (3-4x slower),
so its end-to-end test carries `@pytest.mark.timeout(300)` like the sweep test, and
`scripts/ci/api-test-timings.json` holds a hand estimate for both files until CI measures them.

Not covered: the report judges the picker's realized scene, not whether the model picks well (step 3
plugs its pick in as a `Picker`, a callable `(language, title, seed, plate) -> Look | None`).

## The model arm (KRI-1612): `agents/intro_scene_designer.py`

A small Gemini flash agent (`nova.compose.intro_scene`, one call per generation, no retry) picks the
scene instead of the seed, or declines. Behind a second kill switch, `INTRO_SCENE_MODEL_ENABLED`
(default off), on top of `INTRO_SCENE_ENABLED` + `_USER_IDS`. The question it answers: does the model
beat the seeded pick? If Emir cannot tell them apart in the blind compare, drop the model and keep
the seeded pick.

* **Data shape.** The answer IS an `IntroScene` (ids and voices only; no field reaches the screen as
  text or number). `build_input` issues, for this title: the accent splits (`accent_splits`), the font
  pairs that can draw her words, inks, zones and entrances (not `pen`, see below). The response schema
  sent with the call is closed over those ids; `parse` re-checks every id with `check_scene`, the voices
  against the issued splits, and refuses any sound. Her title words and Gemini's footage notes are
  UNTRUSTED data in one `untrusted` object. Hard rules (one ink, no Fraunces, stroke, fit, clearance
  from the face and captions) stay in the engine, never in the prompt.
* **Taste** is the `intro-design` skill (`agents/skills/intro-design/SKILL.md`, on the closed
  allowlist in `agents/_skills.py`) plus the teardowns as examples (`prompts/intro_teardowns.json`)
  and `prompts/intro_scene.txt`. `prompt_version` is a fingerprint of those three files and the issued
  catalogue, so editing any of them moves it. Fixtures are stamped with `scripts/stamp_eval_fixtures.py
  --agent intro_scene` after an edit.
* **Dispatch** (`stamp_intro_scene`) pins only the decision: with the model switch on, the ask gets a
  `model_ask` marker (nothing else changes; off, the stamp is KRI-1611's exactly). Dispatch holds the item
  lock, so no model call runs there.
* **Worker.** `_intro_scene_model_plan` (in `_run_phone_subtitled_job`, before the title is placed, so
  the call happens once even though the title can be placed twice) asks the agent and carries the answer
  in the ask as `model_pick` (`outcome`: `compose` + ids, `decline`, `failed`, `nothing_to_ask`). The
  answer is written into the job's stamped ask at the end of the run (`pin_model_pick`, never
  overwritten), so a re-render reads the pin and never asks again. `ask_for_fit` turns a `compose` pin
  into one candidate led by the model's ink and zone (the other zones follow); the real
  `place_title_look` still decides if it sits clear, and `intro_ink_pass` still replaces an ink that does
  not read on the take.
* **Fallbacks.** A decline, a failed or void answer, a scene that cannot sit clear, or a missing pin:
  today's title rows (the project's rule: "where the AI declines or fails, the creator gets today's
  intro"). The seeded candidates are NOT used as a fallback, so the two arms stay separable. The call is
  not made when the scene would not apply (a logo above the title, a pinned text rule) or the title has
  nothing to issue. Trace: `intro_scene` events carry `arm`, `model_outcome`, `chosen.source`;
  `intro_scene_model` carries the pin.
* **`pen` is not issued to the model.** The handwriting effect draws strokes with no text layer, so the
  approval check cannot find her words in the drawn rows. The seeded arm can still pick `pen` for the
  `grotesk_pen` pair (KRI-1611); that is KRI-1624. `make verify-intro-scenes` has a
  tripwire (`approval_cannot_read_the_pen_entrance_so_it_is_not_issued`) that flips when approval can read it.

Checks: `tests/evals/test_intro_scene_agent_evals.py` (free structural replay, Skia-free, hand-authored
cassettes: English, Turkish, a decline, void answers), `tests/pipeline/test_intro_scene_model.py`,
`tests/tasks/test_intro_scene_model_worker.py` (the real worker and approval check, provider replayed),
and the model section of `make verify-intro-scenes` (`app/cli/verify_intro_scene_model.py`).

### Blind compare (Emir's)

    python -m app.cli.compare_intro_scenes build --fixtures fixtures.json --model live --out .intro-compare
    python -m app.cli.compare_intro_scenes score --key .intro-compare/key.json --picks picks.json

Renders the seeded pick and the model pick for each Talking fixture through the real worker call, as
unlabeled frames in shuffled order and random sides, with a sealed `key.json` (its sha256 is on the page
and checked by `score`). `--demo` is a check of the tool on synthetic plates, never the compare.
Real stills stay out of git. Done when five real Talking edits have been compared and Emir has decided.
