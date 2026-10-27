# KRI-966: the length card judges the creator's start/end lines, not the whole take

Parent incident: KRI-964 (Don Julio walk-in T1, prod thread F0F3D5FB, 2026-10-26, prod
`a2426885d`). Evidence level: **offline replay** (real thread inputs through the real gate,
backstop, receipts and runtime over scratch Postgres); no device, live model or production
claim. Claim: `local_fix` (KRI-965 / KRI-967 / KRI-968 keep the parent open).

## Original request (kit T1 prompt, excerpt)

> Start at my question "Parrilla Don Julio, what do you think?" and end right after "Nice!" —
> drop the dining room before it and the "Zoom" and the table after it. … About 7 seconds.

One 26.93 s phone Talking take. Brief r1 = `{kind: timing, facts: {start_line, end_line: "Nice!",
duration_s: 7}}`.

## Failure modes (written before the change)

- F1 window found: the take capacity is the kept window (`estimate_take_window(...).kept_s`),
  not the whole take's timed speech.
- F2 window asked, a line not found (and no pick): no length card from the whole take's speech
  (the line picker owns that side).
- F3 no window asked: behaviour unchanged (same card, same copy, same input digest).
- F4 window within the existing 10 % tolerance: no card.
- F5 window shorter than the ask: one honest card that names the lines; no trim option.
- F6 window longer than the ask: the same rule as a take today (one "keep it" option, no
  trim; nothing above the edit cap).
- F7 song / voiceover / footage owners and the continuous-voice montage unchanged.
- F8 the tapped answer never comes back as a creator requirement graded "you asked for N s"
  (stored brief, approval card, after-render review).
- F9 a saved answer / digest from an older question never loops: a start-line pick answered
  earlier in the same planner pass is seen by the length card (otherwise the planner pass
  misses the card, the approval backstop asks it, the answer is never recorded and it is asked
  again); the window card's digest does not depend on snapshot keys that differ between the
  planner pass and the backstop.
- F10 Turkish copy parity (the TR sweep covers the window card).
- F11 (found while tracing) a second, model-written length row makes the brief hold two
  lengths, so `_length_vs_natural` stops collecting, the answer is never recorded and
  `answered_brief` never applies it: the prod r7 both caused the wrong grade and silently
  dropped the creator's answer.
- F12 (found in review) a partial `change` with no `kind` (KRI-805 shape) putting the seconds
  on a timing row is the same echo.

## What the creator saw (prod) and what this change does

| Step | Prod | After this change (replayed) |
| --- | --- | --- |
| Turn 2 card | "…the available spoken take is 10.2 seconds. … The most I can do is: Use the available 10.2 seconds." | "Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can't make a 7-second edit without changing those lines. The most I can do is: Keep those 5 seconds." |
| Turn 3 brief | model adds r7 `{timing, duration_s: 10.2, "Use the available 10.2 seconds"}` | the echo is dropped; stored brief stays r1–r6 (r1 keeps 7) |
| Draft / approval card | lists "Use the available 10.2 seconds" next to the 0:15 → 0:20 window | same rows as prod minus that row; r1 keeps the creator's words |
| Approval pin | r1 `duration_s: 7` + r7 `10.2` (answer never recorded) | r1 `duration_s: 5`, `choice_answers` holds the length answer |
| After render | "Partly: Use the available 10.2 seconds (… you asked for 10.2s)" | no row says "you asked for" (real variant re-judged) |

## Root causes (file:line at `18a46b862`)

1. `app/services/choice_questions.py:2743-2754` (`_natural_capacity_s`, owner `take`) measures
   `_voice_length_s(row)`: the whole clip's timed speech (15.0 → 24.88 s + pad = 10.16 s), and
   `_length_vs_natural` (`:2777`) never looked at the brief's start/end lines, although
   `estimate_take_window` already runs the worker's own `apply_take_window` on the same words.
2. r7 is the Main Creator's brief update on the answer turn: the tapped label is the turn's
   message, and `app/kria/planner.py:5558` (`_plan_live_turn`) binds the model's updates with
   nothing that knows the turn answers a length card; the runtime persists them
   (`app/tasks/kria_runtime.py:1486`). With two lengths (7, 10.2) `_length_vs_natural` returns
   at `:2796`, so the answer is never recorded or applied, and the review's timing check
   (`app/kria/brief_checks.py:7052`) grades r7 as the creator's ask.
3. `resolve_choices` (`:4465`) collects every question once, before the pass; a start line
   picked in the same pass (KRI-844) reaches the brief only after it, so the window card could
   not see the pick (F9).

## Change

- `_take_window` / `_take_window_choice`: on a one-take phone Talking draft whose brief asks a
  start/end line, the take capacity is `estimate_take_window(...).kept_s`; a line not found
  means no card. The card names the lines and the seconds they keep (EN/TR, one line or both,
  a ceiling and a strict whole-video limit), one option "Keep those N seconds" (no trim), effects
  as today, digest from the window itself, and the answer records `detail.basis =
  "take_window"`. No window, a non-phone render or another owner: unchanged.
