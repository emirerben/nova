# KRI-1192 spoken captions in ten more languages

Goal: when a creator speaks Portuguese, Spanish, Greek, German, Korean, Italian, French,
Arabic, Danish or Serbian, the captions come out in that language automatically, in every
iPhone format that shows spoken captions, and can be edited on the iPhone. English and
Turkish stay exactly as they are.

This file was written in two passes. Sections 1–3 (the map and the failure modes) were
written from the code on main 8331ed910 and a free local measurement, BEFORE any code
changed. Section 4 is the evidence for the change.

## 1. The caption path, stage by stage (main 8331ed910)

Formats that show spoken captions and where each decides the language:

| Format | Entry point | Language decided at |
| --- | --- | --- |
| Talk to camera, cloud | `generative_build._render_subtitled_variant` | `_resolve_spoken_lang`, then the cross-check re-run |
| Talk to camera / multi-clip Talking, phone | `_run_phone_subtitled_job` | live whisper + the cross-check re-run |
| Voiceover / Narrated, cloud | `_render_narrated_variant` | whisper auto-detect, `_narrated_caption_transcript` |
| Voiceover / Narrated, phone | `_run_phone_narrated_job` | `resolve_spoken_caption_language` |
| Speech montage, phone | `run_phone_speech_montage_job` | no caption layers; its words come from the clip's stored language |

| Stage | Code | Today, for the ten languages | Change |
| --- | --- | --- | --- |
| Whisper language name | `transcribe._LANG_NAME_TO_ISO` | whisper-1 reports full names; `greek`, `danish`, `serbian`, `croatian` are not mapped, so the caption language is stored as `"greek"` and nothing downstream recognises it | derive the map from the language table |
| Cross-check vs Gemini's transcript | `caption_language.crosscheck_detected_language` → `infer_language_from_text` | the text guess knows only EN/TR. On real speech it calls German, Spanish, Portuguese, French and Italian "Turkish" and Danish "English", and the worker re-runs whisper with that hint, which translates (section 2) | the guess reads the whole table; it may only contradict whisper with a clearly different table language |
| Text language-ID | `detect_text_language` | no Greek, Hangul or Cyrillic evidence; no Danish or Serbian words. Real Danish is read as French/German, Serbian as Portuguese/Dutch | table rows + script evidence + Danish/Serbian function words |
| Mixed-language takes | `mixed_language_transcript._LEXICON_WORDS` | no words or letters for el/ko/ar/da/sr, so a Greek+English take looks English-only | script and letter marks for the new languages |
| Caption correction | `caption_correct` | language-neutral prompt with a never-translate rule and a same-tokens guard; gets "natural el" because el is not in the table | table names |
| Punctuation restore | `transcribe.align_punctuated_text` | Unicode-safe, falls back on mismatch | none |
| Spelling / unclear words | `caption_spelling`, `caption_unclear` | `_sound` keeps only `[a-z]`, a safe no-op for other scripts | none |
| Translation (explicit asks only) | `caption_translate.ensure_caption_language` | needs `detect_text_language` to confirm the language | grows with the language-ID |
| Sentence / clause split | `captions._SENTENCE_END_RE`, `_CLAUSE_END_RE` | no Arabic `؟ ، ؛`; Greek `;` is the Greek question mark | add the Arabic marks; Greek `;` only for Greek captions |
| Phone cue budget | `captions.PHONE_CUE_BUDGET` (40 chars), `guided_caption_presentation` (42 chars) | counts characters; a Hangul syllable is about two Latin letters wide | count wide characters double |
| Upper-casing | `text_fold.upper`; iOS `KriaTextCase`; web `applyTextCase` | Greek all-caps keeps the accent (`ΚΑΛΗΜΈΡΑ`) | Greek rule on all three, in the shared parity fixture |
| Fonts | `assets/fonts` (shared by cloud and iPhone) | no bundled font has Hangul or Arabic; Greek only in TikTok Sans, Inter, Inter Tight, Syne, EB Garamond; Serbian Cyrillic in 18 | bundle Noto Sans KR + Noto Sans Arabic as script fallbacks |
| Whole-run font fallback | `font_coverage.glyph_safe_family`; iOS `NativeGlyphFallback` | partner list only knows Turkish gaps; returns the family unchanged for CJK/Arabic; never called on the spoken-caption path | script fallbacks; run it on caption cues (cloud and iPhone) |
| Cloud caption burn | libass, `captions.py` ASS + `narrated_assembler.burn_captions_on_video` | per-letter fallback to DejaVu (mixed faces in one word), Hangul has no fallback (boxes), `Encoding: 1` forces left-to-right paragraphs | one covering face per burn; base direction auto for right-to-left text |
| Phone caption layout | `phone_text_style.drawable_family` | declines Hangul and Arabic | sees the new fallback faces |
| Phone device recipe (karaoke) | `portable_text_layout` legacy glyphs | one glyph per character, no shaping, words placed left to right: breaks Arabic | shaped text runs for right-to-left text |
| iPhone render | `AuthoredTextLayout`, `PortableTextVectorPainter` | CoreText, but any system-font fallback throws; captions skip `NativeGlyphFallback` | captions use the same fallback rule |
| iPhone caption editor | `LineEditorConfiguration.captionLine` | UITextView, natural alignment, picks the caption-language keyboard | none expected; verify |
| Chat promise | `brief_checks._caption_words_plan`, `_CAPTION_DETAIL_RE`, `can_deliver_caption_language` | "Greek captions" is not understood and the generic captions check reports it **met**; Arabic is refused (`_NOT_DELIVERABLE`) | table rows; Arabic deliverable once both renderers pass |
| Editor caption-language chip | `routes/generative_jobs` + worker `_SUBTITLED_CAPTION_LANGUAGES` (en/tr) | web-only re-transcribe chip | out of scope (web) |

