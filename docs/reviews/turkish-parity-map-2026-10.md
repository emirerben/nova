# Turkish parity map (KRI-773, phase 1)

Date: 2026-10-21. Base: `origin/main` at `a3125c96e`. Scope: `src/apps/api/app/**`, plus the
web and iOS code that changes on-video letter case. Every row below was opened and checked
against the code. Rows marked **(ran)** were run in the API venv and the output is quoted.

Status words used:

- **EN-only**: works for English wording, does nothing (or the wrong thing) for Turkish.
- **EN+TR**: has a Turkish twin or reads language-neutral model output.
- **TR-only**: only knows Turkish wording.
- **neutral**: reads model metadata or ids, not creator wording.

## 1. Summary

- **Decision points: 51 rows.** 28 EN-only, 21 EN+TR, 1 TR-only, 1 neutral.
  Outputs: 41 rows. Text-normalization bugs: 14 rows. Most EN-only code sits in a few files:
  `routes/creator_agent.py` (36 regex calls, no Turkish marker in the file),
  `kria/brief_checks.py` fallbacks, `agents/spoken_script.py`, `agents/edit_proposal.py`,
  `routes/plan_items.py`.
- **Root pattern.** KRI-520 added Turkish twins to the *chat* layer (`app/kria/*`,
  `choice_questions`, copilot). The older deterministic layer underneath (the
  creator-agent route, edit-proposal agent, speech-quote parsing, scoring, memory) still reads
  English words only. It often runs *after* the model and overrides it. So Turkish creators get
  the model's guess and English creators get a deterministic fix.
- **The language never reaches the render for plan items.** `tasks/content_plan_build.py:2471`
  calls `build_generative_job(...)` without `language=`. The job defaults to `"en"`, so
  `intro_writer` is told "Write the hook in English." and spoken captions fall back to `"en"`.
- **Only 4 of 82 agents get the chat language** (`prompt_language_line`): main_creator,
  edit_copilot, clip_intent_planner, clip_request_resolver. Interview, persona, plan, advisor,
  edit-proposal, review and platform-copy prompts have no language input at all.
- **Five worst gaps:**
  1. The plan-item hook is written in English for Turkish creators (section 2).
  2. `brief._legacy_fold` turns capital `I` into `ı` for the **English** patterns, so
     **"TRY AGAIN" and "Do It Again" are no longer read as a redo** (N01, ran). This hurts
     English creators too.
  3. Memory learning matches raw substrings: `"muzik baslasin"` and `"yavaslasin biraz"` are
     classified as **durable "always" rules**, because they contain `asla` (N04, ran).
  4. On-video `upper` / `title` case uses locale-free casing on server, web and iOS:
     `"istanbul iğne"` renders as `"ISTANBUL IĞNE"` (O28–O36, ran).
  5. Five bundled fonts lack Turkish letters (`ğ ş İ`, and Rascal lacks all twelve), and four of
     them are in the phone font picker (O37–O41).

## 2. How language flows today

| Step | Where | What happens |
|---|---|---|
| Detect | `app/kria/reply_language.py:136` `_scores`, `:154` `_detect` | Counts Turkish letters/words vs English words per message. Needs a clear majority, else `None`. |
| Resolve | `reply_language.py:188` `resolve_reply_language` | Message wins if clear. Switching away from an established language needs 3+ clear words. Then `Accept-Language` (top choice only, `:173`). |
| Store | `reply_language.py:218` / `:231` | `CreationThread.state["reply_language"]`. |
| Bind | `reply_language.py:243` `bind_reply_language`, `:258` `reply_language_for` | A contextvar for the turn. |
| Server copy | `reply_language.py:272` `say(en=, tr=)` | Returns Turkish only when bound to `tr`; English otherwise. |
| Model copy | `reply_language.py:277` `prompt_language_line` | Empty for English/unknown; for `tr` it says "write every creator-facing sentence in Turkish". Used in only 4 agents (see O01). |
| Worker copy | `tasks/content_plan_build.py:2604-2611` stamps `creator_reply_language` on `job.assembly_plan`; `tasks/generative_build.py:6100` `_worker_reply_language` binds it | Only for a blocked render's requirement copy (KRI-765). The stamp is written only on the approved-brief branch. |
| Kill switch | `config.py:654` `kria_reply_language_enabled` (`KRIA_REPLY_LANGUAGE_ENABLED`), read at `reply_language.py:212` | Off: every read returns `None`, so copy is English. |
| Caption language (separate stack) | `app/pipeline/caption_language.py:68` `parse_caption_language_request`, `:125` `infer_language_from_text`, `:139` `crosscheck_detected_language`, `:155` `resolve_spoken_caption_language` | Whisper's detected language, then a word guess from the transcript, then `fallback`. The fallback is the job language, or `"en"` (`:170`). |

