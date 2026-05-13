# Nova — Deferred Work

## Agent evals — Phase 2

**Completed in v0.4.6.0 (2026-05-10):**
- ~~Phase 2 evals for the 3 in-pipeline agents (transcript, platform_copy, audio_template)~~ — structural checks + rubrics + test entry points + golden fixtures wired
- ~~Cost cap on live-mode eval runs~~ — `--allow-cost` flag + preflight estimator with $20 cap, replay-skipped, zero-cost-spec warning
- ~~Auto `--shadow` prompt-iteration mode~~ — `--shadow-prompts-dir` flag overlays candidate prompts on prod, side-by-side run + Δ scoring, live-only

### Re-run creative_direction on 10 templates with under-baked descriptions
**What:** When seeding eval fixtures, the export script's structural validator rejected 10 of 14 templates' `creative_direction` text as below the 50-word floor (some as short as 4 words). These templates either pre-date two-pass mode or the model produced a near-empty output that nobody caught. Re-run two-pass analysis on: `that_one_trip_to`, `morocco`, `just_fine_test2`, `just_fine___sunset_reassurance`, `impressing_myself`, `football_face_hook`, plus 4 others surfaced by `python scripts/export_eval_fixtures.py 2>&1 | grep '\[reject\]'`.
**Why:** A 4-word creative_direction means Pass 2 (`template_recipe`) was running with no editorial guidance, so the recipe quality on those templates is whatever Gemini produced cold. Real user-facing impact: the templates' `copy_tone`, `pacing_style`, `transition_style` are likely generic or wrong.
**How:** Trigger reanalysis from the admin UI for each template, OR add a one-off script that calls `analyze_template_task` with `analysis_mode="two_pass"` for each. Verify by re-running `scripts/export_eval_fixtures.py` — rejected count should drop.
**Effort:** S (human: ~1h / CC: ~10 min)
**Priority:** P2

### Phase 2.5 — evals for the unwired-four agents
**What:** Extend `tests/evals/` to cover `text_designer`, `transition_picker`, `clip_router`, `shot_ranker`. The 3 in-pipeline agents (`transcript`, `platform_copy`, `audio_template`) shipped in v0.4.6.0; these four agent classes exist in `app/agents/` but nothing in production calls them yet, so their evals should land alongside the PR that wires each one into the pipeline.
**Why:** Bumping `prompt_version` on a prompt that nothing executes is signal-free. Once these agents are actually invoked, prompt drift starts to matter — and at that point, the eval harness should already cover them.
**How:** Mirror the Phase 1 / Phase 2 pattern: rubric markdown, structural-check function, test entry-point file, hand-authored golden fixtures (no prod-snapshot path until DB persistence is added).
**Effort:** M (human: ~2d / CC: ~20 min per agent, when each lands)
**Priority:** P2
**Depends on:** each agent being wired into the pipeline first

### Run reanalyze script + re-export `creative_direction/prod_snapshots/`
**What:** Operator-side follow-up to the script that landed in v0.4.6.0. Run `cd src/apps/api && .venv/bin/python scripts/reanalyze_underbaked_templates.py --auto-detect --watch` against prod DB. Then re-run `scripts/export_eval_fixtures.py` so the freshly-baked `creative_direction` text replaces the rejected fixtures.
**Why:** v0.4.6.0 only landed the script. The actual reanalysis requires live DB + Celery worker + Gemini API, so it can't run from a CI-style /ship.
**Effort:** XS (operator-run, ~10 min once Gemini quota is available)
**Priority:** P2

### `--record` mode on agent runtime
**What:** A capture path that writes Gemini's actual JSON response (not a JSON dump of the parsed output) into a fixture file in one shot. Unblocks `clip_metadata/prod_snapshots/` (best_moments aren't persisted in DB) and gives `replay` mode a higher-fidelity canary against `live` mode.
**Why:** Today `scripts/export_eval_fixtures.py` writes `json.dumps(parsed_output)` as `raw_text`. If `parse()` does post-processing the dump skips, replay diverges from live silently. A real captured response closes that gap.
**Effort:** S (human: ~4h / CC: ~30 min)
**Priority:** P3

