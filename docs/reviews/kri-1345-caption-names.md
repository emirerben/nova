# KRI-1345 — captions spell the creator's names (offline evidence)

Parent: KRI-1342 (yorum3, 2026-11-09). Branch `codex/caption-names`. Evidence level: **offline
replay** (real whisper-1 prod hearings + local whisper.cpp large-v3-turbo hearings; no model
calls, no device export).

## Incident

Prompt typed "Vela" twice and "Lumo". whisper-1 and Gemini both heard "Vila"; the burned captions
read "vila posted on instagram…", "vila turned lumo's big launch…". The `captions/creator_spellings`
trace had targets `["Yorumla"]` only: `creator_request_names` keeps 5+ letter names, so 4-letter
names were never respelled. Narrated captions never got typed names at all; photo text, photo
brand labels and file names were never a source.

## Fix (flag `CAPTION_NAME_VOCABULARY_ENABLED`, default off)

`app/pipeline/caption_names.py`, after the creator spellings in the phone Talking and Narrated
writers: evidence-gated names (4-letter typed names; photo/video on-screen text, photo brand
labels, asset notes, file names). A heard word changes only when the take writes it as a name
mid-sentence (whisper or Gemini), it is not part of another capitalised name, it sounds like
exactly one name, and the creator did not type it. Word for word, timings unchanged.

## Corpus replay (65 hearings: 26 whisper-1 prod jobs, 39 whisper.cpp; 44 EN, 20 TR, 1 DE)

| Check | Before | After |
|---|---|---|
| yorum3 prod job 7eba9903: "Vela" in captions | 0 of 2 ("Vila") | 2 of 2 |
| Çiğdem S1 take, whisper.cpp: "Vela" | 0 of 2 | 2 of 2 |
| Ecuador N1 TR voiceover, whisper.cpp: "Caro" | 0 of 2 ("Karo") | 2 of 2 |
| Kervan (6 hearings "Curvan"), Lumo, Pamukkale, Kadıköy | right (5+ letter rule, already on) | unchanged |
| Words changed that were not a misheard typed name | — | **0** (65 hearings) |
| Every 4-letter typed name of every prompt × every hearing (1,300 pairs) | — | 0 false changes |
| 129 capitalised prompt words as "photo text" × every hearing (no typed-word guard) | — | 0 false changes (5 changes, all real mishearings: Vila, Karo, Carolina→Karolina, Trotsard→Trossard) |
| 234 real kit upload file names as names × every hearing | — | 0 changes |
| Word / cue timings | — | identical in every run (asserted) |

Still wrong after the fix (not safely fixable by a sound rule; left as they were):
"Dennis" for Deniz (only at a sentence start, no name evidence), "Maripi" for Merope (3 sounds
off), "katakanın" for Atakan'ın (word boundary), "NARS Lipton" for Nar's lip tint (3-letter
name). Not covered: cloud writers (`_render_subtitled_variant`, `_render_narrated_variant`,
retranscribe), the post-render caption check and the reaction-beat trigger match (5-letter rule).

Review fix (Emir, on 93e22977e): a two-word name that opens a sentence ("Mila Kunis starred.
Later Mila returned.", "Aston Villa scored. Later Villa won.") now blocks the heard word in every
position. A capitalised sentence opener counts as half of a name unless the hearings also write it
in lower case or it is a common EN/TR opener ("Then", "Yesterday", "Dün"). The corpus results
above were re-run after this fix and are unchanged.

Guard: `src/apps/api/tests/pipeline/test_kri1345_caption_names.py` (yorum3's real hearing through
`_phone_talking_caption_text`; flag off reproduces "Vila"). Replay scripts and corpus stay outside
git (session notes `caption-names-replay/`): they hold full transcripts of test takes.
