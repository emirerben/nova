# Nova — Deferred Work

## UX Cleanup (template-first)

### #fallback-removal — Remove subject → inputs.location read-side fallback after 2026-05-16
**What:** Delete `_resolve_user_subject()` in `src/apps/api/app/tasks/template_orchestrate.py` and inline the `inputs["location"]` read at its call sites. Remove the legacy-subject branch in `routes/template_jobs.py` reroll. Delete `tests/pipeline/test_subject_fallback_removed.py`.
**Why:** The fallback covers in-flight jobs created before the rename. After 2026-05-16 that window is closed.
**How:** The xfail in `test_subject_fallback_removed.py` flips on the deadline and CI breaks until the fallback is removed.
**Priority:** P1

### Real template thumbnails
**What:** Render a per-template 9:16 still or 3-second loop, upload to GCS, and sign URLs in `/templates`. Today the API returns `thumbnail_url: null` and the gallery falls back to a tone-colored gradient.
**Priority:** P2

### Typed admin editor for `required_inputs`
**What:** Build a typed UI under `/admin/templates/[id]` that adds/removes/reorders entries in `video_templates.required_inputs`. v1 admins set this by editing the JSON column directly.
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