### Per-PR auto-eval on prompt changes
**What:** Detect when a PR touches files under `src/apps/api/prompts/` and auto-trigger structural-only evals for the affected agents on the PR. Today `agent-evals.yml` is `workflow_dispatch`-only.
**Why:** Catch prompt regressions before merge instead of after.
**Effort:** S (human: ~3h / CC: ~20 min)
**Priority:** P3

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

## clip_metadata: best_moments clustered at clip start (added 2026-05-13)

**What:** `nova.video.clip_metadata` (gemini-2.5-flash) returns 3 best_moments compressed into <0.5 seconds with near-identical energies (range 0.5-1.0) on a meaningful fraction of prod clips.
**Why:** Surfaced by the first run of `pytest tests/evals/test_clip_metadata_evals.py --with-judge` on 5 prod snapshots pulled from the Redis clip_analysis cache — judge avg 2.5/5 vs 3.5 threshold. 3 of 5 fixtures exhibit the pattern (clip_2c750692, clip_04022f9e, clip_1fd09d23). The matcher downstream effectively has one moment to choose from, defeating the point of best_moments.
**How:** Likely cause is Gemini interpreting `start_s`/`end_s` in some normalized unit when given a short segment, or a prompt that doesn't enforce timestamp spread. Investigate the `analyze_clip` prompt template — add an explicit instruction to spread moments across the clip duration and require non-trivial energy variation. Bump `prompt_version` in `ClipMetadataAgent.spec` and re-run live eval.
**Evidence:** `src/apps/api/tests/fixtures/agent_evals/clip_metadata/prod_snapshots/clip_2c750692.json` — all 3 moments span 0.0-0.10s.
**Effort:** S (human: ~3hr / CC: ~30 min — prompt iteration + re-run live eval against the same 5 fixtures)
**Priority:** P1 — affects every job
**Depends on:** none

---

## ~~Langfuse: only 2 of 6 prod agents are tracing~~ — RESOLVED 2026-05-13 (caching, by design)

**Resolution:** Not a tracing bug. All four "missing" agents do go through `Agent.run()` via the shims in `app/pipeline/agents/gemini_analyzer.py` and would trace correctly *if* they were invoked per-job. They aren't: their outputs are persisted to `VideoTemplate.recipe_cached` / `MusicTrack.recipe_cached` at admin upload time, and every prod job reads the cached row from Postgres (`template_orchestrate.py:478-481`, `music_orchestrate.py:286,561`). `TranscriptAgent` is only called from the legacy 3-clip pipeline (`app/tasks/orchestrate.py:97`), which isn't on the template-mode hot path. Only `clip_metadata` and `platform_copy` have per-job inputs, so they invoke `Agent.run()` on every job — matching the observed 12 + 6 / 10 sessions ratio.

Full write-up in `src/apps/api/OBSERVABILITY.md` under "What the first prod query revealed".

**Optional follow-up (P3):** emit a lightweight Langfuse `client.event(name="template_recipe_cache_hit", ...)` from `_run_template_job` and `_run_music_job` so per-job dashboards show which cached recipe each job used (cost/latency visibility for the cached path). Not needed for correctness — the cached recipe is already in Postgres and the prior `agent_run` trace from admin upload time is still queryable in Langfuse.

**Original report below for context.**

**What:** Querying Langfuse for `source:prod` traces over 24h returns only `clip_metadata` and `platform_copy` — the other 4 prod-wired agents (`template_recipe`, `creative_direction`, `transcript`, `audio_template`) are missing entirely. 18 traces, 10 distinct job sessions, 0 traces from the missing 4 agents.
**Why:** Surfaced via SDK query `client.api.trace.list(tags=["source:prod"], from_timestamp=now-24h)` after Lane A verification on 2026-05-13.
**Effort (was):** S (human: ~2hr / CC: ~30min)
**Priority (was):** P1 — downgraded after root-cause analysis showed expected behavior.

---

## Langfuse: online judge (Loop B) is not scoring any prod traces (added 2026-05-13)