**Key gap: the render's language.** It is a separate value from the chat language and is never
copied over:

1. `tasks/content_plan_build.py:2471` `build_generative_job(...)` passes persona, theme, idea,
   style, filming guide and voiceover, but **no `language=`**. The chat language is read later,
   at `:2604`, and only stamped as `creator_reply_language`.
2. `services/generative_jobs.py:295` `language: str = "en"` default; `:420` stores
   `all_candidates["language"]`.
3. `tasks/generative_build.py:2973`, `:4977`, `:6950`, `:9261`, `:18288` read
   `all_candidates.get("language") or "en"`.
4. `agents/intro_writer.py:211` `_LANGUAGE_INSTRUCTIONS["en"] = "Write the hook in English."`,
   selected at `:229` / `:513`. The Turkish block (`:213`) exists but is never selected here.
5. The same `language` is the `fallback=` for spoken captions (`generative_build.py:7306`,
   `:7429`, `:9542`), so a Turkish clip that Whisper and the transcript guess both miss gets
   English captions.

Only the public route passes a language: `routes/generative_jobs.py:12283` (`req.language`).

## 3. Decision points

"Model nearby?" means a model already reads the same message and could carry the meaning in a
structured field instead (the KRI-667 direction).

### 3a. Understanding and routing

| ID | file:line | name | decides | status | model nearby? | Turkish user impact |
|---|---|---|---|---|---|---|
| D01 | routes/creation_threads.py:292 (used :2543) | `_STATUS_ONLY_RE` | v1 chat: is this a status question or a revision? | EN-only | no | `"durum ne?"` → False **(ran)**, so it is treated as a new request while a render runs. The twin `kria/language.py:99` handles it (True). |
| D02 | routes/creator_agent.py:724, :754 (applied :1237, :1488, :1891) | `_explicit_media_scope`, `_has_explicit_media_scope` | Overrides the model's `media_scope` with all/selected | EN-only (stated count is EN+TR via creator_policy) | yes, main_creator | `"tüm klipleri kullan"` → has-scope False, scope `selected` **(ran)**. The twin `agents/main_creator.py:1254` calls `turkish_media_scope` (:1281) → `all`. |
| D03 | routes/creator_agent.py:1288-1370 in `_apply_explicit_render_intent` (:1170) | `closing_cue`, `_title_patterns` | Pulls opening and closing title copy from the request | EN-only (`title|intro|hook|text`, `closing|ending|end|outro`) | yes | `"başlık 'Yaz geldi' olsun"` is not extracted. It relies on the model or approved copy. |
| D04 | routes/creator_agent.py:1396 | font-name regex | Sets `font_family` from "X font" / "font is X" | EN-only cue words (names are neutral) | yes | `"Rascal yazı tipi"` matches only the bare-name-before-punctuation branch. |
| D05 | routes/creator_agent.py:1412-1434 | `color_match` | Sets `text_color` | EN-only (`yellow|gold|white|black`) | yes | `"sarı yazı"` is ignored. |
| D06 | routes/creator_agent.py:792, :892 | `_SFX_GENERIC_WORDS`, `_explicit_sfx_name` | Named sound effect = required request | EN-only verbs/nouns | yes | A named effect in Turkish (`"'whoosh' sesi ekle"`) is not made required. |
| D07 | routes/creator_agent.py:1136 | `_SECONDS_WORDS` | "two seconds" → 2.0 | EN-only | yes | `"iki saniye"` is not read here (the twin `kria/planner.py:2232` has Turkish). |
| D08 | routes/creator_agent.py:1445-1475 | photo sub-second promotion | Forces `fast_montage` for "photos 0.1 s among videos" | EN-only | yes | Fallback after `recognize_mixed_media_timing`, which has Turkish (`schemas/edit_proposal.py:958`). Turkish works only through that path. |
| D09 | routes/creator_agent.py:1502-1507 | pacing regexes | `pacing` fast/relaxed/balanced | EN-only | yes | `"hızlı tempo"` does not set pacing. |
| D10 | routes/creator_agent.py:1541 (used :1574) | `_requests_preserved_clip_order` | Keep the previous render's clip order | EN-only | yes | `"klip sırasını koru"` → False **(ran)**, so clips get reordered. |
| D11 | routes/plan_items.py:2620-2690 | `_review_mixed_media_timing` | Draft review: opt out of / reset photo-video timing | EN-only removal grammar (positive path uses TR-aware recognizer) | yes, review model | Turkish "don't flash the photos" or "reset" is ignored. |
| D12 | schemas/edit_proposal.py:1014 | `media_context_group` | Groups clips by sport/context | neutral (reads English model metadata); EN-only for the creator-context field | no | Low. Turkish creator context ("plaj voleybolu") is not used for grouping. |
| D13 | schemas/edit_proposal.py:1052 | `recognize_image_layout` | Photo fit vs fill | EN-only | yes | `"fotoğrafları kırpma, tam görünsün"` → None **(ran)**, so the default crop is used. |
| D14 | schemas/edit_proposal.py:401 | `loose_creator_text` | Feeds every Turkish reader in this module | EN+TR, gated | — | Returns `""` when the whole message reads English. Mixed text loses its Turkish words; see N11. |
| D15 | schemas/edit_proposal.py:384-600, :958 | `_TR_*` cadence, scope, reuse, duration, mixed timing | Timing, scope, reuse, total length | EN+TR | — | Works for Turkish (KRI-520). |
| D16 | kria/brief.py:497, :511, :733, :740 | `_REDO_PATTERNS` / `_TR_REDO_PATTERNS`, `_asks_move`, `_asks_remove` | Full re-plan vs editor op | EN+TR, but English side is broken by N01 | — | Turkish works. English all-caps or Title Case fails. |
| D17 | kria/brief_route.py:16, :23 | `fold_text`, `loose_text` | Canonical fold | EN+TR | — | Reference implementation (see N12). |
| D18 | kria/language.py:99, :105 | `is_status_question`, `is_help_question` | v2 status/help short-circuit | EN+TR | — | `"durum ne?"` → True **(ran)**. |
| D19 | kria/planner.py:3448, :2232 | `_TEXT_EDIT_ASK` + TR twin, `_EN_HOLD_WORDS` + TR | Text-edit asks, hold seconds | EN+TR | — | OK. |
| D20 | agents/main_creator.py:1254 | `_explicit_media_scope_from_request` | Model-side media scope | EN+TR | — | OK. |
| D21 | agents/_schemas/creator_policy.py:125, :149 | `_turkish_stated_count_matches_manifest`, `explicit_scope_from_stated_media_count` | "16 klip ile" = all media | EN+TR | — | OK. |
| D22 | smart_edit/planner.py:43-95 (entry `plan_smart_captions` :1985) | `_DIRECT_ORDINALS`, `_CARDINALS`, `_LIST_TERMS`, prefixes | Smart-caption list/ordinal/payoff events | TR-only, no language gate | yes, `agents/smart_edit_planner.py` | English talking heads get no deterministic list events ("first", "reason two"). |