- `answered_brief`: a window-basis answer sets `duration_s` on the creator's row and keeps its
  words when the row holds the lines (its line receipt names the part kept); a separate length
  row reads "Keep about N seconds (the part your lines keep, instead of M)".
- `resolve_choices`: once a take-line answer with a picked line is recorded, the questions
  after the take lines are collected again from the brief with the pick (what the approval
  backstop sees).
- `without_length_answer_echo` (+ `answered_length_card`), called by `_plan_live_turn` on answer
  turns: when the turn's message is a `choice_selection` of a length card (length_vs_natural,
  voice_vs_duration, duration_vs_count, holds_vs_length), model updates that add a timing with a
  length or change / remove a live length row are dropped. Read from structured events only
  (no regex over creator words, KRI-667); no prompt change.

## Checks

Fixture `tests/fixtures/kri966_donjulio_walkin_t1_length.json` (real snapshot, approval brief,
events #4/#6–#11/#20 and the finished variant without its signed URL; brief v1/v2 derived and
checked: they reproduce prod's two question digests `f1017fed…` and `b6f6cc0a…`).

- `tests/kria/test_kri966_take_window_length.py` (gate → backstop → draft card → review):
  prod questions reproduced (provenance, F3), prod's "you asked for 10.2s" row reproduced from
  the real variant, turn-2 card for both brief shapes (F1/F5/F9), one tap settles it (F8/F9),
  line not found / declined (F2), tolerance both sides and a longer window (F4–F6), Turkish
  (F10), the echo dropped incl. a partial change (F11/F12), wrong-content controls (a typed
  length and the start-line tap keep their updates).
- `tests/kria/test_kri966_t1_runtime_postgres.py`: real runtime turns over scratch Postgres,
  only the Main Creator replaced (prod's brief, its turn-2 rewrite, its turn-3 echo): the card
  names the lines; the stored brief has no r7; the approval card has no echo row; the approval
  pins r1 `duration_s: 5` and the length answer.
- Turkish sweep `tests/services/test_choice_questions_turkish_guard.py` covers three window
  cards.

Red before (`18a46b862` code, new tests): 15 failed, 101 passed (both new modules, the KRI-849
replay and the TR guard). Green after: all pass; 2,481 tests (every module touching the choice
gate, the gate ledger, both new modules) pass. Broad run (`tests/kria tests/services tests/gates
tests/agents tests/routes tests/tasks`): 22,019 passed; the 2 failures
(`test_kri947_kri951_m2_picker_rows.py::test_prod_rounds_1_to_3_…`,
`test_kri949_named_photo.py::test_round_four_offers_the_photo_for_both_rows`) fail the same way
on `18a46b862` without this change, and the 2 errors (`test_apple_revocation_postgres.py`) need a
database named `nova_test`.

Mutation check (scratch, not committed), each turns at least one test red: window ignored (13
red), no in-pass pick recollection (2), planner filter off (1, runtime), filter on any answer (1,
control), whole-take capacity behind the window copy (3), creator's window row reworded (1,
runtime), partial-change kind not resolved (1), Turkish label left English (2).

## Case matrix

- Covered: prod's brief shape and the server-owned pick; both lines / start only / end only;
  shorter, longer, within tolerance; strict whole-video limit (copy); EN + TR; no window (prod
  card unchanged); another owner untouched (existing length-owner, KRI-847, KRI-679 suites green).
- Changed neighbour: `test_kri849_dj_t1_unclear_word_replay.py` start-line cases (11 s window vs
  "About 15 seconds") now get the window card first; its helper taps "Keep those 11 seconds" and
  goes on to the unclear-word question.

## Behaviour to know

- A one-take phone Talking edit with start/end lines and a length can now get a length card it
  did not get before (the window is outside 10 % of the ask while the whole take was inside), as
  in DJ T1 above. Before, it compared against the whole take.
- A start line only or an end line only is judged against what that cut keeps (to the end of
  the take / from its start), not the speech.
- An older thread holding the old whole-take answer gets the window card once (new digest).

## Not verified / remaining gaps

- No device run, no live model: the Main Creator is replaced by recorded outputs; its draft
  strategy is reconstructed (prod kept only its hash). Whether the model still writes r7 does not
  matter to the server (dropped), but other echo shapes (e.g. a `style` row with seconds) are not
  filtered.
- The window is estimated from sentence timings (5.0 s) while the phone render cut on its own
  word timings (4.22 s); the card says "about".
- A declined line (no pick) leaves nothing to compare; the draft receipt states the take length.
- KRI-965 (iPhone editor plays the whole take), KRI-967 ("Parrilla Don Julio" missing from the
  forced-English words) and KRI-968 (simpler version drops ATAKAN) stay open under KRI-964.
- Re-send T1 on the iPhone after deploy: the card should name the lines and ~5 s, and no
  "you asked for" row after the render.
