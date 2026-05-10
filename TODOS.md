# Nova — Deferred Work

## Agent evals — Phase 2

### Re-run creative_direction on 10 templates with under-baked descriptions
**What:** When seeding eval fixtures, the export script's structural validator rejected 10 of 14 templates' `creative_direction` text as below the 50-word floor (some as short as 4 words). These templates either pre-date two-pass mode or the model produced a near-empty output that nobody caught. Re-run two-pass analysis on: `that_one_trip_to`, `morocco`, `just_fine_test2`, `just_fine___sunset_reassurance`, `impressing_myself`, `football_face_hook`, plus 4 others surfaced by `python scripts/export_eval_fixtures.py 2>&1 | grep '\[reject\]'`.
**Why:** A 4-word creative_direction means Pass 2 (`template_recipe`) was running with no editorial guidance, so the recipe quality on those templates is whatever Gemini produced cold. Real user-facing impact: the templates' `copy_tone`, `pacing_style`, `transition_style` are likely generic or wrong.
**How:** Trigger reanalysis from the admin UI for each template, OR add a one-off script that calls `analyze_template_task` with `analysis_mode="two_pass"` for each. Verify by re-running `scripts/export_eval_fixtures.py` — rejected count should drop.
**Effort:** S (human: ~1h / CC: ~10 min)
**Priority:** P2

### Phase 2 — evals for the remaining 8 agents
**What:** Extend `tests/evals/` to cover `transcript`, `platform_copy`, `audio_template`, `text_designer`, `transition_picker`, `clip_router`, `shot_ranker`. Each new agent adds: one rubric markdown under `tests/evals/rubrics/`, one structural-check function in `runners/structural.py`, one test entry-point file, one fixture directory. Phase 1 (Big 3) shipped the harness; Phase 2 is repeated application.
**Why:** Every LLM agent in Nova has a quality dimension that silently regresses on prompt drift. Phase 1 covers the highest-leverage three (TemplateRecipe, ClipMetadata, CreativeDirection). The remaining seven are smaller surface areas but each shapes a real user-facing output.
**How:** For each agent, mirror the Phase-1 pattern: hand-author 2-3 golden fixtures + (if persisted) export prod snapshots. Add the dispatch entry in `run_structural`. Add the agent class to `_build_agent_class_for` in `eval_runner.py`.
**Notes:** `clip_metadata` fixtures cannot be exported from prod DB until `JobClip` (or another row) persists `best_moments`. Either add a column, or capture fixtures via a `--record` mode on the agent runtime.
**Effort:** M (human: ~3d / CC: ~30 min per agent)
**Priority:** P2

### Cost cap on live-mode eval runs
**What:** `pytest tests/evals/ --eval-mode=live` has no per-run cost ceiling. Each agent's `cost_per_1k_*_usd` is already in `AgentSpec`. Estimate cost from token counts and refuse to run if `> $20` unless `--allow-cost` is passed.
**Why:** Avoid an accidental 100-fixture run on Gemini 2.5 Pro burning a credit card.
**Effort:** XS (human: ~30min / CC: ~10 min)
**Priority:** P3

### Auto `--shadow` prompt-iteration mode
**What:** Add `--shadow` to `tests/evals/conftest.py` and wire it through `run_eval` so that one pytest run executes BOTH the prod prompt and a candidate prompt against every fixture, judges both, and reports per-fixture deltas. Reuses `app.agents._runtime.run_with_shadow`.
**Why:** Today the iteration loop is "stash + run + unstash + run + diff manually." The harness primitive (`run_with_shadow`) already exists; the eval-side wiring is the missing piece.
**Effort:** S (human: ~3h / CC: ~20 min)
**Priority:** P2

## UX Cleanup (template-first)

