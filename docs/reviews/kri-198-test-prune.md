# KRI-198 — low-signal test prune (audit)

Linear KRI-198. Goal: delete tests that would not catch a real bug our E2E misses, then add
testing rules to `AGENTS.md` (symlink to `CLAUDE.md` → "Testing rules"). Decision: **evidence-based
cut** — delete by concrete low-signal pattern, keep every guard test, parity fixture, incident
regression, security check and documented kill-switch test. There is no API-level E2E, so most API
logic tests DO catch bugs E2E misses; wholesale deletion was rejected.

Method: 8 parallel subagent lanes (disjoint directories), one shared rubric, orchestrator-applied
follow-ups. All counts are from `git diff origin/main` and `def test_` / `it(` greps.

## Before → after

| Area | Files | Test functions (grep) |
|---|---|---|
| API pytest | 831 → 823 | 13,618 → 13,328 |
| Web Jest | 324 → 293 | 3,591 → 3,479 (grep); Jest reports 3,913 tests after |
| iOS unit (KriaTests + KriaMediaEngineTests) | 121 → 120 | 964 → 952 |
| **Total** | **40 files deleted** | **~-410 functions** |

Diff: 169 files changed, ~5,370 deletions. No runtime code changed (one comment in
`Kria/Features/SignInMotion.swift`).

## Rubric (H = API, W = web, I = iOS)

DELETE when: H1 `prompt_version` literal pins · H2 constants-equal-literals · H3 Pydantic/dataclass
default / round-trip / frozen / extra-forbid read-back · H4 registry membership checks · H5 mock-only
(`assert_called*`, no state/ordering) · H6 prompt-heading substring · H7 flag-off byte-identity for a
shipped default-True flag · H8 dead/one-shot code · W1 className-only · W2 render+text-exists ·
W3 duplicates a Playwright spec · I1 constants/restated formula · I2 source-grep "lint" tests.

Hard KEEP: anything named by a non-test file (CLAUDE.md, DECISIONS, docs, Makefile, scripts, workflows,
`.claude`, app-code docstrings), the 13 interaction suites pinned in `scripts/ci/web-tests.mjs`,
conftests and imported helper modules, `tests/evals/**` wrappers/fixtures, parity fixture producers
(`kria/test_phone_*`), incident regressions, security/IDOR/CAS/lock-order tests, XCUITests and
Playwright specs. When in doubt: KEEP.

## Deleted files (40)

**API (8):** `agents/test_registry.py` (H4) · `scripts/test_add_waka_waka_intro_overlays.py`,
`test_backfill_waka_waka_location.py`, `test_backfill_that_one_trip_to.py`,
`test_backfill_dimples_passport_inputs.py`, `test_analyze_waka_waka_diff.py` (H8: applied one-shot
backfills; the last never ran in CI) · `test_email_task.py` (H8: task is in `KNOWN_UNREGISTERED`; restore
when the Resend go-live registers it) · `test_sound_effects_byteidentity.py` (H2: asserted booleans built
inside the test).

**Web (31):** `ui/{alert-dialog,badge,button,card,InkButton,input,label,progress,scroll-area,separator,
sheet,skeleton,textarea,checkbox,radio-group,switch,toggle,toggle-group,slider,tabs,dialog,sonner}.test.tsx`
(W1/W2: Tailwind-class pins and Radix/sonner behavior on stock shadcn wrappers) ·
`ui/theme-tokens.test.ts` (W1) · `components/chat/ChatMessage` (W1) · `progress/{tone,phase-chip-row,
beam-loader}` (W1/W2) · `admin/JobIdChip` (W2) · `create/redirects` (W2) ·
`plan/items/EditorCanvas-stacking` (W3, dup of `e2e/editor-canvas-stacking.spec.ts`) ·
`plan/items/inspector-panel-captions` (W2).

**iOS (1):** `SignInMotionTests.swift` (I1: animation constants; Reduce Motion is covered by SignInUITests).

## Trimmed (functions removed from kept files)

**tests/pipeline (lane A, 34 functions / 21 files):** `test_agents` template_copy_contains_all_platforms ·
`test_prompt_loader` 2 prompt-substring tests · `test_barn_door_open` TestBarnDoorConstants ·
`test_handwriting_ass` write_on route set-membership · `test_interstitials` 2 curtain constants ·
`test_lyric_injector_defaults` line_defaults_locked · `test_lyric_injector_trailing_drop` constants ·
`test_lyrics_alignment_linear_shift` constants + kill-switch (flag default True, code-only) ·
`test_lyrics_alignment_median_shift` TestConstants + TestPromptVersionBump · `test_phrase_sequence` 2 constant
locks · `test_slide_post_profiles` default platform · `test_style_sets` version · `test_text_overlay`
ass_header_uses_playfair · `test_text_overlay_karaoke`/`_lyric_line` animated-effect registry ·
`text_overlay_v2/{test_grouping,test_phrases,test_pipeline}` default/constant pins ·
`test_shim_job_id_threading` 2 defaults · `test_hdr_tonemap_pipeline` desat default.