**What:** With `NOVA_ONLINE_EVAL_SAMPLE_RATE=0.05` confirmed set on Fly and 18 source:prod traces over 24h, zero traces have `judge_*` scores attached. Statistically should be ~1 judged trace per 24h at current volume.
**Why:** Surfaced alongside the agent-missing finding above. Loop A scores ARE attaching correctly (verified on the 5 eval traces). So the trace + score_trace machinery works — the gap is specifically in the worker-side dispatch.
**How:** Check three suspects: (1) `ANTHROPIC_API_KEY` may not be reachable on the worker process group on Fly even though `fly secrets` shows it deployed — secrets propagate per process. `fly ssh console --process-group worker -C printenv ANTHROPIC_API_KEY`. (2) Rubric files must be present in the worker container — verify `tests/evals/rubrics/*.md` is included in the Dockerfile COPY. The Dockerfile copies `app/`, `assets/`, `prompts/`, `alembic.ini` per CLAUDE.md — `tests/` is NOT copied. **This is likely the root cause** — the worker can't find the rubric file because the entire `tests/` directory is excluded from the prod image. (3) Confirm `score_trace_async` is registered as a Celery task and a worker is consuming it.
**Effort:** S (human: ~1hr / CC: ~30min — likely a 2-line Dockerfile change plus verification)
**Priority:** P1 — Loop B is what makes online observability actually useful; right now it's posting traces but not scoring them
**Depends on:** none

---

## Yasin's prompt rewrite — follow-ups (added 2026-05-14)

These TODOs were filed when the first wave of Yasin's prompt rewrites shipped (`clip_metadata`, `template_recipe`, `text_designer`, `transition_picker`, all prompt_version="2026-05-14"). `clip_router` and `shot_ranker` were deliberately deferred — see the first two items below.

### ~~Live-eval gate broken on fixture file-URI format~~ — RESOLVED in v0.4.9.0
**Resolved:** Option 3-equivalent shipped at the eval-harness layer (not the agent base class — surgical scope). New `tests/evals/_fixture_uploader.py` downloads bucket-relative paths from GCS and uploads to Gemini Files API at test time, substituting the `files/<id>` URI into the agent input. Mirrors prod's `gemini_upload_and_wait` flow exactly. Per-session cache keeps the upload cost bounded (~$0.10–0.20 per full live-eval run). 23 unit tests fence each leg of the path.

### Vertex AI service-account auth swap (P2 follow-up to v0.4.9.0)
**What:** Replace the v0.4.9.0 inline-upload fix with a Vertex AI auth path on `GeminiClient`. Use `GOOGLE_SERVICE_ACCOUNT_JSON` (already in Fly secrets for GCS), prepend `gs://nova-videos-dev/` to all fixture paths, drop the per-test upload step entirely.
**Why:** v0.4.9.0 works but uploads ~17 fixtures × ~5–50MB each on every live-eval run. Vertex auth removes the cost, unifies dev/prod auth, and enables `gs://` URIs in production too (downstream value: skip the Files API upload step in `template_orchestrate` when the source already lives in GCS).
**How:** Add Vertex AI auth code path to `app/agents/_model_client.py::GeminiClient` keyed on URI shape (`gs://` → Vertex SA, `files/<id>` → Studio key). Reconcile with the existing `_get_client()` cache. Update fixtures to `gs://...` form. Document the new env var hierarchy.
**Blast radius:** changes production GeminiClient call path. Needs careful rollout — feature-flag or staged.
**Effort:** M (human: ~1d / CC: ~1h)
**Priority:** P2

### ~~Eval scaffolding for `clip_router`~~ — RESOLVED in v0.4.10.0
**Resolved:** `tests/evals/test_clip_router_evals.py` + `tests/evals/rubrics/clip_router.md` + 3 hand-authored golden fixtures shipped. Rubric dimensions: slot_type_fit, energy_match, sequence_variety, rationale_quality. Yasin-style prompt rewrite (`prompt_version=2026-05-15`) shipped in the same PR. Live-eval baseline will be established when an operator runs the suite with `--with-judge --eval-mode=live --allow-cost` from an environment with creds.

### ~~Eval scaffolding for `shot_ranker`~~ — RESOLVED in v0.4.10.0
**Resolved:** `tests/evals/test_shot_ranker_evals.py` + `tests/evals/rubrics/shot_ranker.md` + 3 hand-authored golden fixtures shipped. Rubric dimensions: rank_1_hook_strength, set_variety, description_quality, thematic_fit. Yasin-style prompt rewrite (`prompt_version=2026-05-15`) shipped in the same PR. Live-eval baseline will be established when an operator runs the suite with `--with-judge --eval-mode=live --allow-cost` from an environment with creds.

