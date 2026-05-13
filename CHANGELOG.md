# Changelog

All notable changes to this project will be documented in this file.

## [0.4.8.0] - 2026-05-13

### Changed
- **Reworked the prompts for four Big-3 agents using Yasin's drafts.** `nova.video.clip_metadata`, `nova.compose.template_recipe`, `nova.layout.text_designer`, and `nova.layout.transition_picker` get longer, structured prompts with calibration anchor tables (hook scores 0–10, energy levels 0–10, transition energy-deltas), a shared hook-type vocabulary (Visual / Verbal / Outcome / Gap / Contrast), explicit decision principles, and "decisions, not suggestions" voice. `prompt_version` bumped to `2026-05-14` on all four agents.
- **`clip_metadata` prompt fuses the new structure with the existing 5 HARD RULES verbatim.** Yasin's draft dropped the span ≥60% / no-overlap-clusters / ≥1s duration / energy variation ≥2.0 / no-generic-labels constraints that were added in v0.4.7.0 to fight the clustered-moments bug. Those rules are preserved exactly; Yasin's hook taxonomy, anchor tables, niche action vocabulary, and "Omit > pad" framing wrap them.
- **`template_recipe` prompts (`analyze_template_single.txt` + `analyze_template_pass2.txt`) carry an explicit Yasin → code-accepted vocabulary reconciliation table.** Yasin's drafts referenced 16 transitions, 8 compound color grades, and aspirational slot types that the code's Pydantic `Literal` types reject. The new prompts list every cinematic concept Yasin mentioned alongside the actual enum to emit (`match-cut → hard-cut`, `barn-door-close → curtain-close`, `teal-orange → warm`, `a24-pastel → desaturated`, etc.) so Gemini cannot drift into invalid output even when reasoning about richer concepts.
- **`text_designer` inline prompt grows from ~1.2KB to ~5KB** with hierarchy-by-placeholder-kind, slot-position awareness (slot 1 = signature hook, mid-slots lighter, final returns to bolder), tone → typography mapping for both current schema tones (`casual, formal, energetic, calm`) and aspirational tones (`chill-lofi, dramatic-cinematic, …`), explicit `start_s` timing rules, and three signature calibration patterns. Agent still runs in shadow mode against `_LABEL_CONFIG` with `max_attempts=1` so cost is bounded.
- **`transition_picker` inline prompt restructured around editor's grammar.** Rule 0 (default fidelity wins when the pair fits), energy-delta as the primary signal (0–1.5 continuation → hard-cut; 1.5–3 step → zoom-in; 3–5 major shift → curtain-close; 5+ chapter break), camera-movement compatibility rules, pacing-style modulation, and a duration envelope table. Vocabulary still strictly limited to the 6 transitions the renderer accepts.

### Deferred
- `clip_router` and `shot_ranker` prompt rewrites deliberately deferred. Editorial-ordering changes need eval scaffolding before they ship — TODOS.md captures the new evals as P2 gates. Yasin's drafts for both agents are preserved in the plan file at `~/.claude/plans/let-s-improve-the-prompts-rosy-floyd.md`.

### Known issue
- **Live-eval gate blocked on test fixture URI format.** CLAUDE.md mandates `pytest tests/evals/<agent>_evals.py --with-judge --eval-mode=live` before merging any prompt change, but every fixture's `file_uri` is a bucket-relative path (`templates/<uuid>/reference.mp4`, `clips/<id>.mp4`) and the current Studio Gemini key cannot resolve those. All 17 fixtures fail with `400 INVALID_ARGUMENT: Unsupported file URI type` in <8s — $0 spent, prompts never reach the model. Replay-mode evals (no API call) still pass 17/17. Three possible fixes filed as P1 TODO: backfill fixtures with Files API IDs, switch eval auth to Vertex service account, or auto-upload bucket paths inside `media_uri()` at call time.

## [0.4.7.2] - 2026-05-13

### Changed
- New `.github/workflows/docker-build.yml` runs `docker build` against the prod Dockerfile on every PR that touches `Dockerfile`, `.dockerignore`, or `src/apps/api/**`. Catches Dockerfile/`.dockerignore` coupling bugs on the PR instead of at deploy-to-Fly time — the same class of bug that broke the v0.4.7.0 deploy (`f156c19`) and required hotfix #119 (`121a6e9`). Uses `docker/build-push-action@v5` with GitHub Actions cache backend so warm runs stay ~30-60s; no Fly tokens or build minutes consumed.
- `CLAUDE.md` gains a "Lessons from prod" section documenting the Dockerfile/`.dockerignore` coupling rule and pointing at the new CI check.

## [0.4.7.0] - 2026-05-13

### Fixed
- **Loop B online judging is now live in prod.** The worker container was missing `tests/evals/rubrics/*.md` because the prod Dockerfile copied `app/`, `assets/`, `prompts/`, `scripts/`, and `alembic.ini` but not `tests/`. `_online_eval.py` resolves the rubric path via `Path(__file__).parent.parent.parent / "tests" / "evals" / "rubrics"`, so the gate `_rubric_path_for(agent_name).exists()` silently returned False on every dice-roll. With `LANGFUSE_*` keys + `ANTHROPIC_API_KEY` + `NOVA_ONLINE_EVAL_SAMPLE_RATE=0.05` already deployed on Fly, this Dockerfile change is the missing piece: ~15 judge calls/day, ~$5/month, scored traces appearing in Langfuse within minutes of deploy.
- **`clip_metadata` agent no longer returns moments compressed into the first 0.5 seconds with identical energies.** Five captured prod fixtures showed `best_moments` clustered at clip start (e.g. all three moments inside a 0.0–0.10s window at energy 7.0), defeating the downstream matcher. The `analyze_clip.txt` prompt now enforces five hard rules with concrete thresholds — moments must span ≥60% of the analyzed segment, adjacent starts must differ by ≥2.0s, each window must be ≥1.0s long, energy range across moments must be ≥2.0, and "moment 1/2/3" labels are forbidden — plus a self-check instruction before emitting JSON. `prompt_version` bumped from `2026-05-09` → `2026-05-13`.