### Hard-coded en/tr lists

About 70 places name only English and Turkish. They split into:

- **Caption language (must grow):** the table and its derived sets, `infer_language_from_text`,
  the language-ID word lists, `_LANG_NAME_TO_ISO`, `_CAPTION_DETAIL_RE`, the mixed-language
  lexicon, and the web-only re-transcribe chip gates (left as they are: web).
- **Creator chat language (out of scope):** `SUPPORTED_REPLY_LANGUAGES`, `say(en=, tr=)`, chip
  copy, dictation, `ClipAnswerLanguage`, the planner/brief `== "tr"` branches, intro/quote writers
  keyed by the job language.

## 2. Measured before the change (free, local)

Material: 12 native clips per language from Google FLEURS (CC BY 4.0, test split, distinct
sentences, 5–20 s), plus 12 English and 12 Turkish as controls. Transcribed with whisper.cpp
large-v3-turbo (q5_0), language auto-detect, the free local stand-in for prod's whisper-1.
The FLEURS human transcript stands in for Gemini's independent transcript in the cross-check.

| Language | whisper heard | Today's cross-check re-runs it in | Text language-ID |
| --- | --- | --- | --- |
| pt | pt 12/12 | **tr 8, en 1** | pt 11, unknown 1 |
| es | es 12/12 | **tr 7** | es 9, unknown 3 |
| de | de 12/12 | **tr 6** | de 12 |
| it | it 12/12 | **tr 1, en 1** | it 12 |
| fr | fr 12/12 | **tr 7** | fr 11, unknown 1 |
| da | da 12/12 (stored as "danish") | **en 5** | **fr 1, de 2**, unknown 9 |
| el | el 12/12 (stored as "greek") | none | unknown 12 |
| ko | ko 12/12 | none | unknown 12 |
| ar | ar 12/12 | none | ar 12 |
| sr | **sr 6, hr 6**, all in Latin letters | none | **pt 1, nl 3**, unknown 8 |
| en | en 12/12 | none | en 12 |
| tr | tr 12/12 | none | tr 12 |