### ~~Retroactive eval scaffolding for `text_designer`~~ — RESOLVED in v0.4.11.0
**Resolved:** Full four-file scaffold shipped (test entry point, rubric with 4 dimensions, structural floor in `check_text_designer`, 4 hand-authored golden fixtures pinning the prompt's documented calibration patterns). Live-eval baseline will be established when an operator runs `pytest tests/evals/test_text_designer_evals.py --with-judge --eval-mode=live --allow-cost` with creds.

### ~~Retroactive eval scaffolding for `transition_picker`~~ — RESOLVED in v0.4.11.0
**Resolved:** Full four-file scaffold shipped. The new structural check also surfaced a latent `parse()` bug — `float(...) or 0.3` silently coerced `duration_s=0.0` to `0.3` (Python treats 0.0 as falsy), overriding every hard-cut/none output the prompt explicitly specified as duration 0.0. Fixed in the same PR.

### Renderer support for `match-cut` as a distinct transition
**What:** Promote `match-cut` from a "collapse to hard-cut" mapping (today's behavior) to a first-class transition in `app/pipeline/transitions.py`. Update `_VALID_TRANSITION_TYPES` and `_VALID_TRANSITIONS` to include it, add FFmpeg motion-vector continuity hinting (`xfade` with appropriate easing), update the prompts' vocabulary-reconciliation tables to remove the collapse.
**Why:** Yasin's prompts identify match-cuts as a meaningful editorial signal (action continuity from shot A to shot B). Today we lose that distinction at the prompt boundary — the schema can't represent it. Real impact: templates with deliberate match-cut grammar render as generic hard-cuts.
**How:** Decide the FFmpeg approach first — `xfade=fadeblack` is too soft, motion-aware blending might need OpenCV. Then visual eval on 3–5 templates known to have match-cuts (TBD which fixtures).
**Effort:** M (human: ~2d / CC: ~1h)
**Priority:** P3
**Depends on:** clear FFmpeg approach for motion-continuity transitions

### Renderer support for `barn-door-open` as a distinct animation
**What:** Add `barn-door-open` as a first-class interstitial type in `app/pipeline/interstitials.py` and `_VALID_INTERSTITIAL_TYPES`. Today every "bars opening" moment collapses to `curtain-close` (same animation, played in reverse where applicable). A distinct enum lets the prompt and recipe carry the directionality.
**Why:** Smallest of the three renderer expansions — `curtain-close` already uses `geq` pixel animation; reversing the direction is a parameter flip.
**How:** Add the reverse-direction code path to `_render_curtain_close()` (or split into `_render_curtain_open()`). Update the prompt vocabulary tables.
**Effort:** S (human: ~6h / CC: ~30 min)
**Priority:** P3

### Promote `speed-ramp` to a first-class transition
**What:** Today `speed_factor` is a per-slot field and `speed-ramp` collapses to `hard-cut + speed_factor` in the prompt vocabulary. A first-class `speed-ramp` transition would carry the *direction* of the ramp (slow-to-fast vs fast-to-slow) and the cut point, which `speed_factor` alone can't.
**Why:** Speed-ramp is a signature TikTok editing move and one of the biggest editorial losses from the current vocabulary collapse.
**How:** This is part of "Speed Ramp FFmpeg Implementation" already listed elsewhere in this file — see the existing TODO. The renderer work and the transition-enum work should land together to avoid having a schema field that nothing reads.
**Effort:** Bundled with the existing "Speed Ramp FFmpeg Implementation" TODO
**Priority:** P3
**Depends on:** Speed Ramp FFmpeg Implementation (existing TODO above)

---

## Vercel Frontend Deploy (added 2026-04-06)

All items completed 2026-04-06:
- ~~Regex CORS for preview deployments~~ — done via `allow_origin_regex` in `main.py`
- ~~Connect Vercel GitHub Integration~~ — done via REST API
- ~~Disable Deployment Protection~~ — done, preview-only
- ~~Update Google OAuth Redirect URIs~~ — done in Google Cloud Console
- ~~GCS bucket CORS~~ — done via `gsutil cors set`