### #fallback-removal — Remove subject → inputs.location read-side fallback after 2026-05-16
**What:** Delete `_resolve_user_subject()` in `src/apps/api/app/tasks/template_orchestrate.py` and inline the `inputs["location"]` read at its call sites. Remove the legacy-subject branch in `routes/template_jobs.py` reroll. Delete `tests/pipeline/test_subject_fallback_removed.py`.
**Why:** The fallback covers in-flight jobs created before the rename. After 2026-05-16 that window is closed.
**How:** The xfail in `test_subject_fallback_removed.py` flips on the deadline and CI breaks until the fallback is removed.
**Priority:** P1

### Smarter poster-frame selection
**What:** `app/services/template_poster.py` always seeks 1.5s into the template. For a few templates this lands inside a fade-in and produces a near-black thumbnail (e.g. "How do you enjoy your life?" backfilled to 3.8KB). Add a brightness/variance check on the extracted JPEG and retry at later seek offsets (3s, 5s, 10s) if the frame is too dark or low-variance. Optional: an admin override field on `VideoTemplate` to pin a specific seek time.
**Why:** A few legacy templates ship with fade-ins; the auto-extracted poster looks broken on the homepage. Gradient fallback only kicks in when `thumbnail_gcs_path IS NULL`, not when the poster is technically present but visually empty.
**Priority:** P3

### Typed admin editor for `required_inputs`
**What:** Build a typed UI under `/admin/templates/[id]` that adds/removes/reorders entries in `video_templates.required_inputs`. v1 admins set this by editing the JSON column directly. Also: extend `PATCH /admin/templates/{id}` to accept `required_inputs` (currently it does not — see `routes/admin.py:705 update_template`). Today every template that needs inputs is set up either via a seed script (Dimples Passport) or a one-off backfill script (`scripts/backfill_that_one_trip_to.py`).
**Priority:** P3

### Reanalysis preserves operator overrides
**What:** When `analyze_template_task` regenerates `recipe_cached` from Gemini (clicking "Reanalyze" in admin UI), it overwrites operator-set fields like `subject_part`, position-tool tunings, and other manual edits. The "That one trip to..." backfill (subject_part tags on "lon"/"don" overlays) would silently regress on next reanalysis.
**Why:** Manual recipe edits are durable intent, not a transient Gemini hint. They must survive reanalysis or admins lose work invisibly.
**How:** Either (a) merge a separate `template_overrides` JSONB on top of the regenerated recipe, (b) preserve overlays where `subject_part` or other override fields are set, or (c) prompt with a diff before reanalysis overwrites overrides.
**Effort:** M (human: ~1d / CC: ~30 min)
**Priority:** P2

### PropertyPanel UI for `subject_part`
**What:** Surface the new `subject_part` field (added 2026-05-09 for "That one trip to..." city slicing) in the admin overlay PropertyPanel as a select: "first_half" / "second_half" / "full" / null. Today the field is invisible in the editor — set only via the backfill script — so admins editing the recipe could accidentally drop it.
**Why:** The TS `RecipeTextOverlay` interface (`recipe-types.ts`) carries the field but no form control writes it.
**How:** Add a Select control in `PropertyPanel.tsx` near the existing role/effect/position controls. Plumb through the existing `UPDATE_OVERLAY_FIELD` action.
**Effort:** XS (human: ~1h / CC: ~10 min)
**Priority:** P3

### Sign-in / auth on the new header
**What:** The Nova header has a placeholder "Sign in" button. Real auth is its own project.
**Priority:** P2

### Decision: bring back `/music`
**What:** The `/music` frontend route is deleted but the backend (`routes/music.py`, `music_jobs.py`, beat-sync orchestrate) is preserved. Revisit when product direction on multi-mode (templates vs music sync) is settled.
**Priority:** P3

## Visual Overlay Editor (shipped v0.2.0.0, 2026-04-11)

### Overlay Editor Component Tests (Tier 2)
**What:** React component tests for `OverlayPreview`, `PropertyPanel` (overlay selection case), and `OverlayTimeline` using `@testing-library/react`.
**Why:** Pure logic (overlay-constants.ts) is 100% tested. Component render behavior (visible overlays, role colors, selection ring) is untested. Deferred due to missing @testing-library/react setup.
**How:** Install `@testing-library/react` + `@testing-library/jest-dom`. Write 3 tests: OverlayPreview renders visible overlays at correct positions, PropertyPanel shows overlay form when selection.type === "overlay", OverlayTimeline renders bars with correct role colors.
**Effort:** S (human: ~4h / CC: ~15 min)
**Priority:** P2

