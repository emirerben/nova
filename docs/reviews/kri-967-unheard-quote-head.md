# KRI-967: a quoted start line whose first words the timed transcript lost

Parent incident: KRI-964 (Don Julio walk-in T1, prod thread F0F3D5FB, 2026-10-26, prod
`a2426885d`). Evidence level: **offline replay** of the thread's real clip record, question,
tap and receipts, plus an ffmpeg cut of the real footage with the worker's own cut function.
No device, live-model or production claim.

## Original request (kit T1 prompt, excerpt)

> Start at my question "Parrilla Don Julio, what do you think?" and end right after "Nice!"
> … Spell "Parrilla Don Julio" right. … About 7 seconds.

## What happened

whisper auto-detected Turkish; the KRI-808 check re-transcribed in English and stored
"So, what do you think?" for the head + tail (the name came out as "So,"). The take-line
question (KRI-844) did not find the quote and recommended "what do you think?"; the creator
took it, and the video opened at 16.05 s, in the silence between the name and the question.

Measured on the uploaded original's audio (sha256 matches the upload contract): the creator's
"Parrilla Don Julio" runs 14.85–15.73 s, silence to 16.13 s, "What do you think?" from
16.14 s. So prod lost the whole head. (The kit's hand transcript, ≈13.0 s, is approximate.)

## Cause

- `app/services/choice_questions.py` `_take_line_choice` (base `18a46b862`, ~L3241): the
  recommendation is `take_lines.offer_lines`'s trigram match (`app/kria/take_lines.py`
  ~L228–265). It scores each line against the WHOLE quote, so the heard tail ("what do you
  think?", 0.65) wins and the line the head was said as ("So,", 0.0) never can. The intro said
  the quote was not found at all, without saying which words.
- The evidence that the head was said right there was already on the record the question
  reads: the clip's Gemini transcript (`understanding.speech.transcript`, KRI-808's reference):
  "Parilla Don Julio. What do you think? …". Nothing used it at question time.

## Why this layer (and not the KRI-808 language check)

The KRI-808 check kept the forced-English re-run because most of its words are words Gemini
heard (`clip_speech_check.pick_spoken_words`). Neither alternative recovers the name: the
first pass was whisper's Turkish reading (wrong language for every caption), and dropping the
words leaves no timed speech, so no question and no cut at all. Neither whisper pass contains
"Parrilla Don Julio"; the timed words cannot be repaired there without a new transcription
call. The question is where a heard tail and an independent listener meet, and the pick it
stores is what the draft card and the worker's cut ground, so the fix is in the picker.

## Change

