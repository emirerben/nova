# Changelog

All notable changes to this project will be documented in this file.

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
