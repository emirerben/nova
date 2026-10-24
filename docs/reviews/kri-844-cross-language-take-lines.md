# KRI-844: a quoted start / end line the take does not say (Don Julio T2)

Offline verification of a bounded local fix. It does not establish fulfillment of the
whole Don Julio T2 request or a physical-iPhone export. Parent KRI-829 keeps those gaps.

## Request and failure

Kit `T2_lucas_hook_photo_cards` (one 12.9 s phone take, `T_lucas_sauces.mp4`): "Cut my
question and his name so it opens straight on "Here we have traditional Argentinian
sauces" … End on the "Thank you". … Under 10 seconds." Lucas says these lines in
Spanish: "Acá tenemos salsas tradicionales argentinas, …" and "Gracias".

`ground_line` matches same-language text only (exact, ≥0.8 token overlap, ≥0.85 character
ratio). On main e04ab19ee, with the take's Spanish words:

- both receipts were `not_found`
- the draft estimate was `start_s=None end_s=None`
- the worker kept 0–12.9 s
- nobody was asked before approval; only the finished card said "Partly … couldn't find".

With forced-English words the start was found at 4.0 s, but only because forcing English
makes whisper translate.

## Fix

The fix is a pre-approval question, `choice_questions._take_line`, using the existing
`choice_question` v1 card.

- **When it asks:** a one-video phone Talking edit whose quoted line is `not_found` in the
  take's clip-record speech. "Not found" is judged by the same `apply_take_window` the
  draft estimate runs.
- **What it asks:** which line of the take to open on, then which to end on.
- **The options:** the take's own clauses, the ones most like the quote first, plus
  "None of these".
- **Where the answer goes:** the pick is stored on `ChoiceAnswer.value`. `answered_brief`
  copies it to `facts.start_line_spoken` / `end_line_spoken`.
- **How it is used:** `apply_take_window` grounds the pick before the quote, in the draft
  estimate, the worker and `verify_take_window`.
- **What does not change:** a found quote never asks and stays byte-identical. A side
  with no usable pick keeps the honest `not_found`.

No translation, no prompt change and no client change.

## Independent expectation

These boundaries are measured on the audio signal of the same clip: a band-passed
20–25 ms energy envelope with an adaptive noise floor, cross-checked with FFmpeg
`silencedetect`. No transcript is involved.

| Boundary | Measured |
| --- | --- |
| Name exchange ends | 3.70 s |
| "Acá" onset | 4.20 s |
| Sauces sentence ends | 9.40 s |
| "Gracias" | 10.74–11.46 s (faint tail to ≈11.64) |

This gives the Done-when bars: start ∈ [3.90, 4.20] s and end ∈ [11.46, 11.96] s.

The fixture `src/apps/api/tests/fixtures/kri844_dj_t2_take.json` carries these values
(`audio_measured_s`) next to the words:

- The words come from whisper.cpp `ggml-medium`, forced `-l es`, `-ml 1 -sow`, run
  locally.
- Only "Gracias" is retimed, from 10.00–12.00 to the measured 10.74–11.46. whisper.cpp
  stretches a word over the silence after it.
- A DTW pass did not get closer, so it was not used.

## Evidence (offline replay)

`src/apps/api/tests/kria/test_kri844_dj_t2_cross_language_replay.py` drives the real
chain. The planner turn and choice gate, the tap and typed answers, `answered_brief`,
`estimate_take_window`, `build_receipts` and `apply_take_window` all run. The clip record
is built with `attach_segments_to_analysis`. Nothing in that chain is mocked.

| | main e04ab19ee | this branch |
| --- | --- | --- |
| Before approval | no question | "I couldn't find “Here we have traditional Argentinian sauces” in what's said in your video. Which line should it open on?", then the same for "Thank you" / end |
| Start options | — | “¿Qué te llaman?” · “Lucas” · “Acá tenemos salsas tradicionales argentinas,” (recommended) · “salsas criollas,” · “chivichurros y sardines” · None of these |
| End options | — | the three lines from “Acá tenemos…” on · “Gracias” · None of these |
| Worker cut | 0–12.9 s, both `not_found` | **4.00–11.71 s (7.71 s)**, both `applied` |
| Finished line verdict | Partly | met |

The replay file runs red on main: 6 failed, five on "the draft went out with no question
at all" and one on the missing `start_spoken`.

The wrong-content control is `test_the_acceptance_window_rejects_the_wrong_lines`. Picks
that open on the name ("Lucas"), open late ("salsas criollas,"), or end early ("chivichurros
y sardines") fail the same window check the right picks pass.

`src/apps/api/tests/kria/test_kri844_take_line_ask.py` covers more cases:

- A pick of "Gracias" never lands inside an earlier "muchas gracias": the stored pick is
  widened until it names one place.
- A found quote is byte-identical to main.
- Multi-clip edits, non-Talking edits and a take with no speech never ask.
- Digest and re-ask behaviour, "none of these", and Turkish copy.
- A later length answer cannot drop the picks.
- A later plain message never re-answers a closed card.
- The CLI reads the picks.

## Not established

- **Original-footage iPhone export.** The phone worker grounds the pick in its own cleanup
  transcript, and a wrong-language pass (KRI-843) gives an honest `not_found`.
- **"Under 10 seconds" still asks.** `length_vs_natural` compares the length with the whole
  take, not the kept window (KRI-847).
- **The draft card's start time.** It is char-share timing inside a merged sentence (0:03
  for a 0:04 cut).
- **End precision depends on ASR word timing.** With raw whisper.cpp timings the end lands
  at 12.25 s.
- **The photo triggers.** "Argentinian" and "chimichurri" are also English words over
  Spanish speech; that is outside this fix.