### 3b. Clarifying questions

| ID | file:line | name | decides | status | model nearby? | Turkish user impact |
|---|---|---|---|---|---|---|
| D23 | services/choice_questions.py:100, :2470 | option builder (`(en, tr)` pairs), `normalize_reply` | Choice copy and typed answers | EN+TR | — | Guarded by KRI-630 test. |
| D24 | kria/opening_cut_ask.py:66, speaker_framing_ask.py:195, speed_ask.py:154, spoken_text_ask.py:38, style_asks.py:45, clip_scope.py:221, voiceover_clips.py:215 | `*_ask` readers | Which follow-up question to ask | EN+TR | — | OK (Turkish twins or model facts). |
| D25 | kria/strategy_policy.py:140 | strategy refusal and policy | Refuse or ask | EN+TR | — | OK. |

### 3c. Editor actions and receipts

| ID | file:line | name | decides | status | model nearby? | Turkish user impact |
|---|---|---|---|---|---|---|
| D26 | kria/editor_receipts.py:100-121 | `_REGEX_CUES` | Which edit families a message asked for (receipt wording) | EN+TR, except `timing` (:120) | yes, copilot `claimed_ops` | `"yazı daha geç çıksın"` is not seen as a timing ask. |
| D27 | kria/editor_receipts.py:136, :139, :143 (used :282-285) | `_FONT_VERB`, `_FONT_NOUN_ONLY`, `_SAME_FONT` | Font-change ask | EN-only (`style_asks.py:141 font_from_words` covers named fonts) | yes | `"yazı tipini değiştir"` is not named as a font change in the receipt. |
| D28 | agents/edit_copilot.py:2305, :4387, :4415, :4675 | `_BilingualPattern`, `_CREATIVE_CAPTION_RE`, `_CAPTION_INSTRUCTION_WORDS`, `_ALIGNMENT_ASK_RE` | Copilot guards and refusals | EN+TR | — | OK. |
| D29 | routes/_copilot.py:100-120 | `_NEGATED_SUCCESS` + `_TR_EDIT_STEMS` | Honest-reply guard | EN+TR | — | OK. |
| D30 | agents/edit_proposal.py:1618 (used :2886) | `_EXCLUSIVE_QUOTE_RE` | A quote plus "only/just" = the complete caption list | EN-only | yes | `sadece "yaz geldi" yaz` → False **(ran)**. AI captions on other beats are kept. |
| D31 | agents/edit_proposal.py:59-72 (used :238) | `_SENSORY_CLAIM`, `_PERSONAL_PRONOUN`, `_UNSUPPORTED_ACTION_LEAD` | Drops ungrounded model captions ("delicious", "enjoying…") | EN-only | it *is* a model-output guard | Turkish captions (`"lezzetli"`, `"keyif"`) pass unchecked. *(new)* |
| D32 | services/creator_sessions.py:73 (used :843) | `_GENERIC_SOUND_EFFECT_REQUEST_RE` | Adds the "no generic SFX" notice | EN-only | no | `"ses efekti ekle"` → no match **(ran)**, so the notice is missing. |
| D33 | kria/brief_checks.py:2140-2187 (used :2395) | `_CUE_SPEAKER`, `_CUE_VERB`, `_CUE_OPENER`, `_CUE_TRIGGER_RE` | Pulls the unquoted trigger word of a pop-in ask | EN-only trigger extraction (`_SPOKEN_CUE_RE` :2152 is EN+TR) | yes, brief `facts` | Reads `description`, which main_creator keeps in the creator's language (`main_creator.py:359`). Turkish unquoted triggers are never extracted. |
| D34 | kria/brief_checks.py:2346 (used :2360) | `_WHOLE_TEXT_RE` | "Title for the whole video" | EN-only fallback (fact `persistent` first) | yes | Only works when the model records the fact. |
| D35 | kria/brief_checks.py:3325-3338, :3340 | `_SPEECH_EXCERPT_RE`, `_SPEECH_OVER_RE`, `_SPEAKER_RE`, `_BACK_TO_MONTAGE_RE`, `_wants_speech` | Is this a speech-excerpt ask, and was it met? | EN-only | yes | Turkish "my words over b-roll" asks are not checked as speech asks. |
| D36 | kria/brief_checks.py:4439 | `_LEADING_ARTICLE_RE` | Strips "the/a/my" before clip names | mostly EN (+`bu`) | no | Minor. |

