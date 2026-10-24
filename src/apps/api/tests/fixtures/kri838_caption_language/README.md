# KRI-838 caption-language fixtures

Real inputs behind `tests/tasks/test_kri838_caption_language_replay.py` and
`tests/pipeline/test_caption_language_id.py`. No audio or video is stored here.

| File | What it is | Source |
| --- | --- | --- |
| `caro_t2_auto_en_words.json` | Caro (café T2 clip, 24.6 s), whisper auto-detect: English | local whisper.cpp 1.8.4, `ggml-medium.bin`, beam 5 (not the prod whisper-1) |
| `caro_t2_forced_tr_words.json` | the same clip with whisper **forced to Turkish**: still English text (a language hint does not reliably translate) - the root of the bug | same |
| `lucas_t3_forced_es_words.json` | Lucas (Don Julio T3 clip, 12.9 s), whisper forced to Spanish: correct Spanish | same |
| `lucas_t3_auto_words.json` | the same clip on auto-detect: a Finnish-looking hallucination (language-ID must say "unknown") | same |
| `prompts.json` | the two real kit prompts (café T2 Turkish, Don Julio T3 English), verbatim | Kria test kits 2026-10-16 / 2026-10-21 |

Word timestamps from whisper.cpp are coarse (Lucas's are quantised to about 1 s, with some
zero-length words); the tests keep them as they are.

Any Turkish rendering used as a stubbed translator answer lives in the test file and is an
authored stub, not recorded model output. Local whisper output differs from prod whisper-1;
the fixtures show the failure shape (English under a Turkish hint), not prod's exact words.