### Role-Aware Preview Resolution (V2)
**What:** Add `_LABEL_CONFIG`-style routing to the frontend `resolveOverlayPreview()` to prevent false-positive subject substitution for title-cased imperative phrases like "Watch Now", "Check This".
**Why:** The Python backend uses `_LABEL_CONFIG` routing (template_orchestrate.py:1162) to differentiate subject labels from prefix/CTA labels after `_is_subject_placeholder()` returns true. The TS preview port has no equivalent, so the preview incorrectly substitutes the preview subject into these phrases. Preview-only impact (backend renders correctly), but reduces WYSIWYG fidelity.
**How:** Add a `role`-aware check in `resolveOverlayPreview()`: skip subject substitution for title-cased 2-word inputs when the overlay role is not "label". Alternatively, port the `_LABEL_CONFIG` prefix detection ("welcome", "check", "watch" starts) as a negative filter.
**Effort:** XS (human: ~1h / CC: ~5 min)
**Priority:** P3
**Depends on:** Unify Text Handling PR shipped

## Font System (shipped v0.2.2.0, 2026-04-12)

### Font-Cycle Cycling Font Customization
**What:** Let admins pick which fonts appear during the rapid cycling phase (not just the settle font). Currently, cycling fonts are hardcoded from registry fonts with `cycle_role="contrast"`.
**Why:** Different templates may want different cycling character (e.g., all serifs for editorial, all sans for modern). Currently only the settle font is customizable via `font_family`.
**How:** Add a `cycle_fonts` array field to the overlay recipe. In `_resolve_cycle_fonts()`, use the admin's list instead of the registry contrast fonts. Frontend: multi-select font picker for cycling fonts.
**Effort:** S (human: ~4h / CC: ~15 min)
**Priority:** P3
**Depends on:** Font expansion PR shipped

### Effect Animation Preview (PR2)
**What:** CSS/JS animations in the admin editor preview for font-cycle, fade-in, typewriter effects. Currently, the preview shows static text regardless of effect.
**Why:** WYSIWYG gap: admin sees static text but the video has animated text. Hard to tune timing without seeing the animation.
**How:** Map each effect to CSS animations (font-cycle: rapid font-family swaps, fade-in: opacity transition, typewriter: clip-path reveal). Only in OverlayPreview component.
**Effort:** S (human: ~4h / CC: ~15 min)
**Priority:** P2
**Depends on:** Font expansion PR shipped

## Gemini + Template Mode (shipped 2026-03-23)

### Gemini Integration Tests
**What:** Real end-to-end tests hitting the Gemini API: `tests/integration/test_gemini_analyzer.py`
**Why:** Current tests mock all Gemini calls — a model API change or schema drift would only surface in production.
**How:** Requires `GEMINI_API_KEY` in local env. Write 3 tests: upload+poll, analyze_clip, analyze_template against a real test video fixture.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P2
**Depends on:** GEMINI_API_KEY in local env

### Gemini API Cost + Rate Limit Guard
**What:** Add per-job Gemini token cost tracking and a per-user/per-day quota guard.
**Why:** 9 parallel analyze_clip calls per job. At scale, costs can grow unbounded and ResourceExhausted errors will increase.
**How:** Track token counts in `Job.probe_metadata`; add a Celery pre-task check against a Redis counter.
**Effort:** M (human: ~3 days / CC: ~30 min)
**Priority:** P2 — add before marketing drives volume
**Depends on:** Usage baseline from real jobs

### Multiple Template Support
**What:** Support different template structures for different content categories (tutorial vs. reaction vs. vlog).
**Why:** v1 validates the single-template UX; after validation, multiple templates unlock more use cases.
**Effort:** M (human: ~1 week / CC: ~30 min)
**Priority:** P2 — after v1 template validated in production

