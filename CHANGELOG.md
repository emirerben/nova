# Changelog

All notable changes to this project will be documented in this file.

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
