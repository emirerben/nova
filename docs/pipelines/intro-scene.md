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
  entrance, a zone order) and an ink order. Her last four pinned looks are tried last. The seeded
  pick never chooses a sound (only the model arm may, KRI-1613). No Skia and no trace event
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

Not covered: a lower-third zone, ref 2's folder sticker and refs 3, 4, 5's extras (KRI-1615,
1616), phone-build font support and per-footage ink (KRI-1619). The late accent and ref 5's tuck
are KRI-1618 (below). The realized pick is not
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
  sent with the call is closed over those ids; `parse` re-checks every id with `check_scene` (the sound
  and its cue included, KRI-1613) and the voices against the issued splits. Her title words and Gemini's footage notes are
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

## Intro styles on tap (KRI-1633)

The automatic single pick (KRI-1611) is retired. Emir's ruling (2026-11-25): Kria OFFERS several
fitted intro looks on tap ("Try intro styles") plus "No intro style", for any video with an
approved title (not only Talking), English and Turkish; the creator picks one; it applies as
ordinary editable title rows and her edits override it. No model: the model arm
(`INTRO_SCENE_MODEL_ENABLED`) stays off and the offer never imports its agent. iPhone only.

**Contract** (`app/pipeline/intro_styles.py`, `app/routes/intro_styles.py`; the failure-mode list
F1-F26 with each guard is in the module docstring):

* Capability: `editor_capabilities.intro_styles: true`, present only when `INTRO_SCENE_ENABLED` +
  `INTRO_SCENE_USER_IDS` admit the account, the request's `X-Kria-Client-Protocol` is at or above
  `INTRO_STYLES_MIN_CLIENT_PROTOCOL` (`INTRO_STYLES_PROTOCOL`, assigned by
  `scripts/bump-protocol.py`; `=999` is the kill switch), the variant is a phone portrait variant
  on a route below, its title lane is saveable and she asked for no look for this title.