### Added
- `tests/fixtures/agent_evals/clip_metadata/` — 5 prod_snapshots captured from the Redis `clip_analysis` cache (the canonical regression archive for the clustered-moments bug, scoring 2.5/5 against the rubric) plus 2 hand-authored golden fixtures demonstrating compliant output (scoring 4.50/5 and 4.75/5). Replay-mode eval has positive controls for the first time; the prod snapshots stay red until the new prompt rolls out and fresh fixtures get captured.
- `scripts/export_clip_metadata_fixtures.py` — pulls clip_metadata outputs from the Redis cache and emits them as eval fixtures. Necessary because `best_moments` are not persisted in Postgres (only the chosen window per JobClip survives). Bucketed sampling by speech / moment density for diversity. Supports `--stdout-json` for piping out of `fly ssh console`.

### Changed
- `OBSERVABILITY.md` — added a "Verified loops" section documenting the exact env vars confirmed deployed on Fly (date-stamped), the SDK query that proves Loop A traces land in Langfuse with `source:eval` + `judge_*` scores per dimension, and the cross-loop findings surfaced by the activation work (only 2 of 6 prod agents currently appear in traces — caching is the answer, not a bug).
- `TODOS.md` — three new P1 entries: (1) clip_metadata clustered-moments bug (now fixed by this PR, kept for traceability), (2) Langfuse trace gap on 4 of 6 prod agents (resolved as docs-only after investigation — caching by design), (3) Loop B worker rubric path (resolved by this PR).

## [0.4.6.0] - 2026-05-10

### Added
- Phase 2 evals: structural checks, rubrics, and per-fixture pytest gates for the three remaining in-pipeline LLM agents — `transcript` (word-level timestamps + `low_confidence` calibration), `platform_copy` (TikTok/IG/YouTube native voice + placeholder leakage + duplicate-hook detection), and `audio_template` (slot-duration arithmetic + monotonic beat list + non-empty style metadata + valid `color_grade`). Each ships with at least one hand-authored golden fixture; `prod_snapshots/` populates from a live DB export.
- `--shadow-prompts-dir <path>` flag: live-mode-only side-by-side run of prod and a candidate prompt overlay. Per-fixture summary prints `primary_avg=… shadow_avg=… Δ=…`. Implemented via a `prompt_loader._get_raw` overlay context manager so any prompt file not in the candidate dir falls through to prod. Closes the prompt-iteration loop in one pytest run instead of stash-and-diff.
- `--allow-cost` flag + automatic live-mode preflight: estimates token cost per fixture × `AgentSpec.cost_per_1k_*_usd`, refuses to run if total > $20 unless overridden. Heuristic is deliberately pessimistic (chars/3 input + fixed 1500 output). Replay mode skips the gate entirely. Prints a warning when an agent's spec has zero cost so the gate isn't silently bypassed.
- `scripts/reanalyze_underbaked_templates.py` one-off: snapshots `recipe_cached` to `/tmp/`, dispatches `analyze_template_task` (two-pass mode), optionally polls until each template reaches `ready` or `failed`. Auto-detects templates whose `creative_direction` has fewer than 50 words.
- `scripts/export_eval_fixtures.py` extended to walk `Job.transcript`, `JobClip.platform_copy`, and `MusicTrack.recipe_cached` so prod-snapshot fixtures for the new agents can be exported alongside the Phase 1 set.
- Eval dashboard (`scripts/build_eval_dashboard.py`) now surfaces the three new agents with their fixtures (both `prod_snapshots/` and hand-authored `golden/`) in the Agents tab. Per-agent fixture picker labels `golden` cases distinctly so they don't collide with prod exports.
- `.github/workflows/agent-evals.yml` exposes `transcript`, `platform_copy`, and `audio_template` as workflow_dispatch agent choices. Workflow now passes `--allow-cost` so the new gate doesn't block CI runs.

### Changed
- `runners/eval_runner.py`: `_build_agent_class_for` dispatches all six in-pipeline agents. Added a `_RUBRIC_FILENAME_OVERRIDES` map so `nova.audio.template_recipe` (audio_template) and `nova.compose.template_recipe` no longer collide on a shared rubric file.
- `tests/evals/README.md` rewritten: documents all six in-pipeline agents, both prompt-iteration loops (manual stash and auto `--shadow-prompts-dir`), the cost cap, and the limitations of `prod_snapshots/` fixtures (`raw_text` is a JSON dump of the persisted output, not the original Gemini response).

### Tests
- 5 new unit tests for the shadow context manager (round-trip + exception-path restore) and `estimate_live_cost` (per-agent breakdown, zero-cost-spec warning, unknown-agent skip). Coverage of new code paths: 8/13 (62%) by count; load-bearing structural checks at 100%.

## [0.4.5.0] - 2026-05-10

