# KRI-838 caption language: offline evidence

Offline verification of a local fix. It does not establish the original café T2 or
Don Julio T3 request on an iPhone export; parent KRI-829 keeps that gap.

## Requests and expected outcome

| Kit case | Creator request | Speech | Expected |
| --- | --- | --- | --- |
| Café T2 (2026-10-16 kit) | "İngilizce konuşuyor, altyazılar Türkçe olsun." | Caro, English | Turkish captions, review "met" only for Turkish text |
| Don Julio T3 (2026-10-21 kit) | "keep his English voice, but make the subtitles Spanish." | Lucas, Spanish (the kit's "English voice" is a forced-English translation, see KRI-808) | Spanish captions, or an honest "can't" before approval |

Both prompts are verbatim in `src/apps/api/tests/fixtures/kri838_caption_language/prompts.json`.

## Root cause (traced on main 004f38182, re-checked after KRI-839)

1. `brief_checks` knew 9 caption languages and promised "they'll be in Spanish, and I'll
   check" before approval. The dispatcher parser (`parse_caption_language_request`) and
   `coerce_caption_language_request` knew only en/tr, so the Spanish ask never reached the
   worker, and the captions came out in the spoken language.
2. For a language the worker did know, "translation" was one whisper pass with the request
   as its language hint. The variant was stamped with the request whatever text came back,
   and `_check_caption_language` / `_check_caption_words` read only that label.

## Independent evidence

**A forced-language whisper pass is not a translator.** We ran local whisper.cpp 1.8.4 with
`ggml-medium.bin` (beam 5, temperature 0) on the kit clips. This is free, and it is an
approximation of prod's whisper-1, not the same model:

- Caro, auto: English (p=0.86).
- Caro, forced `-l tr`: the English text came back almost verbatim. This is the shape of
  the bug. Fixture: `caro_t2_forced_tr_words.json`.
- Caro, forced `-l es`: a loose Spanish rendering ("desierto" for dessert).
- Lucas, auto: a Finnish-looking hallucination (p=0.42). The short English question at the
  start derails whole-clip detection. Fixture: `lucas_t3_auto_words.json`.
- Lucas, forced `-l es`: correct Spanish. For him, Spanish captions are transcription, not
  translation. Fixture: `lucas_t3_forced_es_words.json`.

**The language-ID agrees with an independent detector.** Nova's deterministic
`detect_text_language` was compared with lingua-language-detector (offline, restricted to the
9 table languages). The reviewer wrote 121 caption sets of 1–6 lines, covering ASCII-only
Turkish, es/pt/it overlap, all-caps, names, and EN/ES code-switching:

- Nova: 102 identified, 0 wrong, 19 "unknown". "Unknown" means no evidence, never a failure.
- On the 65 sets where lingua was ≥ 0.9 confident, Nova decided 60 and agreed on all 60.
- Names-heavy one-line sets were the weak spot. Sentence-initial names now carry no
  character evidence.

## Change

- **One shared language table**, the 9 languages in `pipeline/caption_language.py`, read by
  the dispatcher, `brief_checks` and the worker.
- **The parser needs a binding.** It counts a language only when it is bound to the caption
  word: "Spanish food with subtitles", "Lucas speaks Spanish so add subtitles" and "Turkish
  coffee tutorial with subtitles" are not asks.
- **`ensure_caption_language`** (`pipeline/caption_translate.py`) checks the hinted words
  before anything is stamped:
  - Text confirmed in the request: kept, no model call.
  - Otherwise: one structured translation call. The creator's spellings stay verbatim, and
    the words are spread over each sentence's original span. The result is checked again.
  - Any failure: the original words, stamped with the language they really are, never the
    request, and `caption_language_request_unmet` is recorded.
  - Minority-language sentences inside a confirmed set are translated on their own.
- **Kill switch.** `CAPTION_TRANSLATION_ENABLED`, default on. Off means the legacy en/tr world
  at render time.
- **The review reads the caption text before the label.** The pre-approval line promises only
  what `can_deliver_caption_language` allows. Arabic gets an honest "can't" until right-to-left
  rendering is verified.

## Checks

Offline replay: `src/apps/api/tests/tasks/test_kri838_caption_language_replay.py` drives
the real dispatcher parser, the real phone Talking job (both cleanup branches), the
Voiceover caption helper, the cue builder and the brief checks. Whisper and the translator
are stubbed from the fixtures above. The Turkish translator answers are authored stubs,
not recorded model output.

| Case | Result with fix | On main |
| --- | --- | --- |
| Café T2, Turkish asked, English heard, translator answers | Turkish cues, stamped `tr`, review met | English cues stamped `tr`, review met |
| Café T2, translator off, down, or hands back English (wrong-content control) | English cues stamped `en`, `caption_language_request_unmet`, review partial "English, not Turkish" | stamped `tr`, met |
| English text under a `tr` label (any producer) | review partial | met |
| Don Julio T3 prompt | parses `es`; hinted Spanish confirmed, no translator call, stamped `es`, met | parse `None`, Spanish never hinted |
| Spanish asked on English speech | translated, `es` | dropped |
| Draft line, translation on / off | promises Spanish / "I can't make Spanish captions yet" (EN+TR); en/tr still promised | promised Spanish in both |
| Ambiguous short text and a failed translator | stamped with the independent spoken language, or "" ("can't check"), never the request | stamped with the request |

Counts and the red-on-main run are recorded in the PR body for its head SHA.

## Remaining gaps (owners)

- **iPhone re-run** of café T2 and Don Julio T3 (KRI-838, Yasin). Read the frames for the
  caption language.
- **Spoken-language detection** of mixed takes is KRI-843. A misdetected spoken pass still
  feeds the hinted pass.
- **Editor and web.** The editor's re-transcribe route and the web `CaptionLanguage` type are
  still en/tr only: an `es` variant shows "Captions in es" and a zero-cue re-transcribe of
  `es` is refused.
- **No translation cache.** A regenerate translates again (about $0.005, only on renders that
  ask for a language). The translator is not an AgentSpec and has no usage ledger, the same as
  `caption_correct`.
- **Multi-clip Talking.** The variant stamp follows the main speaker.