### Librosa-Based Beat Detection
**What:** Replace or augment FFmpeg energy-peak beat detection with librosa's onset detection and beat tracking.
**Why:** FFmpeg `silencedetect`/`astats` catches energy transients (drum hits, bass drops) but misses melodic transitions. librosa provides proper onset detection, tempo estimation, and beat tracking with significantly higher accuracy.
**How:** Add librosa dependency (~50MB with numpy/scipy). Replace `_detect_audio_beats()` in `template_orchestrate.py`. Compare quality against FFmpeg approach on 20+ templates.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P3 — upgrade path if FFmpeg+Gemini beat detection proves insufficient
**Depends on:** ~~Beat sync feature shipped~~ ✓ unblocked by v0.3.0.0

### Re-Analyze Existing Templates for Beat Data
**What:** One-time migration task to re-run `analyze_template_task` on all existing templates so they get `beat_timestamps_s` in their cached recipe.
**Why:** Existing templates have `recipe_cached` without `beat_timestamps_s`. They work (backward-compatible default=[]) but don't benefit from beat sync until re-analyzed.
**How:** Admin endpoint or management command that queries all templates with `analysis_status="ready"` and dispatches `analyze_template_task` for each.
**Effort:** XS (human: ~2 hours / CC: ~5 min)
**Priority:** P2 — run after beat sync code ships
**Depends on:** ~~Beat sync feature shipped~~ ✓ unblocked by v0.3.0.0

### TikTok Content API Application
**What:** Submit TikTok Content API application at developer.tiktok.com.
**Why:** 4-8 week approval window. Clock must start now even though platform posting (Phase 2) is deferred. This is a time-gated blocker — every day of delay pushes the TikTok launch date by a day.
**How:** Create TikTok developer account, submit Content API application form (15 min).
**Effort:** XS (15 minutes, one-time form)
**Priority:** P1 — submit by 2026-03-28 at latest
**Depends on:** Nothing

### Platform Posting (Phase 2)
**What:** POST /post endpoint, OAuth token refresh, Instagram/YouTube/TikTok upload integrations.
**Why:** Core monetization path — users want one-click posting, not just clip downloads.
**How:** Separate PR; OAuth infra already in models. See agents/DECISIONS.md.
**Effort:** L (human: ~2 weeks / CC: ~2 hours)
**Priority:** P1 — next sprint after template validation

---

## Template Prompt Improvement (from CEO review 2026-03-24)

### Color Grading Visual Verification
**What:** Post-merge, visually review color grading output on 3-5 templates within 48 hours. Tune the FFmpeg colorbalance/eq values if they look over- or under-processed.
**Why:** The color grade → FFmpeg filter mappings are starting-point values based on theory, not empirical testing. Subtle adjustments may be needed.
**How:** Run template jobs, inspect output videos. Compare warm/cool/vintage/desaturated slots against the original template.
**Effort:** XS (human: ~1 hour / CC: N/A — manual visual review)
**Priority:** P2 — within 48 hours of merge
**Depends on:** Template prompt improvement merged

### A/B Analysis Mode Evaluation
**What:** After running both single-pass and two-pass Gemini analysis on 3-5 templates, compare recipe quality and decide which to keep as default. Remove the losing approach to prevent dead code.
**Why:** Outside voice challenged the two-pass assumption. A/B ships both; need to evaluate and converge.
**How:** Run `analyze_template()` with `analysis_mode="single"` and `"two_pass"` on the same templates. Compare: creative_direction quality, field accuracy (transition_in, color_hint), and overall recipe usefulness.
**Effort:** XS (human: ~2 hours / CC: ~5 min for cleanup)
**Priority:** P2 — within 1 week of merge
**Depends on:** Template prompt improvement merged