### Added
- Per-agent quality eval harness for the Big 3 LLM agents (`template_recipe`, `clip_metadata`, `creative_direction`). Combines deterministic structural assertions (slot durations sum to total ±5s, energy ∈ [0,10], valid enums, overlay-bounds, football-filter compliance, creative-direction topic coverage) with an opt-in Claude Sonnet 4.6 LLM-as-judge that scores outputs against per-agent rubrics. Sits on top of the existing `app/agents/_runtime` primitives (`ModelClient`, `Agent.run`, `run_with_shadow`) — no new agent abstractions added.
- Replay mode (`pytest tests/evals/`) is the default: 0 cost, no network, runs against recorded `raw_text` cassettes from production templates. Live mode (`--eval-mode=live`) actually re-calls Gemini for prompt-iteration A/B testing. Judge mode (`--with-judge`) adds Claude scoring against the rubric.
- `scripts/export_eval_fixtures.py` ports `VideoTemplate.recipe_cached` and the `creative_direction` text from the local DB into JSON cassettes under `tests/fixtures/agent_evals/`. Validates each fixture against the structural floor before writing — broken DB rows surface as `[reject]` lines instead of breaking CI.
- `scripts/inspect_eval_fixtures.py` pretty-prints what each agent returned for every template (slot tables, interstitials, creative_direction body, raw JSON dumps) for fast CLI inspection.
- `scripts/build_eval_dashboard.py` generates a self-contained HTML dashboard at `.dev/agent-evals-report/dashboard.html` with five tabs (Overview · Agents · Templates · Issues · Tests). The Agents tab shows the full pipeline flow, every agent's spec / prompt files / `render_prompt()` source / Pydantic schemas / per-template returns. `--watch` rebuilds on fixture, prompt, or agent-source change. `--serve` adds an HTTP server with browser auto-reload over a 2s heartbeat.
- `tests/evals/` ships 67 unit tests (27 structural assertions, 11 judge tests, 15 runner tests, 14 parametrized fixture tests). Default CI path runs the structural-only flavor (no secrets needed, ~30s). The `agent-evals.yml` GitHub Actions workflow is `workflow_dispatch`-only for the live + judge run.
- Editorial dark/light dashboard theme — Fraunces serif display + Inter body + JetBrains Mono code, oxblood accent, `prefers-color-scheme` aware. Numbered flow rail diagrams the pipeline, agent cards expand into magazine-style detail panels with syntax-highlighted Python source for `render_prompt()`.