A re-run with a wrong hint is a translation: 31 of 60 Romance/German clips (29 into Turkish, 2 into English) and 5 of 12
Danish clips would ship Turkish or English captions. Serbian: whisper writes Latin even
when told `-l sr`, and labels half the speakers Croatian.

## 3. Failure modes (written before the code)

| # | Failure | Languages |
| --- | --- | --- |
| F1 | a clean-up or cross-check step translates the captions into English/Turkish | all; measured for pt/es/de/it/fr/da |
| F2 | an en/tr list drops the language, forces en/tr, or refuses an edit | all |
| F3 | whisper misdetects a close language and writes its spelling | da↔no, sr↔hr/bs, pt↔es |
| F4 | the text language-ID calls real text a WRONG table language and triggers a re-run | da, sr measured |
| F5 | Serbian mixes Cyrillic and Latin in one video | sr |
| F6 | missing glyphs: empty boxes, or one word in two faces | ko, ar everywhere; el, sr-Cyrillic in most faces |
| F7 | Arabic drawn left to right, letters unjoined, end punctuation on the wrong side, highlight sweeping the wrong way | ar |
| F8 | Greek ALL CAPS keeps accents; ΐ/ΰ lose the dialytika | el |
| F9 | long German compounds overflow; Hangul cues too wide for a character budget | de, ko |
| F10 | sentence ends missed (`;` in Greek, `؟` in Arabic): run-on cues | el, ar |
| F11 | iPhone and cloud renders pick different faces | all non-Latin |
| F12 | the iPhone editor breaks right-to-left editing or a saved edit is refused | ar |
| F13 | the chat promises a language it cannot render, or reports an unknown language ask as met | all |
| F14 | a mixed take (Turkish + Greek) loses one language | all |
| F15 | English or Turkish output changes at all | en, tr |
| F16 | an old iPhone build receives captions it cannot draw | ko, ar |

## 4. Evidence

### 4.1 Language decisions on real speech (free, reproducible)

`src/apps/api/scripts/dev/kri1192_fleurs_eval.py` streams the FLEURS clips, runs
whisper.cpp large-v3-turbo with auto-detect, then Nova's real decisions: whisper's language
as whisper-1 reports it (a full name) through `_normalize_lang`, the cross-check against the
FLEURS transcript, and, when the cross-check asks for one, a whisper re-run with that hint
(the text the worker would ship). Caption text goes through the Serbian one-alphabet step,
`build_plain_cues` and `resplit_cues_into_sentences`. Error is jiwer WER (CER for ko, ar)
after Whisper-style normalisation on both sides; Serbian is compared in Latin. lingua
(offline, restricted to the 13 table languages plus Croatian/Bosnian, which it uses for
Latin Serbian) is the independent language check.

