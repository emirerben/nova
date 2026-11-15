# Çiğdem S2 "ss2b" fixes — failure modes (written before the code)

Run: 2026-11-08 20:51–20:58Z, thread `B562466E-1B84-449C-A1E8-82318300A301`, job
`f4f81c07-13ec-4944-b0d8-066151ae6737` (device render, client protocol 45). Prompt (Çiğdem's
"Yorumla" green-screen format, English stand-in take, 31.2 s raw):

> Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green screen: me
> small at the bottom right in front, and behind me full screen whichever photo or video I'm
> talking about, changing every 3-5 seconds. Cut the pauses and the sentence I restarted.
> Captions small, white, lowercase, in the middle. No music.

## What the raw records say (not the check script's summary)

| Miss | Evidence | Decision point |
|---|---|---|
| "okay, we're recording." kept at 0.0–1.04 s | `silence_cut.removed` = 4 silences + 1 retake (16.4–20.14 s); the slate line is never a candidate | Speech cleanup has no category for a production slate: it cuts pauses, filler sounds and abandoned takes (`retake_detector` = a restart of the SAME content). `speech_cleanup_analysis.analyze_speech_cleanup`, `generative_build._silence_cut_retake_spans` |
| Instagram collab post (IMG_7127, "kervan × lumo … Shot on Lumo") on "feeds fashion" (~20 s) instead of "Lumo was the show's technology partner" (7.9 s raw) | `turns[0].plan…background_media.sequence` | Main Creator's `spoken_phrase` choice (LLM). The layout put every entry on its phrase correctly. |
| Backgrounds held 5–6.5 s against "every 3-5 seconds" while the receipt said "met" | windows 0–4 / 4–10 / 10–16 / 16–20 / 20–26.5 s; `cadence_s` absent from the strategy; r1 "5 photos or videos take turns behind you" | 5 photos for 26.5 s and the decided no-repeat rule (S57–S62, "a longer scene beats a repeat") make 3–5 s impossible. The bug is the silent "met": the planner drops the asked rhythm when every entry has a phrase, and the receipt never compares holds to it. |
| Caption line 68 px OCR (her reels 40–52 px) | `caption_look_receipt.size_px` 52 for "small" | `caption_look_ask.CAPTION_ASK_SIZE_PX["small"] = 52` (default 78). OCR line box ≈ 1.3 × font px. |
| (S4) backgrounds stop after ~30 s | — | Already fixed on main: `CHAT_EVIDENCE_SPEECH_BUDGET_CHARS` (a5353685f). No change here. |

Not misses (kit corrections, outside this repo): IMG_7125.MP4 is a runway clip on the Pamukkale
terraces, not a drone shot, so "Pamukkale" is the right word for it (answer-key keyword "drones"
was wrong); the kit's full-screen detector does not recognise a background behind a cut-out
speaker, so it scored 2/5 where frames show 4/5 on their words.

## A. Production slate at the start of a take ("okay, we're recording")

Fix: a deterministic, closed-list detector for the take's first utterance, fed into the same
retake-span path (same snapping, voice-guard exemption, receipts). Flag
`SPEECH_CLEANUP_SLATE_CUT_ENABLED`, default off.

1. Cuts a real opening that contains a slate word ("We're recording our first album today") →
   the WHOLE first utterance must equal a slate phrase after stripping leading/trailing fillers;
   a longer utterance never matches.
2. Cuts a real hook that is short and generic ("Ready?", "Here we go.", "Action!", "Let's go") →
   those are not on the list; only recording/rolling/countdown/mic-check talk is.
3. The transcript glues the slate to the first real sentence without a pause or full stop →
   the utterance ends only at sentence-final punctuation or a ≥ 0.25 s gap; no boundary within
   the first 6 words = no cut.
4. A slate in the middle of the take ("okay we're recording again") → only an utterance that
   starts at word 0 (after optional fillers) is considered.
5. The take is nothing but the slate, or almost → at least 3 words must remain after it.
6. Turkish casing/diacritics ("Tamam, KAYITTAYIZ.", "kayıt başladı") → Turkish-aware casefold
   (İ→i, I→ı) and punctuation strip before comparing; list carries EN + TR forms.