### Changed
- `pyproject.toml` adds `anthropic>=0.40` to dev dependencies for the LLM judge.
- `CLAUDE.md` documents the agent-eval workflow and the prompt-version bump rule (any change to `prompts/*.txt` or `render_prompt()` must bump the agent's `prompt_version` and re-run evals before merge).
- `TODOS.md` records Phase 2 follow-ups (the remaining 8 agents, per-run cost cap, automatic `--shadow` mode) and surfaces 10 production templates whose stored `creative_direction` is below the 50-word floor — a real prompt-quality issue the eval caught.

## [0.4.4.0] - 2026-05-10

### Added
- Admin overlay editor now shows a WYSIWYG preview that matches the exported video. The preview panel renders text overlays as a server-rendered transparent PNG produced by the same Pillow code path the export uses, so shadow, font weight axis, line spacing, stroke, span baseline, and auto-shrink all match the final video pixel-for-pixel. New `POST /admin/overlay-preview` endpoint accepts the slot's overlays plus a time cursor and returns the overlay layer at that moment.
- Editor playback keeps live DOM-rendered text as a fallback so users still see overlays during playback; the server PNG takes over once the cursor settles for ~400ms (debounced, with abort-in-flight + 32-entry blob URL LRU cache).
- Font-cycle preview picks the active frame at the cursor time using a new `_compute_font_cycle_frame_specs` pure helper, so scrubbing across an accelerating font cycle shows the same fonts the export will render.

### Changed
- Refactored `_render_font_cycle` in `text_overlay.py` to share its frame-spec math with the new preview path. Production output is unchanged; pinned by regression tests.

### Fixed
- `reframe.py` no longer crashes local dev jobs on HDR/HLG sources when the local Homebrew ffmpeg lacks libzimg. Detects zscale availability once per process and falls back to a no-tonemap path with a structured warning. Production Docker (which ships with libzimg) uses the proper Mobius tonemap as before.

## [0.4.3.0] - 2026-05-10

### Added
- Reject template jobs whose clips can't fill the template's audio length. The upload UI reads each video's duration on file select and shows a live "Footage total: X.Xs / Y.Ys required" readout that turns amber when short. The submit button disables until the user adds enough footage. The backend enforces the same rule on `POST /template-jobs` and `POST /admin/templates/:id/test-job` via a new `validate_clip_total_duration` check, so clients that bypass the FE still get a clear 422.
- New visual reference doc at `docs/pipeline.html` mapping the full template pipeline (admin-time analysis → job-time render), what Gemini decides versus what hardcoded rules override, and every if/else fork in the path.

## [0.4.2.0] - 2026-05-10

### Added
- Admin templates page now has a "Has intro slot" toggle (Settings tab) that controls whether the upload UI splits into a pinned slot-1 intro asset + action clips for the rest. Replaces the prior name-substring heuristic that gated this behavior on the word "face" appearing in the template name. Renaming a template no longer changes how its upload UI behaves.

### Changed
- Template-name renames are now fully decoupled from runtime behavior. The flag persists in `recipe_cached.has_intro_slot` and is registered in `_ROUTING_ONLY_RECIPE_KEYS` so it survives Gemini re-analysis.
- Seed scripts (`seed_how_do_you_enjoy_your_life.py`, `seed_dimples_passport_brazil.py`) look up templates by pinned UUID instead of name. Renaming a seeded template no longer causes the next re-seed to create a duplicate row.

## [0.4.1.0] - 2026-05-10

### Added
- Homepage tiles now show the actual template playing instead of a flat 2-color gradient. Each tile loads a poster JPEG by default, fades in the muted looping video on hover (desktop) or when most-visible in the viewport (mobile), and pauses the previous tile so only one video plays at a time. Click a tile to open a modal preview with native controls and audio, then either close it or jump straight to the upload flow with a "Use this template" button.
- Backend FFmpeg poster extraction: the template analysis pipeline now generates a 540px-wide JPEG from each template right before `analysis_status` flips to `ready`, uploaded to GCS at `templates/<id>/poster.jpg`. `GET /templates` signs a 7-day URL inline (no per-tile round-trip) and returns it as `thumbnail_url`. Templates without a poster (mid-analysis or extraction failed) still appear in the list — the homepage falls back to the existing gradient.
- `scripts/backfill_template_posters.py` — idempotent management script that walks templates with `analysis_status='ready' AND thumbnail_gcs_path IS NULL` and generates posters. Race-safe via conditional UPDATE, supports `--dry-run` and `--limit`.
- New frontend primitives in `src/apps/web/`: `lib/template-playback.ts` (module-level active-tile coordination via `useSyncExternalStore`, signed-URL cache, reduced-motion + save-data helpers, tab-visibility auto-pause), `app/TemplateTile.tsx` (200ms hover debounce, 4s video-load timeout safety net, IntersectionObserver autoplay on touch devices, gradient fallback, mute icon overlay), `app/TemplatePreviewModal.tsx` (focus trap, ESC, click-outside, focus restore, "Use this template" CTA).

### Changed
- `src/apps/web/src/app/page.tsx` rewired to render `TemplateTile` components and mount the preview modal at the page level. The skeleton loader, error banner, empty state, and grid layout are unchanged.

## [0.4.0.2] - 2026-05-09

### Fixed
- `ClipMetadataAgent.parse()` now raises `SchemaError` when Gemini returns a non-empty `best_moments` list but every entry fails coercion (wrong field types, negative durations, etc.). Previously each malformed entry was silently dropped, parse returned `best_moments=[]` as success, and downstream matching had nothing to work with. The runtime now retries with schema clarification before giving up.
- Orchestrator's `_analyze_clips_parallel` adds defense-in-depth: if `analyze_clip` succeeds but returns 0 `best_moments`, treat it the same as an analysis failure and engage the Whisper fallback (which generates synthetic moments). This protects against any future regression where moments are silently lost in the agent pipeline.

## [0.4.0.1] - 2026-05-09

### Fixed
- Template-job clip matcher no longer rejects perfectly usable clips when Gemini extracts moments whose durations fall outside the slot's ±6s tolerance window. The downstream assembler uses `moment.start_s` and trims to `slot.target_duration_s`, so a 12s moment is fine for a 5s slot. Added a last-resort fallback that accepts any moment when both tight (±2s) and loose (±6s) passes are empty. The only remaining path to `TEMPLATE_CLIP_DURATION_MISMATCH` is a clip with literally zero `best_moments` (Gemini analysis failure). Reproduces the user-reported case where a 12s upload produced "no clip fits slot 2 requiring ~5.0s."

## [0.4.0.0] - 2026-05-09

### Added
- Hand-rolled agent runtime with structured output validation, retry/repair loops, tool-call dispatch, and per-call telemetry
- 12-agent catalog under `app/pipeline/agents/` with shared registry and conftest fixtures
- Agent runtime tests: `tests/agents/test_runtime.py`, `test_output_validator.py`, `test_registry.py`

### Changed
- Refactored `gemini_analyzer.py` and `copy_writer.py` to call agents through the new runtime instead of bespoke per-call wrappers

### Fixed
- Template-job failures from `TemplateMismatchError` (no clip fits a slot) now persist `failure_reason="user_clip_unusable"` instead of `"unknown"`. The frontend's existing `user_clip_unusable` copy and `error_detail` passthrough now surface the actual matcher reason instead of "Processing failed. Please try again."
- Same fix applied to single-video orchestrator path with explicit defense-in-depth handler
- Matcher's "no clip fits" hint no longer renders meaningless `≥0s` for short slots; new copy reads "Upload longer clips — slot N needs a moment about Xs long" with a usable range only when meaningful

## [0.3.3.0] - 2026-04-19

### Added
- Audio-only template creation: admins can create templates directly from music tracks without a reference video
- Gemini audio analysis: `analyze_audio_template()` listens to audio tracks and designs visual recipes with mood-driven transitions, energy-based color grading, and beat-synced slots
- `POST /admin/templates/from-music-track` endpoint creates `audio_only` templates from analyzed tracks
- `merge_audio_recipe()` combines exact FFmpeg beat timing with Gemini visual properties via proportional mapping
- "Create Template" button on music track detail page (visible when track analysis is ready)
- Audio-only template handling in admin UI: amber badge, audio placeholder instead of video player, gcs_path null guards throughout
- Migration `0011_music_track_recipe`: adds `recipe_cached` and `recipe_cached_at` to music_tracks, makes `video_templates.gcs_path` nullable, extends template_type enum with `audio_only`
- Gemini audio analysis prompt template (`prompts/analyze_audio_template.txt`)
- Graceful fallback: if Gemini audio analysis fails, tracks still reach "ready" status with a beat-only recipe
- 16 new tests: 4 Gemini analyzer, 2 merge algorithm, 4 orchestration task, 6 API endpoint

### Fixed
- TypeScript operator precedence: `||` and `??` mixed without parentheses in music track audio player props

## [0.3.2.0] - 2026-04-18

### Added
- Music template variants: parent templates can now have per-song sub-templates with beat-synced timing and the parent's visual recipe merged in
- "Enable Music Variants" toggle in admin template Settings tab switches template_type between standard and music_parent
- Music tab on parent templates: browse children, add songs from published track library, navigate to child detail
- `POST /admin/templates/{id}/children` creates a sub-template by merging parent recipe with a track's beats (proportional slot mapping, overlay timing scaling)
- `GET /admin/templates/{id}/children` lists sub-templates with track info
- `POST /admin/templates/{id}/remerge-children` re-merges all children when parent recipe changes, with skipped-child visibility in response
- `merge_template_with_track()` algorithm: proportional visual mapping, overlay timing scaling, interstitial index remapping
- Track picker modal in Music tab filters to published+ready tracks, excludes already-added songs
- Breadcrumb navigation on child templates links back to parent's Music tab
- Migration `0010_template_music_variants`: adds `template_type`, `parent_template_id`, `music_track_id` columns with FK constraints, unique constraint, and indexes
- Admin template list now filters out music_child templates by default (`?exclude_children=true`)
- Public template gallery now excludes music_child templates (they're accessed via their parent)
- 28 new tests: 7 merge algorithm unit tests, 9 API route tests, 12 existing tests updated

### Fixed
- `TemplateRecipeVersion` check constraint now includes `'remerge'` trigger in both migration and SQLAlchemy model (was only in migration, causing model/DB drift)
- Overlay deep copy in merge function prevents parent recipe mutation via shared nested references
- Tab state gracefully falls back to "recipe" when active tab is removed from visible tabs (e.g., disabling Music Variants while on Music tab)

## [0.3.1.0] - 2026-04-18

### Added
- Interactive audio player on music track detail page — play/pause, play selected section, click-to-seek, click-to-set start/end times on beat waveform
- Direct audio file upload (`POST /admin/music-tracks/upload`) — bypasses yt-dlp for environments where YouTube blocks bot IPs (Fly.io, cloud servers)
- Signed audio URL endpoint (`GET /admin/music-tracks/{id}/audio-url`) — 1-hour GCS signed URL for browser playback
- Upload file tab on admin music page (default) alongside existing URL tab

### Fixed
- Beat detection now works on continuous music — replaced `silencedetect` (finds silence gaps, returns 0 for music) with RMS energy peak detection via `astats` (finds rhythmic peaks)
- Fixed RMS value parsing crash on bare `-` values from FFmpeg `ametadata` output (`could not convert string to float: '-'`)
- Fixed admin proxy dropping query parameters (e.g., `?limit=50&offset=0` silently stripped)
- Fixed admin proxy returning raw 500 when backend is unreachable (now returns 502 with clear error)
- Registered `music_orchestrate` task module in Celery worker (tasks were silently stuck in Redis queue)
- Documented `ADMIN_TOKEN` env var in `.env.example` (required for Next.js admin proxy)

## [0.3.0.0] - 2026-04-17

### Added
- **Music beat-sync template** — users can now browse a music gallery, select a song, upload clips, and receive a video where every cut lands on a detected beat
- `MusicTrack` data model with beat timestamps, track config, publish/archive lifecycle, and GCS audio path
- Database migrations: `0008_create_music_tracks` table and `0009_add_music_to_jobs` FK column
- `POST /admin/music-tracks` — admin uploads a YouTube/SoundCloud URL; audio is downloaded via yt-dlp and beat analysis runs as a background Celery task
- `GET /admin/music-tracks` and `GET /admin/music-tracks/{id}` — admin list and detail views
- `PATCH /admin/music-tracks/{id}` — tune `best_start_s`, `best_end_s`, `slot_every_n_beats`; publish or archive a track; now validates the merged config window (not just the patch payload)
- `POST /admin/music-tracks/{id}/reanalyze` — re-trigger beat detection and config computation
- `GET /music-tracks` — public gallery endpoint returns published, ready tracks with section duration and clip count requirements
- `POST /music-jobs` — submit a beat-sync job; guards validate track state, publication, analysis readiness, audio availability, and per-track clip count limits
- `GET /music-jobs/{job_id}/status` — poll job progress
- `analyze_music_track_task` Celery task: yt-dlp audio download → `_detect_audio_beats` → `_auto_best_section` → config storage
- `orchestrate_music_job` Celery task: load track → `generate_music_recipe` → parallel Gemini clip analysis → `template_matcher.match` → `_assemble_clips` with beat-snap → `_mix_template_audio` with music track audio
- Music gallery page (`/music`) — browse tracks, select, upload clips, submit, and poll for result
- Admin music management pages (`/admin/music`, `/admin/music/[id]`) — upload, monitor analysis, publish/archive, tune config
- Next.js API proxy for admin routes (`/api/admin/[...path]`) — keeps admin token server-side only

### Fixed
- Beat-sync polling interval now correctly clears on terminal job states (was referencing undefined `terminal` variable, causing perpetual polling)
- Tracks with 0 detected beats are now marked `failed` at analysis time with a descriptive error, preventing silent job failures downstream
- `PATCH /admin/music-tracks/{id}` now validates the merged `track_config` window (not just keys present in the request body), preventing inverted `best_start_s`/`best_end_s` from reaching the pipeline

## [0.2.3.1] - 2026-04-14

### Changed
- Parallel slot rendering via ThreadPoolExecutor (max_workers=3), replacing sequential _render_slot loop
- Parallel font-cycle PNG generation via ThreadPoolExecutor (max_workers=4)
- Burn preset changed from `fast` to `ultrafast` for final text overlay encode pass

### Removed
- Dead `_render_slot()` function (replaced by `_plan_slots` + `_render_planned_slot`)

### Added
- `pytest-xdist` and `pytest-timeout` for parallel CI test execution with timeout protection
- Jest `maxWorkers: "50%"` to prevent OOM on CI runners
- CI now runs `pytest -n auto --timeout=60`

## [0.2.3.0] - 2026-04-13

### Added
- Rich inline text spans: per-word font, color, and size within a single overlay line
- `TextSpan` data model (TypeScript + Python Pydantic validation) stored in existing recipe JSONB
- `_draw_spans_png()` renderer with baseline alignment, line wrapping, and overflow scale-down
- SpanEditor segment list in admin template editor with inline preview and "split text" button
- OverlayPreview per-word CSS rendering for WYSIWYG span preview
- Font-cycle spans integration: fixed-font spans stay constant while cycling spans swap fonts
- `_draw_frame()` DRY helper for spans/flat text dispatch in font-cycle rendering
- Per-span text sanitization and length cap to prevent unbounded Pillow measurement
- 10 new backend tests covering all span rendering paths and edge cases

### Fixed
- Timing bypass when spans present but text empty (dead branch removed, proper clamping added)
- Ghost PNG reference when all spans have empty text (file existence guard)
- Overflow scale-down fallback using wrong size key (now correctly scales to "small")
- Font-cycle override font preserved through overflow scale-down via font_variant()

## [0.2.2.0] - 2026-04-12

### Added
- Font expansion: 7 new curated Google Fonts (Space Grotesk, DM Sans, Instrument Serif, Bodoni Moda, Fraunces, Space Mono, Outfit) alongside existing Playfair Display and Montserrat
- Shared font registry (`font-registry.json`) as single source of truth for both Python pipeline and TypeScript admin editor
- Font picker in admin template editor showing real font names instead of abstract style categories ("display", "sans", "serif")
- `font_family` field on overlay recipes, overriding legacy `font_style` when set
- Variable font weight axis support in Pillow rendering for WYSIWYG fidelity with CSS preview
- 31 new tests: 21 registry validation, 10 font_family resolution and regression tests

### Changed
- Merged duplicate `_draw_text_png` / `_draw_text_png_with_font` into a single function (40 lines DRY reduction)
- Font-cycle cache now keyed by `(size, settle_font_name)` to prevent cross-overlay pollution
- Cross-slot overlay merge now compares `font_family` to prevent merging overlays with different fonts

### Fixed
- Production font fallback: replaced all macOS system font paths (`/System/Library/Fonts/...`) with bundled fonts from the registry, fixing degraded rendering on Fly.io (Linux)
- Font-cycle contrast fonts now render at correct weight via `set_variation_by_axes` instead of defaulting to Light/Thin

## [0.2.1.2] - 2026-04-12

### Fixed
- Wired `consolidate_slots()` into the production pipeline to eliminate excessive clip repetition when users upload fewer clips than template slots (e.g., 3 clips + 11 slots no longer repeats each clip 3-4 times)
- Moved `consolidate_slots()` call inside the existing error-handling block for consistent error reporting

### Added
- Regression test (T14) for the 3-clip/11-slot passport vlog scenario

## [0.2.1.1] - 2026-04-12

### Fixed
- FFmpeg concat demuxer timeout on long template jobs: switched intermediate concat to `ultrafast` preset (re-encoded at `fast` in the text overlay pass), bumped timeout from 600s to 1200s
- Celery worker concurrency reduced from 2 to 1 to give each FFmpeg job the full 2 shared CPUs on Fly.io, preventing CPU contention timeouts
- Aligned concurrency setting across all deploy configs (fly.toml, Dockerfile.worker, Procfile)

## [0.2.1.0] - 2026-04-11

### Changed
- Unified text handling in admin template editor: admins now work with a single "Text" field per overlay instead of separate "Text" and "Sample Text" fields
- Overlay preview on the video canvas now shows resolved text (subject substitution applied) instead of raw template text
- Added "Preview Subject" input in the editor header to preview how placeholder text like "PERU" resolves to a real subject like "PUERTO RICO"
- CTA overlays now show "(CTA — auto)" hint instead of generic "(empty)" to explain why CTA text is intentionally blank
- Double-click inline editing and property panel both read/write `sample_text` directly (the backend's source of truth)
- Legacy recipe migration on load: overlays with populated `text` but empty `sample_text` are automatically migrated
- 19 new unit tests for `isSubjectPlaceholder` and `resolveOverlayPreview` (41 total overlay editor tests)

## [0.2.0.0] - 2026-04-11

### Added
- Visual overlay editor on the admin recipe page: click to select overlays directly on the video preview, drag vertically to reposition (snaps to top/center/bottom), double-click to edit text inline
- Overlay timeline strip below the video showing each overlay as a colored bar (color-coded by role: hook/reaction/cta/label), with a live playhead indicator
- Property panel now shows a focused overlay editor when an overlay is selected from the video or timeline, with a Remove button for quick deletion
- New `overlay-constants.ts` module shared across overlay editor components, with full unit test coverage (22 tests)

### Fixed
- Inline text edit no longer commits partial text when the user's focus moves to the timeline bar (onBlur/pointerdown ordering)
- Inline edit commit now guards against stale overlay index when an overlay is deleted externally during an active edit

## [0.1.10.0] - 2026-04-06

### Added
- Minimum-coverage clip rotation ensures all uploaded clips appear in the output video, not just the best-matched ones
- Differentiated label sizing: city/subject names render at 120px (large), prefix text like "Welcome to" renders at 72px (medium)
- First-slot label timing: prefix text appears at second 2, subject text at second 3 (staggered reveal)
- Subject labels now always get font-cycle animation with acceleration at second 8

### Changed
- Curtain-close animation minimum increased from 3.0s to 4.0s for more dramatic effect
- Curtain-close duration clamp changed from 50% to 60% of slot duration (more curtain, still 40% visible footage)
- Label detection is now content-based (matches "PERU", "Welcome to") instead of relying on Gemini's inconsistent role field

### Fixed
- Curtain-derived font-cycle acceleration now correctly uses post-timing-override start time, preventing accel from firing before text appears
- Combined "Welcome to PERU" overlays on later template slots are filtered out, preventing redundant text after the staggered reveal

## [0.1.9.0] - 2026-04-04

### Added
- Location input on template upload page so users can type city/country for overlay text
- Timing overrides (`start_s_override` / `end_s_override`) for recipe-level overlay timing corrections
- Role-based visual overrides for label text: 120px maize/gold Montserrat ExtraBold (replaces unreliable Gemini-returned styles)
- Text exit clamp on curtain-close slots prevents text from lingering on black frames
- Subject validation (`max_length=50`) on template job creation

### Fixed
- Curtain animation safety clamp now ensures at least 50% visible footage (was only triggered when clip was shorter than animation, missing equal-duration case)
- Font-cycle acceleration timestamp now uses the same 50% clamp as the visual curtain, keeping text cycling in sync with closing bars
- Reroll endpoint now preserves the subject field from the original job instead of silently dropping it

### Changed
- Curtain animation minimum increased from 1.0s to 3.0s for more dramatic slow-curtain effect
- 14 new unit tests covering timing overrides, role overrides, curtain sync, and exit clamp

## [0.1.8.0] - 2026-04-04

### Fixed
- White screen after video upload caused by stale `.next` build cache referencing missing webpack chunks from pre-route-move layout
- `next build` failure from invalid named exports (`saveBatchToStorage`, `readBatchFromStorage`, `clearBatchStorage`) in `template/page.tsx` — Next.js App Router only allows default + metadata exports from page files
- Batch storage entries without `saved_at` timestamp now expire correctly instead of bypassing the 30-minute TTL indefinitely
- `readBatchFromStorage` now returns only declared fields (`batch_id`, `template_id`) instead of leaking raw parsed localStorage data

### Added
- Root error boundary (`error.tsx`) catches page-level runtime errors with retry UI
- Global error boundary (`global-error.tsx`) catches root layout crashes with inline-styled fallback (Tailwind unavailable when layout fails)
- Batch storage helpers extracted to `lib/batch-storage.ts` for proper module separation

## [0.1.7.0] - 2026-04-04

### Changed
- Upload UI is now the primary entry point at `/` — users land directly on the upload page instead of the waitlist. The old `/nova-studio` path redirects to `/`.
- Page metadata updated to reflect product positioning: "Turn raw footage into viral clips"

### Fixed
- Server-side MIME normalisation for presigned uploads: `video/quicktime` (common on macOS/iOS) is now normalised to `video/mp4` before GCS signing, preventing signature mismatch errors when the client PUTs the file
- Architecture dashboard module config updated to reference upload UI at new root path

### Added
- 11 presigned upload endpoint tests: MIME normalisation unit tests, integration tests for happy path, quicktime normalisation, AVI passthrough, unsupported type rejection, file size limit, aspect ratio validation, and GCS failure handling

## [0.1.6.0] - 2026-04-03

### Fixed
- Curtain-close animation too fast to perceive: minimum animation duration raised from 0.5s to 1.0s via `MIN_CURTAIN_ANIMATE_S` constant, enforced at both `_assemble_clips` and `_collect_absolute_overlays` call sites
- Font-cycle settle phase interfering with curtain-close sync: when `font_cycle_accel_at_s` is active, settle phase is now skipped entirely so cycling runs to the end, reinforcing the kinetic energy of the closing bars
- Font-cycle frame cap leaving timing gaps: gap-fill PNG now bridges the frame cap boundary to `cycle_end`, preventing silent gaps in the overlay timeline
- Cross-slot text merging: adjacent slots sharing the same text (e.g., "PERU" on slots 1 and 2) now merge into a single continuous overlay instead of dropping duplicates, correctly spanning interstitial hold durations

### Added
- 8 new tests covering all 4 root causes: curtain minimum clamp, settle-phase skip with accel, frame-cap gap-fill, cross-slot merge (same text, different positions, non-adjacent gaps, accel inheritance)

## [0.1.5.0] - 2026-03-30

### Fixed
- Template analysis pipeline leaves `recipe_cached = NULL` on Fly.io when Celery hard `time_limit` sends SIGKILL before DB commit. Added `soft_time_limit` (catchable) before all hard limits so the error handler runs and persists failure state.
- Infinite SIGKILL-requeue loop: tasks killed by hard `time_limit` were requeued forever via `task_acks_late + task_reject_on_worker_lost`. Added Redis-based attempt counter (max 3, 1hr TTL) as a circuit breaker.
- Hardcoded `"gemini-2.5-flash"` in 3 Gemini API calls now uses `settings.gemini_model` config, matching the pattern already used by `analyze_clip` and `transcribe`.
- `float("NaN")` and non-numeric Gemini slot durations now rejected during template analysis instead of silently propagating to FFmpeg commands.
- Redis connections created in admin reanalyze endpoint and Celery tasks are now explicitly closed after use.

### Added
- `error_detail` column on `video_templates` table: stores the failure reason (timeout, refusal, quota) so admins can see why a template failed without reading logs.
- `error_detail` exposed in admin API responses (`GET /admin/templates`, `GET /admin/templates/:id`).
- Reanalyze endpoint clears stale `error_detail` and Redis attempt counter so manual retry always gets a fresh set of attempts.
- Slot semantic validation: Gemini responses with missing/zero `target_duration_s` or missing `slot_type` are rejected with clear error messages instead of producing broken recipes.
- `soft_time_limit` + `SoftTimeLimitExceeded` handlers on all 4 Celery tasks: `analyze_template_task` (840s/900s), `orchestrate_template_job` (1740s/1800s), `orchestrate_job` (1080s/1200s), `render_clip` (540s/600s).
- 12 new tests: timeout handling, error_detail persistence, max attempts guard, settings model config, slot validation, admin error_detail flow.

## [0.1.4.0] - 2026-03-29

### Fixed
- GCS `DefaultCredentialsError` on Fly.io: added `GOOGLE_SERVICE_ACCOUNT_JSON` env var support as tier-2 in the credential chain (file path → JSON string → ADC)
- Clear error messages for misconfigured GCS credentials: malformed JSON and invalid service account key structure both produce actionable `RuntimeError` instead of cryptic library tracebacks
- Whitespace-only `GOOGLE_SERVICE_ACCOUNT_JSON` (trailing newlines from shell piping) now correctly falls through to ADC instead of erroring

### Added
- 6 unit tests for the GCS credential chain covering all tiers, priority ordering, and error paths
- `GOOGLE_SERVICE_ACCOUNT_JSON` documented in `.env.example` and CLAUDE.md Fly secrets list

## [0.1.3.0] - 2026-03-29

### Added
- Batch import progress recovery via localStorage: users can navigate away during a Drive import and return to find progress resumed automatically
- `saveBatchToStorage`, `readBatchFromStorage`, `clearBatchStorage` helpers with SSR guard, type validation, and 30-minute staleness protection
- Extracted `startBatchPolling(batchId, templateId)` reusable polling function from inline handler, supports both new imports and recovery
- Mount-time recovery `useEffect` that reads localStorage and restarts polling on page load
- Retry logic for transient network errors during polling (3 consecutive failures before giving up, exponential backoff)
- Recovery UI indicator: "Resuming Drive import..." message distinguishes recovery from new imports
- 11 tests for batch recovery: localStorage helpers, staleness detection, malformed data handling, overwrite behavior

## [0.1.2.0] - 2026-03-28

### Added
- Interactive architecture dashboard at `/architecture` with react-flow diagram, dark mission-control theme
- Module graph config (`architecture-config.ts`): 5 L1 pipeline modules, 3 data stores, 17 L2 sub-modules with dependencies, file paths, and business context
- Business/Technical view toggle: business view shows user-facing descriptions, metrics, and status; technical view shows file counts, issue badges, and data flow labels
- L1→L2 drill-down: click any pipeline module to expand and see sub-modules
- Module detail slide-out panel: description, file list with GitHub links, recent commits, open issues
- GitHub integration via Next.js API route proxy (`/api/architecture/github`): fetches issues by module label and commits by directory path
- Impact highlighting: right-click any module to highlight direct downstream dependents
- Live job activity overlay: polls active jobs via SWR, maps job status to pipeline modules with emerald pulse animation
- localStorage job tracking: `trackRecentJob()` called on upload and template job creation for live overlay
- 32 tests across 5 suites: architecture config validation, react-flow diagram, detail panel, GitHub route, SWR hooks
- Jest configuration (`jest.config.js`) for Next.js with path aliases
- Production Dockerfile for Fly.io deployment with cached dependency layer (parses pyproject.toml at build time)
- `fly.toml` with api + worker process groups, auto-stop for API, VM sizing, health checks
- Alembic `release_command` in fly.toml for automatic migrations on deploy
- `asyncpg_database_url` property on Settings for Fly Postgres compatibility (translates libpq sslmode to asyncpg ssl)
- `normalize_postgres_scheme` field validator on database_url (postgres:// to postgresql://)
- DejaVu fonts installed in Docker image for font-cycle variety
- `.dockerignore` to exclude media, tests, frontend, and dev files from production image
- Procfile for process group definitions
- 6 new tests for Settings database URL normalization and asyncpg URL translation

### Fixed
- Rate limit check in `ModuleDetailPanel` was dead code (always caught by empty items check first)
- L2 view highlighting now passes through `highlightedIds` and `selectedModuleId` to child nodes
- Migration 0004 `down_revision` pointed to filename instead of revision ID
- TypeScript build errors for Vercel production: explicit Set generic, missing priority field, session.user null guard

### Removed
- Dead code: `hook_scorer.py` (109 LOC, never imported — superseded by `score.py`)
- Stale file reference in architecture config: redis module pointed to non-existent `celery_app.py` (now `worker.py`)

## [0.1.1.0] - 2026-03-27

### Added
- Font-cycle acceleration: cycling interval drops from 0.15s to 0.07s when curtain-close animation starts, syncing font switching speed with closing bars
- `text_color` passthrough for font-cycle overlays, enabling colored text (yellow country names, red highlights) during rapid font switching
- Per-size font caching in `_resolve_cycle_fonts()` so large/medium/small overlays get correctly sized fonts
- `MAX_FONT_CYCLE_FRAMES` safety cap (60) preventing PNG explosion on long overlays
- 14 new tests: 11 for text overlay (color, acceleration, size), 3 for template orchestration (curtain-close wiring)

### Fixed
- Font-cycle timing corruption: removed broken 1:1 timing reassignment in `_burn_text_overlays()` that overwrote multi-PNG font-cycle timestamps with single overlay timestamps
- Curtain-close animation: replaced broken drawbox approach with `geq` pixel expression filter for reliable top/bottom bar closing
- `font_cycle_accel_at_s` clamped to overlay start time to prevent full-speed cycling when `animate_s >= slot_duration`
- Stale `font_cycle_accel_at_s` removed when dedup truncation pushes overlay `end_s` before acceleration timestamp

### Changed
- Pillow added as explicit dependency for text overlay PNG rendering
- Font-cycle font resolution now accepts pixel `size` parameter with per-size dict cache

## [0.1.0.0] - 2026-03-27

### Added
- Interstitial pipeline for compound transitions: curtain-close detection via FFmpeg luminance band analysis, `render_color_hold()` for black/white/flash holds, `apply_curtain_close_tail()` for drawbox animation overlays
- Curtain-close detection classifier (`classify_black_segment_type()`) that samples pre-black-segment frames and compares top/bottom vs middle luminance to distinguish curtain-close from fade-to-black
- Gemini vocabulary translation layer (`translate_transition()`) mapping LLM output types (whip-pan, zoom-in, dissolve) to internal FFmpeg transition types
- Interstitial validation in Gemini analyzer (`_validate_interstitials()`) with schema enforcement
- Playfair Display font bundle (Bold + Regular .ttf) replacing Montserrat for editorial text overlays
- Soft gaussian shadow rendering for text overlays (replaces hard outline/stroke)
- `fontsdir` parameter to ASS subtitle filter for bundled font discovery in `reframe.py`
- Comprehensive prompt updates for curtain-close, barn-door wipe, and interstitial classification in Gemini template analysis (pass1, pass2, schema, single)
- 16 new tests: 10 for interstitial classification, 6 for Playfair Display font rendering

### Fixed
- Beat-snap clock drift after interstitials: `cumulative_s` now accounts for interstitial hold duration
- Transition from interstitial forced to hard-cut: prevents broken xfade from solid color frames
- Pre-existing test failures: 8 tests using real asyncpg connection now use `app.dependency_overrides[get_db]` with mock session, 3 email task tests now patch `httpx.post` at correct module level
- `firebase-debug.log` removed from tracking and added to `.gitignore`

### Changed
- `blackdetect` thresholds lowered: `black_min_duration` 0.3 to 0.15, `pixel_threshold` 0.10 to 0.15 for better detection of short holds
- Default font style changed from "sans" (Montserrat) to "display" (Playfair Display Bold)
- ASS overlay header updated: transparent background, no outline, soft shadow