### 3d. Speech roles and transcripts

| ID | file:line | name | decides | status | model nearby? | Turkish user impact |
|---|---|---|---|---|---|---|
| D37 | agents/spoken_script.py:25, :30, :43, :48, :60, :63, :72 | `_SPEECH_ATTRIBUTION_RE`, `_DISPLAY_COPY_CUE_RE`, `_QUOTE_PLACEMENT_RE`, `_DISPLAY_QUOTE_SUFFIX_RE`, `_SPOKEN_QUOTE_*`, `_SPOKEN_SCRIPT_CUE_RE` | Is a quote on-screen copy, reported speech, or the voiceover script? | EN-only | yes | `"'X' dedi"` (reported speech) or `"seslendirmede 'X' diyorum"` is printed on screen as a caption. |
| D38 | agents/semantic_edit_proposal.py:197-203 | quote harvest via `is_reported_speech_quote` / `is_spoken_script_quote` | Allowed creator captions | EN-only (inherits D37) | yes | Same as D37. |
| D39 | services/spoken_roles.py:100 (`_folded_phrases` :185) | spoken role phrases | Intro/outro/CTA lines in speech | EN+TR | — | OK. |
| D40 | services/spoken_quotes.py:60 | `_SPEECH_CUES` | Quote = something said | EN+TR | — | OK. |
| D41 | pipeline/silence_cut.py:311 | `is_filler_token` | Filler removal | EN+TR | no | OK (I→ı fold is deliberate here). |
| D42 | pipeline/score.py:27 (used :240) | `FILLER_WORDS` | −1 hook score if it starts with a filler | EN-only | no | Turkish `"şey"`, `"yani"`, `"ee"` hooks are not penalized. |
| D43 | pipeline/narration_labels.py:39, :71 | `_NUMBER_WORDS`, `_SCORE_WORDS` | Spoken score → on-screen score label | EN-only | yes (storyboard) | `"üç bir"` never becomes a score label. |
| D44 | tasks/generative_build.py:25233-25305 | number words, `score_context_re`, `hybrid_score_re` | Narrated storyboard score safety net | EN-only | yes | Same as D43. *(new)* |
| D45 | tasks/generative_build.py:25102 (used :25146, :25180) | `_creator_requests_narrated_treatment` | Narrated intro title / player names wanted? | EN-only nouns and negation | yes | `"giriş başlığı"` and `"oyuncu isimleri"` are not detected. *(new)* |
| D46 | services/visual_treatment_planner.py:40-95, :227 | `_NUMBER_WORDS` (EN+TR), `_section_normalized_tokens` | Section numbers in speech | EN+TR, casefold bug N10 | no | Lowercase works; `"ALTI"` fails. |
| D47 | agents/intro_writer.py:45; agents/voiceover_script_writer.py:47 | `_REFUSAL_PATTERNS`, `_REFUSAL_RE` | Reject a model refusal before it is burned or spoken | EN-only | it is the guard | `"daha fazla bilgiye ihtiyacım var"` → not a refusal **(ran)**. It would be burned as the hook. |
| D48 | pipeline/caption_language.py:47, :68 | `_CAPTION_KEYWORD`, `parse_caption_language_request` | "Türkçe altyazı" → `tr` | EN+TR | — | OK. |

