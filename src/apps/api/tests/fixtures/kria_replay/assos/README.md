# Assos replay fixture

Prod request: "With animations, explain why people should visit Assos, Turkey" (montage, 16 clips,
15 landscape 4K plus one foreign clip first). One creation turn, one chip follow-up ("Animate the
title"). Scorecard: `scripts/kria/score_assos.py`. Builder: `scripts/kria/build_assos_fixture.py`.

    make kria-replay FIXTURE=assos            # readable summary + test-results/assos-replay/report.json
    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_assos.py --json --strict   # full JSON, exit 1 unless green
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_assos_request_to_recipe.py [--runxfail]

Offline and free: a socket guard fails any network attempt, no model is called, JSON only, no media.
The fixture was captured from origin/main `9c5a453b0`.

## Recorded (exact, from the saved prod capture)

- creator message and Creative Brief v1 (one `text/global` requirement; r1's `facts` is `{}` in
  prod, see Reconstructed)
- the planner's post-parse strategy, including the frozen catalog song section and its beats
- the 16 clip analyses (media snapshot `clip_assignments`), durations, original sizes and rotation
- `nova.research.public_facts` evidence (14 facts), `nova.research.caption_binding` raw text,
  `nova.research.claim_review` raw text
- approval card text, change chips, requirement receipt, plan-block look chips, render shape
- the recipe prod produced: cut lengths, the four researched texts and their (static) effect,
  burned font, length owners (planner 20 s, song 18.005 s, Save 17.0 s), export verification
- turn 2: chip, assistant reply, `brief_coverage` cause, copilot run outcome (`terminal_transient`)

## Reconstructed (prod redacts model I/O, or it was never stored)

- Creative Brief v1 r1 `facts.animation = "any"`: prod ran planner prompt v105 and recorded
  `facts={}`; planner prompt v106 (prompt fidelity, animation) records an unnamed animation ask
  there. The model leg is not replayed offline; the server reads only the structured fact
  (KRI-667), and `tests/kria/test_text_animation_ask.py` pins the round trip.
- `nova.creator.main` raw text: the parsed strategy is recorded, the raw model JSON is not replayed
  through `MainCreatorAgent.parse`
- `nova.edit.copilot` (creation text composer) raw text: rebuilt as a `research_request` whose
  topic and quote are the recorded `public_facts` input
- `nova.compose.phone_typography` raw text: rebuilt from the recorded burned font
- phone source bindings (synthetic proxy paths and hashes), capability manifest
- the approval request body: whether the app echoed the server's preselected render shape is
  inferred from the iOS code, not recorded
- the approval card is assembled from the real pure functions the draft turn calls
  (`_strategy_changes`, `build_receipts`, `reply_from_receipts`, `assess_readiness`); the
  database-bound turn body itself is not run (level: offline replay of the card inputs, not the turn)
- turn 2 failure: a real `TerminalError` is injected at the brief extractor and flows through the
  real `plan_live_turn`; prod's failing agent run was logged as `nova.edit.copilot`, which of the
  two raised in prod is unknown (the reply is identical either way)

## Scrubbed

User, thread, item, job and turn ids; storage paths; library ids; file names; content hashes;
grounding redirect URLs (token replaced by `SCRUBBED-n`; host and title kept because the
institution filter reads them); media ids are now `analysis-proxy-assos-NN`.

## Reproduction of prod

The scorecard prints "baseline reproduction": on the unchanged code the replay reproduces prod's
cut grid (16 cuts, 18.005 s), four captions, static effects, fonts, approval card, receipt, render
shape default and orientation stamp. Those rows are informational; a phase that changes the output
on purpose changes them too (`ASSOS_EXPECT_BASELINE=1` asserts them in pytest).

## Replay of the recorded caption binding

The recorded `nova.research.caption_binding` answer names research windows by id (`window-3`), and
a window id only means something against the timeline it was written for. When a plan changes the
timeline (a text_explainer cut as story beats, a relaxed pace), `score_assos._Sequence(rebind=True)`
keeps what the model decided (each caption's words, evidence ids and the quote of the picture it
describes) and re-seats the caption on the first free window of the plan that shows that picture
and fits its character limit, in the recorded order; a caption whose picture is in no free window
is left out. On the timeline the answer was recorded against it picks the same windows. The Assos
replay also verifies `crossfade` (real prod does), which the story directions draw.

## What the checks do not prove

They check the named observable, not that the video is good. Check 2 compares mean cut length
(relaxed must be at least 10 percent longer than fast) and snapshot pace/direction. Check 4 is a
differential (reversed `story_structure` must change the order). Check 5 treats the draft
strategy's `target_duration_s` as creator-visible: whether the iOS card renders it is unknown.
Check 6 cannot tell an echo from a pick in the worker input (no such field exists today), so only the
echoed-default case decides. Check 10 is report-only. The iOS render, export verification and
device behaviour are not covered (`make ios-verify` is a separate evidence level).
