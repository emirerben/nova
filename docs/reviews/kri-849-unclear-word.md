# KRI-849: ask about an unclear word instead of guessing it (Don Julio T1)

Parent: KRI-829. Evidence level: **offline replay**. No live model, no native export, not deployed.

## Request

Don Julio kit `T1_lucas_full_take_talking/PROMPT.txt` (one 25.7 s phone take; the waiter speaks Spanish):

> … Spell the sauces right: salsa criolla, chimichurri. I didn't catch the third sauce he names: if you
> can't tell what he says, ask me instead of guessing. …

Expected: before approval Kria asks about the third sauce (≈8.8–10.0 s in the take), or the captions leave it
out. A guess is never printed.

## Root cause on main

- The Main Creator could only file the sentence as a prose `style` row. The draft showed it as
  "I can't verify this one automatically yet." and asked nothing.
- The phone Talking captions are the raw whisper-1 words plus the creator's spelling list, so the guess
  shipped ("… chimichurri and sardines").
- whisper-1 returns no per-word confidence (every word is `1.0`), so a "low-confidence word" check has no data
  in production.

## Signal used

Before approval the snapshot row already holds two independent hearings of the speech:

- Gemini's transcript (untimed);
- whisper-1's timed sentences.

On the recorded production hearing of this exact utterance (`tests/fixtures/kri803_don_julio_m1_clips.json`,
clip `m1_03`) they disagree exactly on the third sauce: Gemini "chimichurri con sal marina", whisper-1
"chivichurri con salma y limón". "Chivichurri" is covered by the creator's spelling list. The remaining
disagreement is the unclear word.

## Fix