### 3e. Memory and learning

| ID | file:line | name | decides | status | model nearby? | Turkish user impact |
|---|---|---|---|---|---|---|
| D49 | services/creator_memory_learning.py:25-77, :89, :106 | `_DURABLE_MARKERS`, `_LOCAL_MARKERS`, `_SOFT_MARKERS`, `_STATEFUL_MARKERS` | Should this message become a durable memory? | EN+TR (+ES/FR), broken matching | yes, direction extractor | False durable rules; see N04. |
| D50 | services/finished_style_learning.py:41, :69 | `TEMPORARY_MARKERS`, `explicitly_temporary` | "Just this once" = don't learn the style | EN+TR, casefold only | no | See N05. |
| D51 | services/creator_direction.py:47 (used :203) | `_FONT_PATTERNS` | "Always use X font" → typed direction | EN-only | yes | `"her zaman X yazı tipini kullan"` is not learned as a typed font rule. |

## 4. Outputs

### 4a. Prompts with no language input

None of these prompt files or `render_prompt` bodies mention language, and none call
`prompt_language_line`. The model either mirrors the input language or defaults to English.

| ID | prompt | agent file:line (`render_prompt`) | creator-facing output |
|---|---|---|---|
| O01 | (summary) | `prompt_language_line` is used only at clip_request_resolver.py:241, main_creator.py:627, edit_copilot.py:3324, clip_intent_planner.py:1077 | — |
| O02 | prompts/interviewer.txt | agents/interviewer_agent.py:125 | Onboarding questions |
| O03 | prompts/voiceover_interview.txt | agents/voiceover_interviewer.py:79 | Voiceover interview questions |
| O04 | prompts/write_voiceover_script.txt | agents/voiceover_script_writer.py:152 | Spoken script (and D47 guard) |
| O05 | prompts/style_intent.txt | agents/style_intent.py:141 | Style replies |
| O06 | prompts/idea_expander.txt | agents/idea_expander.py:71 | Expanded idea text |
| O07 | prompts/generate_content_plan.txt | agents/content_plan_generator.py:306 | Plan items, filming guide |
| O08 | prompts/generate_persona.txt | agents/persona_generator.py:124 | Persona "why this lane" rationale |
| O09 | prompts/plan_item_advisor.txt | agents/plan_item_advisor.py:121 | Advisor tips |
| O10 | prompts/slide_post_composer.txt | agents/slide_post_composer.py:94 | Slide captions |
| O11 | prompts/edit_proposal.txt | agents/edit_proposal.py:2359 | Story beats, captions, summary |
| O12 | prompts/semantic_edit_proposal.txt | agents/semantic_edit_proposal.py:668 | Proposal copy |
| O13 | prompts/conformance_feedback.txt | agents/conformance_feedback.py:111 | Feedback to creator |
| O14 | prompts/montage_review.txt | agents/montage_reviewer.py:101 | Review notes |
| O15 | prompts/clip_question.txt | agents/clip_question.py:122 | Questions about clips |
| O16 | inline prompt | agents/platform_copy.py:60 | TikTok/IG/YT caption + hashtags |
| O17 | prompts/shot_list_writer (prompt_id) | agents/shot_list_writer.py:118 | Shot list |
| O18 | intro hook | agents/intro_writer.py:211 via content_plan_build.py:2471 | Hook told "in English" (section 2) |

### 4b. English-only server copy

| ID | file:line | what | status |
|---|---|---|---|
| O19 | routes/creation_threads.py:3020 (`_sync_agent`), :3973 (`create_thread`), :4663, :4722, :4750 (`message_thread`), :7372, :1414, :1421 | v1 chat event content ("I've saved your request…", "What are we making?…", "This project is archived.") | EN-only; the file has 0 `say()` calls and 82 English `detail="…"` strings |
| O20 | kria/plan_review.py:233, :236 | `ScopeError` messages ("Choose which parts of the video to change.") | EN-only; the rest of the file uses `say()` (8 calls) |
| O21 | routes/dictation.py:144-210 | HTTP errors ("Recording is too long — keep it under two minutes") | EN-only |
| O22 | services/creation_editor_actions.py:67, :70, :141 | Visuals-request limits, "Cancelled videos cannot be edited." | EN-only |
| O23 | services/finished_style_learning.py:606 `_instruction` | Learned style memory text ("Favor X when automatically choosing title fonts."), shown via routes/memory.py:138, routes/me.py:3136 | EN-only |