- `take_lines.partly_heard`: a quote the take says only part of. For a start quote the
  creator's last words must be said from a line's first word on (≥ 2 tokens, cut between the
  creator's words); for an end quote the first words up to a line's last word. It returns the
  heard line, the creator's heard / unheard words, and `said_as`: the adjacent line the unheard
  words were said as, only when ALL hold — the clip's Gemini transcript says the unheard words
  (≥ 0.85 similar, the `ground_line` bar) right next to the heard ones; the line is ≤ 1 s away;
  it has ≤ (unheard tokens + 2) tokens; its words are not words of the quote; and that
  transcript did not hear its words on their own elsewhere.
- `offer_lines(prefer=...)`: the said-as line is always offered (even when more lines than
  fit) and is the recommendation.
- `_take_line_choice`: the said-as option keeps its key (`line_0`), is shown and pinned
  joined to the heard line ("So, what do you think?" — the joined text grounds where the head
  is followed by the tail, so a stray "so" elsewhere can't move the cut), keeps its old alias
  and gets a description ("Opens where “Parrilla Don Julio” is said."). The intro names what
  was found and what wasn't: "I found “what do you think?” in your video, but not “Parrilla
  Don Julio” before it. The transcript has “So” there." Without the evidence only the intro
  changes; the old recommendation stays. EN + TR.
- `opening_cut_ask.TakeSpeech.reference`: the record's `speech.transcript` (default "").
- Unchanged: option keys, other options, the question digest (an earlier answer keeps its
  value), the worker's cut, prompts. No regex reads creator words (KRI-667).

## Failure modes (written before the code) and where each is checked

All in `src/apps/api/tests/kria/test_kri967_unheard_quote_head.py` (fixture
`tests/fixtures/kri967_dj_walkin_t1_unheard_head.json`).

- **F1** head unheard + tail heard + adjacent line: recommend it, name the missing words —
  `test_the_card_recommends_opening_where_parrilla_don_julio_was_said`.
- **F2 / F11** no invented start: no Gemini transcript, it never heard the head, heard it
  only elsewhere, the line is its own utterance, far away (5 s), much longer than the head, or
  made of quote words → tail stays recommended —
  `test_without_evidence_the_head_was_said_there_the_tail_stays_recommended[7 cases]`,
  `test_without_evidence_the_card_still_names_the_words_it_could_not_find`.
- **F3** found quote asks nothing — `test_a_quote_the_take_holds_asks_nothing`.
- **F4** unmatched quote asked as before — `test_a_quote_that_matches_no_line_is_asked_as_before`
  (plus the unchanged KRI-844 suites).
- **F5** end side: symmetric and cheap, so fixed —
  `test_the_end_side_recommends_ending_after_the_unheard_tail` (synthetic: "Nice, Atakan!"
  heard as "Nice, I tell you.").
- **F6** the pick really moves the draft card and the worker's cut; degrades, never worse —
  `test_the_recommended_pick_opens_the_draft_and_the_cut_at_the_head`,
  `test_a_pin_the_worker_words_do_not_hold_never_opens_later_than_today_or_at_zero[2 cases]`,
  `test_a_brief_rewritten_to_the_tapped_label_cuts_at_the_head_too` (prod's Main Creator
  rewrote `start_line` to the tapped label).
- **F7** stability — `test_the_replayed_question_is_the_prod_question` (digest
  `f1017fed1df5aefc1d17c0cb` reproduced, keys equal, every prod alias still answers),
  `test_the_prod_tap_on_the_tail_line_keeps_its_meaning` (value, digest, prod cut).
- **F8** Turkish — `test_a_turkish_chat_gets_the_whole_card_in_turkish` + the existing
  KRI-630 guard (source check: intro through `say()`).
- **F9** KRI-667 guard suite green; no prompt touched.
- **F10** capped lines — `test_the_head_line_is_offered_even_when_more_lines_than_fit_look_like_the_quote`.
- **F12** "you choose" takes the head line — inside the F1 test.

Negative control: `test_the_acceptance_window_rejects_the_prod_cut_and_other_wrong_openings`
(the same window check rejects prod's 16.05 s opening, the whole take, a later line and an
opening in the street; the reconstructed worker words reproduce prod's 16.05 / 20.27 cut).

## Results

| | prod pick ("what do you think?") | new recommended pick ("So, what do you think?") |
| --- | --- | --- |
| draft card (`estimate_take_window`) | 15.28–20.29 s | 14.85–20.29 s (5.44 s) |
| worker cut, reconstructed worker words | 16.05–20.27 s (= prod receipts) | 14.85–20.27 s |
| worker words without "So," | 16.05 s | 16.05 s (degrades to today) |
| reference listener's words ("Parella Don Julio.") | 16.33 s | 16.33 s (same as today) |
| real footage cut (`app.cli.verify_take_window --video`) | opens on 0.1 s silence, then "What" | opens on "Parrilla" (−13 dB from 0.0 s), "What" at 1.3 s; first frame: Atakan at the PARRILLA plaque |

Acceptance window: opens within 0.3 s before the measured onset (14.85–14.855 s) and ends
within −0.1/+0.5 s of the reference listener's "Nice." end.

- Red before: on `18a46b862` the final test file fails 6 of 21 (recommendation, cut, copy,
  cap, end mirror, Turkish); 15 controls pass both ways. Green after: 21/21.
- Mutations (scratch, not committed), each caught: no reference evidence needed (4 red);
  said-as line dropped when lines are capped (1); pin not joined to the heard line (2, incl.
  the "worker words without So" case opening at 0 s); no gap check (1); own-utterance line
  accepted (1); quote-word line accepted (1); length unbounded (1).
- Neighbouring suites: every test file that touches `choice_questions` / take lines / the
  opening cut (2067 passed), `test_runtime_postgres_integration.py` on a scratch DB (53).

## NOT verified

- The worker's own pre-cut word timings (speech-cleanup preflight words) were not in the run
  folder; they are reconstructed from the prod caption cues (each kept line's bounds are
  real). If the real words lack "So," the cut stays at 16.05 s while the draft card says 0:14
  (the "without So" case above).
- The cut opens at the measured onset with no pre-roll (14.85 s vs onset 14.850–14.855 s):
  the whole head is kept, but any audio fade the phone applies at a cut (not checked) could
  soften the first "P". The 0.15 s lead is the worker's rule for every line cut; not changed.
- On words that heard the name itself, the joined pin grounds fuzzily on the tail and opens
  where prod did; the creator's quote would have grounded at "Don" (still not at "Parella").
- iPhone card (the option description is additive and already rendered by the app), the
  phone render, the length question (KRI-966) and the "meet"/"meat" caption are not checked.
  Re-send T1 from the kit prompt on the iPhone after deploy: expect the recommended
  "So, what do you think?" and an `opening_cut` receipt near 14.85 s with that `spoken`.
