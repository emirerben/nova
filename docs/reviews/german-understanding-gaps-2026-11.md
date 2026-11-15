# German understanding: Turkish test → German test (KRI-1453, 2026-11-14)

Step 4 of the German/Turkish app plan. For every behaviour that has a test proving Kria
understands a Turkish request, this table names the German test and whether German passes.

Rules applied (language policy, `agents/DECISIONS.md` 2026-10-21):

- **Fixed** only where the code matches a closed list: choice-answer phrases, a seconds unit,
  number words beside a unit, filler sounds, the chat-language detector's German words.
- **Model path** where the Turkish rule guesses intent from free words (a regex twin per
  language). German gets no new twin; the model's structured field is the German reader, and
  the rule stays a strict xfail in `tests/parity/test_decision_parity_de.py` (closing one makes
  it XPASS and fail, so the list stays honest).
- **Web only**: code only the frozen web route runs (`routes/creator_agent`); out of Kria scope.

All checks are offline. No paid model call was made.

## Table

| Behaviour | Turkish test | German test | German today |
|---|---|---|---|
| Rule-based decision points (115 parity cases) | `tests/parity/test_decision_parity.py` | `tests/parity/test_decision_parity_de.py` | 62 pass, 53 strict-xfail gaps (rows below); 4 closed here (D10, D11, D38, D55) |
| Model reads the chat ask (32 EN/TR pairs: pacing, music off, photo timing, sound effects, speed, delete, undo, clip exclude/all/order, length, captions, caption language, titles, text position/animation/font/colour, brightness, quote vs voiceover, status, redo, memory, slides) | `tests/evals/parity/test_model_parity.py` (EN/TR cassettes) | `tests/evals/parity/test_model_parity_de.py` + `de` in `tests/fixtures/parity_pairs/pairs.json` | Dry run passes for all 32: German input validates, reaches the prompt, reply-language agents are told to answer in German, canned answer parses. Replay skipped: no German cassette (recording = paid live run, not approved) |
| Choice card: ordinals ("die zweite", "Nummer 2", "Nr. 2") | `tests/services/test_kri520_choice_turkish.py::test_turkish_ordinals_pick_the_numbered_option` | `tests/parity/test_german_understanding.py::test_german_ordinals_pick_the_numbered_option` | **Fixed** (`choice_questions._german_ordinal_names`, read at match time) |
| Choice card: unclear numbers stay unanswered ("zwei", "egal") | `…::test_unclear_turkish_numbers_are_not_answers` | `…::test_unclear_german_numbers_are_not_answers` | Pass |
| Choice card: decline the title ("kein Titel", "nein", "überspringen") | `…::test_turkish_no_title_replies_decline_the_title` | `…::test_german_no_title_replies_decline_the_title` | **Fixed** (`choice_questions._GERMAN_KEY_ALIASES`) |
| Creative-copy question: skip, but bare "no" is not a skip | `…::test_creative_copy_skip_accepts_turkish_declines_everywhere` | `…::test_german_copy_skip_but_not_a_bare_no` | **Fixed** (`choice_questions._GERMAN_KEY_ALIASES["cancel"]`, no bare "nein") |
| Choice card: hand the choice over ("du entscheidest") | `…::test_turkish_delegation_picks_the_recommended_option`, parity D10/D11 | `…::test_german_delegation_picks_the_recommended_option`, parity D10/D11 | **Fixed** (`_DELEGATION_PHRASES`) |
| Copilot: typed seconds answer ("10 Sekunden", "2,5 Sekunden") | `tests/test_kri520_copilot_turkish.py::test_duration_answer_reads_turkish_units` | `…::test_duration_answer_reads_german_seconds` | **Fixed** (`edit_copilot._DURATION_ANSWER_RE` unit) |
| Copilot: vague length, pronouns, negation, media-kind words, stacking | `tests/test_kri520_copilot_turkish.py` (regex twins) | none | Model path (would be new intent regexes) |
| Title hold in number words ("zwei Sekunden") | `tests/kria/test_title_hold_grounding.py`, parity D55 | `…::test_a_german_title_hold_is_the_creators_own_words`, parity D55 | **Fixed** (`planner._DE_HOLD_WORDS`, German unit) |
| Seconds as a number word, web route (DC1-1) | parity DC1-1 | parity DC1-1 | Web only (gap kept) |
| Speech cleanup: filler sounds ("äh", "ähm", "öhm") | `tests/pipeline/test_silence_cut.py`, parity D38 | `…::test_german_hesitations_are_fillers`, parity D38 | **Fixed** (`silence_cut._FILLER_LEXICON`) |
| Hook score filler penalty (cloud scorer) | parity D40 | parity D40 | Gap kept (cloud-only scorer) |
| Caption-language check of the speech (cleanup / captions cross-check) | `tests/pipeline/test_caption_language_id.py` | `…::test_german_speech_is_german_never_turkish` (+ German rows already in `test_caption_language_id.py`, `test_kri1192_caption_languages.py`, `test_kri843_mixed_language_transcript.py`) | Pass, no fix needed |
| Chat reply language from the creator's words | `tests/kria/test_kri520_reply_language.py`, `test_kri1363_chat_language_own_words.py` | `…::test_german_messages_read_as_german`, `test_model_parity_de.py::test_german_messages_read_as_german` | **Fixed** two bugs (German switch on): "Lösch alle Beschriftungen" read as Turkish; "Ordne die Clips in der Reihenfolge, in der ich sie gefilmt habe." read as English (`reply_language._DE_WORDS`) |
| Status / help questions (D01, D02, DC1-6, D03, DC1-15) | parity | parity | Model path |
| Paraphrase-only reply (D04) | parity | parity | Model path |
| Media scope "all the videos", "every clip" (D12, DC1-17) | parity, `tests/schemas/test_kri520_turkish_parsers.py` | parity | Model path |
| Keep clip order (D13) | parity | parity | Model path |
| Named / generic sound effect (D14, DC1-2, DC1-9) | parity | parity | Model path |
| Opening / closing title extraction (D15, D16, DC1-12) | parity | parity | Web only (`_apply_explicit_render_intent`) |
| Pace words (D17, DC1-16) | parity | parity | Model path |
| Photo timing, round-robin cadence, reuse, total length, image layout (D18–D25, DC1-3, DC1-5) | parity, `tests/schemas/test_kri520_turkish_parsers.py` | parity | Model path |
| Text edit "replace X with Y" (DC1-13) | parity | parity | Model path |
| Exclusive quoted caption (D26, C2-15) | parity | parity | Model path |
| Reported speech quote (D27), use my speech (D29) | parity | parity | Model path |
| Durable memory / temporary style (D30–D32, D34, D51–D53) | parity | parity | Model path (memory extractor pair `mem_always_font`) |
| Named cue: font / timing (D35, C2-04) | parity | parity | Model path |
| Caption language ask ("Englische Untertitel", D36, D37, D37b) | parity, `tests/pipeline/test_caption_language.py` | parity | Model path (brief extractor pair `be_caption_language`) |
| Ungrounded caption claims, refusal text (C2-02, C2-05, C2-07) | parity | parity | Model path |
| Score words (C2-03, C2-16), score treatment (C2-11) | parity | parity | Model path |
| Remove one named clip (D54) | parity, `tests/kria/test_kri520_brief_routing.py` | parity | Model path |

## Follow-ups

- Record German model cassettes (`<id>.de.json`) for the 32 pairs: a paid live run under the
  ledger cap (`--eval-mode=live`, ≤ $2). Needs Yasin's approval of the spend. Until then the
  German model layer is checked for wiring only, not for the decision the model makes.
- Only the detector words (`reply_language`) wait for `KRIA_GERMAN_REPLIES_ENABLED` (on in prod
  since 2026-11-11). The choice answers, seconds unit, number words and filler sounds work in
  every chat language, like their Turkish rows.
- German choice answers are matched when a reply arrives and are not written into the stored
  question, so English and Turkish question payloads stay byte-identical
  (`tests/kria/test_copy_routing.py`).