### 4c. Captions and speech recognition

| ID | file:line | what | effect |
|---|---|---|---|
| O24 | iOS Kria/Core/Services.swift:1238 `transcribeDictation` | Sends `file` and `context_terms`, **no `locale_hint`** | routes/dictation.py:187 gets `""`, so Whisper runs with `language=None` (auto-detect, which misreads short Turkish clips). |
| O25 | routes/dictation.py:58, :104-109 | Vocabulary prompt `"Video edit request. montage, lip-sync, captions, voiceover, b-roll, transition."` | English prompt biases Whisper toward English spelling/translation. |
| O26 | pipeline/lyrics_alignment.py:226 `_normalize` | Lyric/Whisper word key | Drops `ı` (see N07); Turkish lyric words align worse. |
| O27 | pipeline/caption_language.py:170 | Fallback `"en"` | Fed the job language, which is `"en"` for plan items (section 2). |

### 4d. On-video letter case

Python `str.upper/lower/title`, JS `toUpperCase/toLowerCase`, and Swift `uppercased()/lowercased()`
without a locale all use non-Turkish rules. **(ran)**: `apply_text_case("istanbul iğne","upper")`
→ `ISTANBUL IĞNE`. `"İSTANBUL"` lower → `i̇stanbul` (i + combining dot). `"IŞIK"` lower → `işik`.
`"irmak ılık".title()` → `Irmak Ilık`. `"emir'in kızı".title()` → `Emir'In Kızı`. Node gives
the same `ISTANBUL` / `i̇stanbul`. `toLocaleUpperCase('tr')` gives the correct `İSTANBUL`.

| ID | file:line | what | status |
|---|---|---|---|
| O28 | api agents/_schemas/text_element.py:171-187 `apply_text_case` (callers generative_overlays.py:1063, :1067; phone_text_style.py:71; slide_post/build.py:400) | Server-rendered text case | EN-only casing |
| O29 | web src/lib/overlay-layout.ts:447-453 `applyTextCase` | Web preview case | EN-only casing; `<html lang="en">` (src/app/layout.tsx:50) |
| O30 | iOS Kria/Core/NativeEditorRenderCompiler.swift:564-565, :897 | Phone render case | EN-only casing |
| O31 | api pipeline/text_stack_layout.py:33-39 | Layout measure uses `.title()`, not `apply_text_case` | EN-only, and measures different text than is drawn (`Don'T` vs `Don't`) |
| O32 | api services/clip_intent_resolution.py:2236 | `output.answer.title()` for clip labels | `"ırmak"` → `Irmak`, `"Emir'in"` → `Emir'In` |
| O33 | api services/slide_post_chat_edit.py:347 `_apply_case` | Slide text case uses `.title()` | EN-only casing |
| O34 | api tasks/template_orchestrate.py:5979 `_match_casing` | Matches `inputs.location` to sample case | `"izmir"` → `IZMIR` |
| O35 | api pipeline/lyrics_alignment.py:388 `_case_like` | Whisper replacement case | EN-only casing |
| O36 | api pipeline/unified_montage.py:322-323 | Short headline upper | **Partial fix exists**: skips upper when the text has a dotted i. A pattern to reuse. |

### 4e. Fonts missing Turkish letters

Checked every `.ttf` in `src/apps/api/assets/fonts/` (52 font files) with fontTools for
`ğĞşŞıİçÇöÖüÜ`. 47 fonts have all twelve. Missing:

| ID | font file | missing | in phone picker? (schemas/phone_text_style.py:14 `CURATED_PHONE_FONTS`) |
|---|---|---|---|
| O37 | AlteHaasGrotesk-Bold.ttf | ğ Ğ ş Ş İ | yes |
| O38 | BigCurls-Regular.ttf | ğ Ğ ş Ş ı İ | yes ("Big Curls") |
| O39 | PermanentMarker-Regular.ttf | ğ Ğ ş Ş İ | yes |
| O40 | Satisfy-Regular.ttf | ğ Ğ ş Ş İ | yes |
| O41 | Rascal-Regular.ttf | all 12 (ğĞşŞıİçÇöÖüÜ) | no; registry only (font-registry.json:410), and named in creator_agent.py:1395 |
| O41z | ZTBrosOskon90s (KRI-785) | ı is mapped but drawn WITH a dot ("ışığı" reads "işiği") | no |
| O41b | BigCurls-Regular.ttf (KRI-785) | ç Ç ö Ö ü Ü â î û (and é è ...) are mapped to EMPTY glyphs: a cmap check passes, nothing is drawn | yes |
| O41c | 19 faces incl. Playfair Display (style default), Instrument Serif, Bebas Neue (KRI-785) | ₺ U+20BA | some |
| O41d | 11 faces incl. Poppins Bold/Regular/Black, Satisfy, Patrick Hand (KRI-785) | U+0307, which locale-free lower/title case of İ emits (see O28) | some |

