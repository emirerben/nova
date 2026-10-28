# KRI-1015 mixed-language caption fixtures

Real inputs behind `tests/tasks/test_kri1015_mixed_caption_language_replay.py`. No audio or
video is stored here. Incident: Plaza T2 "maximo 2" (2026-10-28), job `e46baf42`, a 48.0 s
mixed English/Turkish take on the phone "subtitled" (Talking) path; the creator asked to keep
every language as spoken and prod captioned the English in Turkish.

| File | What it is | Source |
| --- | --- | --- |
| `incident.json` | `dispatched_request` (the text the dispatcher parsed, verbatim), `prompt_txt` (the kit `PROMPT.txt`, verbatim), `stamped_in_prod` (`caption_language_request` on the job row: `"tr"`), `r2` (the brief requirement the creator's sentence became: kind, facts, description), `original_duration_s`, `removed` (the 13 speech-cleanup removals prod applied, source time), `prod_variant` (prod's `caption_language: "tr"` and its 12 Turkish `caption_cues`, output time) | `job_debug.json` of the live run `T2_translate_1810_mixed_language_talking/runs/live_2026-10-28`: `job.all_candidates`, `job.assembly_plan.creator_brief_binding`, `job.assembly_plan.variants[0].silence_cut` / `.caption_cues` |
| `spoken_words.json` | the preflight snapshot's spoken words in ORIGINAL clip time (41 words, English for 0-26 s, Turkish text from 31.7 s). This is what captions use when no language is requested | same file, `variants[0].overlay_transcript`; prod's whisper-1 output, as the worker persisted it |
| `truth_words_tr.json` | local whisper pass over the same clip forced to Turkish. It translated the English into Turkish ("Tamam, Türkçe'dir.", "Evet."), the failure shape. The replay uses it as the stubbed tr-hinted whisper pass | `truth/source_words_tr.csv` of the same run: local whisper.cpp large-v3-turbo, `-l tr` |
| `truth_words_en.json` | the same clip forced to English: the English stays English, the creator's Turkish is written in Turkish. The independent expectation source for the English stretches | `truth/source_words_en.csv`: same tool, `-l en` |

Word times in the two `truth_*` files are whisper.cpp's: coarse (quantised to about 2 s in
places, with zero-length words, ms in the CSV converted to seconds here). Empty rows were dropped.
They are used for the words and their order, not for timing.

Local whisper large-v3-turbo differs from prod whisper-1. The truth passes show the failure
shape (English rewritten in Turkish under a Turkish hint), not prod's exact words; prod's own
Turkish cues are in `incident.json`.

What is authored, not recorded:

- The preflight analysis language is `"en"`: the job's own `caption_language_requested` event
  recorded `spoken: "en"` (`job.pipeline_trace`), and the run's `repro_output.txt` merge gives
  `en`; the persisted snapshot row (`speech_cleanup_analyses` b06acb2b) was not read.
- The cleanup plan is rebuilt by the real `analyze_speech_cleanup` with the 13 recorded
  removals fed as pre-approved removals (no silence detection run), then checked to equal the
  recorded regions to the millisecond (`test_o_the_recorded_cuts_replay_exactly...`).
- The brief's start/end ask (r1) is not replayed: its opening cut was a no-op at 0.0 s and its
  end line ("country") is not in the clip (the take ends on "...of the whole").

Not covered here (separate tickets): the Turkish text prod wrote for 31.7-47 s where the guide
spoke English (KRI-1016), the cuts that clipped words (KRI-1017), the invisible "1810" (KRI-1018).