* `POST /plan-items/{item}/variants/{variant}/intro-styles` `{version, base_generation,
  title_rows}` answers up to 4 looks (distinct font pair, never the `pen` entrance) plus one
  `none`, each with complete `TextElement` rows and a server-compiled `preview` (the A1-B1
  PortableTextLayer shape; all or nothing, 192 KiB cap). It computes and STORES NOTHING. Gates in
  order: 404 `intro_styles_disabled`, 426 `client_update_required`, the usual 404, 409
  `baseline_conflict` (editor-commit's body), 422 `intro_styles_title_rows_invalid`; 10/min per IP,
  60/hour per caller. Unavailable reasons: `not_phone_render`, `route_unsupported`,
  `canvas_unsupported`, `no_title`, `title_look_asked`, `title_too_short`, `nothing_fits`,
  `save_refused`.
* Apply: the editor stages `option.rows` and her normal editor-commit Save persists them. Each row
  carries `source_params.title_look.intro_style` so the next offer can mark the current look; line
  1 also carries `restore`, the title rows the look replaced. "No intro style" over a stack gives
  `restore` back exactly (the worker's font, size and place are kept nowhere else) while the words
  are still hers, else her words as one plain row under the pinned text policy (an approximation of
  the route's builder, notably on montage).

| Route | Title family | Face evidence | In |
| --- | --- | --- | --- |
| Talking (`subtitled`) | `opening-title` | saved `opening_title_placement.face_band` (canvas space) | yes |
| Narrated (phone voiceover) | `narrated-title` | none (top-band zones only) | yes |
| Guided story / unified montage | `guided-title` | none (top-band zones only) | yes |
| Voiceover montage, speech montage, authored timelines, slides, cloud, landscape | | | no (a reason code) |

Each look is compiled with `compile_intro_scene`, placed with the real `place_title_look` against
the variant's own evidence (a stub sampler replays the saved face; the other text layers of the
pinned recipe are avoided) and kept only on a clean receipt, then dry-run through the route's real
phone Save on a detached copy (a refused look is dropped). Ink is the first light ink of the
seeded order; nothing is measured on her footage at request time (KRI-1619 owns per-footage ink).
A caption layer is never treated as her own title, even when it repeats her words; layers are
matched whole, not by substring. Known v1 limit: placement works on layout boxes and a few script
faces draw ink a hair outside them (under 1% of looks on random jobs); the tile is drawn over her
frame so she can judge.

**`has_text_ask`** is True only when `requested.title` holds `font_family`, `color`,
`stroke_width` or `shadow_enabled` (`TITLE_LOOK_ASK_KEYS`). Saved preferences, `all_text` (the
strategy writer's own knobs), a position and font lists never block; `excluded_fonts` only removes
the pairs set in them. `stamp_intro_scene` now stamps only for the model arm, so no new job gets a
scene she did not pick; a KRI-1611 stamp still renders byte-identically (golden
`tests/fixtures/kri1633_old_stamp_rows.json`).

**Her edits win.** A stack is one text for the render contract (`intro_plain_text`; `any`
requirements fall back to `stacked_title_matches` like `opening`). Chat ops and the Review Plan
title field address every line of a title family: a style patch, an id selector, an index op on
the title bar and the appearance group `titles` reach every line; a whole-title rewrite or
`set_title` collapses the stack and a remove takes every line (edits aimed at one `-line-N` stay
line edits).

**Deliberately not gates:** `TITLE_LOOK_ENABLED` (it gates typed stacks and the worker's look pass;
the Save path does not read it), the job's `informative_text_shapes` stamp (the live protocol gate
replaces it), suggestion chips and cards.

**Checks:** `make verify-intro-styles` (prod image; `.intro-styles-verify/{report.json,montage.png}`,
eight negative controls (N7: captions that repeat her words; N8: "none" rebuilt without the saved title)), `tests/pipeline/test_intro_styles.py`, `tests/routes/test_intro_styles_e2e.py`
(real PostgreSQL, three routes, EN and TR), `tests/services/test_intro_styles_{contract,edit_ops}.py`,
and the generated iOS fixture `src/apps/ios/Tests/Fixtures/KRI1633IntroStyles.json`
(`KRI1633_REGENERATE=1 pytest tests/pipeline/test_intro_styles_ios_fixture.py`).

**Rollback:** `INTRO_SCENE_ENABLED=false` or `INTRO_STYLES_MIN_CLIENT_PROTOCOL=999` removes the
key and the route answers 404/426; looks she already saved stay as her rows.

## The late accent and the tucked script (KRI-1618)

Two more option ids on `IntroScene`, each mapped through an engine table, so no time or distance is
ever written by the pick or the model:

* `stagger`: `together` | `accent_after` (`STAGGERS`, 0.6 s, ref 1 on the taste board: the caps at
  0.15 s, the handwritten accent at 0.75 s). The accent lines wait, the solid lines enter at the
  title's start, every line leaves together.
* `stack`: `gap` | `script_over_serif` (`STACKS`, `script_serif` only). A script accent line
  directly above a serif solid line tucks onto it: its baseline sits `tuck_caps` (1.28) serif cap
  heights above the serif baseline, the taste board's B2, so only the script's descenders cross.
  Every other pair keeps its gap. The serif draws on top (line order, as B2 asks).

Both default to today's look and are omitted from dumps then, so old pins, cassettes, teardowns and
goldens read byte for byte. `check_scene` refuses a tuck without a script line above a serif line,
and a stagger with nothing to stagger.

* **Rows declare, times are derived.** The compiler writes `stagger` / `stack` into each row's
  `title_look` param; `title_family.retime_title_rows(rows, start, end)` derives every line's
  entrance from that declaration (a window too short for the wait shows the lines together). It is
  the only retime: the offer's window, its restored title, a chat `set_text_timing` on a stack, and
  a Range save's projected text lane (`retime_title_stacks`: each text row is re-anchored through
  source time on its own, so a cut inside the first 0.6 s would bend the delay).
* **Windows.** `title_family.title_window(rows)` (first entrance, last exit) in the worker's caption
  avoidance, resolved-caption re-check and caption move; the offer's preview frame is taken after the
  LAST entrance.
* **Placement.** `place_title_look` reads the declaration (`tuck_of`) and sets the tuck from font
  metrics at the size the renderer draws (`_tuck_gaps`), in the normal stack and in the squeeze.
* **Approval.** `stacked_title_matches` accepts lines that enter apart only at an issued delay
  (`title_family.issued_delay`), still requires them to leave together, and reads the title's start
  as its first entrance (so ref 5's leading accent still opens the video). A line 2.0 s late is
  refused, as before.
* **Phone.** No client protocol number: observed on the export's own text pass (the editor preview
  shares it) that overlapping layers are each drawn whole, the later recipe layer on top, and a late
  layer draws nothing before its start. `IntroRowMotionRenderTests` (KriaMediaEngine) reads
  `tests/fixtures/intro-row-motion-parity/`, generated from the real compile by
  `tests/pipeline/test_intro_row_motion_parity.py` (`KRI1618_REGENERATE=1`).
* **Menus.** The seeded pick draws both from their own random stream (a seed's other choices are
  unchanged) and only where `check_scene` allows; the agent is issued them in its menu and closed
  response schema (prompt_version moved). Ref 1 now expresses its late accent, ref 5 its tuck (still
  `partial`: KRI-1615 lays out the 2+2 photo grid, not offered yet).
* **Render check.** `make verify-intro-scenes` adds both moves in EN and TR (the late accent also as
  a still before it enters), tucks in the sweep, and four controls that must fail: a tuck drawn
  without its declaration, a declared tuck drawn as a gap, a late line at a delay nobody issued, a
  solid line waiting instead of the accent. Rows are checked against the placement policy by their
  line boxes, because no pixel measure tells a tuck from a loopy plain stack.

Not covered: a per-line entrance time (B2's serif lands 0.2 s after its script) and any other
overlap (`engine_limits`).

## The scene's own sound (KRI-1613)

Emir's ruling (2026-11-27): an intro scene's sound is automatic when the scene carries one. This
lifts "no sound unless she asked" for intro scenes only; every sound-effect gate still applies.

* **Data shape.** `IntroScene.sound` (a slug of the seeded creator library: `accent-pop`,
  `soft-pop`, `click`, `sparkle`, `whoosh-fast`, `camera-shutter`) plus `sound_cue`, an option id
  mapped through `SOUND_CUES`: `none`, `accent` (once, as the accent lines enter) or `each` (once
  per entrance; lines that enter on the same frame share one sound). `check_scene` refuses a cue
  with no sound and an `accent` cue with no accent line. A slug without a cue is silent, as every
  scene, teardown and intake reply before was; `sound_cue` is omitted from dumps while `none`.
* **Rows declare, times are derived.** The compiler writes `sound` and `sound_cue` into the
  lines' `title_look` param (only when a cue plays). `title_family.intro_sound_cues(rows)` is the
  one reader: an `introsfx-<line id>` sound-effect row at the line's `start_s`, the entrance's
  first frame. With `accent_after` the accent-only cue is 0.6 s in.
* **Worker.** `fit_intro_scene(sound=...)` compiles the sound only when `_intro_scene_sound_open`
  holds: `INTRO_SCENE_SOUND_ENABLED` (default off, the kill switch, read on every render), the
  Talking sound-effect lane the KRI-1561 chooser writes to (`sfx_capability.subtitled_sfx_limit`:
  `SOUND_EFFECTS_ENABLED`, the Talking media and editor lanes, `soundEffects` + `audioMix`
  verified) and no live "no sound effects" in her brief. Closed: the rows declare nothing and no
  row is written. `_with_intro_scene_sounds` adds the rows on the cut timeline after every lane
  was remapped and before the chooser, so a chosen effect keeps its spacing. The slug resolves to
  the library row the seed script owns (`services.sfx_library`: provenance
  `kria-sfx-library-v1:<slug> `, file `<slug>.m4a`, ready, published, not archived, newest).
  A cue within `MIN_SPACING_S` of another lane sound is skipped (hers wins); a missing or
  unplayable row is skipped. The render never fails on a sound; the `intro_scene_sound` trace
  event lists each cue and why it was skipped. Ducking under speech stays as compiled.
* **Editor.** Every Talking Save re-pins each `introsfx-*` row to the entrance its line declares
  in that Save's text (`phone_editor.rebind_intro_scene_sounds`) and drops it when the line is gone
  or no longer declares it (removed, a whole-title rewrite, another look). A Save never adds one,
  so a sound she deleted stays deleted, and saved sounds stay when the switch is turned off later
  (the colour-moment precedent, KRI-1350). Range leaves the rows out of its source-time
  reprojection (`phone_talking_range.reproject_lanes`).
* **Taste.** The model is issued the sounds and cues; the `intro-design` skill pairs them with the
  entrances (pops with pop and ink, a click or shutter for a crisp title, sparkle for a reveal, a
  whoosh for a title that sweeps in). Teardowns: refs 1 and 3 cue the accent, refs 4 and 5 each
  entrance. That timing is a reading of the taste board's stills: it stays a guess until the
  reference videos are measured.
* **Phone.** No protocol number: the rows are ordinary `sfx` clips. `IntroSceneSoundExportTests`
  (KriaMediaEngine) exports the clips the worker compiled
  (`tests/fixtures/intro-scene-sound/`, generated by `test_intro_scene_sound_phone_fixture.py`,
  `KRI1613_REGENERATE=1`) and finds each onset on its line's entrance frame, and silence with the
  switch off. Keep the MP4s with `KRIA_INTRO_SOUND_EVIDENCE_DIR` and measure them with
  `scripts/verify-colour-moments.py measure`.

Not covered: the seeded pick and the "Try intro styles" offer stay silent (the offer's look is
applied by a Save, which never adds a sound); the intake prompt does not offer sounds yet; a sound
on an element other than a line entrance (`engine_limits`).

Checks: `tests/pipeline/test_intro_scene_sound.py`, `tests/tasks/test_intro_scene_sound_worker.py`
(real worker, EN and TR, every gate closed), `tests/tasks/test_intro_scene_sound_save.py` (real
worker then real Saves, Range), `tests/services/test_sfx_library.py` (real Postgres) and the free
replay evals.

## Photo cards (KRI-1615, stage A)

Her own photos as cards in an intro scene, with the title laid out between them (the moodboard's
Pinterest collage and 2 + 2 grid; refs 3 and 5). Stage A is the backend layout only: the seeded
pick and the model arm pass no photos, so they issue no arrangement. Stage B (next section) offers
cards in "Try intro styles" as a one-tap suggestion (never automatic); stage C draws them on the
phone.

* **Data shape.** `IntroScene.arrangement`: `none` | `around_speaker` | `grid_2x2`, mapped through
  `ARRANGEMENTS` (card rows: `top`, above the title, or `bottom`, under the speaker; how many cards
  side by side, their alignment and width share; a grid's rows share one height). The scene holds
  no media id: slots `prop_1`..`prop_N` take the owned photos the caller issued, in that order,
  row by row, left to right. `issue_menu(title, photo_count=n)` issues an arrangement only with a
  photo for every slot (2 and 4); `check_scene` refuses the rest and a `mid_frame` title
  (`arrangement_needs_a_top_zone`). Omitted from dumps while `none`; title rows never declare it.
* **Layout** (`app/services/intro_props_layout.py`, no I/O): `layout_intro_props(arrangement,
  aspects, rows, stage=Stage(faces, caption_band), canvas=, position=)`. The title alone first
  (its size and height), then the card rows in the room left (the top row above the title, which
  sits right above the face; the bottom row under the face, above or below the caption row), then
  the title again with the card rows, full width and padded, in `avoid` (`place_title_look`
  tolerates 5% coverage of an `avoid` box). It declines unless that receipt is clean, the title
  keeps the size it had alone, no line box touches a card and the block sits between the rows.
  Every box stays `EDGE_INSET` inside the frame (no bleed: KRI-1616), off every face, the caption
  row (from `CAPTION_BAND_TOP_FRAC` down when the band was not measured) and the Kria watermark,
  off the other cards (strict `boxes_intersect`), in its photo's own shape, no side under
  `MIN_WIDTH`. A centred row that meets the watermark narrows on both sides, so a grid's columns
  stay over each other. `phone_card_layout` is unchanged.
* **Compile.** `compile_intro_scene(..., photo_aspects=, stage=)`: an arrangement scene runs the
  layout instead of the typical-face preflight, then draws the placed rows and measures the ink
  (off the faces, captions and safe zone, touching no card). Its rows come back already placed,
  with `props` (`PropsLayout`: `cards`, each `slot`, `box`, `z` and `x_frac`/`y_frac`/`scale`; the
  title `receipt`). Any failure is `props_do_not_fit` with `rows=None`: today's title, no card.
  The default stage is the typical close speaker, where the title alone fills the room above the
  head, so it always declines there; a caller with real evidence passes it as `stage`.
* **Time.** Clean cards share the title's window; pop cards show briefly (see "Short cards over
  her" below). `shift_past_text_windows` keeps cards a chooser placed off a title; it does not
  apply to cards laid out with the title.
* **For stages B and C.** Boxes are fractions of the 1080x1920 canvas. A phone card has a width
  only (`SubtitledOverlayCard.scale`; its height is the photo's aspect), so the aspects passed must
  be the real photos'. A bottom-row card can sit below `CAPTION_BAND_TOP_FRAC` when the band was
  measured: it then needs `caption_clear`, or the compile clamps its centre up to 0.62, off its
  planned box (on the verified framing, from 0.735 onto her face). Every bottom row the render
  check passes sits there.
  Anything that places the title again (the worker's caption re-placement, a chat move) must keep
  the card rows in `avoid`. Removing one card leaves the others where they are; a layout for fewer
  photos is not defined here.

Checks: `tests/pipeline/test_intro_scene_props.py` (M19-M30 in the `intro_scene` docstring) and
the props cases of `make verify-intro-scenes`: ref 3 with `around_speaker` and ref 5 with
`grid_2x2`, EN and TR, on a speaker framed with room above and below (`ROOMY_FACE`), cards drawn
as neutral plates under the real Skia title; four controls that must fail (a card on the title,
on the face, past the frame edge, on the caption row); wiring checks (a close speaker declines, no
grid from three photos, no arrangement without photos); and `sweep_props`.

Not covered: five, overlapping or tilted cards (ref 3), bleed (KRI-1616), drawing on the phone
(stage C). Refs 3 and 5 stay `partial` until her photos can be offered.

## Photo cards in the offer (KRI-1615, stage B)

For the creator: on a Talking edit, "Try intro styles" may add one or two looks that lay two or
four of her own Visuals photos out with the title. She taps one, Save keeps it, and she can drag
or delete a card like any other. Nothing is automatic. For the next engineer: the offer still
stores nothing; the Save pins the photos; the failure modes M31-M45 and their guards are in the
`app/pipeline/intro_styles.py` docstring.

**Gates.** `INTRO_CARDS_ENABLED` (default off, the kill switch) and a build at
`INTRO_CARDS_PROTOCOL` = 62 (`intro_cards_min_client_protocol`, set by
`scripts/bump-protocol.py intro_cards`). The route resolves both (`intro_styles.cards_allowed`) and
only then reads her photos; otherwise the offer is today's byte for byte, and a new build's offer
minus its card looks equals it (tested). Keep the switch off until the stage C build ships: a
protocol-62 build without the card parser would apply the title placed between cards and no
cards.

**Which photos.** `intro_card_pool.card_photo`, one rule for the offer's query and the Save's pin:
her own `PlanItemAsset` rows of this plan item, `kind` image, `status` and `media_status` ready,
in `users/{user}/plan/{item}/pool/`, with a generation, not deduplicated, not library-backed,
under the bind's 25 MB cap when its size is known, a browser-safe preview when HEIC, an upright shape
(`analysis.dims_upright` and `aspect`), and `analysis.has_alpha` false (a transparent PNG is a
sticker, KRI-1616). Oldest first (`created_at`, `id`); the slots take them in that order, as many
as the arrangement has (two for `around_speaker`, four for `grid_2x2`). Under two: no card look.

**Where.** Talking only, and only with her face saved (`opening_title_placement.face_band`; a
typical face is never assumed for cards). Her captions come from `intro_card_pool.caption_band`:
the union of the title receipt's measured `caption_boxes` and the pinned recipe's `caption*`
layers shown with the title (estimated from font size, padded 0.01). The same function feeds the
layout stage, the Save's mark and the measurement below. Another card (the worker's or hers) or a
text line that is not hers or a caption on screen with the title: no card look (`in_the_way`);
her content stays. Each arrangement her photos fill gets at most one look, after the plain looks:
the seeded candidates x the two top-band zones, `MAX_CARD_COMPILES` (8) per arrangement, each
compiled with the arrangement on her real stage; the first one the dry-run Save accepts with
every card compiled exactly where it was placed is the look (`card_moved` otherwise).

**The wire** (an option of `POST .../intro-styles`; stage C parses `cards`, `IntroCard` in
`intro_styles.py`). Options are the plain looks, then the card looks, then `none` last. A card
look's `key` is `<font_pair>+<arrangement>`, which no plain look uses, and it is `current` when
her title rows carry it. A real offer on the verify fixture:

```json
{
  "key": "grotesk_pen+around_speaker",
  "recommended": false,
  "current": false,
  "rows": ["... the title rows, placed between the cards, as for every look ..."],
  "cards": [
    {"id": "intro-card-1", "slot": "prop_1",
     "media_id": "00000000-0000-0000-0000-000000000001",
     "gcs_path": "users/<user>/plan/<item>/pool/0.jpg", "generation": "7",
     "x_frac": 0.1254, "y_frac": 0.099, "scale": 0.2107, "caption_clear": true, "z": 0,
     "start_s": 0.0, "end_s": 2.5},
    {"id": "intro-card-2", "slot": "prop_2",
     "media_id": "00000000-0000-0000-0000-000000000002",
     "gcs_path": "users/<user>/plan/<item>/pool/1.jpg", "generation": "7",
     "x_frac": 0.8278, "y_frac": 0.9158, "scale": 0.3045, "caption_clear": true, "z": 1,
     "start_s": 0.0, "end_s": 2.5}
  ],
  "preview": {"media_id": "...", "media_kind": "...", "at_s": "...", "time_s": "...",
    "text_layers": ["..."], "fonts": "...",
    "cards": [
      {"media_id": "00000000-0000-0000-0000-000000000001", "media_kind": "image",
       "box": {"left": 0.02, "top": 0.02, "right": 0.2307, "bottom": 0.1781}, "z": 0},
      {"media_id": "00000000-0000-0000-0000-000000000002", "media_kind": "image",
       "box": {"left": 0.6755, "top": 0.8515, "right": 0.98, "bottom": 0.98}, "z": 1}
    ]}
}
```

Fractions are of the 1080x1920 canvas: `x_frac`/`y_frac` the card's centre, `scale` its width; the
phone draws the photo at its own shape. Each card shows for its own `start_s`-`end_s`: a clean
card exactly the title's window, a pop card a shorter window inside it (see "Short cards over
her"); the phone must honour each card's own times.
`caption_clear` is informational: the Save decides it again from the same evidence (a
`MediaOverlay` has no such field).

**Applying it (the stage C contract).** Applying ANY option, plain looks and `none` included:
replace the `replace_ids` title rows with `rows` (as today), remove every media overlay whose id
starts with `intro-card-`, and add one per card: `{id, kind: "image", src_gcs_path: gcs_path,
display_mode: "pip", position: "custom", x_frac, y_frac, scale, start_s, end_s, z}`. A plain look
or `none` therefore takes the cards away (its title was placed without them). Ids are the slots',
so applying the same look twice converges. Her normal editor-commit Save posts the full
`media_overlays` section (her other cards first). The preview's `cards` are the boxes to draw her
photos in (or a neutral plate until the photo loads).

**The iPhone (stage C, `Core/IntroStyles.swift`, `NativeEditorSession+IntroStyles.swift`).**
`IntroStylesOffer.parse` reads `cards` into `IntroStyleCard`. A card it cannot place exactly (a
slot that is not the id's, no `media_id` or `generation`, `scale` outside (0, 1], more than four,
a repeated id, an id without the prefix, an empty window) drops that one look; the plain looks and
`none` stay, so a card look's rows never go out without its cards. Applying is one undo step over
the text and overlay lanes, refused whole where the `overlays` lane is locked (she could not move
or delete the cards), and the strip hides card looks there. Her own delete removes one card; Save
sends the rest. The row keeps the offer's `caption_clear` mark so the live preview draws a card
under her captions where it exports (the device clamp otherwise lifts it to 0.62); the commit body
drops it from every `intro-card-*` row. The preview reads a new card's photo from her Visuals
library by `src_gcs_path` (`GET plan-items/{id}/assets`: its `id` is the card's `media_id`, with a
short-lived signed `source_url` and the `generation`), or from the source pool once the card is
saved; a photo it cannot read leaves that card undrawn, never the preview failed. Tiles draw the
`preview.cards` boxes under the title from cached thumbnails or as neutral plates, never fetching.
No wire field was added. Guards: `IntroStylesOfferTests` (C1-C8),
`NativeEditorSessionTests.testAnIntroCard*`, `IntroStylesUITests` (the commit body on the fixture).

**Save.** `pin_background_for_commit` (the HTTP Save, the copilot edit and the approval task) now
also calls `intro_card_pool.pin_intro_cards_for_commit`: with the switch on, for every posted
`intro-card-*` card on one of her card photos that the job has not pinned, it binds the photo
through `bind_phone_visual_assets` (owner, item, path and generation rechecked, bytes hashed) and
appends the receipt to `_phone_visuals_v1`, the KRI-1117 / KRI-1253 precedent. A card on any other
photo is not pinned, so `_overlays_from_sections` refuses it with today's "isn't a photo Kria
pinned" error; unreadable bytes are a 422 `intro_card_unavailable`. The pin also decides each intro
card's `caption_clear` (the card's box, from its place and the photo's `aspect`, clear of
`caption_band`) and hands it to the lane rebuild for this request only
(`intro_card_pool.CAPTION_CLEAR_ATTR`, the `NEW_BACKGROUND_COLUMNS_ATTR` pattern); every other card
keeps today's rule. Without the mark the phone compile lifts a card centred under 0.62 up to 0.62,
onto her face. Deleting a card is an ordinary Save without it: it leaves the lanes and the recipe,
the other card keeps its place, and the photo's receipt stays (append-only, like every pool pin).
The dry run at offer time pins provisional receipts and reads no bytes, so it proves less than the
Save (M42). Cost: a Save that adds a grid downloads and hashes up to four photos one after another
in the request (each at most 25 MB), plus a text-column measure while the background-focus or
card-strip switch is on; later Saves reuse the receipts.

**Cloud.** No new code: the Save persists the cards on `variant.media_overlays`, and
`pipeline/media_overlay.py` places a card at its `x_frac`/`y_frac` centre, `scale` wide, with no
caption clamp (read, not rendered here).

**Real footage (measured 2026-11-27).** `python -m app.cli.intro_cards_fit --debug-dumps <dir>`
runs the offer's own stage and fit (no Save, no "in the way" check) over saved
`/admin/jobs/{id}/debug` answers (read only; they hold titles, so they stay out of git). Prod, 236
recent jobs: 75 Talking variants, 23 with an intro title, 13 portrait phone edits with a saved face
band (8 distinct framings; one take accounts for 5). The debug view carries no pinned recipe, so
the caption band is the receipt's `caption_boxes` (4 edits, all that one take) or, second column,
an estimate from the caption style's anchor and size (two lines; 11 edits). On that take the
estimate (0.748-0.832) brackets the measured boxes (0.768-0.812) by 0.02 a side. Four synthetic
photos per shape.

| Captions from | Card look (either arrangement) | Declines (stage A layout) |
| --- | --- | --- |
| receipt boxes only | 0 of 13 | `no_room_for_cards` 12, `title_unclean` 1 |
| receipt, else style estimate | 1 of 13 (1 framing) | `no_room_for_cards` 11, `title_unclean` 1 |

The same in every photo shape (3:4, square, 4:3): the rows either have room or clearly do not.
Both arrangements need a row above the title AND a row under her, and the smallest card is 0.084
of the frame height (0.112 for a 3:4 photo). By framing (room from `room` in the report):

* No room above the title: 4 framings, 9 edits (face tops at 0.07-0.26 leave -0.13 to 0.04 once
  the title, 0.12-0.22 tall, sits over the head).
* No room under her: 4 framings (chins at 0.77-0.81, or captions unmeasured so the band stops at
  0.62), 2 of them with her captions at the TOP of the frame (anchored at 0.13 and 0.20), where
  the layout's bottom bands assume captions under the face.
* The one fit: face 0.38-0.52 with captions estimated at 0.67-0.71.

So on today's Talking framings the clean layout will almost never fit (Emir's answer, 2026-11-27:
short cards may cover her, next section). Proposals considered for the taste call:

1. One-row arrangements: cards only above the title, or only under her. By the room numbers
   (estimated, not compiled), a row under her alone fits 3 framings (8 edits) and a row above the
   title alone 2 framings (2 edits, one shared): 9 of 13 edits together.
2. Captions above her face: let the bottom row run from under the chin to the frame bottom (0.17
   and 0.19 of room on the 2 framings with top captions; 2 more edits with proposal 1).
3. Let the title shrink with cards (`cards_shrink_the_title` today): helps only the edge cases
   (0.04 of room above the title needs a title about 0.05 shorter).

**Checks.** `tests/routes/test_intro_cards_e2e.py` (real PostgreSQL, the real routes, offer, Save
pin, phone recompile and the phone's asset grant, EN and TR, a card of hers later in the video, a
row in her namespace that is not hers, the switch turned off after a Save; only auth and the
storage download replaced), and the card
section of `make verify-intro-styles` (EN and TR on a roomy framing, cards as neutral plates under
the real Skia title, measured against her face, her recipe's caption layers, the wordmark, the
frame and the title ink; controls N9 a look without the Save's mark is not offered, N10 a card on
her face is caught).

**Rollback.** `INTRO_CARDS_ENABLED=false` (Fly secret, api restart): offers lose their card looks
and a Save refuses a new card photo as before; saved cards stay (their photos are pinned).

Not covered: a chat move or retime of a card look's title (its cards stay where and when they
were, M46), a request-time measure of the real photo bytes (the Save hashes them), a look for a
plain title over intro cards on a build below 62 (it never sees card looks, but a card look saved
from another device keeps its cards under a plain look it applies), stickers (KRI-1616), and the
phone (stage C).

## Short cards over her (KRI-1615, pop)

Emir's ruling (2026-11-27), after card looks on real close Talking stills: "it is okay for cards
to cover the face sometimes if it is shown for a short time". For the creator: on a close selfie,
where no card fits off her face, a card look now flashes her photos around the title for a moment
instead of offering nothing. For the next engineer: a second card mode in the layout, asked only
after the clean one has no room; the wire and the Save path are stage B's. Failure modes
M47-M59 and their guards are in the `services/intro_props_layout.py` docstring.

* **Data shape.** `intro_props_layout.CARD_MODES`, one `CardMode` per mode: `over_her` (may a card
  cover her face and body), `max_width`, and the card's time (`lead_s`, `stagger_s`, `hold_s`).
  `clean` is stage A exactly (no face, the title's whole window). `pop`: up to 0.45 of the frame
  wide; the first card enters 0.25 s after the title, each next one 0.25 s later in slot order,
  each shows at most `SHORT_CARD_S` (1.5 s, Emir's "short time") and never after the title
  leaves; a card left under `MIN_CARD_S` (0.5 s, the shortest title) declines the look
  (`title_too_brief`). `PropCard` carries `start_s`/`end_s`, `PropsLayout.mode` the mode used.
* **Layout** (`layout_intro_props(..., mode="pop")`). The title is placed alone, exactly as without
  cards (its rows and receipt are the alone placement, and the compile first runs the plain
  look's preflight, so a pop title is one the plain look would show). Every card keeps every clean
  rule except the face: off the title's block padded by `GAP`, the caption row, the watermark,
  the frame edge (no bleed) and the other cards (`GAP` apart). One width for all cards of a look:
  the widest step of `POP_WIDTHS` (0.45 down to the card minimum) at which every card finds a spot.
  Each card, in slot order, takes the free spot covering the least of her eyes and mouth
  (`face_centre`: the middle 60% across, 25-85% down, of the saved face box, checked by eye on the
  real stills), then the least face, then the one nearest its slot's home (the arrangement's rows
  against the frame's top and bottom edges). Candidates are the edges of the frame, the home and
  every box, where coverage and clearance change; a brute force on a 0.005 grid agrees (tested).
* **Which scenes.** The compile pops only when asked (`compile_intro_scene(card_mode="pop")`) and
  never on the stand-in speaker. The offer (`fit_card_looks`) runs every clean try first, exactly
  as stage B (a golden holds the clean card looks byte for byte); a pop pass, capped at
  `MAX_POP_COMPILES` (4), runs for an arrangement only when none of its clean tries laid cards out
  and one declined for `no_room_for_cards`. A clean layout the Save refused never falls to pop.
* **Wire.** Unchanged: the same `IntroCard` keys, now with each card's own `start_s`/`end_s`
  (absolute, from `CardMode.windows` over the title's window; clean cards keep the title's window
  exactly). The key stays `<font_pair>+<arrangement>`; at most one look per arrangement, so a pop
  and a clean look never share it. A pop look's rows carry `card_mode: "pop"` in
  `title_look.intro_style`, which the phone carries untouched (server only: tools and the
  verify read it).
* **Save.** Stage B's path: the pin, `caption_clear` decided over each card's own window, and the
  dry run now requires every card compiled where AND when it was offered (`card_moved`). The
  e2e proves a pop card starting 0.25 s in and leaving before the title Saves and compiles at its
  window, the title still passes the render contract's approval, and a photo that is not hers is
  still refused.
* **Preferences, not rules** (Emir, 2026-11-27). Every card rule here (`SHORT_CARD_S`, a mode's
  `max_width`, `MIN_WIDTH`, `MIN_CARD_S`, the stagger, the slots, off her face, the title ink,
  the captions, the watermark and the frame edge) decides only what the offer lays out. The card
  layout (`layout_intro_props`, reached only for a scene with an arrangement, which only the
  offer issues with her photos), `fit_card_looks`, its dry run (`card_moved`) and
  `MAX_POP_COMPILES` run only inside `POST .../intro-styles` (and the verify and fit tools).
  Once the cards are in her edit, her drag, resize, retime, copy and delete, a chat ask
  (`patch_overlay`) and a later title change Save exactly what she set; nothing re-checks a card
  against these rules. What stays is what every card has: a photo the job pinned for this edit;
  the Save's card checks (scale clamped to 0.05-1, centre to 0-1, times to >= 0, end after
  start, unique ids, a path under her item); a window cut at the speaker clip's end (a card
  wholly past it is not drawn); the tilt, zoom, video and full-screen gates; chat's card ops only
  once the edit has its video; and the caption line: a card centred under 0.62 keeps its place
  only when the Save finds it clear of her captions over its own window, else it is drawn at
  0.62. Tapping another look (or "No intro style") in "Try intro styles" still replaces every
  `intro-card-*` card, as applying an option always has. Guard: the "her edits" section of
  `tests/routes/test_intro_cards_e2e.py` (P1-P8).

**Measured (2026-11-27, offline: real face bands, synthetic photos, no Save).**
`app/cli/intro_cards_fit.py` over stage B's 13 prod Talking edits (portrait photos; square and
landscape the same):

| Captions from | Card look before | Card look now | Declines now |
| --- | --- | --- | --- |
| receipt boxes only | 0 of 13 | 12 of 13 (all pop) | `title_unclean` 1 |
| receipt, else style estimate | 1 of 13 | 12 of 13 (11 pop, 1 clean) | `title_unclean` 1 |

On those pop looks a single card covers at most 0-61% of her eyes and mouth (median 13%) for
`around_speaker` and 0-20% (median 0-13%) for the grid with portrait photos; square photos cover
more (up to 90% on one small face, where a 0.45-wide card cannot avoid it). Each card shows
1.5 s, but some card is on her face for up to 1.75 s (`around_speaker`) or 2.25 s (the grid),
because the cards enter 0.25 s apart. On the five real stills of the blind compare: 3 of the 4
with a saved face now get both looks (0 before); the fourth's title cannot sit clean alone, the
fifth has no saved face (M45).

**Checks.** `tests/pipeline/test_intro_scene_props.py` (pop layout, windows, the brute force, the
stand-in speaker, the plain preflight, EN and TR, the golden), `tests/routes/test_intro_cards_e2e.py`
(a real close face band: offer, Save, recompiled windows, approval, the wrong-photo control),
`make verify-intro-scenes` (pop cases on a close speaker drawn on the plate, the orange box is
her eyes and mouth; four controls that must fail: a pop card on the title, on the caption row,
past the frame edge, held 2 s; a pop sweep) and `make verify-intro-styles` (the offer on a real
close face band: every card look pop, every roomy one clean; N11 a pop card held 2 s is caught).

Not covered: whether 1.5 s per card, or 2.25 s of some card on her face, reads as "a short time"
(Emir's call on the real-stills sheet); card styling (a border or shadow) and the phone (stage
C); the preview frame is taken after the title's last entrance and draws every card even when a
short title cuts the last ones before that moment. On a host (local Skia and fonts) the pop sweep
lists two `grotesk_loop` titles the squeeze presses into each other on the close face, with or
without cards (the plain offer's own placement measures the same); the prod image lists none.

## Stickers and edge bleed (KRI-1616)

For the creator: on a Talking edit, "Try intro styles" may add one sticker look that puts one or
two of her own transparent pictures beside the title, and photo cards and stickers may hang past
the side of the frame (refs 2, 3 and 4). A sticker look is offered only when she already has
transparent pictures in her Visuals; an overlay she made with the existing generate-overlay flow
counts. Nothing is generated for the offer and nothing is paid. Once the look is in her edit she
drags, resizes, retimes or deletes a sticker like any card. For the next engineer: the failure
modes M60-M66 are in `services/intro_props_layout.py`, the Save and recipe ones in
`tests/routes/test_edge_bleed.py` (B1-B9) and the offer ones in
`tests/routes/test_intro_stickers_e2e.py` (S1-S8).

**Data shape.** Two arrangements in `intro_scene.ARRANGEMENTS` hold `StickerAnchor`s instead of
photo rows: `sticker_edge` (one sticker, top right) and `stickers_edge` (top left and bottom
right). An anchor names a side of the frame and the title's top or bottom line. Two card modes in
`intro_props_layout.CARD_MODES` place them spot by spot like pop cards. `sticker` keeps every face
out for the title's whole window. `sticker_pop` may cover her for a pop card's window, and it is
asked only when no spot off her face exists. Every sticker of a look takes one width, the widest
of `STICKER_WIDTHS` (0.30 to 0.18 of the frame) at which each finds a spot. Stickers keep off the
title block, the caption row, the watermark and each other, and take z from `STICKER_Z` (10), over
any photo card. `CardMode.bleed` (0.35 for `pop`, `sticker`, `sticker_pop`; 0 for `clean`) is how
far past a side of the frame a card may hang, as a share of its width, and only where the layout
is asked with `bleed`. A sticker's home hangs `BLEED_HOME` (0.25) past. Nothing hangs past the top
or bottom.

**Which pictures.** `intro_card_pool.card_photo` decides the kind with one rule for the offer and
the Save. `analysis.has_alpha` false means a photo card. True means a sticker, which must also be
a PNG or WebP (a HEIC's browser-safe preview is a JPEG). `has_alpha` comes from `image_has_alpha`:
a pixel under full opacity, so an opaque RGBA screenshot stays a photo. The generate-overlay
admission (`generated_media_admission`) now records `has_alpha` and `dims_upright` from the bytes
it stores. Before, a generated picture qualified as neither, and the alpha-aware matchers
(`phone_overlay_grounding`, `overlay_autoplace`) now see a generated transparent overlay as the
alpha sticker it is. Generated pictures admitted before this keep no `has_alpha`, so they are not
stickers until analysed again. A picture whose only transparent pixels are rounded corners counts
as a sticker, because the measure is any pixel under full opacity.

**Gates, one protocol each.**

| Move | Protocol | Why a number | Kill switch |
| --- | --- | --- | --- |
| Sticker looks | `INTRO_STICKERS_PROTOCOL` = 63 | The 62 build's card parser takes `prop_N` slots only, and its tile draws the cached JPEG thumbnail (a box). The export needs nothing new: overlay stills keep their alpha (`Composition.swift`, flattened only on video tracks). | `INTRO_STICKERS_MIN_CLIENT_PROTOCOL=999` |
| Edge bleed in the offer | `INTRO_EDGE_BLEED_PROTOCOL` = 64 | A 62 or 63 build refuses a preview box outside the frame, and one such box turns every tile into a label tile. | `INTRO_EDGE_BLEED_MIN_CLIENT_PROTOCOL=999` |
| Her drag past the frame | `INTRO_EDGE_BLEED_PROTOCOL` = 64 (the constant) | An older engine throws `invalidTimeline` for a placement centred outside [0, 1]. | none: the build's capability |

`intro_styles.card_moves(protocol)` resolves the offer's `CardMoves` from the two config fields, on
top of `cards_allowed` (`INTRO_CARDS_ENABLED`). No new boolean flag: each `=999` turns its move off
for every build. The offer's bleed (at most 0.35 of a card's width) keeps every card centre inside
[0, 1], so a recipe from the offer is one every build draws. Only her own drag can put a centre
outside [0, 1]. The renderer's bound and the 426 therefore read the constant, never the kill
switch, so turning bleed looks off never locks her out of an edit she saved.

**Cloud alpha.** A sticker look is offered only where the phone keeps its alpha. The offer reaches
only phone renders (`route_of`: `not_phone_render` otherwise), and `alphaOverlay` must be in
`PHONE_RENDER_VERIFIED_FEATURES` (`card_moves`). No code moves a `render_destination: device`
variant to the cloud: every writer sets it to `device`, and `phone_editor` keeps it. The cloud
card pass (`pipeline/media_overlay.py`) flattens a picture to JPEG unless
`MEDIA_OVERLAY_ALPHA_ENABLED`, so it never draws one of these stickers. With that flag on it
draws one as the phone does (parity below). Guards: S2 in the e2e, `test_s2_*`, and N13 in
`make verify-intro-styles`.

**The renderer bound** (`portable_visual.EDGE_BLEED_KEEP_ON_CANVAS` = 0.25, Swift
`VisualMediaPlacement.edgeBleedKeepOnCanvas`, both reading `tests/fixtures/edge-bleed-bound/v1.json`).
A placed card keeps a quarter of its own width on the canvas, so the part she can touch never goes.
Its centre's x lies in `[-0.25 * width, 1 + 0.25 * width]`. A card's height is its picture's,
which the recipe does not know, so y keeps [0, 1]. It is the only limit on her drag, which every
offer preference above leaves alone:

* `VisualMediaPlacement` (Python and Swift) and `SubtitledOverlayCard` accept exactly that range.
* The Save (`generative_jobs._keep_edge_bleed`) keeps a phone Talking pip card's centre past the
  frame, clamped to the bound, when the sending build is at 64 (or outside a request: the copilot
  edit and the approval task keep a stored centre).
* An older build that posts a bled card (an edit it opened) gets 426 `client_update_required`
  `edge_bleed` instead of a card pulled back onto the frame. So does an older build that polls a
  bled recipe (`device_render._client_update_detail`), instead of failing it as
  `invalidTimeline`.
* The shared card schema (`MediaOverlay`) still clamps to [0, 1] for agents, the web and cloud
  renders.
* The editor's drag (`NativeEditorSession.mediaOverlayXRange`) stops at the bound on a phone
  Talking pip card: she sees the card stop with a quarter of it on the frame. The handle keeps the
  card's real centre. The live preview's selection box is the card's visible part (unchanged), so
  a card at the bound can still be grabbed.

**The wire.** No new field. A sticker is a card with id `intro-card-sticker-N` and slot
`sticker_N`. The prefix is the one every apply replaces, so applying any look converges, on every
build. A bled card is a card whose preview box crosses a side. `IntroStyleCard.kind`
(`IntroStyles.swift`) is read from the id. The tile draws a sticker from the original on the
iPhone through `CreationMediaPreview.transparentImage` (the cached thumbnail is a JPEG), else the
neutral plate, cut by the tile's canvas. The live preview marks a sticker from her Visuals library
`preserveAlpha` before the Save, as the saved card's pool asset does after it. The strip names the
look "<pair> + stickers", and VoiceOver says "sticker look".

**Parity, cloud and phone.** `tests/pipeline/test_sticker_bleed_parity.py` runs the real
`build_media_overlay_command` with alpha kept on a flat base. It draws one star 25% past the left
edge and one 35% past the right, measures each sticker's ink box, its share of its card box and
its colour, and writes `tests/fixtures/sticker-bleed-parity/v1.json` (`KRI1616_REGENERATE=1`).
`StickerBleedParityTests` (KriaMediaEngine) draws the same recipe through the phone compositor.
Measured: ink boxes within 2 px (cloud `[0, 335, 225, 625]`, phone `[0, 334, 226, 625]`; both cut
at the frame edge), box coverage 0.372 against 0.374 and 0.370 against 0.364. Colour is within 11
a channel, because the cloud's 4:2:0 H.264 path darkens the star. Tolerances: 2 px (ffmpeg's even
`-2` height and chroma blocks), 0.03 coverage, 12 a channel. The negative control renders the
cloud's JPEG path and must be caught (coverage over 0.9: a box).

**Measured on the five real stills (2026-11-27, offline, stand-in stickers).** 3 of 5 get a
sticker look, all in the calm `sticker` mode (off her face for the title's window). With bleed the
stickers hang 25-30% past the side on all three; without bleed (a 63 build) they sit inside the
frame, 0.22-0.30 wide. The other two are stage B's: one title cannot sit clean alone, one take has
no saved face (M45). Pop photo cards with bleed hang 35% past the side at 0.45 of the frame wide.
The top anchor often lands beside her head, because the title fills the room above it.

**Checks.** `tests/pipeline/test_intro_stickers_layout.py` (M60-M66),
`tests/routes/test_intro_stickers_e2e.py` (real PostgreSQL, the real routes and Save),
`tests/routes/test_edge_bleed.py`, `tests/services/test_generated_media_admission.py` (a generated
transparent overlay is a sticker, an opaque RGBA one a photo),
`tests/pipeline/test_sticker_bleed_parity.py` with `StickerBleedParityTests`,
`IntroStylesOfferTests` (sticker looks, bounds, apply, her drag), `IntroStylesUITests`
(`testAStickerLookHangingPastTheFrameSavesWhereTheServerPlacedIt`). `make verify-intro-scenes`
adds ref 4's stickers (two and one) and ref 3's bled pop cards in EN and TR. It has three controls
that must fail: a sticker on the title, a sticker half its width past the frame, and stickers
flattened to boxes. `make verify-intro-styles` adds the sticker look on the roomy and close
framings in EN and TR, plus N12 (a 62 build gets none), N13 (none without `alphaOverlay`) and N14
(a sticker half past the frame is caught).

**Rollback.** `INTRO_STICKERS_MIN_CLIENT_PROTOCOL=999` or `INTRO_EDGE_BLEED_MIN_CLIENT_PROTOCOL=999`
(Fly secret, api restart) removes that move from every offer. Stickers and bled cards she saved
stay, and her drag past the frame stays on 64 builds.

Not covered: a label written on a sticker (ref 2), tilted or overlapping stickers (refs 3, 4), a
sticker beside a photo card in one look, a vertical bleed, and a phone check of the tile's alpha
on a real device.
