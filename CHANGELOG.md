# Changelog

All notable changes to this project will be documented in this file.

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