KRI-785 fix: `pipeline/font_coverage.py` switches the WHOLE run to one covering registry face
(`assets/fonts/glyph-fallbacks.json` partner, else same category, else the display default),
with one face per requested family across a burn (no line-by-line flip). Skia, Pillow/libass,
the phone styling/montage pickers and the iOS compiler (`NativeGlyphFallback.swift`) share
the rule; `overlay_verify` FAILs a resolved face that still lacks a needed glyph. Parity rows
O37-O41g.

What happens at render: Skia draws a tofu box for a missing glyph
(`pipeline/text_overlay_skia.py:891`). A per-letter system-font fallback exists only on the
shaped path (`_shaped_paragraph` :1279), which runs only when `shape_text` is on
(`:2236`, `:3162`, `phone_captions.py:365`). Hard failure is wired only for intro clusters
(`intro_cluster.py:603`, `:989`) and lyrics (`portable_text_layout.py:763`, `:867`). Phone
renders (iOS) use the bundled font and have their own fallback behaviour, not checked here.

## 5. Text-normalization bugs

Reference: `kria/brief_route.py:16 fold_text` (İ/I/ı → i, casefold) and `:23 loose_text`
(plus diacritics stripped). These are correct. Everything below differs from them.

| ID | file:line | what it does with I / İ / ı / ğ / ş | wrong output **(ran)** |
|---|---|---|---|
| N01 | kria/brief.py:558 `_legacy_fold` (used :588, :733, :740) | `I` → `ı`, then casefold. Used for the **English** redo/move/remove patterns. | `"DO IT AGAIN"` → `"do ıt agaın"`. `wants_full_replan`: `"do it again"` True, `"Do It Again"` False, `"DO IT AGAIN"` False, `"TRY AGAIN"` False. English regression from KRI-520. `"DO IT AGAIN BASED ON MY PROMPT"` is still True only because "prompt" matches. |
| N02 | kria/brief_checks.py:4981 `_spelling_key` | İ → i; `I` → `ı` only when `turkish=True` | Correct when the flag is right. With `turkish=False`: `"KIRMIZI"` → `kirmizi` ≠ `kırmızı`. The verdict depends on the flag source. |
| N03 | bare `casefold()` / `lower()` on creator text: creator_memory_learning.py:89, finished_style_learning.py:70, schemas/text_style_intent.py:58, services/suggestion_chips.py:892, routes/creator_agent.py:727 (and every `.casefold()` in that file) | `"İ".casefold()` → `"i̇"` (two code points), `"I"` → `i` | Any marker containing `i` misses capital-İ input: `"BU VİDEODA"` ≠ `"bu video"`. |
| N04 | services/creator_memory_learning.py:89, :106 | raw substring `marker in text.casefold()` | `"muzik baslasin"` → **explicit**. `"yavaslasin biraz"` → **explicit** (contains `asla`). `"BU VİDEODA asla müzik olmasın"` → explicit (lowercase → noop). `"TÜM VİDEOLARIMDA altyazı olsun"` → noop (lowercase → explicit). `"unutma: başlık ekle"` → stateful True (`unut`). |
| N05 | services/finished_style_learning.py:69 | substring after casefold, diacritics kept | `"SADECE BU VİDEODA"` → False. `"gecici olarak"` → False. `"bu videoya ozel"` → False. (`"geçici"` → True.) |
| N06 | services/content_plan_dedup.py:132, services/seed_provenance.py:105, services/lrclib_client.py:509 | NFKD then `encode("ascii","ignore")`: `ı` has no decomposition, so it is **deleted** | dedup `"kırmızı çiçek ağacı"` → `{krmz, cicek, agac}`. seed `"Kırmızı ışık sığınak"` → `{krmz, sk, sgnak}`. lrclib `"Kırmızı Işıklar"` → `{krmz, isklar}`. Weakens idea dedup, seed provenance and song-title matching. |
| N07 | pipeline/lyrics_alignment.py:226 `_normalize` | NFKD, drop marks, `lower()`, keep `[a-z0-9]` | `kırmızı` → `krmz`. `ışık` → `sk` but `Işık` → `isk`, so the same word gets two keys. |
| N08 | pipeline/narrated_alignment.py:41 `_norm` | `lower()` then keep `[a-z0-9]` only, so every Turkish letter is deleted | `şöyle` → `yle`, `çiğ` → `i`, `güzel` → `gzel`, `Işık` → `ik`, `ağaç` → `aa`. Voiceover-to-script alignment for narrated edits. *(new)* |
| N09 | services/clip_selection.py:87 `_words`; services/edit_direction_planner.py:854 `_label_match_tokens` | NFKD + casefold, but `ı` is kept as `ı` | `"IŞIK"` → `isik` but `"ışık"` → `ısık`; the same word does not match by case. |
| N10 | services/visual_treatment_planner.py:227 `_section_normalized_tokens` | casefold, drop U+0307 | `"ALTI NEDEN"` → `['alti','neden']` but `"altı neden"` → `['6','neden']`. |
| N11 | schemas/edit_proposal.py:401 `loose_creator_text` | `""` when `detect_chat_language` says en | `"fotoğrafları fit the screen, make them fullscreen please"` → `""`; Turkish readers never see the Turkish words. |
| N12 | Correct, keep as-is: kria/brief_route.py:16/:23, pipeline/caption_language.py:41, pipeline/caption_spelling.py:32, pipeline/caption_correct.py:74, smart_edit/compiler.py:124, smart_edit/planner.py:155, agents/landmark_guess.py:48, agents/clip_intent_planner.py:574, services/clip_file_names.py:77, services/clip_facts.py:302, services/sfx_catalog.py:131 (casefold first, drops U+0307), services/transcript_quality.py:20, pipeline/silence_cut.py:327 (I→ı on purpose) | — | compiler/caption_language/landmark: `"KIRMIZI İstanbul"` → `kirmizi istanbul`. sfx: `"IŞIK ışık"` → `isik isik`. |
| N13 | (cross-cutting) 15+ private copies of "the Turkish fold" | — | N01–N11 are the copies that drifted. One shared helper (brief_route) plus a ban on new copies would stop the drift. |
| N14 | services/lrclib_client.py:520 `_norm` | casefold only | `"İ"` artist names compare as `i̇`; minor. |

