# Template pipeline — internals

Reference doc for deep pipeline internals. CLAUDE.md carries the invariants and guard
tests; this file carries the mechanics and "how it works".

## Interstitials

`app/pipeline/interstitials.py` detects curtain-close vs fade-to-black via FFmpeg
luminance band analysis, renders color holds and `geq` pixel-expression curtain-close
animations. `drawbox` cannot animate bar height over time — `geq` evaluates
per-pixel per-frame with access to `T`, `X`, `Y`, `H`, `W`.

`MIN_CURTAIN_ANIMATE_S=4.0` enforced at both `_assemble_clips` and
`_collect_absolute_overlays` call sites, clamped to 60% of slot duration
(`_CURTAIN_MAX_RATIO=0.6`, ensures at least 40% visible footage).

## Transitions

`app/pipeline/transitions.py` translates Gemini vocabulary (whip-pan, zoom-in,
dissolve) to internal FFmpeg xfade types. Unknown types default to "none" (hard-cut).

## Text overlays

`app/pipeline/text_overlay.py` renders gaussian-shadow text (no hard outlines), supports
font-cycle and ASS animated overlays.

**Renderer dispatch** (`template_orchestrate.py`): agentic templates + music-job lyrics
→ `text_overlay_skia.py` (skia-python, HarfBuzz shaping, per-frame PNG sequences);
classic non-music + non-agentic → Pillow + libass.

**Skia file-descriptor pressure:** Skia outputs per-overlay PNG sequences fed to
FFmpeg's `image2` demuxer + `setpts` shift — O(overlay_count) FDs regardless of frame
count. Pillow path has `MAX_FONT_CYCLE_FRAMES=60` cap (was 100) to avoid blowing past
`ulimit -n`.

### Font-cycle mechanics

- Acceleration syncs with curtain-close (`font_cycle_accel_at_s`); `text_color`
  passthrough for colored text; per-size font caching in `_resolve_cycle_fonts()`.
- When `font_cycle_accel_at_s` is active, settle phase is skipped (cycling runs to
  `end_s`).
- Gap-fill PNG bridges frame cap to `cycle_end`.

### Cross-slot text merge

`_collect_absolute_overlays()` merges same-text+same-position overlays across adjacent
slots when gap < `_MERGE_GAP_THRESHOLD_S` (2.0s), inheriting effect and accel from
later slots.

### Label config

`_LABEL_CONFIG` in `template_orchestrate.py` differentiates subject labels
(large/120px/sans/#F4D03F/font-cycle/accel@8s) from prefix labels (medium/72px).
Detection is content-based (`_is_subject_placeholder()` or "welcome" prefix), not
role-based, because Gemini inconsistently returns `role=hook` instead of `role=label`.
First-slot labels get timing overrides (prefix@2s, subject@3s).

### Clip rotation

`_minimum_coverage_pass()` in `template_matcher.py` pre-assigns most-constrained clips
first to maximize variety before the greedy quality pass.

### Timing internals

- Timing overrides: `start_s_override` / `end_s_override` on overlay JSON correct
  Gemini's approximate timing; curtain exit clamp always wins.
- Beat-snap: `cumulative_s` in `_assemble_clips()` must account for interstitial hold
  durations to keep beat-snap calculations accurate.
- Font-cycle bug (fixed v0.1.1.0): `_burn_text_overlays()` must not reassign font-cycle
  multi-PNG timestamps with single overlay timestamps.

## Agentic pct-timing

AGENTIC templates emit relative timings — slots carry `target_duration_pct` and text
overlays carry `start_pct`/`end_pct` instead of absolute seconds.
`template_orchestrate.py` dispatches via the `is_agentic` flag through
`app/pipeline/agentic_timing.py`, which resolves pct → seconds against the matched
clip's effective duration. Classic templates are entirely unaffected.

`template_recipe.parse()` adds two D2 guards for agentic output:
- `_dedup_overlays_across_slots` — collapses duplicate overlays across adjacent slots
  before render-time merge.
- Interstitial grounding — rejects interstitial slots that don't line up with an
  FFmpeg-detected `black_segments` boundary.

Bump `prompt_version` (currently `2026-05-17`) when editing the agentic recipe prompt.

## Placeholder substitution

`_resolve_overlay_text()` + `_is_subject_placeholder()` in `template_orchestrate.py`
can rewrite short title-case or ALL-CAPS overlay text to the user's subject. Literal
overlays SHOULD set `"subject_substitute": False` as defense-in-depth; the per-seed
audit in `tests/scripts/test_seed_overlays_literal.py` fails on any new overlay that
matches the heuristic without declaring intent.