**tests/tasks (lane B, applied by orchestrator after the subagent was blocked, 12 functions / 11 files):**
`test_agentic_build_cache_routing` (import hasattr; v1 default row duplicated by the parametrized routing
table) · `test_auto_music_orchestrate` feature_flag_default · `test_generative_build` (music_requires_request
default; variant-decision JSON round-trip; fast-reburn eligibility duplicate of `test_fast_reburn_kill_switch`)
· `test_generative_build_silence_cut` budget_clamp default · `test_kri126_video_analysis_threading`
ANALYSIS_VERSION literal · `test_lyrics_preview_worker_registration` include/name pins · `test_m3_byte_identity`
3 default tests · `test_style_build` enabled_proceeds_to_db · `test_subtitled_retranscribe` CaptionCue basic
round-trip · `test_template_orchestrate` mix_audio_happy_path · `test_template_text_extraction`
force_layer2 + gcs_path defaults.

**tests/routes (lane C, 25 / 12 files):** `test_generative_jobs` 10 request-schema default/accept tests +
persist_backfill no-op (+3 unused imports) · `test_admin_recipe` · `test_sound_effect_library_metadata` ·
`test_creation_editor_actions` (route-literal pin; COPILOT_HONEST_REPLIES flag-off) ·
`test_creation_threads` (SLIDE_POSTS flag-off; projection-sync single assert) · `test_creator_agent`
allow_chat forwarding · `test_me_jobs` log-only outbox test · `test_admin_font_default` ·
`test_admin_music`, `test_music_jobs` (vacuous `status in (404, 500)` assertions) · `test_template_jobs` ·
`test_plan_item_sync_dispatch` (GUIDED_RENDER_RECOVERY flag-off).

**tests/services (lane D, 18 / 13 files):** `ocr/test_engines` (protocol/frozen/env-leak) ·
`ocr/test_cross_check` · `test_clip_facts` · `test_video_frames` · `test_video_grader` ·
`test_transcript_source` · `test_text_overlay_ocr` · `test_beat_heartbeat` · `test_job_dispatch` ·
`test_job_phases` · `test_generative_jobs_service` · `test_edit_training_dataset` · `test_ai_cost_control`.

**agents / schemas / kria / evals / cron / smart_edit (lane E, ~40 functions / 27 files + 6 files deleted):**
prompt_version pins in narration_annotations, narration_focus, narrated_storyboard, retake_detector,
creator_memory_extractor, creator_sfx_contract, kria/test_creative_brief · registry checks in
music_matcher, text_classification, text_alignment, `evals/test_structural` dispatch tests · default
read-backs in edit_format, runtime, intro_writer, overlay_format_matcher, style_derivation,
style_observation, voiceover_agents, `schemas/test_reaction_beats`, `schemas/test_lyrics_config_override` ·
prompt-phrase pins in direction_aware_planning, main_creator_agent, narration_annotations.

**root-level / integration (lane F, 50 functions / 17 files + 3 files deleted):** default/round-trip tests in
`test_sound_effect_schema`, `test_slide_post_schema`, `test_media_overlay_schema`, `test_user_style_schema`,
`test_custom_effects`, `test_nova_steps`, `test_overlay_autoplace_trim`, `test_sound_effects_command`,
`test_creation_threads_schema` · edit_copilot version/registry/heading pins · `test_template_recipe_version_
build_started_at` column checks · vacuous `in (404, 500)` route tests in `test_template_jobs_week2`,
`test_templates`, `test_waitlist_utm` · duplicate alembic-head assertion in
`test_speech_cleanup_analysis_schema` (kept by `test_content_plan_schema`) · duplicated IDOR-validator tests in
`test_media_overlay_byteidentity`.

**web (lane G, 37 blocks / 18 files):** ChatThinking, AgentApprovalCard, error-boundaries (backend-detail test
kept), JsonTreeView, CreationThreadPage (callback-URL cases kept), plan-page-canonical, tiktok-page,
upload-bar (clamp kept), ModuleDetailPanel, ArchitectureMap, nova-step-row, StyleChip, EditToolbar,
EditorCanvas-caption-preview, pocket-chrome, plan-item-clip-upload, ToolDrawer-sounds, plan-item-page.