## 6. Existing guards and what they miss

| Guard | Covers | Does NOT cover |
|---|---|---|
| tests/kria/test_turkish_chat_guard.py (17 tests) | Runtime net over v2 chat writers `_append_sync_event`, `_append_event`, `CreationThreadEvent` (`_WRITERS` :760) plus `KriaTurnPlan.response`, `RefusedStrategy.question`. Static AST net: no fixed English handed to those. `*_TR` tables have their English keys (scans `app/kria`, `app/services`, `tasks/kria_runtime.py`). | `routes/creation_threads.py` writes through its own `_append(...)` (:2050). `_append` is not in `_WRITERS`, and it hands `CreationThreadEvent` a variable, so the static net cannot see the fixed English at the call sites (O19). Only the runtime net could catch it, and only if a scenario drives those v1 paths. HTTP `detail=` (O19–O22, by design). Memory instructions (O23). Model-written copy from the agents in 4a. On-video text. |
| tests/kria/test_kri667_no_new_intent_regexes.py | Ratchet: regex count per module can only go down. Scans all `app/kria` plus 22 named modules (`_OTHER_MODULES`). | Not scanned: routes/plan_items.py (D11), routes/creation_threads.py (D01), agents/edit_proposal.py (D30, D31), agents/semantic_edit_proposal.py, services/creator_sessions.py (D32), pipeline/score.py, narration_labels.py, tasks/generative_build.py (D44, D45), intro_writer/voiceover_script_writer refusals. It caps regex counts but does not require a Turkish twin, and does not see substring tuples (D49, D50) or fold copies (section 5). |
| tests/services/test_choice_questions_turkish_guard.py (4 tests) | Every choice builder renders Turkish with `reply_language="tr"`; `intro=`/`reason=` go through `say()`. | Only choice questions. |
| KRI-520 tests (tests/kria/test_kri520_*.py, tests/services/test_kri520_*.py, tests/schemas/test_kri520_turkish_parsers.py, tests/test_kri520_copilot_turkish.py, tests/test_kri520_editor_notes.py) | Turkish twins of the chat layer: reply language, brief routing, receipts, planner copy, strategy policy, parsers, copilot. | No test runs the **English** patterns on upper-case input (N01 shipped). Nothing covers the render language (section 2), casing (4d), fonts (4e), dictation locale (O24), or memory classification (N04). |
| pipeline/text_overlay_skia.py:899 `assert_glyphs_present` | Used for intro clusters and lyrics only. | No CI check that picker fonts cover Turkish (4e). |

## 7. Out of scope

- iOS and web UI string localization (buttons, screens, app copy). That is a separate project.
- Non-Turkish languages. ES/FR markers in D49 are noted, not audited.
- Model quality of Turkish output (prompt wording, evals). This map only shows where no language
  is passed in.