### Speed Ramp FFmpeg Implementation
**What:** Implement `setpts` filter for `speed_factor` per slot. Currently schema-only.
**Why:** Speed ramping is a signature TikTok editing move — high visual impact.
**How:** First investigate: does template mode mute clip audio? If yes, simple `setpts=PTS/{speed_factor}` in `-vf` chain. If no, need `atempo` for audio compensation. Template mode mixes clip audio with template music track — need to understand the audio handling before touching video speed.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P3 — after audio handling is understood
**Depends on:** Template prompt improvement merged (for speed_factor data)

---

## Music Beat-Sync (shipped v0.3.0.0, 2026-04-17)

### Auth on POST /music-jobs
**What:** Replace the synthetic `SYNTHETIC_USER_ID` stub in `music_jobs.py` with real user authentication via `get_current_user(db)`.
**Why:** `POST /music-jobs` is currently unauthenticated — any caller can trigger Gemini API calls and GCS reads. Acceptable for internal MVP; must be fixed before public launch. See `src/apps/api/app/routes/music_jobs.py:23`.
**How:** Wire in the existing `get_current_user` dependency (already used by other routes). Add user_id to music job records for attribution.
**Effort:** XS (human: ~1h / CC: ~5 min)
**Priority:** P1 — required before public launch
**Depends on:** Auth infrastructure (already exists in other routes)

---

## P1 — Required before GTM campaigns go live

### UTM Capture
**What:** Add utm_source, utm_medium, utm_campaign columns to `waitlist_signups` table.
**Why:** Organic TikTok (and any future channel) sends tagged URLs — without server-side capture, attribution is lost even if analytics JS fires.
**How:** New Alembic migration adds 3 nullable VARCHAR columns; `POST /api/waitlist` reads them from query params in the request.
**Effort:** S (human: ~1 day / CC: ~5 min)
**Priority:** P1 — add before first TikTok campaign goes live
**Depends on:** Waitlist table must exist (landing page PR must be merged first)

---

### Resend Confirmation Email
**What:** Send a transactional "You're on the Nova waitlist" email on successful signup.
**Why:** Without confirmation, ~15% of collected emails will be typos, disposable addresses, or bots — poisoning the list before launch. A clicked confirmation is a real lead signal.
**How:** Add Resend (or Postmark) as dependency; `POST /api/waitlist` dispatches a Celery task to send the email after insert. Email: subject "You're on the Nova waitlist", body: value prop + "we'll reach out when your spot opens."
**Effort:** S (human: ~1 day / CC: ~10 min)
**Priority:** P1 — add before GTM campaigns drive significant traffic
**Depends on:** Resend API key (free tier: 3k emails/month)

---

## Template Fidelity (from CEO/eng review 2026-03-25)

### Automated Visual Regression Testing
**What:** On every pipeline code change, auto-run 3-5 reference templates, generate eval grids, diff against golden baselines.
**Why:** Catches quality regressions automatically. Currently manual-only via eval harness.
**How:** CI step that runs template jobs on reference videos, generates per-slot eval grids, compares pixel-diff against golden baselines. Fail if delta > threshold.
**Effort:** S (human: ~1 day / CC: ~30 min)
**Priority:** P2
**Depends on:** Eval harness shipped (this PR)

### Converge Overlay Rendering to ASS-Only
**What:** Migrate font-cycle from multi-PNG to ASS rendering.
**Why:** Reduces two overlay code paths (PNG + ASS) in text_overlay.py and reframe.py to one. Lower maintenance burden.
**How:** Replace `_render_font_cycle()` PNG generation with ASS karaoke tags using rapid style switching. Update reframe.py to remove PNG overlay path.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P3
**Depends on:** ASS animated overlays shipped and validated (this PR)

---

## Vercel Frontend Deploy (added 2026-04-06)

All items completed 2026-04-06:
- ~~Regex CORS for preview deployments~~ — done via `allow_origin_regex` in `main.py`
- ~~Connect Vercel GitHub Integration~~ — done via REST API
- ~~Disable Deployment Protection~~ — done, preview-only
- ~~Update Google OAuth Redirect URIs~~ — done in Google Cloud Console
- ~~GCS bucket CORS~~ — done via `gsutil cors set`
