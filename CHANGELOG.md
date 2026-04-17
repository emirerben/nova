# Changelog

All notable changes to this project will be documented in this file.

## [0.2.3.2] - 2026-04-17

### Fixed
- FFmpeg error messages now visible in logs: stderr truncation changed from first 500 chars (hidden behind FFmpeg config banner) to last 2000 chars, making actual errors readable
- `colorspace=all=bt709:iall=bt709` fallback added for clips with no colorspace stream metadata (split/intermediate files); real stream metadata (e.g. bt2020/HLG iPhone clips) still overrides and converts correctly

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