**iOS (lane H, 8 functions / 3 files):** `SignInTypographyGuardTests` 2 source-grep tests (font-registration
runtime test kept; `docs/runbooks/ios-development.md` reworded), `NativeEditorIslandMetricsTests` 5
restated-formula tests (min-cover invariant kept), `NativeFontCatalogTests` hard-coded `count == 44` line.

## Orchestrator overrides (restored or skipped, and why)

- **Restored** `test_template_mode_unchanged.py`, `test_reroll_endpoint_unchanged.py`,
  `test_music_mode_manual_unchanged.py` — named as IRON RULE guards in the `app/tasks/auto_music_orchestrate.py`
  docstring (a non-test reference).
- **Restored** `test_transcribe_punctuation_alignment.py` — its kill-switch tests back the documented
  `CAPTION_PUNCTUATION_ENABLED=false` rollback (CLAUDE.md, `docs/pipelines/smart-captions.md`).
- **Skipped** from lane B's list: `test_reaper::test_broker_hiccup_reap_orphans_is_a_full_no_op` (deliberate
  half of a documented two-scenario pair), `test_style_build::test_status_not_edited_proceeds` and
  `test_conformance_build::test_conformance_persisted_on_success` (control / fence-kwarg pins),
  `test_template_orchestrate::test_old_recipe_without_beats_loads` (old-cache-dict compatibility),
  `test_template_text_extraction::test_extract_transcript_words_defaults_to_empty_list` (Stage E
  empty-transcript contract), both `test_text_elements_snapshot` kill-switch tests (`TEXT_ELEMENTS_ENABLED`
  rollback documented in app code).
- **Rebase onto main (2026-09-25):** two deletions collided with concurrent edits and were re-decided.
  `test_speech_cleanup_analysis_schema::test_0112_is_the_single_alembic_head` (a duplicate of the head guard in
  `test_content_plan_schema.py`, which main also bumped to 0112) stays deleted. In `agents/test_thinking_budget.py`
  main's new KRI-178 behavioral tests are all kept; only the constants pin
  `test_main_creator_uses_bounded_low_thinking_and_output_budget` (H2) is dropped.

## Borderline KEPT (candidates for a stricter follow-up pass)

- `agents/test_thinking_budget.py` matcher / critical-path cap tests (constant pins with measured-latency docstrings).
- `web plan/items/EditorShell-lyrics-optional-flag-off` (flag still default False, so this is the live path).
- `ChatMessageCopyTests` VoiceOver custom-action test (XCUITest cannot list custom actions, so not a duplicate).
- Seed/backfill script tests that may still run in fresh environments (`test_seed_dimples_passport_brazil`,
  `test_seed_rule_of_thirds`, `test_backfill_lyrics`, `test_backfill_video_posters*`).
- `test_drive_import.py` (no web/iOS caller, but routes mounted, task registered, docs name 3 tests).
- Kill-switch tests for default-True flags documented as rollback levers (ORIENTATION_NORMALIZE,
  GENERATIVE_FAST_REBURN, NARRATIVE_CLIP_ORDER, SMART_MUSIC_BED, RECONCILE_STUCK_VARIANTS, EDITORIAL_SEQUENCE,
  PLAN_SYNC_DISPATCH, PHONE_*_RENDERING).

## Environment notes

- `tests/pipeline/test_slide_post_build.py` (4 tests) fail on machines whose ffmpeg lacks `drawtext`
  (brew ffmpeg without libfreetype). They fail identically on origin/main; CI's prod image has it.
- `TODOS.md`: removed the two stale `test_analyze_waka_waka_diff` entries.
- `KriaTests` (Xcode app-hosted target) was not compiled locally for the iOS deletions (only the
  `KriaMediaEngine` package build and `scripts/ios/tests` were run); the `ios.yml` native build is the arbiter.
- `scripts/preship-check.sh` reports FAIL on 3 already-unformatted files (`test_music_matcher`,
  `test_style_observation`, `test_text_alignment`): they are unformatted at HEAD under local ruff 0.15.20
  (`ruff>=0.4` is unpinned), so this is a version artifact, and they were left as-is to avoid format churn.

## Follow-ups (out of scope)

- `KriaMediaEngineTests` (57 files) is not run by any CI lane (scheme lists only KriaTests + KriaUITests).
- The real gap behind this ticket: API-level E2E. Web Playwright runs against a mocked backend.
- Upload the Playwright report on success as an artifact (today: failure only).