**Before (main 8331ed910):** 36 of 144 clips re-run in another language: Portuguese 9,
Spanish 7, German 6, French 7, Italian 2, Danish 5. On those 36 clips the shipped text's
error rate goes from 0.062 to 0.514. Danish comes out in English ("Finland is a fantastic
destination for the Sejlore"), and 9 Romance/German clips come out written in Turkish.
Greek, Danish, Serbian and Croatian were stored as `"greek"`, `"danish"`, `"serbian"`,
`"croatian"`.

**After:**

| Language | Clips | Caption language = spoken | Re-run in another language | Script ok | Text-ID wrong | Raw whisper error | Final caption error | lingua agrees |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pt | 12 | 12 | 0 | 12 | 0 | 0.049 | 0.049 | 11/11 |
| es | 12 | 12 | 0 | 12 | 0 | 0.021 | 0.021 | 9/9 |
| el | 12 | 12 | 0 | 12 | 0 | 0.111 | 0.111 | 12/12 |
| de | 12 | 12 | 0 | 12 | 0 | 0.030 | 0.030 | 12/12 |
| ko | 12 | 12 | 0 | 12 | 0 | 0.060 | 0.060 | 12/12 |
| it | 12 | 12 | 0 | 12 | 0 | 0.019 | 0.019 | 12/12 |
| fr | 12 | 12 | 0 | 12 | 0 | 0.101 | 0.101 | 11/11 |
| ar | 12 | 12 | 0 | 12 | 0 | 0.024 | 0.024 | 12/12 |
| da | 12 | 12 | 0 | 12 | 0 | 0.168 | 0.168 | 12/12 |
| sr | 12 | 12 | 0 | 12 | 0 | 0.167 | 0.167 | 10/10 |
| en | 12 | 12 | 0 | 12 | 0 | 0.059 | 0.059 | 12/12 |
| tr | 12 | 12 | 0 | 12 | 0 | 0.068 | 0.068 | 12/12 |

**Held out.** The Danish and Serbian word lists were tuned while reading the test-split clips
above, so a second set the code never saw was run afterwards: 12 clips per language from the
FLEURS **dev** split (other speakers, other sentences; `--split dev`):

| Language | Clips | Caption language = spoken | Re-run in another language | Script ok | Text-ID wrong | Raw whisper error | Final caption error | lingua agrees |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pt | 12 | 12 | 0 | 12 | 0 | 0.047 | 0.047 | 12/12 |
| es | 12 | 12 | 0 | 12 | 0 | 0.037 | 0.037 | 11/11 |
| el | 12 | 12 | 0 | 12 | 0 | 0.133 | 0.133 | 12/12 |
| de | 12 | 12 | 0 | 12 | 0 | 0.022 | 0.022 | 11/11 |
| ko | 12 | 12 | 0 | 12 | 0 | 0.028 | 0.028 | 12/12 |
| it | 12 | 12 | 0 | 12 | 0 | 0.025 | 0.025 | 12/12 |
| fr | 12 | 12 | 0 | 12 | 0 | 0.036 | 0.036 | 10/10 |
| ar | 12 | 12 | 0 | 12 | 0 | 0.029 | 0.029 | 12/12 |
| da | 12 | 12 | 0 | 12 | 0 | 0.201 | 0.201 | 12/12 |
| sr | 12 | 12 | 0 | 12 | 0 | 0.094 | 0.094 | 11/11 |
| en | 12 | 12 | 0 | 12 | 0 | 0.028 | 0.028 | 12/12 |
| tr | 12 | 12 | 0 | 12 | 0 | 0.104 | 0.104 | 12/12 |

"Final = raw" is the point: no deterministic step changes a word. Serbian's 0.167 is
measured in Latin; against the Cyrillic FLEURS text it would read 0.99, which is the
alphabet, not the words. The text language-ID (`detect_text_language`) makes 0 wrong calls
on all 288 texts (reference + whisper) and abstains ("unknown") on 12.

**Not measured (paid):** whisper-1 itself and the gpt-4o caption-correction pass. The free
run proves the decisions and every deterministic step; the correction prompt is
language-neutral with a never-translate rule and a same-tokens guard.

### 4.2 Greek all-caps

`text_fold.upper` against ICU (`toLocaleUpperCase("el")` in Node 22): 0 mismatches on 309
Greek words (every word of the 24 Greek FLEURS texts plus accent, dialytika, disjunctive ή
and polytonic edge cases). Python, web and the shared parity fixture agree.

### 4.3 Guards

- `tests/pipeline/test_kri1192_caption_languages.py`: the fixture of all 144 real clips:
  no cross-check re-run, 0 wrong language-ID calls, whisper names → codes, Serbian one
  alphabet, mixed takes, the brief side's Greek/Korean/Danish/Serbian asks.
- `tests/pipeline/test_kri1192_script_fallback.py`: every choosable face draws every new
  language in one face; Latin text keeps the KRI-785 face exactly; the registry hash still
  matches `registry-embeddings.npz`.
- `tests/fixtures/text-element-parity/text_case.json`: 8 Greek cases (Python, web, iOS).