| Layer | Change |
| --- | --- |
| Brief (Main Creator v76, extractor v21) | `ask_if_unclear` on the captions row; a typed answer is `unclear_answer` (`omit` or the creator's words). The server never reads the words (KRI-667). |
| Detector (`app/pipeline/caption_unclear.py`) | Compares the two hearings inside the kept take window. Agreement includes names spelled differently, fillers, contractions, repetitions and spelling-list words. Hearings in another language or with little in common are "not comparable": no spans. |
| Gate (`services/choice_questions.py`, `unclear_word`) | One question per span before approval: "Leave it out" plus the two heard candidates (EN/TR). "You decide" never picks a word. The tapped window is pinned on the approved brief. |
| Render (`_run_phone_subtitled_job`, after creator spellings) | Acts on tapped windows, approval-time spans recomputed from the pinned snapshot, and comparable render-time spans. An unanswered span is left out (fail closed). An error drops the pinned windows and says so. |
| Receipts | Draft: "around 0:09 in your clip". Finished video: the edited-video time. No receipt quotes a heard guess. |

## Initial evidence (before the integration corrections below)

- `tests/kria/test_kri849_dj_t1_unclear_word_replay.py` (71 cases) and `tests/agents/test_kri849_unclear_word_prompt.py` (2):
  - **On main 6453cbca6:** 67 of 73 fail (no question, guess in captions, old placeholder line, no detector).
  - **The 6 controls** (no ask, hearings agree, hearings not comparable) pass on both.
  - **On the branch:** 73 of 73 pass.
- `tests/kria/test_kri849_unclear_word_postgres.py` (2): the real `run_kria_turn` → `_complete_draft_turn`
  on Postgres (question, draft receipt, reply and card).
- **Mutations** (each restored after):
  - render hook removed: red;
  - detector unregistered: red;
  - fail-closed off: red;
  - delegation guard removed: red;
  - `answered_brief` pinning removed: red;
  - approval-time windows dropped: 6 red;
  - error fallback removed: 2 red;
  - cut-plan window mapping ignored: 2 red;
  - contraction table off: 5 red;
  - draft-turn wiring removed: Postgres case red.
- **Free replay evals:** `tests/evals` 2148 passed, including the two hand-authored `kri849_dj_t1_*` Main
  Creator cassettes.

### What is recorded vs hand-authored

- **Recorded:** the kit prompt and timings, and the production `m1_03` hearing pair. That pair is re-cast onto
  T1 timings.
- **Hand-authored:** the brief, the take row and the render words, because DJ T1 was never sent. The two Main
  Creator cassettes are hand-authored replies, not model output.

## Gaps

- A live Main Creator v76 emitting `ask_if_unclear` / `unclear_answer` is unverified (no paid calls).
- If both hearings agree on a wrong word, the guess still ships. The draft says both listens agreed.
- Missing or non-comparable hearings leave the target unresolved. The planner and approval
  backstop ask which caption word/line and where in the clip; a legacy render without any
  grounded target declines with `needs_choice`, rather than printing a guess or removing
  unrelated captions. A later creator correction retains a previously confirmed window.
- When the ask is active, a new unclear stretch found only at render is left out without a question. The
  receipt says what was left out.
- Not covered:
  - the cloud `_render_subtitled_variant` path;
  - multi-take Talking;
  - `variant.overlay_transcript` (media-lanes flag, default off).
- No iPhone export.

## iPhone re-run (after merge and deploy)

1. In a new chat, attach `T1_01` as Footage.
2. Send the kit prompt.
3. Expect a question about the word around 0:09, with "Leave it out" and the heard words.
4. Tap "Leave it out", or type the kit answer "I don't know it either, leave that word out of the captions."
5. Approve.
6. Check that the captions show no word for the third sauce, and that the finished-video line says it was left out.

## Integration review: correcting an earlier tap (2026-10-24)

Failure mode: a creator taps Leave it out, then types a correction for that same
word. A partial brief update preserves the old structured answer, which used to
win over the later typed words. The real writer replay
`test_later_typed_correction_replaces_the_earlier_tapped_caption_answer` failed
before the correction: the captions omitted the word instead of showing
`sal de mar`. A model-recorded typed answer now takes precedence over older taps;
with it present the gate no longer offers a new tap. The 74-case writer/prompt
replay passes, including the new correction and existing wrong-word controls.

## Integration review: unavailable comparison (2026-10-24)

The earlier non-comparable control incorrectly allowed approval and unchanged disputed
captions. The replacement gate-to-writer cases reproduced this on missing, different-language,
and insufficient hearings before implementation. These now remain questions before approval;
repeated "go ahead" messages and a disabled choice-card flag cannot settle the unknown target.
The writer and error fallback decline when no window is available. Existing confirmed
windows, later typed corrections, and unrelated caption timings are preserved. A creator's
replacement wording alone is insufficient when its target cannot be located; the question
continues to request that location instead of claiming the correction was applied.

The correction first reproduced four failures through the real gate/writer replay and
an additional approval-card bypass on real PostgreSQL with choice cards disabled.
The corrected runtime/gate/writer suite passes 85 cases, including both flag states,
repeated unanswered replies, Turkish clarification, and preservation of existing answers.
The no-guess requirement remains persisted after the question. Replacement wording or
"omit" without a located target cannot create an approval card. The broader choice,
narrated-cut, duration, audio-save and freeze/protocol integration run passed 836 cases.

## Integration review: source-bound answers (2026-10-24)

A further replay reproduced an old answered window clearing clarification after the footage
changed. The expanded red controls also found unchanged original-byte hashes were not part
of the identity, stale answer IDs could clear the approval backstop, and a new source pick
could inherit old typed wording.

Server-written answers now carry an opaque scope for clip/path, generation, original SHA-256,
and the request. Gate, receipts, writer and fallback require that scope. Analysis-only changes
retain confirmed windows; replacement clip/generation/hash, a changed request, or a legacy
unbound answer requires a new decision. A new scoped pick clears wording tied to an old
source. The backstop also checks the current question digest rather than accepting an old ID.
The 851-case combined replay/Postgres integration passes, including the same-source positive
controls and replacement/legacy negatives; legacy rendering without a valid target declines
instead of removing unrelated words. No new client protocol or model prompt is needed.

## Integration review: typed answers without a prior window (2026-10-24)

Five red gate/writer cases reproduced a remaining gap: a legacy typed-only fact had no
stored window, so replacement footage could inherit its wording and a new tap could be
ignored. An unbound fact now has no caption authority. Fresh typed words are accepted
only when the exact words occur in a creator reply to the still-open server question
for the current source, request and window. Omission replies use the existing exact
option/alias matcher. The server records the confirmed window and discards the unbound
text; it never retroactively stamps legacy wording onto whatever footage is current.
A newer scoped tap wins over an older typed reply. Existing corrections to an already
confirmed same-source window remain supported. Free-form paraphrases that cannot be
bound to the question stay unresolved rather than becoming guessed caption text.

The positive writer cases now ask a real question before the creator types their answer,
and the PostgreSQL acceptance case submits a second real turn before expecting approval.
The replacement negatives start with typed-only facts and no earlier tapped window.

## Integration review: target-window authority (2026-10-24)

Three further red cases kept the same immutable source, request and heard word pair while
moving only the analysis window by ten seconds. Neither an old tap nor an old typed reply
may authorize that new occurrence. The question digest now includes its canonical source
time bounds (the same millisecond bounds recorded on the answer), so a moved occurrence
requires a fresh decision. Legacy questions whose digest never included a window do not
match the new question identity. Matching a historical reply verifies its original target;
the handler cannot silently reconstruct a different time interval under the same digest.

A later wording correction now rewrites only the already confirmed answer windows. It is
never used as a global default for newly detected spans. Those confirmed bounds survive
analysis changes, while a newly detected, separate occurrence remains a question. The
96-case gate/writer suite passes, retaining same-window typed, tap, omission, missing-hearing,
and caption-timing mapping controls.