7. Slate longer than a slate (a mumbled 5 s preamble) → utterance must end within 3.0 s.
8. Same span also flagged by the LLM retake detector → spans merge in `_merge_removals`; no
   double count.
9. Voice guard drops the cut because the slate is voiced → passed as a retake span, which the
   guard exempts (`voice_guard_exempt_spans`).
10. Flag off → no span added; plans and receipts byte-identical.
11. Several talking clips → each clip's analysis sees its own word 0; a slate at the start of
    clip B is cut too (same rule per take).
12. Malformed / empty words → no span, never an exception.
13. (Review) "Okay." transcribed as its own sentence before the slate → a filler-only utterance
    does not end the search; "Okay. We're recording." is still cut, "Okay. Kervan…" is not.
14. (Review) Retake switches off (`RETAKE_CUT_ENABLED` / `SPEECH_CLEANUP_RETAKES_ENABLED`) → the
    slate is a retake-kind cut, so it stays behind the same consent fence; nothing is cut.

## B. Photo on the wrong sentence (planner choice)

Fix: one rule in the Main Creator's background-sequence section: anchor each photo/video on the
sentence that is about what it shows (its people, product, place, names, on-screen text such as
a collab post's account names); never move one to a later or closing line to fill time — a
longer hold of the previous one is fine. Prompt fingerprint bumps automatically.

1. The rule makes the planner drop photos it can't place → the existing "leave out only what is
   never talked about" rule stays; nothing new is left out.
2. The rule fights the order rule ("list in the order spoken") → it only says which sentence,
   the order still follows the talk.
3. Free replay evals can't prove the semantic change (recorded responses) → proven only by a
   live run (paid, needs Yasin's OK) or the post-merge phone run; stated as pending.

## C. Asked rhythm vs real holds (honest receipt)

Fix: the planner keeps `cadence_s` when the creator names a rhythm even with spoken phrases; the
worker records the longest background window (output seconds) and the asked cadence on the lane
receipt; the finished-video receipt says, in EN/TR, when a photo stays clearly longer than asked
and why (too few photos, none repeats), status "partial".

1. No rhythm asked → no `cadence_s` → no sentence (default 4 s is not an ask).
2. A small overshoot from word alignment (4.6 s vs 4 s) → only flagged above cadence + 1 s
   ("3-5 s" = 4 → over 5 s).
3. The last window holds to the end after the final mention → counts (that is what the viewer
   sees).
4. Longest measured in source time across a speech cut → measured in OUTPUT (kept) time from the
   layout.
5. A render recorded before this change (no keys) → no claim either way.
6. A colour fill or a single photo → the existing "never changes" branches win first.
7. Draft (before render) → windows unknown; no claim.

## D. "Small" captions

Fix: `CAPTION_ASK_SIZE_PX["small"]` 52 → 42 (her reels: 40–52 px OCR line ≈ 31–40 px font; the
readable floor is 40 px). Default and "large" unchanged.

1. Below the readable floor → 42 ≥ 40.
2. A size test pinned to 52 → updated with the reason.
3. Older iPhone builds → they draw the px the server sends; no protocol change.

## Checks

- Unit tests per failure mode above (A, C), negative controls: a real hook containing
  "recording", a mid-take slate, a slate glued to the next sentence.
- Offline replay of this thread's words through the cleanup plan (slate cut at 0–1.23 s) and of
  its strategy through `lay_out` + receipt (rhythm sentence appears).
- `ruff`, focused pytest, `make verify-kria`, `bash scripts/preship-check.sh`.
- Pending, needs a phone run after merge (and Yasin's OK for any paid run): planner places the
  collab post on the partnership sentence; S4 Turkish 2-minute run.

# Çiğdem S2 "Cigdem 2" fixes — failure modes (written before the code)

Run: 2026-11-15 17:59–18:07Z, thread `9ab37a68-baf2-4037-be07-8950e4e91fdd`, job
`ef12ddfd-11b7-4ac3-9efa-69283f0ddced` (build 1997, protocol 51, server `e74148c6c`). Same prompt
and take as ss2b. Linear: KRI-1493 (parent), KRI-1494, KRI-1492, KRI-1497, KRI-1498.

## What the raw records say

| Miss | Evidence | Decision point |
|---|---|---|
| Drone photo held 10.4–25.5 s (15.1 s) against "every 3-5 seconds", and neither the draft nor the review said so | stored `background_media` has no `cadence_s`; lane receipt has no `cadence_s`/`longest_s`; the brief recorded `background_cadence_min_s: 3`, `background_cadence_max_s: 5` | The planner is taught "3-5 seconds" = 4, which equals `DEFAULT_BACKGROUND_CADENCE_S`; `BackgroundMedia._omit_default_start` drops a 4.0 from the dump (proven: `cadence_s=4.0` dumps without it, `3.0` keeps it). So #2230's honest rhythm sentence (C) never fires for this exact ask. Even when it fires, S57 (never repeat) leaves no way to keep the pace with 4 pictures for 25 s. |
| One 0.6 s pause at 22.8–23.4 s (output) | source silencedetect span 28.52–29.14 s; whisper stamped `So` 28.58–29.32 s (the pause sits in the token's HEAD); `story.` ends 28.58 s; stamped gap 0.00 s | Rule 0 carves only interior spans and (KRI-236) a sentence-final token's TAIL. A pause whisper stamps onto the HEAD of the next sentence's first token is invisible to rule 3 (stamped gap < `MAX_PAUSE_S`). |
| `editor_playback_failed` for 3 jobs at 18:07:05/07Z | `-11819` then `-11800` with underlying `561210739` (`'!ses'`), 8 s after the device render completed; the creator saw nothing wrong | Not a playback bug: `NativeEditorSession.handleMediaServicesReset` (KRI-626) already rebuilds every player. The server logs these as warnings named "failed". |
| (not a miss) "review never says what cleanup cut" (KRI-1498) | the `assistant_review` content ends "Speech cleanup cut what it heard as 2 long pauses and 2 restarted sentences (5.8 s)." | Filed in error from the receipt's `reason` field alone; the review text keeps the draft's promise. Closed, no code change. |

## E. Keep the pace the creator named (background returns)

Fix: (1) `BackgroundMedia.cadence_s` is `None` unless the creator named a pace, so an asked 4 s
survives; (2) behind `BACKGROUND_KEEPS_ASKED_PACE_ENABLED` (default on), when a pace was named the
layout keeps every window within `cadence_s + 1 s` (the receipt's own slack): a small overshoot
pulls the next window in, a long hold after a picture's own sentence is filled by pictures already
shown. No named pace: S57 unchanged (a longer scene beats a repeat).

1. A strategy stored before the change (no `cadence_s`) → `None`, same as before: no pace, no
   returns, no rhythm sentence.
2. The planner writes `cadence_s` for an unasked pace → it is taught to set it only for a named
   rhythm; if it does anyway, pictures return at a 4 s pace (no wrong content, only earlier
   pictures). Not provable offline (paid replay); noted as a residual risk.
3. A return shows a picture before its own word (spoils the reveal) → only pictures whose first
   window already started can return.
4. A return lands while the creator still talks about the current picture → the current picture
   keeps its window to the end of the sentence that names it (and at least `cadence_s`); returns
   start only after that.
5. The same picture twice in a row (A, A) → each return differs from the window before it; with
   only one other picture they alternate.
6. A return flashes (a sliver) → the leftover is split into equal pieces of at most `cadence_s`,
   each at least 1 s (a long hold is over `cadence_s + 1 s`, so the leftover is over 1 s).
7. The first picture is too long and nothing was shown before it → no return is possible: the next
   window is pulled in by up to 1 s when that brings it within the pace; otherwise the hold stays
   and the reply says so truthfully (new wording: nothing else had come up yet).
8. Pulling the next window in moves it before its word by more than 1 s → capped at 1 s, and never
   before the previous window's own `cadence_s`.
9. Phone limits (S62): over 40 windows, under 0.5 s, gaps, overlaps → windows stay sorted,
   disjoint, cover every second they covered before, at most 40 (past the budget a hold stays
   long and the reply says so).
10. A full-screen opening (item 16), list labels (item 19), `unshown`/`left_out` receipts → returns
   only split windows after the opening; a label anchors on an entry's FIRST window (unchanged);
   receipts count distinct media (KRI-1177), so returns do not inflate "N photos".
11. A video returns → it plays from its own `source_start_s` again (a loop, as the phone already
   draws a video window).
12. The editor's `set_background_sequence` rotation → not passed the flag: byte-identical.
13. Flag off → layout byte-identical to today; only fix (1) remains, which makes #2230's sentence
   fire for "3-5 seconds" as intended.
14. The rhythm sentence says "none repeats" while pictures do return → with the flag on, the
   sentence names the real reason for a remaining long hold (nothing else had come up yet).

## F. A pause stamped onto the next sentence's first word (rule 0 head trim)

Fix: the mirror of KRI-236's tail trim, V2 only (`_sentence_initial_head_carve`, kind
`trim_sentence_head`): the token follows a sentence-final token (`. ? !`, no ellipsis); exactly ONE
long (>= 0.6 s) span overlaps it by >= 0.3 s; that span covers the token's whole stamped head and
starts within `SENTENCE_TAIL_REACH_S` of the previous token's end (no unexplained sound between);
the token keeps >= `SENTENCE_HEAD_MIN_VOICED_S` (0.15 s) after the span. The token starts at the
span end; a previous token whose stamped end runs into the span by less than the overlap minimum
ends at the span start. `DETECTOR_VERSION` -> `mixed-gap-v5`.

1. A quiet first syllable under the -30 dB floor is cut → rule 3 keeps `KEPT_GAP_S/2` (125 ms) of
   the span before the voice, as for the tail trim.
2. The previous word is not sentence-final ("and … so") → refused: a mid-sentence pause is a
   hesitation the existing rules judge.
3. Sound between the previous word and the span (a breath, a trailing syllable) → the reach check
   refuses it.
4. The token is mostly silence (a quiet word) → refused unless >= 0.15 s of it is outside the span.
5. Several quiet patches in the token → refused (one span only), as for the tail trim.
6. The span also overlaps the previous token's tail by >= 0.3 s → the tail rule handles it; the
   head rule does not run twice on one pause.
7. A first token of the take (no previous word) → refused.
8. The cut moves a caption or a retake span → `retake_spans` index the original list (unchanged);
   captions are re-timed from the reconciled words like every carve.
9. Existing goldens (KRI-236 tail, "a..." split, the seeded property test) → unchanged; a negative
   control with a voiced head (span ends in the token's middle with real voice after) stays whole.

## G. Media-services reset reported as a playback failure

Fix: `/me/jobs/{id}/playback-diagnostics` logs a report whose error is the media-services reset
(AVFoundation `-11819`, or `561210739` `'!ses'` as code or underlying code) as
`editor_playback_reset` at info level; every other report stays `editor_playback_failed` warning.

1. A real failure that happens to carry `!ses` for another reason → it is the reset's own
   signature (KRI-626 treats it the same way on the phone); still logged, only renamed.
2. A report without error codes → unchanged warning.
3. A reset that the phone does NOT recover from → the phone reports the next failure of the new
   player normally (a different item, new report).

## Checks

- E: the real worker over the Cigdem 2 strategy (4 phrased pictures, `cadence_s` 4 through the
  real repair) and the kit's ground-truth words: every picture on its word, no window over 5 s,
  no return before its picture's first window, the drone sentence intact, receipt "met";
  negative controls: no pace named (S57 unchanged), flag off (byte-identical layout).
- F: golden case built from this take's stamped words and silencedetect spans (the cut removes
  only inside 28.52–29.14 s and leaves <= 0.4 s); negative controls 2-5.
- G: route test for both log events.
- Pending: the S2 rerun on the phone after merge (`tools/check_result.sh … S2`: every line PASS).
