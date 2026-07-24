"""Skia-based text overlay renderer for agentic templates + music lyrics.

This module is the Skia sibling of `text_overlay.py`. It takes the same overlay
dict shape, but renders via skia-python (HarfBuzz text shaping, real kerning,
paint-based shadows, uniform animation primitives) and emits per-frame PNG
sequences instead of Pillow PNGs + libass ASS files.

Routing — the orchestrators decide which renderer to use:
  - Agentic templates → this module
  - Music jobs (any track-based job) → this module
  - Classic non-music + non-agentic templates → app/pipeline/text_overlay.py
    (Pillow + libass), unchanged.

Kill switch: `settings.text_renderer_skia_enabled` (read in the orchestrator
wrappers `_burn_text_overlays` and `_pre_burn_curtain_slot_text`). This module
itself is a pure renderer — the orchestrator decides whether to call it. Flip
`TEXT_RENDERER_SKIA_ENABLED=false` on Fly and restart workers; agentic + music
jobs revert to Pillow + libass per-process. No in-flight job is mid-rendered
with a switched-on flag because the flag is read at burn time, not job start.

Design notes:
  - ONE renderer for all effects. Static, font-cycle, pop-in, karaoke, etc.
    all become "what does the canvas look like at time t?". libass is not used.
  - Per-overlay PNG sequences via FFmpeg's image2 demuxer + `setpts=PTS+start/TB`.
    Each overlay = 1 FFmpeg input regardless of frame count, so file-descriptor
    pressure is O(overlay_count), not O(total_frames).
  - PNG encoding uses Pillow (~8ms/frame on 1080x1920 RGBA) instead of Skia's
    built-in encoder (~44ms/frame). Skia's encoder is the bottleneck for
    long-running animations (font-cycle, karaoke); Pillow is the production
    standard and already in deps.
  - Typeface cache: every registered font is loaded once at module import.
    No per-frame `MakeFromFile` calls during render.
  - Visual parity with Pillow is NOT a requirement. Skia output is a quality
    improvement, not a port. Where layout differs (line wrap break points,
    sub-pixel kerning), Skia wins. Classic templates that need byte-identical
    output keep using the Pillow path.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol

import numpy as np
import regex
import skia
import structlog
from PIL import Image

from app.pipeline.canvas import PORTRAIT, Canvas
from app.pipeline.generative_overlays import (
    resolve_letter_spacing_em,
    resolve_letter_spacing_px,
    resolve_line_spacing,
    resolve_max_width_frac,
)
from app.pipeline.text_overlay import (
    _FONT_REGISTRY,
    _FONT_SIZE_MAP,
    _POSITION_Y,
    FONT_CYCLE_FAST_INTERVAL_S,
    FONT_CYCLE_INTERVAL_S,
    FONT_CYCLE_SETTLE_RATIO,
    FONTS_DIR,
    MAX_FONT_CYCLE_FRAMES,
    _emoji_codepoint,
    _emoji_png_path,
    _finite_float,
    _hex_to_rgba,
    _parse_gradient_spec,
    _validate_overlay,
)
from app.pipeline.text_wrap import balanced_word_wrap_indices

log = structlog.get_logger()

# Output framerate for animated overlay sequences. 30 fps is enough for the
# fastest production animation (font-cycle FAST_INTERVAL = 70ms = 14fps native
# cycle rate) while keeping file count bounded. Frame budget per overlay:
# MAX_OVERLAY_FRAMES caps effects whose animation can be visually unique on
# every frame (font-cycle, plain pop-in) the same way Pillow's
# MAX_FONT_CYCLE_FRAMES does. Lyric-timed text has a separate sanity ceiling
# because the settled text must remain visible through its full audio window.
FPS = 30
CANVAS_W = PORTRAIT.width
CANVAS_H = PORTRAIT.height
MAX_OVERLAY_FRAMES = max(120, MAX_FONT_CYCLE_FRAMES)
LONG_RUNNING_TEXT_FRAME_CEILING = int(FPS * 30)

# The sequence COMPOSITE stream (see _render_sequence_composite) spans the
# union window of every generative_sequence overlay — for a full editorial
# edit that is the whole output video, so the per-overlay 30s ceiling would
# truncate it. 120s = 2x the platform's sub-60s output target. Disk stays
# bounded regardless: held/blank frames are hard links, so unique-PNG count —
# not this ceiling — drives scratch usage.
SEQUENCE_COMPOSITE_FRAME_CEILING = int(FPS * 120)

# behind_subject overlays get their own, larger ceiling. Generative intro
# overlays can be hold-to-EOF (effect="static", end_s spanning nearly the
# whole clip — see generative_overlays.py's _HOLD_TO_END_S); a normal static
# overlay that long would take the single-PNG `-loop 1` path and just persist
# forever, but behind_subject overlays can't use that trick — the subject
# mask differs every frame, so every second of the window needs a real,
# uniquely-masked PNG (see the hold-economy note on `_generate_overlay_sequence`
# below). Without a dedicated ceiling these fall back to
# LONG_RUNNING_TEXT_FRAME_CEILING (30s) and the text silently vanishes for
# the remainder of the video once the PNG sequence runs out (eof_action=pass
# on the ffmpeg overlay input). 120s == SEQUENCE_COMPOSITE_FRAME_CEILING,
# deliberately: it covers Nova's sub-60s output target with 2x margin. Frame
# economy stays fine at this length — behind frames are mostly-transparent
# text-on-alpha PNGs (small), and PNG encode parallelizes over
# _ENCODE_WORKERS — so do NOT re-enable hold-linking for behind_subject to
# "save" frames here.
BEHIND_SUBJECT_FRAME_CEILING = int(FPS * 120)

# Encoder thread pool: Pillow PNG encode releases the GIL during compression,
# so threading actually helps. 4 workers matches the production Celery worker
# CPU count (shared-cpu-4x). Skia rendering is also GIL-released inside the
# C++ layer.
_ENCODE_WORKERS = 4

# Text layout constants — mirror Pillow's _TEXT_MAX_LINE_W / _TEXT_LINE_SPACING.
_MAX_LINE_W_FRAC = 0.9
_LINE_SPACING = 1.15
_MIN_FONT_SIZE = 24
_POP_IN_KEYFRAMES_S = (0.0, 0.150, 0.250)
_POP_IN_SCALES = (0.30, 1.15, 1.00)
_POP_IN_MAX_SCALE = max(_POP_IN_SCALES)
_POP_SUFFIX_MAX_LINES = 2
_GIANT_TITLE_WIPE_HOLD_FRAC = 0.68
_GIANT_TITLE_WIPE_END_SCALE = 60.0
_GIANT_TITLE_WIPE_ALPHA_FADE_START_FRAC = 0.80
_GIANT_TITLE_WIPE_SCALE_EASE = (0.76, 0.0, 0.24, 1.0)
_GIANT_TITLE_WIPE_TARGET_O_OFFSET_X = 13.0
_GIANT_TITLE_WIPE_TARGET_O_OFFSET_Y = -80.0

_STAGGERED_SLICE_FIRST_WORD_END_S = 0.55
_STAGGERED_SLICE_REMAINDER_START_S = 0.95
_STAGGERED_SLICE_REMAINDER_END_S = 1.35
_STAGGERED_SLICE_LINES_START_S = 1.5
_STAGGERED_SLICE_LINE_STAGGER_S = 0.12
_STAGGERED_SLICE_LINE_GLYPH_STAGE_S = 0.35
_STAGGERED_SLICE_GLYPH_DURATION_S = 0.16
_STAGGERED_SLICE_MAX_SETTLE_S = 2.4


@dataclass(frozen=True)
class StaggeredSliceGlyphState:
    grapheme: str
    opacity: float
    translate_y_em: float
    rotate_deg: float


@dataclass(frozen=True)
class StaggeredSliceLineState:
    text: str
    kind: str
    glyphs: tuple[StaggeredSliceGlyphState, ...] = ()


@dataclass(frozen=True)
class StaggeredSliceState:
    lines: tuple[StaggeredSliceLineState, ...]
    settled: bool
    settle_s: float


def _segment_graphemes(text: str) -> list[str]:
    return regex.findall(r"\X", text)


def _staggered_slice_choreography_s(text: str) -> float:
    line_count = max(1, len(_normalize_reveal_text(text).split("\n")))
    if line_count == 1:
        return _STAGGERED_SLICE_REMAINDER_END_S
    last_line_start = (
        _STAGGERED_SLICE_LINES_START_S + max(0, line_count - 2) * _STAGGERED_SLICE_LINE_STAGGER_S
    )
    return last_line_start + _STAGGERED_SLICE_LINE_GLYPH_STAGE_S


def _giant_title_wipe_scale_at(t_local: float, duration_s: float) -> float:
    """Hold, then scale a title until it crops off-screen like a scene wipe."""
    hold_until = max(0.0, duration_s) * _GIANT_TITLE_WIPE_HOLD_FRAC
    wipe_for = max(0.01, max(0.0, duration_s) - hold_until)
    progress = _motion_cubic_bezier(
        (t_local - hold_until) / wipe_for,
        *_GIANT_TITLE_WIPE_SCALE_EASE,
    )
    return 1.0 + (_GIANT_TITLE_WIPE_END_SCALE - 1.0) * progress


def _giant_title_wipe_alpha_at(t_local: float, duration_s: float) -> float:
    """Once the camera is inside the O counter, leave only the content visible."""
    hold_until = max(0.0, duration_s) * _GIANT_TITLE_WIPE_HOLD_FRAC
    wipe_for = max(0.01, max(0.0, duration_s) - hold_until)
    wipe_progress = (t_local - hold_until) / wipe_for
    if wipe_progress <= _GIANT_TITLE_WIPE_ALPHA_FADE_START_FRAC:
        return 1.0
    fade_progress = (wipe_progress - _GIANT_TITLE_WIPE_ALPHA_FADE_START_FRAC) / (
        1.0 - _GIANT_TITLE_WIPE_ALPHA_FADE_START_FRAC
    )
    return 1.0 - _ease_out_cubic(fade_progress)


def _giant_title_wipe_scale_origin() -> tuple[float, float]:
    """Scale around the target O counter instead of sliding the title toward it."""
    return (
        _GIANT_TITLE_WIPE_TARGET_O_OFFSET_X,
        _GIANT_TITLE_WIPE_TARGET_O_OFFSET_Y,
    )


def _theme_transition_type(overlay: dict) -> str | None:
    raw = overlay.get("theme_transition")
    if isinstance(raw, dict):
        transition_type = str(raw.get("type") or "").strip()
    elif isinstance(raw, str):
        transition_type = raw.strip()
    else:
        return None
    if transition_type == "giant-title-wipe":
        return transition_type
    if transition_type:
        log.warning("skia_unknown_theme_transition_static_fallback", type=transition_type)
    return None


@contextmanager
def _theme_transition_canvas(
    canvas: skia.Canvas,
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    render_canvas: Canvas = PORTRAIT,
):
    """Apply scene-transition transforms around the entire text-effect draw."""
    if _theme_transition_type(overlay) != "giant-title-wipe":
        yield
        return

    scale = _giant_title_wipe_scale_at(t_local, duration_s)
    alpha = _giant_title_wipe_alpha_at(t_local, duration_s)
    scale_origin_x, scale_origin_y = _giant_title_wipe_scale_origin()
    cx, cy = _resolve_anchor(overlay, render_canvas)
    origin_x = cx + scale_origin_x
    origin_y = cy + scale_origin_y

    canvas.save()
    canvas.saveLayerAlpha(None, _clamp_byte(255 * alpha))
    canvas.translate(origin_x, origin_y)
    canvas.scale(scale, scale)
    canvas.translate(-origin_x, -origin_y)
    try:
        yield
    finally:
        canvas.restore()
        canvas.restore()


def _staggered_slice_settle_s(text: str) -> float:
    return min(_STAGGERED_SLICE_MAX_SETTLE_S, _staggered_slice_choreography_s(text))


def _staggered_slice_nominal_time(
    t_local: float, duration_s: float, settle_s: float, choreography_s: float
) -> float:
    available = max(0.01, min(duration_s, settle_s))
    return max(0.0, t_local) / (available / choreography_s)


def _staggered_slice_progress(t: float, start: float, duration: float) -> float:
    return _ease_out_cubic((t - start) / max(0.01, duration))


def _staggered_slice_state(text: str, t_local: float, duration_s: float) -> StaggeredSliceState:
    """Pure timing model mirrored by `staggeredSliceStateAt` in the editor."""
    normalized = _normalize_reveal_text(text)
    logical_lines = normalized.split("\n")
    choreography_s = _staggered_slice_choreography_s(normalized)
    settle_s = _staggered_slice_settle_s(normalized)
    t = _staggered_slice_nominal_time(t_local, duration_s, settle_s, choreography_s)
    lines: list[StaggeredSliceLineState] = []

    for line_index, line in enumerate(logical_lines):
        if line_index == 0:
            graphemes = _segment_graphemes(line)
            first_space = next(
                (i for i, grapheme in enumerate(graphemes) if grapheme.isspace()), -1
            )
            first_word_count = len(graphemes) if first_space == -1 else first_space
            remainder_count = max(0, len(graphemes) - first_word_count)
            first_stagger = (
                0.0
                if first_word_count <= 1
                else min(
                    0.07,
                    (_STAGGERED_SLICE_FIRST_WORD_END_S - _STAGGERED_SLICE_GLYPH_DURATION_S)
                    / (first_word_count - 1),
                )
            )
            remainder_stagger = (
                0.0
                if remainder_count <= 1
                else (
                    _STAGGERED_SLICE_REMAINDER_END_S
                    - _STAGGERED_SLICE_REMAINDER_START_S
                    - _STAGGERED_SLICE_GLYPH_DURATION_S
                )
                / (remainder_count - 1)
            )
            glyph_states: list[StaggeredSliceGlyphState] = []
            for glyph_index, grapheme in enumerate(graphemes):
                if glyph_index < first_word_count:
                    start = glyph_index * first_stagger
                else:
                    start = (
                        _STAGGERED_SLICE_REMAINDER_START_S
                        + (glyph_index - first_word_count) * remainder_stagger
                    )
                progress = _staggered_slice_progress(t, start, _STAGGERED_SLICE_GLYPH_DURATION_S)
                glyph_states.append(
                    StaggeredSliceGlyphState(
                        grapheme=grapheme,
                        opacity=progress,
                        translate_y_em=0.18 * (1.0 - progress),
                        rotate_deg=(-4.0 if glyph_index % 2 == 0 else 4.0) * (1.0 - progress),
                    )
                )
            lines.append(
                StaggeredSliceLineState(text=line, kind="glyphs", glyphs=tuple(glyph_states))
            )
            continue

        line_start = (
            _STAGGERED_SLICE_LINES_START_S + (line_index - 1) * _STAGGERED_SLICE_LINE_STAGGER_S
        )
        graphemes = _segment_graphemes(line)
        glyph_stagger = (
            0.0
            if len(graphemes) <= 1
            else (_STAGGERED_SLICE_LINE_GLYPH_STAGE_S - _STAGGERED_SLICE_GLYPH_DURATION_S)
            / (len(graphemes) - 1)
        )
        glyph_states = []
        for glyph_index, grapheme in enumerate(graphemes):
            progress = _staggered_slice_progress(
                t,
                line_start + glyph_index * glyph_stagger,
                _STAGGERED_SLICE_GLYPH_DURATION_S,
            )
            glyph_states.append(
                StaggeredSliceGlyphState(
                    grapheme=grapheme,
                    opacity=progress,
                    translate_y_em=0.18 * (1.0 - progress),
                    rotate_deg=(-4.0 if glyph_index % 2 == 0 else 4.0) * (1.0 - progress),
                )
            )
        lines.append(StaggeredSliceLineState(text=line, kind="glyphs", glyphs=tuple(glyph_states)))

    return StaggeredSliceState(
        lines=tuple(lines), settled=t + 1e-9 >= choreography_s, settle_s=settle_s
    )


def _overlay_max_width_px(overlay: dict, canvas: Canvas = PORTRAIT) -> float:
    """Per-overlay wrap budget. Missing field preserves the legacy 0.9 width."""
    return canvas.width * resolve_max_width_frac(overlay.get("max_width_frac"))


def _render_canvas_kwargs(canvas: Canvas) -> dict[str, Canvas]:
    return {"render_canvas": canvas} if canvas != PORTRAIT else {}


def _canvas_kwargs(canvas: Canvas) -> dict[str, Canvas]:
    return {"canvas": canvas} if canvas != PORTRAIT else {}


# -- Typeface cache (load once at import) -------------------------------------
#
# Loading TTFs via Skia.Typeface.MakeFromFile re-parses the font (~5-15ms for
# a 400KB file). For an animated overlay rendered at 30fps, that's most of the
# per-frame budget. Preload every font in the registry at import time and
# fail-fast if any is missing — better to crash the worker boot than to render
# a job with the wrong font silently.

_TYPEFACE_BY_PATH: dict[str, skia.Typeface] = {}

# Overlay role for editorial transcript-synced typographic sequences ("role" is
# an existing burn-dict field — "generative_intro" is the precedent value; this
# adds a sibling VALUE, not a new field name, per decision D17). Sequence
# overlays hold a scene on screen for its whole transcript window and carry the
# lyric-path fade field names (fade_out_ms / fade_out_curve) for their tail.
SEQUENCE_OVERLAY_ROLE = "generative_sequence"

# Effects a sequence overlay may carry: the fade-in head reuses the existing
# "fade-in" effect; "static"/"none" scenes appear settled and only need the
# fade-out tail.
_SEQUENCE_FADE_EFFECTS = ("fade-in", "static", "none")

# The fade-in effect's alpha ramp settles at this t_local (mirrors the
# `effect == "fade-in"` branch in _draw_with_animation: progress = t / 0.4).
_FADE_IN_SETTLE_S = 0.4


def _link_or_copy(src_path: str, dst_path: str) -> None:
    """Hard-link src→dst for frame economy, falling back to a byte copy.

    Single source for the hold-frame de-dup used by both the per-overlay
    sequence renderer and the composite stream: a hard link costs ~nothing and
    keeps PNG-sequence scratch flat, but cross-device temp dirs (or a FS without
    hard links) raise OSError — there a copy keeps correctness.
    """
    try:
        os.link(src_path, dst_path)
    except OSError:
        shutil.copy2(src_path, dst_path)


def _uses_long_running_frame_ceiling(overlay: dict) -> bool:
    """True for text effects that must hold through their full lyric window.

    Font-cycle-like effects need a short cap because every frame can be unique.
    Lyric-timed effects settle visually but remain semantically visible until
    the next lyric boundary, so capping them at ~4s makes the text disappear
    before late words can highlight or before the next cumulative stage starts.
    Sequence overlays (role="generative_sequence") hold an editorial scene
    through its full transcript window — often well past the 120-frame cap —
    so they use the same generous sanity ceiling. `behind_subject` overlays
    get a long ceiling too (their own, larger one —
    BEHIND_SUBJECT_FRAME_CEILING, selected by the caller): every frame renders
    uniquely (the hold-frame hard-link economy is disabled once occlusion is
    on, since the subject moves), and hold-to-EOF generative intro overlays
    can span nearly the whole clip, so a plain 5s intro would otherwise blow
    the 120-frame cap and vanish mid-word.
    """
    if overlay.get("role") == SEQUENCE_OVERLAY_ROLE:
        return True
    if overlay.get("behind_subject"):
        return True
    effect = overlay.get("effect", "none")
    return effect in {"lyric-line", "karaoke-line"} or (
        effect == "pop-in" and bool(overlay.get("pop_animated_suffix"))
    )


def _sequence_fade_out_ms(overlay: dict) -> int:
    try:
        return max(0, int(overlay.get("fade_out_ms") or 0))
    except (TypeError, ValueError):
        return 0


def _is_sequence_overlay(overlay: dict) -> bool:
    return (
        overlay.get("role") == SEQUENCE_OVERLAY_ROLE
        and overlay.get("effect", "none") in _SEQUENCE_FADE_EFFECTS
    )


def _sequence_fade_out_alpha(overlay: dict, t_local: float, duration_s: float) -> float:
    """Fade-out multiplier for role="generative_sequence" overlays.

    REUSES the lyric-line field names and curve semantics (`fade_out_ms`,
    `fade_out_curve` — see `_lyric_line_alpha`): full opacity until the last
    `fade_out_ms` of the window, then the libass accel=2.0 lingering curve
    (1 - progress²), or the sqrt mirror (1 - √progress) when
    `fade_out_curve == "sqrt"`. Unlike the lyric path there is NO default —
    only an overlay that carries `fade_out_ms` gets a tail ramp, so sequence
    overlays without the field render exactly like before.
    """
    if duration_s <= 0:
        return 1.0
    duration_ms = max(0, int(round(duration_s * 1000.0)))
    fade_out = min(_sequence_fade_out_ms(overlay), duration_ms)
    if fade_out == 0:
        return 1.0
    t_ms = max(0.0, min(t_local, duration_s)) * 1000.0
    fade_out_start_ms = duration_ms - fade_out
    if t_ms < fade_out_start_ms:
        return 1.0
    progress = (t_ms - fade_out_start_ms) / float(fade_out)
    if overlay.get("fade_out_curve") == "sqrt":
        return max(0.0, 1.0 - progress**0.5)
    return max(0.0, 1.0 - progress**2)


def _preload_typefaces() -> None:
    for entry in _FONT_REGISTRY.get("fonts", {}).values():
        path = os.path.join(FONTS_DIR, entry["file"])
        if not os.path.exists(path):
            log.warning("skia_typeface_missing_at_preload", path=path)
            continue
        tf = skia.Typeface.MakeFromFile(path)
        if tf is None:
            log.warning("skia_typeface_load_returned_none", path=path)
            continue
        _TYPEFACE_BY_PATH[path] = tf


_preload_typefaces()


@dataclass(frozen=True)
class TypefaceResolution:
    typeface: skia.Typeface
    requested_font_family: str | None
    requested_font_style: str | None
    name: str
    file: str
    source: str

    @property
    def fallback(self) -> bool:
        return bool(self.requested_font_family and self.name != self.requested_font_family)

    def report_dict(self) -> dict[str, Any]:
        return {
            "requested_font_family": self.requested_font_family,
            "requested_font_style": self.requested_font_style,
            "name": self.name,
            "file": self.file,
            "source": self.source,
            "fallback": self.fallback,
        }

    def event_typeface_dict(self) -> dict[str, str]:
        return {"name": self.name, "file": self.file, "source": self.source}


class MissingGlyphsError(ValueError):
    """Raised when a typeface lacks glyphs needed for the text it will render.

    Skia draws `.notdef` (tofu box) for absent glyphs without raising — for
    languages with diacritics (e.g. Turkish ç ş ğ ı İ ö ü) a font-bundle
    regression would ship visually broken renders to users with NO log signal.
    Callers verifying glyph coverage in advance (eval gates, CI, the language
    rollout check) use this to fail loudly.
    """


def assert_glyphs_present(typeface: skia.Typeface, text: str) -> None:
    """Verify every non-whitespace codepoint in `text` is present in `typeface`.

    Whitespace is skipped because SkFont metrics for space/tab/newline are
    layout-level concerns, not glyph-coverage concerns. Raises MissingGlyphsError
    listing each missing codepoint as both U+HHHH and the literal character so
    operators can grep the log for a known glyph.
    """
    missing = [
        f"U+{ord(ch):04X} ({ch!r})"
        for ch in dict.fromkeys(text)  # dedupe, preserve order
        if not ch.isspace() and typeface.unicharToGlyph(ord(ch)) == 0
    ]
    if missing:
        raise MissingGlyphsError(f"Typeface missing {len(missing)} glyph(s): {', '.join(missing)}")


def assert_lyric_glyphs(typeface: skia.Typeface, text: str) -> None:
    """Glyph-coverage fail-fast for the lyric burn paths (karaoke / lyric pop).

    Checks alphabetic codepoints only: a display face missing an em-dash or a
    curly quote renders one tofu char (cosmetic), but missing LETTERS means the
    whole lyric line is unreadable — the CJK-track case. Raising here turns
    "silently render tofu" into a typed per-variant failure
    (error_class=lyrics_unsupported_language in generative_build). Letters-only
    scope keeps previously-working Latin renders unaffected.
    """
    letters = "".join(ch for ch in text if ch.isalpha())
    if letters:
        assert_glyphs_present(typeface, letters)


def _overlay_text(overlay: dict) -> str:
    """Return the text the renderer should burn for this overlay.

    Prefers `display_text` when set (Layer 2 line-style finalization in
    `lyric_injector._finalize_lyric_audible_window` writes this when it
    truncates a partial lyric line to its audible-word substring). Falls
    back to `text` for every other case so non-music callers are byte-
    identical to pre-PR behavior.

    Plan: plans/geli-me-var-ama-hatalar-robust-reddy.md §2c, §2g.
    """
    dt = overlay.get("display_text")
    if dt:
        return dt
    return overlay.get("text", "") or ""


def _registry_typeface(name: str) -> tuple[str, str, skia.Typeface] | None:
    entry = _FONT_REGISTRY.get("fonts", {}).get(name)
    if not entry:
        return None
    path = os.path.join(FONTS_DIR, entry["file"])
    tf = _TYPEFACE_BY_PATH.get(path)
    if tf is None:
        return None
    return name, entry["file"], tf


def _resolve_typeface_for_overlay(
    overlay: dict, font_name_override: str | None = None
) -> TypefaceResolution:
    """Resolve a typeface for an overlay following the same priority as
    text_overlay._draw_text_png: font_name_override → overlay.font_family →
    overlay.font_style → final fallback (Playfair Display Bold).
    """
    requested_family = overlay.get("font_family") or None
    requested_style = overlay.get("font_style", "display") or None

    name = font_name_override or requested_family
    if name:
        resolved = _registry_typeface(str(name))
        if resolved is not None:
            resolved_name, file_name, tf = resolved
            source = "font_name_override" if font_name_override else "font_family"
            return TypefaceResolution(
                typeface=tf,
                requested_font_family=requested_family,
                requested_font_style=requested_style,
                name=resolved_name,
                file=file_name,
                source=source,
            )

    style = requested_style or "display"
    style_default = _FONT_REGISTRY.get("style_defaults", {}).get(style)
    if style_default:
        resolved = _registry_typeface(str(style_default))
        if resolved is not None:
            resolved_name, file_name, tf = resolved
            return TypefaceResolution(
                typeface=tf,
                requested_font_family=requested_family,
                requested_font_style=requested_style,
                name=resolved_name,
                file=file_name,
                source="style_default",
            )

    # Last resort: Playfair Display Bold (always in the registry)
    p = os.path.join(FONTS_DIR, "PlayfairDisplay-Bold.ttf")
    tf = _TYPEFACE_BY_PATH.get(p)
    if tf is None:
        # Should never happen — preload would have logged a warning
        tf = skia.Typeface.MakeFromFile(p)
    return TypefaceResolution(
        typeface=tf,
        requested_font_family=requested_family,
        requested_font_style=requested_style,
        name="Playfair Display",
        file="PlayfairDisplay-Bold.ttf",
        source="last_resort",
    )


def resolved_typeface_for_overlay(overlay: dict) -> dict[str, Any]:
    """Public metadata helper for overlay_verify report.json."""
    return _resolved_typeface_for_render(overlay).report_dict()


def _typeface_for_overlay(overlay: dict, font_name_override: str | None = None) -> skia.Typeface:
    return _resolve_typeface_for_overlay(overlay, font_name_override).typeface


def _resolved_typeface_for_render(overlay: dict) -> TypefaceResolution:
    if overlay.get("effect") != "font-cycle":
        return _resolve_typeface_for_overlay(overlay)

    settle_name = overlay.get("font_family") or "Playfair Display"
    resolution = _resolve_typeface_for_overlay(overlay, font_name_override=str(settle_name))
    if resolution.source == "font_name_override":
        return replace(resolution, source="font_cycle_settle")
    return resolution


def _record_font_resolved_event(overlay: dict, overlay_index: int) -> None:
    resolution = _resolved_typeface_for_render(overlay)
    level = "warning" if resolution.fallback else "info"
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            "overlay",
            "font_resolved",
            {
                "overlay_index": overlay_index,
                "text": _overlay_text(overlay),
                "effect": overlay.get("effect", "none"),
                "requested_font_family": resolution.requested_font_family,
                "requested_font_style": resolution.requested_font_style,
                "resolved_typeface": resolution.event_typeface_dict(),
                "fallback": resolution.fallback,
                "level": level,
            },
        )
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break render
        log.warning("font_resolved_event_emit_failed", error=str(exc))


def _cycle_contrast_font_names() -> list[str]:
    out = []
    for name, entry in _FONT_REGISTRY.get("fonts", {}).items():
        if entry.get("cycle_role") == "contrast":
            p = os.path.join(FONTS_DIR, entry["file"])
            if p in _TYPEFACE_BY_PATH:
                out.append(name)
    return out


_CYCLE_CONTRAST_NAMES = _cycle_contrast_font_names()


# Emoji image cache. `skia.Data.MakeFromFileName` + `Image.MakeFromEncoded` cost
# real time on each call (~3-5 ms for a 72×72 Twemoji PNG). Animated overlays
# with `emoji_prefix` render the same emoji on every frame — caching makes the
# repeated cost vanish. Keyed by absolute path; bounded by the emoji asset
# directory size (~3 KB per entry, well under 1 MB total).
_EMOJI_IMG_CACHE: dict[str, skia.Image] = {}


def _load_emoji_image(path: str) -> skia.Image | None:
    img = _EMOJI_IMG_CACHE.get(path)
    if img is not None:
        return img
    try:
        img = skia.Image.MakeFromEncoded(skia.Data.MakeFromFileName(path))
    except Exception as exc:
        log.warning("skia_emoji_load_failed", path=path, error=str(exc))
        return None
    if img is None:
        return None
    _EMOJI_IMG_CACHE[path] = img
    return img


# -- Color helpers ------------------------------------------------------------


def _skia_color_from_rgba(rgba: tuple[int, int, int, int]) -> int:
    r, g, b, a = rgba
    return skia.ColorSetARGB(a, r, g, b)


def _skia_color_from_hex(hex_color: str, alpha: int = 255) -> int:
    r, g, b, _ = _hex_to_rgba(hex_color)
    return skia.ColorSetARGB(alpha, r, g, b)


def _clamp_byte(value: float) -> int:
    return max(0, min(255, int(value)))


def _resolve_glow_kwargs(overlay: dict, alpha: float = 1.0) -> dict[str, Any]:
    """Skia-only outer text halo. Missing/zero fields preserve the legacy path."""
    strength = _finite_float(overlay.get("glow_strength"), 0.0)
    strength = max(0.0, min(1.0, strength)) * max(0.0, min(1.0, alpha))
    color = overlay.get("glow_color")
    if strength <= 0.0 or not isinstance(color, str) or not color:
        return {}
    try:
        r, g, b, _ = _hex_to_rgba(color)
    except Exception:  # noqa: BLE001 - invalid optional decoration should fail closed
        return {}
    return {"glow_rgb": (r, g, b), "glow_strength": strength}


def _skia_gradient_shader(
    spec: dict,
    block_x: float,
    block_top: float,
    block_w: float,
    block_h: float,
    alpha: float,
) -> skia.Shader | None:
    """Build a Skia linear gradient shader from a normalised gradient spec.

    The shader spans the block bounding box along ``spec["angle_deg"]``, so the
    full colour range is visible across the text regardless of canvas position.
    Returns ``None`` when the spec is invalid so callers fall back to solid fill.
    """
    import math  # noqa: PLC0415 — avoid circular at module level

    parsed = _parse_gradient_spec(spec)
    if parsed is None:
        return None

    alpha_int = int(255 * alpha)
    color_ints = [skia.ColorSetARGB(alpha_int, r, g, b) for r, g, b, _ in parsed["colors"]]
    stops = [float(s) for s in parsed["stops"]]

    angle_rad = math.radians(parsed["angle_deg"] % 360)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    cx = block_x + block_w / 2
    cy = block_top + block_h / 2
    # Half-span: project the block rectangle's extent onto the gradient axis so
    # stop 0 maps to one edge and stop 1 to the opposite edge.
    half = (abs(block_w * cos_a) + abs(block_h * sin_a)) / 2

    p0 = skia.Point(cx - cos_a * half, cy - sin_a * half)
    p1 = skia.Point(cx + cos_a * half, cy + sin_a * half)

    return skia.GradientShader.MakeLinear(
        [p0, p1],
        color_ints,
        stops,
    )


# -- Layout helpers -----------------------------------------------------------


def _resolve_font_size_px(overlay: dict) -> int:
    px = overlay.get("text_size_px")
    if px:
        return max(_MIN_FONT_SIZE, int(px))
    size_class = overlay.get("text_size", "medium")
    return max(_MIN_FONT_SIZE, _FONT_SIZE_MAP.get(size_class, 72))


def _resolve_anchor(overlay: dict, canvas: Canvas = PORTRAIT) -> tuple[float, float]:
    """Return (anchor_x_px, baseline_y_px) for the overlay's position.

    For center-anchored overlays the x is the line CENTER; for left-anchored
    overlays (`text_anchor="left"`) it is the line's LEFT edge. The draw
    functions branch on `_resolve_text_anchor` to place glyphs accordingly.
    """
    x_frac = overlay.get("position_x_frac")
    y_frac = overlay.get("position_y_frac")
    if y_frac is None:
        y_frac = _POSITION_Y.get(overlay.get("position", "center"), 0.5)
    if x_frac is None:
        x_frac = 0.5
    return float(x_frac) * canvas.width, float(y_frac) * canvas.height


def _resolve_text_anchor(overlay: dict) -> str:
    """Horizontal anchor: "left" pins the line's left edge at position_x_frac,
    "right" pins the right edge, "center" (default) centers the line on it.

    Layer-2 overlays set `text_anchor="left"` + `position_x_frac=0.05`. Before
    this was honored, Skia centered every line on the 5% point, pushing the
    left half of each cumulative line off-screen ("It's not just luck" →
    "s not just luck" on prod template 89cde014). The schema also permits
    "right" (app/agents/_schemas/template_text.py); both renderers must honor
    all three or the same class of clip recurs on the un-handled value."""
    anchor = overlay.get("text_anchor")
    return anchor if anchor in ("left", "right") else "center"


def _anchored_left_x(anchor: str, cx: float, width: float) -> float:
    """Left edge of a `width`-wide line anchored at cx. Mirrors the anchor
    math in text_overlay.py: left → cx, right → cx-width, center → cx-width/2.
    Centralized so every Skia draw path (centered, pop-in, karaoke) agrees."""
    if anchor == "left":
        return cx
    if anchor == "right":
        return cx - width
    return cx - width / 2.0


def _resolve_vertical_anchor(overlay: dict) -> str:
    """Resolve vertical anchoring without changing legacy overlay semantics.

    Historical left-anchored cumulative reveals grow down from their y value.
    Authored TextElements explicitly carry ``vertical_anchor=center`` so their
    horizontal line alignment can change without moving the block vertically.
    """
    vertical_anchor = overlay.get("vertical_anchor")
    if vertical_anchor in ("top", "center"):
        return vertical_anchor
    return "top" if _resolve_text_anchor(overlay) == "left" else "center"


def _vertical_block_top(vertical_anchor: str, cy: float, block_h: float) -> float:
    """Top y of a text block for the resolved vertical anchor."""
    if vertical_anchor == "top":
        return cy
    return cy - block_h / 2.0


# ── Letter-spacing primitives (T11 parity slice) ──────────────────────────────
#
# Skia has no per-font tracking control on drawString, so a non-zero
# `letter_spacing` (em, from the burn dict) renders per-character with an
# explicit advance. Measure and draw MUST share the same advance model:
# per-char width sum + spacing×(n−1). Kerning pairs are lost when tracking is
# applied — the same behavior as CSS letter-spacing rendering in practice
# (documented tolerance; the layout contract locks the em→px resolution, not
# glyph-level kerning).


def _overlay_letter_spacing_px(overlay: dict, font_size_px: float) -> float:
    """Tracking in px at the FINAL drawn size (0.0 = pristine legacy path)."""
    return resolve_letter_spacing_px(overlay.get("letter_spacing"), font_size_px)


def _measure_line(font: skia.Font, text: str, spacing_px: float = 0.0) -> float:
    """Line width under the spaced advance model. spacing_px == 0 keeps the
    kerned single-call measure — byte-identical to the pre-T11 renderer."""
    if spacing_px == 0.0 or len(text) <= 1:
        return font.measureText(text)
    return sum(font.measureText(ch) for ch in text) + spacing_px * (len(text) - 1)


def _draw_string_spaced(
    canvas: skia.Canvas,
    text: str,
    x: float,
    baseline_y: float,
    font: skia.Font,
    paint: skia.Paint,
    spacing_px: float = 0.0,
) -> None:
    """drawString with optional per-character tracking (advance model matches
    _measure_line exactly, so anchoring math stays consistent)."""
    if spacing_px == 0.0 or len(text) <= 1:
        canvas.drawString(text, x, baseline_y, font, paint)
        return
    cx = x
    for ch in text:
        canvas.drawString(ch, cx, baseline_y, font, paint)
        cx += font.measureText(ch) + spacing_px


def _wrap_text_to_lines(
    text: str, font: skia.Font, max_width: float, spacing_px: float = 0.0
) -> list[str]:
    """Greedy word-wrap, mirrors text_overlay._wrap_text_to_lines.

    If the input already contains explicit newlines, each line is wrapped
    separately and the results concatenated (matches Pillow's `text.split("\\n")`
    sites elsewhere in the codebase).
    """
    out: list[str] = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        if not words:
            out.append("")
            continue
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word]) if current else word
            if _measure_line(font, candidate, spacing_px) <= max_width or not current:
                current.append(word)
            else:
                out.append(" ".join(current))
                current = [word]
        if current:
            out.append(" ".join(current))
    return out


def _wrap_word_indices(
    words: list[str], font: skia.Font, max_width: float, spacing_px: float = 0.0
) -> list[list[int]]:
    """Balanced word-wrap that preserves original word indices."""
    if spacing_px == 0.0:
        return balanced_word_wrap_indices(words, font.measureText, max_width)
    return balanced_word_wrap_indices(
        words, lambda s: _measure_line(font, s, spacing_px), max_width
    )


def _shrink_to_fit(
    text: str,
    typeface: skia.Typeface,
    initial_size: int,
    max_width: float,
    letter_spacing_em: float = 0.0,
) -> tuple[skia.Font, int, list[str]]:
    """Wrap + iteratively shrink the font until every line fits inside
    max_width. Caps at 6 iterations and a 24px floor, matching Pillow.
    Tracking (letter_spacing_em) scales with the size under test so the
    fitted result matches what actually draws.

    Returns (font, final_size_px, wrapped_lines).
    """
    size = initial_size
    font = skia.Font(typeface, size)
    font.setSubpixel(True)
    spacing_px = letter_spacing_em * size
    lines = _wrap_text_to_lines(text, font, max_width, spacing_px)

    iterations = 0
    while iterations < 6 and size > _MIN_FONT_SIZE:
        widest = max((_measure_line(font, ln, spacing_px) for ln in lines), default=0)
        if widest <= max_width:
            break
        size = max(_MIN_FONT_SIZE, int(size * 0.85))
        font = skia.Font(typeface, size)
        font.setSubpixel(True)
        spacing_px = letter_spacing_em * size
        lines = _wrap_text_to_lines(text, font, max_width, spacing_px)
        iterations += 1

    return font, size, lines


def _wrap_at_fixed_size(
    text: str,
    typeface: skia.Typeface,
    size: int,
    max_width: float,
    letter_spacing_em: float = 0.0,
) -> tuple[skia.Font, int, list[str]]:
    """Wrap text at the requested font size without shrinking.

    Used for lyric pop-up stages where typography should stay constant while
    the cumulative sentence grows. Long phrases move onto another line instead
    of getting smaller as more words appear.
    """
    size = max(_MIN_FONT_SIZE, int(size))
    font = skia.Font(typeface, size)
    font.setSubpixel(True)
    return font, size, _wrap_text_to_lines(text, font, max_width, letter_spacing_em * size)


def _shrink_to_fit_max_lines(
    text: str,
    typeface: skia.Typeface,
    initial_size: int,
    max_width: float,
    max_lines: int,
) -> tuple[skia.Font, int, list[str]]:
    """Wrap + shrink until text fits horizontally and within max_lines."""
    size = initial_size
    font = skia.Font(typeface, size)
    font.setSubpixel(True)
    lines = _wrap_text_to_lines(text, font, max_width)

    iterations = 0
    while iterations < 10 and size > _MIN_FONT_SIZE:
        widest = max((font.measureText(ln) for ln in lines), default=0)
        if widest <= max_width and len(lines) <= max_lines:
            break
        size = max(_MIN_FONT_SIZE, int(size * 0.9))
        font = skia.Font(typeface, size)
        font.setSubpixel(True)
        lines = _wrap_text_to_lines(text, font, max_width)
        iterations += 1

    return font, size, lines


def _measure_block(
    font: skia.Font,
    lines: list[str],
    *,
    line_spacing: float = _LINE_SPACING,
    letter_spacing_px: float = 0.0,
) -> dict[str, Any]:
    """Compute per-line widths, max line height, total block height.

    `line_spacing` is the per-overlay multiplier (burn-dict `line_spacing`,
    default = the legacy 1.15 constant); `letter_spacing_px` feeds the spaced
    advance model so widths match what _draw_string_spaced draws. TS mirror of
    the step/height math: blockMetrics in lib/overlay-layout.ts (parity
    fixture line_spacing.json)."""
    metrics = font.getMetrics()
    line_height_raw = metrics.fDescent - metrics.fAscent
    line_step = int(line_height_raw * line_spacing)
    widths = [_measure_line(font, ln, letter_spacing_px) for ln in lines]
    block_h = line_step * (len(lines) - 1) + int(line_height_raw) if lines else 0
    return {
        "widths": widths,
        "line_step": line_step,
        "block_h": block_h,
        "ascent_offset": -metrics.fAscent,
    }


def _normalize_reveal_text(text: str) -> str:
    """Match the wrapper's whitespace normalization before reveal slicing."""
    return "\n".join(" ".join(raw_line.split()) for raw_line in text.split("\n"))


def _fixed_reveal_lines(lines: list[str], visible_text: str) -> tuple[list[str], int]:
    """Map a visible prefix onto lines wrapped from the complete text.

    The complete lines own geometry; the returned prefixes only control which
    glyphs are painted. A single source separator (space or newline) is
    consumed between wrapped lines so reveal timing continues naturally.
    """
    remaining = len(visible_text)
    revealed: list[str] = []
    cursor_line = 0
    for index, line in enumerate(lines):
        visible_count = min(len(line), remaining)
        revealed.append(line[:visible_count])
        if visible_count > 0:
            cursor_line = index
        if remaining <= len(line):
            remaining = 0
        else:
            remaining -= len(line)
            if index < len(lines) - 1:
                remaining = max(0, remaining - 1)
    return revealed, cursor_line


def fit_text_size_px(
    text: str,
    typeface: skia.Typeface,
    box_w_px: float,
    box_h_px: float,
    *,
    max_px: int,
    min_px: int = _MIN_FONT_SIZE,
) -> int:
    """Largest px (<= max_px, >= min_px) at which `text` — word-wrapped to
    `box_w_px` — fits inside a (box_w_px x box_h_px) box without clipping.

    The inverse of `_shrink_to_fit`: that one only shrinks a known start size to
    stop horizontal overflow; this searches DOWNWARD from `max_px` to FILL an
    empty box (both width and stacked-line height), so a calm frame gets large
    text and a tight one gets small text. Uses the same wrap + block-measure
    primitives the renderer uses, so the size this returns is the size that
    actually renders. `_shrink_to_fit` still runs at draw time as the final
    clip-safety clamp.
    """
    min_px = max(_MIN_FONT_SIZE, int(min_px))
    size = max(min_px, int(max_px))
    while size > min_px:
        font = skia.Font(typeface, size)
        font.setSubpixel(True)
        lines = _wrap_text_to_lines(text, font, box_w_px)
        widest = max((font.measureText(ln) for ln in lines), default=0.0)
        block_h = _measure_block(font, lines)["block_h"]
        if widest <= box_w_px and block_h <= box_h_px:
            return size
        size = int(size * 0.92)
    return min_px


def measure_text_overlay_box(
    overlay: dict,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> dict[str, float]:
    """Measure a title with the exact typeface/wrap primitives used to draw it.

    Smart layout arbitration calls this before the Skia burn. Returning a plain
    normalized dict keeps this module independent from the compositor's models
    while guaranteeing both paths protect the same rendered block.
    """

    text = _overlay_text(overlay)
    if not text:
        return {"left": 0.5, "top": 0.5, "right": 0.5, "bottom": 0.5}
    typeface = _typeface_for_overlay(overlay)
    initial_size = _resolve_font_size_px(overlay)
    max_width = _overlay_max_width_px(overlay, render_canvas)
    letter_spacing_em = resolve_letter_spacing_em(overlay.get("letter_spacing"))
    font, size, lines = _shrink_to_fit(
        text,
        typeface,
        initial_size,
        max_width,
        letter_spacing_em,
    )
    spacing_px = letter_spacing_em * size
    block = _measure_block(
        font,
        lines,
        line_spacing=resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=spacing_px,
    )
    cx, cy = _resolve_anchor(overlay, render_canvas)
    width = max(block["widths"], default=0.0)
    left = _anchored_left_x(_resolve_text_anchor(overlay), cx, width)
    top = _vertical_block_top(_resolve_vertical_anchor(overlay), cy, block["block_h"])
    return {
        "left": max(0.0, left / render_canvas.width),
        "top": max(0.0, top / render_canvas.height),
        "right": min(1.0, (left + width) / render_canvas.width),
        "bottom": min(1.0, (top + block["block_h"]) / render_canvas.height),
    }


# -- Per-frame drawing -------------------------------------------------------


def _draw_centered_text(
    canvas: skia.Canvas,
    text: str,
    overlay: dict,
    *,
    render_canvas: Canvas = PORTRAIT,
    font_override: skia.Font | None = None,
    color_override: int | None = None,
    alpha: float = 1.0,
    scale: float = 1.0,
    x_translate: float = 0.0,
    y_translate: float = 0.0,
    scale_origin_x: float = 0.0,
    scale_origin_y: float = 0.0,
    layout_text: str | None = None,
    show_cursor: bool = False,
) -> None:
    """Draw `text` centered horizontally at the overlay's anchor, with
    shadow + optional stroke + fill. Mirrors Pillow's _draw_text_png layout:
    multi-line vertical centering, 0.9*CANVAS_W word-wrap, auto-shrink.

    Effect transforms (alpha, scale, translate, origin offset) are applied via
    a canvas matrix so glyph metrics are not perturbed.
    """
    if not text and not show_cursor:
        return

    geometry_text = layout_text if layout_text is not None else text
    if not geometry_text:
        return
    typeface = _typeface_for_overlay(overlay)
    letter_spacing_em = resolve_letter_spacing_em(overlay.get("letter_spacing"))
    max_width = _overlay_max_width_px(overlay, render_canvas)
    if font_override is not None:
        font = font_override
        size = int(font.getSize())
        lines = _wrap_text_to_lines(geometry_text, font, max_width, letter_spacing_em * size)
    else:
        initial_size = _resolve_font_size_px(overlay)
        if overlay.get("preserve_font_size"):
            font, size, lines = _wrap_at_fixed_size(
                geometry_text, typeface, initial_size, max_width, letter_spacing_em
            )
        else:
            font, size, lines = _shrink_to_fit(
                geometry_text, typeface, initial_size, max_width, letter_spacing_em
            )

    if not lines:
        return

    draw_lines, cursor_line = (
        _fixed_reveal_lines(lines, text) if layout_text is not None else (lines, len(lines) - 1)
    )

    letter_spacing_px = letter_spacing_em * size
    block = _measure_block(
        font,
        lines,
        line_spacing=resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=letter_spacing_px,
    )
    cx, cy = _resolve_anchor(overlay, render_canvas)
    anchor = _resolve_text_anchor(overlay)

    block_top = _vertical_block_top(_resolve_vertical_anchor(overlay), cy, block["block_h"])
    first_baseline = block_top + block["ascent_offset"]

    # Emoji prefix: composite to the left of line 0
    first_w = block["widths"][0] if block["widths"] else 0
    emoji_metrics = _resolve_emoji_metrics(overlay, first_w, font, render_canvas=render_canvas)

    base_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"), int(255 * alpha))
    fill_color = color_override if color_override is not None else base_color
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    shadow_alpha = 0 if overlay.get("shadow_enabled") is False else int(160 * alpha)

    # Build gradient shader when text_gradient is set and no explicit
    # color_override (karaoke highlight keeps solid colour).
    gradient_shader: Any = None
    if overlay.get("text_gradient") and color_override is None:
        block_w = max(block["widths"]) if block["widths"] else float(render_canvas.width)
        block_x = _anchored_left_x(anchor, cx, block_w)
        gradient_shader = _skia_gradient_shader(
            overlay["text_gradient"],
            block_x,
            block_top,
            block_w,
            float(block["block_h"]),
            alpha,
        )

    canvas.save()
    # Center transform on anchor unless an effect supplies a different focal point.
    rotation_deg = _finite_float(overlay.get("rotation_deg"), 0.0)
    origin_x = cx + scale_origin_x
    origin_y = cy + scale_origin_y
    canvas.translate(origin_x + x_translate, origin_y + y_translate)
    if rotation_deg:
        canvas.rotate(rotation_deg)
    canvas.scale(scale, scale)
    canvas.translate(-origin_x, -origin_y)

    for i, line in enumerate(lines):
        baseline_y = first_baseline + i * block["line_step"]
        line_w = block["widths"][i]
        if i == 0 and emoji_metrics is not None:
            combined_w = emoji_metrics["size"] + emoji_metrics["gap"] + line_w
            block_left = _anchored_left_x(anchor, cx, combined_w)
            line_x = block_left + emoji_metrics["size"] + emoji_metrics["gap"]
        else:
            line_x = _anchored_left_x(anchor, cx, line_w)

        draw_kwargs: dict[str, Any] = {"shader": gradient_shader}
        if letter_spacing_px != 0.0:
            draw_kwargs["letter_spacing_px"] = letter_spacing_px
        draw_kwargs["layer_alpha"] = alpha
        draw_kwargs.update(_resolve_glow_kwargs(overlay, alpha))
        draw_line = draw_lines[i]
        if draw_line:
            _draw_line_with_layers(
                canvas,
                draw_line,
                line_x,
                baseline_y,
                font,
                fill_color,
                stroke_px,
                shadow_alpha,
                **draw_kwargs,
            )
        if show_cursor and i == cursor_line:
            cursor_x = line_x + _measure_line(font, draw_line, letter_spacing_px)
            if draw_line and letter_spacing_px:
                cursor_x += letter_spacing_px
            _draw_line_with_layers(
                canvas,
                " |",
                cursor_x,
                baseline_y,
                font,
                fill_color,
                stroke_px,
                shadow_alpha,
                **draw_kwargs,
            )

    # Emoji compositing onto the canvas at the resolved combined-block x
    if emoji_metrics is not None and lines:
        first_line_h = block["line_step"]
        combined_w = emoji_metrics["size"] + emoji_metrics["gap"] + block["widths"][0]
        combined_x = _anchored_left_x(anchor, cx, combined_w)
        emoji_x = combined_x
        emoji_y = block_top + (first_line_h - emoji_metrics["size"]) / 2.0
        _draw_skia_image(canvas, emoji_metrics["img"], emoji_x, emoji_y, alpha=alpha)

    canvas.restore()


def _draw_line_with_layers(
    canvas: skia.Canvas,
    line: str,
    x: float,
    baseline_y: float,
    font: skia.Font,
    fill_color: int,
    stroke_px: int,
    shadow_alpha: int,
    *,
    shader: Any = None,
    letter_spacing_px: float = 0.0,
    layer_alpha: float = 1.0,
    glow_rgb: tuple[int, int, int] | None = None,
    glow_strength: float = 0.0,
) -> None:
    """Optional glow → shadow → stroke → fill, matching Pillow's base compositing.

    When ``shader`` is provided (a ``skia.Shader`` from
    ``_skia_gradient_shader``), the fill paint uses it instead of the solid
    ``fill_color``.  Shadow and stroke remain solid black so the gradient
    glyphs keep depth and legibility. ``glow_*`` is Skia-only and ignored by
    Pillow by design; absent/zero values skip the glow passes entirely.

    ``letter_spacing_px`` applies per-character tracking to every layer
    (glow, shadow, stroke, fill share one advance model — see _draw_string_spaced).
    """
    if glow_rgb is not None and glow_strength > 0.0:
        r, g, b = glow_rgb
        glow_strength = max(0.0, min(1.0, glow_strength))
        for sigma, base_alpha in ((8.0, 120.0), (20.0, 220.0)):
            glow_alpha = _clamp_byte(base_alpha * glow_strength * layer_alpha)
            if glow_alpha <= 0:
                continue
            glow_paint = skia.Paint(
                AntiAlias=True,
                Color=skia.ColorSetARGB(glow_alpha, r, g, b),
                MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma),
            )
            _draw_string_spaced(canvas, line, x, baseline_y, font, glow_paint, letter_spacing_px)

    # Shadow: soft black blur, offset 6px down (Pillow uses y+6, alpha 160).
    if shadow_alpha > 0:
        shadow_paint = skia.Paint(
            AntiAlias=True,
            Color=skia.ColorSetARGB(shadow_alpha, 0, 0, 0),
            MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 12.0),
        )
        _draw_string_spaced(
            canvas, line, x, baseline_y + 6.0, font, shadow_paint, letter_spacing_px
        )

    # Crisp black stroke (TikTok caption look)
    if stroke_px > 0:
        stroke_paint = skia.Paint(
            AntiAlias=True,
            Color=skia.ColorSetARGB(_clamp_byte(230 * layer_alpha), 0, 0, 0),
            Style=skia.Paint.kStroke_Style,
            StrokeWidth=stroke_px * 2.0,
            StrokeJoin=skia.Paint.kRound_Join,
        )
        _draw_string_spaced(canvas, line, x, baseline_y, font, stroke_paint, letter_spacing_px)

    # Fill: solid colour OR gradient shader
    fill_paint = skia.Paint(AntiAlias=True, Color=fill_color)
    if shader is not None:
        fill_paint.setShader(shader)
    _draw_string_spaced(canvas, line, x, baseline_y, font, fill_paint, letter_spacing_px)


def _draw_staggered_slice(
    canvas: skia.Canvas,
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> None:
    """Draw the editor-directed staggered glyph-build title animation."""
    text = _normalize_reveal_text(_overlay_text(overlay))
    if not text:
        return
    state = _staggered_slice_state(text, t_local, duration_s)
    if state.settled:
        _draw_centered_text(canvas, text, overlay, render_canvas=render_canvas)
        return

    typeface = _typeface_for_overlay(overlay)
    letter_spacing_em = resolve_letter_spacing_em(overlay.get("letter_spacing"))
    max_width = _overlay_max_width_px(overlay, render_canvas)
    initial_size = _resolve_font_size_px(overlay)
    if overlay.get("preserve_font_size"):
        font, size, _ = _wrap_at_fixed_size(
            text, typeface, initial_size, max_width, letter_spacing_em
        )
    else:
        font, size, _ = _shrink_to_fit(text, typeface, initial_size, max_width, letter_spacing_em)
    letter_spacing_px = letter_spacing_em * size

    visual_rows: list[tuple[int, str]] = []
    for logical_index, logical_line in enumerate(text.split("\n")):
        wrapped = _wrap_text_to_lines(logical_line, font, max_width, letter_spacing_px)
        visual_rows.extend((logical_index, row) for row in wrapped)
    lines = [row for _, row in visual_rows]
    block = _measure_block(
        font,
        lines,
        line_spacing=resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=letter_spacing_px,
    )
    cx, cy = _resolve_anchor(overlay, render_canvas)
    anchor = _resolve_text_anchor(overlay)
    block_top = _vertical_block_top(_resolve_vertical_anchor(overlay), cy, block["block_h"])
    first_baseline = block_top + block["ascent_offset"]
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    shadow_alpha = 0 if overlay.get("shadow_enabled") is False else 160
    base_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"), 255)
    block_w = max(block["widths"]) if block["widths"] else float(render_canvas.width)
    block_x = _anchored_left_x(anchor, cx, block_w)
    gradient_shader = (
        _skia_gradient_shader(
            overlay["text_gradient"],
            block_x,
            block_top,
            block_w,
            float(block["block_h"]),
            1.0,
        )
        if overlay.get("text_gradient")
        else None
    )
    draw_kwargs: dict[str, Any] = {"shader": gradient_shader}
    if letter_spacing_px != 0.0:
        draw_kwargs["letter_spacing_px"] = letter_spacing_px
    draw_kwargs.update(_resolve_glow_kwargs(overlay, 1.0))

    glyph_cursors = [0] * len(state.lines)
    seen_logical_rows: set[int] = set()

    for row_index, (logical_index, row) in enumerate(visual_rows):
        baseline_y = first_baseline + row_index * block["line_step"]
        row_width = block["widths"][row_index]
        line_x = _anchored_left_x(anchor, cx, row_width)
        logical_state = state.lines[min(logical_index, len(state.lines) - 1)]

        glyph_states = logical_state.glyphs
        glyph_cursor = glyph_cursors[logical_index]
        if logical_index in seen_logical_rows:
            while (
                glyph_cursor < len(glyph_states) and glyph_states[glyph_cursor].grapheme.isspace()
            ):
                glyph_cursor += 1
        seen_logical_rows.add(logical_index)
        x = line_x
        for grapheme in _segment_graphemes(row):
            if glyph_cursor >= len(glyph_states):
                break
            glyph_state = glyph_states[glyph_cursor]
            if glyph_state.grapheme != grapheme:
                match_index = next(
                    (
                        i
                        for i in range(glyph_cursor, len(glyph_states))
                        if glyph_states[i].grapheme == grapheme
                    ),
                    glyph_cursor,
                )
                glyph_cursor = match_index
                glyph_state = glyph_states[glyph_cursor]
            glyph_width = font.measureText(grapheme)
            if glyph_state.opacity > 0.001:
                canvas.saveLayerAlpha(None, _clamp_byte(255 * glyph_state.opacity))
                pivot_x = x + glyph_width / 2.0
                pivot_y = baseline_y - size * 0.4
                canvas.translate(pivot_x, pivot_y + size * glyph_state.translate_y_em)
                canvas.rotate(glyph_state.rotate_deg)
                canvas.translate(-pivot_x, -pivot_y)
                _draw_line_with_layers(
                    canvas,
                    grapheme,
                    x,
                    baseline_y,
                    font,
                    base_color,
                    stroke_px,
                    shadow_alpha,
                    **draw_kwargs,
                )
                canvas.restore()
            x += glyph_width + letter_spacing_px
            glyph_cursor += 1
        glyph_cursors[logical_index] = glyph_cursor


# -- Emoji compositing -------------------------------------------------------


def _resolve_emoji_metrics(
    overlay: dict,
    first_line_width: float,
    font: skia.Font,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> dict[str, Any] | None:
    """Resolve emoji prefix as a Skia Image + sizing metrics. Mirrors Pillow's
    sizing: 95% of first line height, max combined width 95% of canvas."""
    emoji_prefix = overlay.get("emoji_prefix", "")
    if not emoji_prefix:
        return None
    path = _emoji_png_path(emoji_prefix)
    if not path:
        log.warning(
            "skia_emoji_asset_missing",
            emoji=emoji_prefix,
            codepoint=_emoji_codepoint(emoji_prefix),
        )
        return None

    metrics = font.getMetrics()
    first_line_h = metrics.fDescent - metrics.fAscent
    emoji_size = max(24, int(first_line_h * 0.95))
    gap = max(8, emoji_size // 6)
    combined = emoji_size + gap + first_line_width
    max_combined = int(render_canvas.width * 0.95)
    if combined > max_combined:
        overshoot = combined - max_combined
        new_size = max(20, emoji_size - int(overshoot))
        if new_size != emoji_size:
            emoji_size = new_size
            gap = max(6, emoji_size // 6)

    img = _load_emoji_image(path)
    if img is None:
        return None
    return {"img": img, "size": emoji_size, "gap": gap}


def _draw_skia_image(
    canvas: skia.Canvas, img: skia.Image, x: float, y: float, *, alpha: float = 1.0
) -> None:
    """Composite a Skia Image at (x, y) with optional alpha. Used for emoji."""
    paint = skia.Paint(AntiAlias=True)
    if alpha < 1.0:
        paint.setAlphaf(alpha)
    target_size = canvas.imageInfo()  # noqa: F841 — kept for future scaling
    # Image already at the right size from caller's resize step
    canvas.drawImageRect(
        img,
        skia.Rect(x, y, x + img.width(), y + img.height()),
        skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear),
        paint,
    )


# -- Effect implementations --------------------------------------------------


def _select_cycle_font(overlay: dict, t_local: float, duration_s: float) -> str | None:
    """Font-cycle state: which font name to render at time t_local within the
    overlay. Matches text_overlay.FONT_CYCLE_INTERVAL_S / settle behavior.
    """
    settle_font = overlay.get("font_family") or "Playfair Display"
    contrast = overlay.get("cycle_fonts") or _CYCLE_CONTRAST_NAMES
    if not contrast:
        return settle_font

    settle_start = duration_s * (1.0 - FONT_CYCLE_SETTLE_RATIO)
    if t_local >= settle_start:
        return settle_font

    accel_at = overlay.get("font_cycle_accel_at_s")
    if accel_at is not None and t_local >= float(accel_at):
        interval = FONT_CYCLE_FAST_INTERVAL_S
    else:
        interval = FONT_CYCLE_INTERVAL_S

    step = int(t_local / interval)
    if step % 2 == 0:
        return settle_font
    return contrast[(step // 2) % len(contrast)]


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _ease_in_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4.0 * t**3
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def _motion_cubic_bezier(t: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Evaluate a Motion/CSS cubic-bezier easing curve at progress t."""
    target_x = max(0.0, min(1.0, t))
    if target_x <= 0.0:
        return 0.0
    if target_x >= 1.0:
        return 1.0

    def sample(axis_1: float, axis_2: float, u: float) -> float:
        inv = 1.0 - u
        return 3.0 * axis_1 * inv * inv * u + 3.0 * axis_2 * inv * u * u + u**3

    def sample_x(u: float) -> float:
        return sample(x1, x2, u)

    def sample_y(u: float) -> float:
        return sample(y1, y2, u)

    def sample_x_derivative(u: float) -> float:
        inv = 1.0 - u
        return 3.0 * x1 * inv * inv + 6.0 * (x2 - x1) * inv * u + 3.0 * (1.0 - x2) * u * u

    u = target_x
    for _ in range(8):
        error = sample_x(u) - target_x
        if abs(error) < 1e-6:
            return sample_y(u)
        derivative = sample_x_derivative(u)
        if abs(derivative) < 1e-6:
            break
        u = max(0.0, min(1.0, u - error / derivative))

    lower = 0.0
    upper = 1.0
    u = target_x
    for _ in range(12):
        if sample_x(u) < target_x:
            lower = u
        else:
            upper = u
        u = (lower + upper) / 2.0
    return sample_y(u)


def _clamped_keyframes_s(keyframes_s: tuple[float, ...], duration_s: float) -> tuple[float, ...]:
    last = keyframes_s[-1]
    if last <= 0 or last <= duration_s:
        return keyframes_s
    ratio = max(0.0, duration_s) / last
    return tuple(k * ratio for k in keyframes_s)


def _pop_in_scale_at(t_local: float, duration_s: float) -> float:
    k0, k1, k2 = _clamped_keyframes_s(_POP_IN_KEYFRAMES_S, duration_s)
    s0, s1, s2 = _POP_IN_SCALES
    if t_local <= k0:
        return s0
    if t_local < k1 and k1 > k0:
        p = (t_local - k0) / (k1 - k0)
        return s0 + (s1 - s0) * p
    if t_local < k2 and k2 > k1:
        p = (t_local - k1) / (k2 - k1)
        return s1 + (s2 - s1) * p
    return s2


def _draw_pop_in_with_suffix(
    canvas: skia.Canvas,
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> None:
    """pop-in with `pop_animated_suffix`: prefix renders static at 100% scale,
    suffix appears immediately at 100% scale.

    Used by music's per-word-pop lyric style. The reveal timing comes from the
    overlay's `start_s`, which is the sung word onset. Without the suffix split,
    every new lyric word re-renders the full accumulated line and looks like the
    whole sentence popped again.
    """
    text = _overlay_text(overlay)
    suffix = overlay.get("pop_animated_suffix") or ""
    if not text:
        return

    # If the suffix doesn't actually trail the text, fall back to a regular
    # pop-in (animate the whole line). Mirrors production's null-suffix path.
    if not suffix or not text.endswith(suffix):
        _draw_with_animation(
            canvas,
            overlay,
            t_local,
            duration_s,
            effect="pop-in",
            render_canvas=render_canvas,
        )
        return

    typeface = _typeface_for_overlay(overlay)
    assert_lyric_glyphs(typeface, text)
    initial_size = _resolve_font_size_px(overlay)
    cx, cy = _resolve_anchor(overlay, render_canvas)
    anchor = _resolve_text_anchor(overlay)
    # Lay out the FULL text so prefix + suffix stay in their final positions.
    # Lyric pop-up stages can opt into fixed typography: cumulative lines wrap
    # at the resolved size instead of shrinking as later words are added.
    # Center/right captions without that flag are capped at two lines.
    if overlay.get("preserve_font_size"):
        full_font, _full_size, full_lines = _wrap_at_fixed_size(
            text,
            typeface,
            initial_size,
            _overlay_max_width_px(overlay, render_canvas),
        )
    elif anchor == "left":
        full_font, _full_size, full_lines = _shrink_to_fit(
            text,
            typeface,
            initial_size,
            _overlay_max_width_px(overlay, render_canvas),
        )
    else:
        full_font, _full_size, full_lines = _shrink_to_fit_max_lines(
            text,
            typeface,
            initial_size,
            _overlay_max_width_px(overlay, render_canvas),
            _POP_SUFFIX_MAX_LINES,
        )
    if not full_lines:
        return

    suffix_line_idx = len(full_lines) - 1
    if not full_lines[suffix_line_idx].rstrip().endswith(suffix):
        _draw_with_animation(
            canvas,
            overlay,
            t_local,
            duration_s,
            effect="pop-in",
            render_canvas=render_canvas,
        )
        return

    block = _measure_block(full_font, full_lines)
    block_top = _vertical_block_top(_resolve_vertical_anchor(overlay), cy, block["block_h"])
    first_baseline = block_top + block["ascent_offset"]

    fill_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"))
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    glow_kwargs = _resolve_glow_kwargs(overlay)
    static_lines: list[str] = []
    for i, line in enumerate(full_lines):
        if i == suffix_line_idx:
            static_lines.append(line.rstrip()[: -len(suffix)].rstrip())
        else:
            static_lines.append(line)

    full_w = full_font.measureText(full_lines[suffix_line_idx])
    suffix_w = full_font.measureText(suffix)
    for i, line in enumerate(static_lines):
        if not line:
            continue
        baseline_y = first_baseline + i * block["line_step"]
        line_w = full_font.measureText(line)
        anchor_w = full_w if i == suffix_line_idx else line_w
        line_start_x = _anchored_left_x(anchor, cx, anchor_w)
        _draw_line_with_layers(
            canvas,
            line,
            line_start_x,
            baseline_y,
            full_font,
            fill_color,
            stroke_px,
            shadow_alpha=160,
            **glow_kwargs,
        )

    suffix_static_prefix = static_lines[suffix_line_idx]
    suffix_line_x = _anchored_left_x(anchor, cx, full_w)
    suffix_x = suffix_line_x + full_font.measureText(
        (suffix_static_prefix + " ") if suffix_static_prefix else ""
    )
    suffix_cx = suffix_x + suffix_w / 2.0
    baseline_y = first_baseline + suffix_line_idx * block["line_step"]
    scale = 1.0

    canvas.save()
    canvas.translate(suffix_cx, baseline_y)
    canvas.scale(scale, scale)
    canvas.translate(-suffix_cx, -baseline_y)
    _draw_line_with_layers(
        canvas,
        suffix,
        suffix_x,
        baseline_y,
        full_font,
        fill_color,
        stroke_px,
        shadow_alpha=160,
        **glow_kwargs,
    )
    canvas.restore()


def _draw_karaoke_line(
    canvas: skia.Canvas,
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> None:
    """karaoke-line: each word switches from primary color to highlight color
    when t_local crosses its start timestamp. word_timings is a list of
    {text, start_s, end_s, duration_cs} relative to the overlay start.

    Karaoke does NOT consume `display_text` — its per-word highlight order
    is keyed off `word_timings`. If audible-window finalization trims a
    karaoke line, it must rebuild `text` + `word_timings` before render.
    """
    text = overlay.get("text", "") or ""
    word_timings = overlay.get("word_timings") or []
    if not word_timings:
        _draw_centered_text(canvas, text, overlay, render_canvas=render_canvas)
        return

    words: list[str] = []
    starts: list[float] = []
    acc = 0.0
    for w in word_timings:
        word_text = str(w.get("text", "")).strip()
        if not word_text:
            continue
        words.append(word_text)
        start = _finite_float(w.get("start_s"), acc)
        fallback_dur_s = max(0.05, _finite_float(w.get("duration_cs"), 5.0) / 100.0)
        end = _finite_float(w.get("end_s"), start + fallback_dur_s)
        dur_s = end - start if end > start else fallback_dur_s
        clamped_start = max(0.0, start)
        starts.append(clamped_start)
        acc = max(acc, clamped_start + dur_s)
    if not words:
        _draw_centered_text(canvas, text, overlay, render_canvas=render_canvas)
        return

    typeface = _typeface_for_overlay(overlay)
    assert_lyric_glyphs(typeface, " ".join(words))
    initial_size = _resolve_font_size_px(overlay)
    font = skia.Font(typeface, initial_size)
    font.setSubpixel(True)
    max_width = _overlay_max_width_px(overlay, render_canvas)
    spacing_px = _overlay_letter_spacing_px(overlay, initial_size)
    line_word_indices = _wrap_word_indices(words, font, max_width, spacing_px)
    if not line_word_indices:
        return

    # Inter-word gap under the spaced advance model: a space character plus
    # the tracking on BOTH of its sides (word|sp|space|sp|word), so word-drawn
    # lines occupy exactly the width _measure_line reports for the full line.
    space_w = font.measureText(" ") + 2.0 * spacing_px
    word_widths = [_measure_line(font, w, spacing_px) for w in words]
    line_texts = [" ".join(words[i] for i in line) for line in line_word_indices]

    cx, cy = _resolve_anchor(overlay, render_canvas)
    anchor = _resolve_text_anchor(overlay)
    block = _measure_block(
        font,
        line_texts,
        line_spacing=resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=spacing_px,
    )
    block_top = _vertical_block_top(_resolve_vertical_anchor(overlay), cy, block["block_h"])
    first_baseline = block_top + block["ascent_offset"]

    primary_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"))
    highlight_color = _skia_color_from_hex(overlay.get("highlight_color") or "#FFD24A")
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    glow_kwargs = _resolve_glow_kwargs(overlay)

    for line_idx, line in enumerate(line_word_indices):
        line_w = sum(word_widths[i] for i in line) + space_w * max(0, len(line) - 1)
        x = _anchored_left_x(anchor, cx, line_w)
        baseline_y = first_baseline + line_idx * block["line_step"]
        for i in line:
            already_sung = starts[i] <= t_local
            color = highlight_color if already_sung else primary_color
            draw_kwargs: dict[str, Any] = {"shadow_alpha": 160}
            if spacing_px != 0.0:
                draw_kwargs["letter_spacing_px"] = spacing_px
            draw_kwargs.update(glow_kwargs)
            _draw_line_with_layers(
                canvas,
                words[i],
                x,
                baseline_y,
                font,
                color,
                stroke_px,
                **draw_kwargs,
            )
            x += word_widths[i] + space_w


def _lyric_line_alpha(overlay: dict, t_local: float, duration_s: float) -> float:
    """Per-frame alpha for `effect='lyric-line'`, mirroring the libass
    `_emit_lyric_line_alpha_tags` semantics in text_overlay.py:

      alpha rises 0 → 1 during [0, fade_in_ms]                          (fade-in)
      alpha holds at 1 during [fade_in_ms, duration_ms - fade_out_ms]   (hold)
      alpha falls 1 → 0 during [duration_ms - fade_out_ms, duration_ms] (fade-out)

    Curve matches libass `\\t(t1, t2, accel, tag)` exactly:
      fade-in  uses accel=0.5 → alpha = sqrt(progress)        (snaps in early)
      fade-out uses accel=2.0 → alpha = 1 - progress**2       (lingers, then drops)

    Clamp semantics also match libass: fade_out is shrunk if fade_in already
    consumed the duration, so very short overlays never wrap around.
    """
    if duration_s <= 0:
        return 1.0
    duration_ms = max(0, int(round(duration_s * 1000.0)))
    # Defaults match libass `_overlay_int(overlay, "fade_*_ms", 150/250)` in
    # text_overlay.py:476-477. Renderer-parity: identical overlay dict must
    # render identically across renderers, even when fade keys are absent.
    raw_in = overlay.get("fade_in_ms")
    raw_out = overlay.get("fade_out_ms")
    fade_in_ms = max(0, int(raw_in if raw_in is not None else 150))
    fade_out_ms = max(0, int(raw_out if raw_out is not None else 250))
    fade_in = min(fade_in_ms, duration_ms)
    fade_out = min(fade_out_ms, max(0, duration_ms - fade_in))
    if fade_in == 0 and fade_out == 0:
        return 1.0

    t_ms = max(0.0, min(t_local, duration_s)) * 1000.0
    if fade_in > 0 and t_ms < fade_in:
        # libass \t(0, fade_in, 0.5, \alpha&H00&) → alpha = progress**0.5
        progress = t_ms / float(fade_in)
        return progress**0.5
    fade_out_start_ms = duration_ms - fade_out
    if fade_out > 0 and t_ms >= fade_out_start_ms:
        progress = (t_ms - fade_out_start_ms) / float(fade_out)
        if overlay.get("fade_out_curve") == "sqrt":
            # Mirror of the sqrt fade-in: libass \t(start, end, 0.5, \alpha&HFF&)
            # → alpha = 1 − progress**0.5. Set by the dynamic crossfade
            # post-pass in lyric_injector.py for inter-line crossfades only;
            # over a matched duration window paired with the sqrt fade-in on
            # the incoming line, α_outgoing + α_incoming = 1 at every t — no
            # readable stacked text. See plan §2 / Mirea fix.
            return max(0.0, 1.0 - progress**0.5)
        # Default lingering fade-out (solo lines, sparse pairs, hard-cut /
        # solo-demoted / override / kill-switch-off — every non-crossfade
        # case): libass \t(fade_out_start, duration_ms, 2.0, \alpha&HFF&) →
        # alpha = 1 − progress².
        return max(0.0, 1.0 - progress**2)
    return 1.0


def _draw_with_animation(
    canvas: skia.Canvas,
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    effect: str | None = None,
    render_canvas: Canvas = PORTRAIT,
) -> None:
    """Apply animation transforms (scale, alpha, position, reveal length) for
    `effect` at time `t_local`, then draw the (possibly transformed) text.
    """
    effect = effect or overlay.get("effect", "none")
    text = _overlay_text(overlay)
    reveal_text = _normalize_reveal_text(text) if effect in ("typewriter", "stream-in") else text

    scale = 1.0
    alpha = 1.0
    y_translate = 0.0
    x_translate = 0.0
    scale_origin_x = 0.0
    scale_origin_y = 0.0
    visible_text = text
    show_cursor = False

    if effect == "scale-up":
        if duration_s > 0.6:
            progress = min(1.0, t_local / 0.6)
        else:
            progress = min(1.0, t_local / max(duration_s, 0.01))
        scale = 0.6 + 0.4 * _ease_out_cubic(progress)
    elif effect == "fade-in":
        if duration_s > 0.4:
            progress = min(1.0, t_local / 0.4)
        else:
            progress = min(1.0, t_local / max(duration_s, 0.01))
        alpha = _ease_out_cubic(progress)
    elif effect == "typewriter":
        raw_schedule = overlay.get("reveal_schedule_s")
        if isinstance(raw_schedule, list) and raw_schedule:
            start_s = _finite_float(overlay.get("start_s"), 0.0)
            schedule = sorted(
                max(0.0, _finite_float(at_s, start_s) - start_s) for at_s in raw_schedule
            )
            revealed_steps = max(1, sum(at_s <= t_local for at_s in schedule))
            visible_chars = max(
                1,
                math.ceil(len(reveal_text) * revealed_steps / max(1, len(schedule))),
            )
        else:
            chars_per_s = 12.0
            visible_chars = max(1, int(t_local * chars_per_s) + 1)
        visible_text = reveal_text[:visible_chars]
    elif effect == "stream-in":
        # "How an AI returns an answer" — reveal WORD by word (not char) with a
        # blinking cursor while streaming. Pairs with text_anchor="left" so the
        # answer grows rightward from a fixed margin like a chat response.
        words = list(re.finditer(r"\S+", reveal_text))
        words_per_s = 6.0
        n = max(1, int(t_local * words_per_s) + 1)
        last_visible_word = words[min(n, len(words)) - 1] if words else None
        visible_text = reveal_text[: last_visible_word.end()] if last_visible_word else ""
        if n < len(words) and int(t_local * 2) % 2 == 0:
            show_cursor = True  # blink cursor at ~2 Hz
    elif effect in ("slide-up", "slide-down"):
        animate_for = min(0.35, duration_s * 0.5)
        progress = min(1.0, t_local / animate_for) if animate_for > 0 else 1.0
        eased = _ease_out_cubic(progress)
        direction = -1.0 if effect == "slide-up" else 1.0
        y_translate = direction * 220.0 * (1.0 - eased)
    elif effect == "pop-in":
        scale = _pop_in_scale_at(t_local, duration_s)
    elif effect == "bounce":
        animate_for = min(0.5, duration_s * 0.8)
        if t_local < animate_for:
            p = t_local / animate_for
            if p < 0.36:
                scale = 1.0 + 0.25 * (p / 0.36)
            elif p < 0.72:
                scale = 1.25 - (1.25 - 0.90) * ((p - 0.36) / 0.36)
            else:
                scale = 0.90 + 0.10 * ((p - 0.72) / 0.28)
        else:
            scale = 1.0
    elif effect == "lyric-line":
        alpha = _lyric_line_alpha(overlay, t_local, duration_s)
    elif effect not in ("none", "static"):
        # Unknown effect → render as static. This is intentionally lenient:
        # in production we'd rather render the text at its base style than
        # raise and lose the overlay entirely. A logged warning gives us
        # visibility into schema drift.
        log.warning("skia_unknown_effect_static_fallback", effect=effect)

    # Sequence overlays (role="generative_sequence") multiply a fade-out tail
    # on top of the effect's own alpha: fade-in head → full-opacity hold →
    # fade-out over the final fade_out_ms (lyric-path field names + curves).
    if _is_sequence_overlay(overlay):
        alpha *= _sequence_fade_out_alpha(overlay, t_local, duration_s)

    _draw_centered_text(
        canvas,
        visible_text,
        overlay,
        render_canvas=render_canvas,
        alpha=alpha,
        scale=scale,
        x_translate=x_translate,
        y_translate=y_translate,
        scale_origin_x=scale_origin_x,
        scale_origin_y=scale_origin_y,
        layout_text=reveal_text if effect in ("typewriter", "stream-in") else None,
        show_cursor=show_cursor,
    )


# -- Top-level frame dispatch ------------------------------------------------


_ANIMATED_EFFECTS_SKIA = {
    "font-cycle",
    "scale-up",
    "fade-in",
    "typewriter",
    "stream-in",
    "slide-up",
    "slide-down",
    "pop-in",
    "bounce",
    "staggered-slice",
    "karaoke-line",
    # lyric-line must be animated to honor fade_in_ms / fade_out_ms. Without
    # this, two consecutive lyric overlays whose [start_s, end_s] windows
    # overlap (the designed crossfade window — see _inject_line's
    # dynamic_max_overlap in lyric_injector.py) render as two stacked
    # full-opacity PNGs at the same y_frac instead of crossfading. libass
    # honors the same fade tags via _emit_lyric_line_alpha_tags in
    # text_overlay.py — the renderer-parity invariant requires Skia match.
    "lyric-line",
}


def _is_animated(overlay: dict) -> bool:
    effect = overlay.get("effect", "none")
    if _theme_transition_type(overlay) == "giant-title-wipe":
        return True
    # pop-in with pop_animated_suffix is animated even if base effect is "pop-in";
    # plain pop-in without suffix is also animated.
    if effect in _ANIMATED_EFFECTS_SKIA:
        return True
    # Static sequence overlays that carry a fade-out tail need per-frame alpha
    # (the single-PNG `-loop 1` static path renders at full opacity throughout).
    # Without fade_out_ms they stay on the static path, which already holds the
    # full window.
    return _is_sequence_overlay(overlay) and _sequence_fade_out_ms(overlay) > 0


def _draw_overlay_on_canvas(
    canvas: skia.Canvas,
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> None:
    """Draw one overlay's frame-at-time-t onto an existing canvas.

    Shared by `_draw_frame` (one overlay per surface — the per-overlay PNG
    sequence path) and `_render_sequence_composite` (every visible sequence
    overlay stacked onto ONE surface). This is the single dispatch point for
    effect drawing — the composite path must never reimplement glyph logic.
    """
    effect = overlay.get("effect", "none")

    with _theme_transition_canvas(
        canvas, overlay, t_local, duration_s, render_canvas=render_canvas
    ):
        if effect == "player-card":
            # Out of scope for Skia migration — classic templates only.
            raise NotImplementedError(
                "Skia renderer does not support effect='player-card'; this overlay "
                "must route through Pillow + libass. Classic templates only."
            )

        if effect == "font-cycle":
            font_name = _select_cycle_font(overlay, t_local, duration_s)
            font = skia.Font(_typeface_for_overlay(overlay, font_name_override=font_name))
            font.setSize(_resolve_font_size_px(overlay))
            font.setSubpixel(True)
            _draw_centered_text(
                canvas,
                _overlay_text(overlay),
                overlay,
                render_canvas=render_canvas,
                font_override=font,
            )
        elif effect == "karaoke-line":
            _draw_karaoke_line(canvas, overlay, t_local, duration_s, render_canvas=render_canvas)
        elif effect == "pop-in" and overlay.get("pop_animated_suffix"):
            _draw_pop_in_with_suffix(
                canvas, overlay, t_local, duration_s, render_canvas=render_canvas
            )
        elif effect == "staggered-slice":
            _draw_staggered_slice(canvas, overlay, t_local, duration_s, render_canvas=render_canvas)
        elif _is_animated(overlay):
            # `lyric-line` is in this branch because its fade_in_ms / fade_out_ms
            # are honored per-frame in `_draw_with_animation`. `_overlay_text`
            # still resolves `display_text` (the Layer 2 finalizer's truncated
            # partial line) when present — the alpha multiplies on top of that.
            _draw_with_animation(canvas, overlay, t_local, duration_s, render_canvas=render_canvas)
        else:
            # Static effects render at full opacity throughout [start_s, end_s].
            _draw_centered_text(
                canvas, _overlay_text(overlay), overlay, render_canvas=render_canvas
            )


def _draw_frame(
    overlay: dict,
    t_local: float,
    duration_s: float,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> skia.Image:
    """Render one frame of `overlay` at time `t_local`. Returns a Skia Image
    that the caller writes to disk via Pillow."""
    surface = skia.Surfaces.MakeRasterN32Premul(render_canvas.width, render_canvas.height)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    _draw_overlay_on_canvas(canvas, overlay, t_local, duration_s, render_canvas=render_canvas)
    return surface.makeImageSnapshot()


# -- PNG encoding via Pillow (faster than Skia's built-in encoder) -----------


def _write_png_pillow(img: skia.Image, out_path: str) -> None:
    """Encode a Skia Image to PNG via Pillow.

    Skia's built-in encoder is ~44ms/frame at 1080x1920 RGBA. Pillow's drops
    to ~8ms — Pillow's PNG encoder releases the GIL and uses zlib-level 3 by
    default. For an N-overlay job this saves seconds.

    Alpha mode: the source surface is `MakeRasterN32Premul` (premultiplied
    alpha) because that's the fastest rasterization mode in Skia. Pillow's
    `frombytes("RGBA", ...)` treats bytes as STRAIGHT alpha, so a raw
    `img.tobytes()` would feed premultiplied pixels into a straight-alpha
    consumer and darken every anti-aliased glyph edge. Solve by requesting
    `kUnpremul_AlphaType` on readPixels — Skia unpremuls during the copy
    in one pass, with no impact on render-time perf.
    """
    info = skia.ImageInfo.Make(
        img.width(),
        img.height(),
        skia.ColorType.kRGBA_8888_ColorType,
        skia.AlphaType.kUnpremul_AlphaType,
    )
    row_bytes = img.width() * 4
    buf = bytearray(row_bytes * img.height())
    if not img.readPixels(info, buf, row_bytes, 0, 0):
        # Fallback: best-effort raw bytes if readPixels fails (older skia or
        # surface mismatch). Worst case is the pre-fix edge darkening.
        log.warning("skia_readpixels_failed_falling_back_to_tobytes")
        buf = img.tobytes()
    pil = Image.frombytes("RGBA", (img.width(), img.height()), bytes(buf))
    pil.save(out_path, "PNG", compress_level=3)


# -- Subject-matte occlusion (behind_subject) ---------------------------------
#
# `behind_subject: True` on a burn dict multiplies the text layer's per-frame
# alpha by (1 - subject_mask) so a person composited over the burned video
# appears IN FRONT of the text. The matte provider (app.pipeline.subject_matte,
# a concurrently-developed sibling module) is consumed structurally via
# `SubjectMatteProvider` — this module never imports it, so Lane B has zero
# build-order dependency on Lane A.


class SubjectMatteProvider(Protocol):
    def mask_at(self, t_abs: float) -> np.ndarray | None: ...


def _apply_subject_mask(rgba: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Zero out text alpha where `mask` says the subject sits on top.

    `rgba` must be STRAIGHT (non-premultiplied) alpha uint8 (H, W, 4) — the
    format `_skia_image_to_rgba_array` produces (see `_write_png_pillow` for
    why: Skia's N32Premul surface is unpremultiplied during the readPixels
    copy). Straight alpha means only the alpha channel needs scaling; RGB is
    untouched. Premultiplied output would additionally require scaling RGB by
    the same factor, or the occluded edge shows bright fringing.

    `mask` is float32 in [0, 1], shape (H, W). A shape mismatch fails open
    (returns `rgba` unchanged) rather than raising mid-render.
    """
    if mask.shape != rgba.shape[:2]:
        log.warning(
            "text_behind_subject_mask_shape_mismatch",
            mask_shape=tuple(mask.shape),
            rgba_shape=tuple(rgba.shape[:2]),
        )
        return rgba
    factor = 1.0 - np.clip(mask, 0.0, 1.0).astype(np.float32)
    out = rgba.copy()
    out[..., 3] = np.clip(out[..., 3].astype(np.float32) * factor, 0, 255).astype(np.uint8)
    return out


def _skia_image_to_rgba_array(img: skia.Image) -> np.ndarray:
    """Read a Skia Image into a straight-alpha (H, W, 4) uint8 numpy array.

    Same `kUnpremul_AlphaType` readPixels copy as `_write_png_pillow`, shared
    here so the behind_subject frame path can mask the alpha channel before
    PNG encode instead of after."""
    info = skia.ImageInfo.Make(
        img.width(),
        img.height(),
        skia.ColorType.kRGBA_8888_ColorType,
        skia.AlphaType.kUnpremul_AlphaType,
    )
    row_bytes = img.width() * 4
    buf = bytearray(row_bytes * img.height())
    if not img.readPixels(info, buf, row_bytes, 0, 0):
        log.warning("skia_readpixels_failed_falling_back_to_tobytes")
        buf = img.tobytes()
    return np.frombuffer(bytes(buf), dtype=np.uint8).reshape(img.height(), img.width(), 4)


def _write_rgba_array_png(arr: np.ndarray, out_path: str) -> None:
    """Encode a straight-alpha (H, W, 4) uint8 array to PNG via Pillow.

    Used by the behind_subject frame path once `_apply_subject_mask` has
    already scaled alpha on the numpy array — mirrors `_write_png_pillow`'s
    compress_level so both paths produce the same PNG weight."""
    Image.fromarray(arr, "RGBA").save(out_path, "PNG", compress_level=3)


# -- Public API: render overlays to FFmpeg-ready PNG sequences ---------------


def _sequence_hold_plan(
    overlay: dict, n_render: int, frame_dur: float, duration_s: float
) -> tuple[list[int], int, list[int]] | None:
    """Frame-economy plan for role="generative_sequence" overlays.

    A sequence scene holds on screen for its whole transcript window (often
    5-15s). Rendering a unique PNG per frame would mean hundreds of identical
    "settled" frames between the fade-in head and the fade-out tail. This
    generalizes the existing +1-hold-frame trick (the seam fix below in
    `_generate_overlay_sequence`, which renders one settled extra frame past
    the logical end) to the middle of the window: Skia renders real frames
    ONLY where pixels change, and the first settled frame is hard-linked into
    every hold slot so FFmpeg's image2 demuxer still sees a dense,
    uniformly-timed sequence:

        frame idx: 0 .. s-1 | s | s+1 ........ t-1 | t ........ n-1
                   fade-in    ^   hold (hard links   fade-out tail
                   head       |   to frame s, no     (fade_out_ms,
                   (0.4s      |   Skia render, no    alpha 1 → 0)
                   ease)      settled frame          PNG encode)

    Returns (render_indices, settled_idx, hold_indices), or None when the
    overlay is not a sequence overlay / has no holdable middle — in which case
    every frame renders, the pre-existing behavior. Hold slots are hard links
    (copy fallback): same bytes on disk once, microseconds per extra frame.
    """
    if not _is_sequence_overlay(overlay):
        return None
    effect = overlay.get("effect", "none")
    settle_s = min(_FADE_IN_SETTLE_S, duration_s) if effect == "fade-in" else 0.0
    fade_out_s = min(_sequence_fade_out_ms(overlay) / 1000.0, duration_s)
    fade_out_start_s = duration_s - fade_out_s

    render_indices: list[int] = []
    hold_indices: list[int] = []
    settled_idx = -1
    for i in range(n_render):
        t = i * frame_dur
        if t < settle_s or t >= fade_out_start_s:
            render_indices.append(i)
        elif settled_idx < 0:
            settled_idx = i
            render_indices.append(i)
        else:
            hold_indices.append(i)
    if settled_idx < 0 or not hold_indices:
        # Window too short for a holdable middle — render everything.
        return None
    return render_indices, settled_idx, hold_indices


def _staggered_slice_hold_plan(
    overlay: dict, n_render: int, frame_dur: float, duration_s: float
) -> tuple[list[int], int, list[int]] | None:
    if overlay.get("effect") != "staggered-slice" or n_render <= 1:
        return None
    if _theme_transition_type(overlay) is not None:
        return None
    settle_s = min(duration_s, _staggered_slice_settle_s(_overlay_text(overlay)))
    settled_idx = min(n_render - 1, int(math.ceil(settle_s / frame_dur - 1e-9)))
    if settled_idx >= n_render - 1:
        return None
    render_indices = list(range(settled_idx + 1))
    hold_indices = list(range(settled_idx + 1, n_render))
    return render_indices, settled_idx, hold_indices


def _generate_overlay_sequence(
    overlay: dict,
    work_dir: str,
    idx: int,
    *,
    matte: SubjectMatteProvider | None = None,
    render_canvas: Canvas = PORTRAIT,
) -> dict[str, Any] | None:
    """Render every frame for one overlay, return a single config describing
    the sequence (or single PNG for static effects)."""
    start_s = float(overlay.get("start_s", 0.0))
    end_s = float(overlay.get("end_s", 0.0))
    duration_s = max(0.0, end_s - start_s)
    if duration_s <= 0:
        return None

    pattern_prefix = f"skia_overlay_{idx:03d}_f"
    _record_font_resolved_event(overlay, idx)

    wants_behind = bool(overlay.get("behind_subject"))
    behind = wants_behind and matte is not None
    if wants_behind and matte is None:
        log.warning(
            "text_behind_subject_no_matte_fallback",
            role=overlay.get("role"),
            text=_overlay_text(overlay)[:40],
        )

    if not _is_animated(overlay) and not behind:
        out_path = os.path.join(work_dir, f"{pattern_prefix}0000.png")
        img = _draw_frame(overlay, 0.0, duration_s, render_canvas=render_canvas)
        _write_png_pillow(img, out_path)
        return _with_masonry_layer_origin(
            {
                "pattern": os.path.join(work_dir, f"{pattern_prefix}%04d.png"),
                "first_frame": out_path,
                "n_frames": 1,
                "fps": FPS,
                "start_s": start_s,
                "end_s": end_s,
                "is_animated": False,
            },
            overlay,
        )

    # MAX_OVERLAY_FRAMES protects effects whose animation can be visually
    # unique on every frame (font-cycle, plain pop-in) from runaway PNG counts.
    # Lyric-timed effects (`lyric-line`, `karaoke-line`, suffix pop-in reveal
    # stages) settle visually but must stay present until their audio boundary;
    # applying the ~4s cap there chops late words/tails before the next stage.
    # They instead use a generous sanity ceiling (LONG_RUNNING_TEXT_FRAME_CEILING,
    # or BEHIND_SUBJECT_FRAME_CEILING for behind_subject overlays — see that
    # constant's comment) so a malformed transcript with `end_s = 240.0`
    # cannot blow scratch disk on the encode worker:
    # 30s × 30fps = 900 frames × ~1MB PNG ≈ 1GB worst case, vs 7200 frames ×
    # 1MB ≈ 7GB unbounded.
    effect = overlay.get("effect", "none")
    wanted = max(1, int(round(duration_s * FPS)))
    uses_long_ceiling = _uses_long_running_frame_ceiling(overlay)
    # behind_subject gets the larger BEHIND_SUBJECT_FRAME_CEILING (hold-to-EOF
    # windows must outlive the video); every other long-running effect keeps
    # the tighter 30s LONG_RUNNING_TEXT_FRAME_CEILING. Single source of truth
    # so the clamp below and the +1 seam-frame clamp further down can't drift.
    long_ceiling = BEHIND_SUBJECT_FRAME_CEILING if wants_behind else LONG_RUNNING_TEXT_FRAME_CEILING
    if uses_long_ceiling:
        if wanted > long_ceiling:
            log.warning(
                "skia_long_running_text_duration_clamped",
                effect=effect,
                duration_s=duration_s,
                wanted_frames=wanted,
                clamped_to=long_ceiling,
            )
        n_frames = min(long_ceiling, wanted)
    else:
        n_frames = min(MAX_OVERLAY_FRAMES, wanted)
    frame_dur = 1.0 / FPS
    # Render ONE extra "hold" frame past the logical end. FFmpeg's image2
    # secondary stream EOFs at its last frame's PTS (not PTS + frame_dur), so
    # without this the overlay disappears one frame (~33ms) before `end_s` —
    # and because cumulative reveal stages butt edge-to-edge, that hole lands
    # exactly at every word boundary, blanking the whole line for a frame
    # before the next word's overlay opens (the prod 89cde014 flicker). The
    # extra frame is the settled final state (animation progress clamps to 1.0
    # at t_local >= duration), so it just holds the line through the seam; the
    # `between(t, start, end)` enable still gates the overlay off at `end`.
    n_render = (
        min(long_ceiling, n_frames + 1)
        if uses_long_ceiling
        else min(MAX_OVERLAY_FRAMES, n_frames + 1)
    )

    # behind_subject: text is occluded by the moving subject, so the settled
    # frame differs every time the mask differs — cache the drawn RGBA once
    # for non-animated overlays (the glyphs never change) and re-mask it per
    # frame; animated overlays still redraw the glyphs per frame and get
    # masked on top, same as the static case.
    static_behind_arr = (
        _skia_image_to_rgba_array(
            _draw_frame(overlay, 0.0, duration_s, render_canvas=render_canvas)
        )
        if behind and not _is_animated(overlay)
        else None
    )

    def _render_one(i: int) -> None:
        t_local = i * frame_dur
        out_path = os.path.join(work_dir, f"{pattern_prefix}{i:04d}.png")
        if behind:
            arr = (
                static_behind_arr
                if static_behind_arr is not None
                else _skia_image_to_rgba_array(
                    _draw_frame(overlay, t_local, duration_s, render_canvas=render_canvas)
                )
            )
            mask = matte.mask_at(start_s + t_local)
            if mask is not None:
                arr = _apply_subject_mask(arr, mask)
            _write_rgba_array_png(arr, out_path)
        else:
            img = _draw_frame(overlay, t_local, duration_s, render_canvas=render_canvas)
            _write_png_pillow(img, out_path)

    # Sequence overlays render only their fade-in head + fade-out tail; the
    # settled middle is hard-linked from one frame (see _sequence_hold_plan).
    # Staggered-slice uses the same economy after its deterministic entrance
    # settles, so a 4s editor window does not encode ~2s of duplicate PNGs.
    # behind_subject disables the hold economy — the mask can change on every
    # frame even when the glyphs don't, so a hard-linked frame would freeze
    # the occlusion at whatever the settled frame's mask happened to be.
    hold_plan = None
    if not behind:
        hold_plan = _sequence_hold_plan(overlay, n_render, frame_dur, duration_s)
        if hold_plan is None:
            hold_plan = _staggered_slice_hold_plan(overlay, n_render, frame_dur, duration_s)
    if hold_plan is None:
        render_indices: list[int] = list(range(n_render))
        settled_idx = -1
        hold_indices: list[int] = []
    else:
        render_indices, settled_idx, hold_indices = hold_plan

    with ThreadPoolExecutor(max_workers=_ENCODE_WORKERS) as pool:
        list(pool.map(_render_one, render_indices))

    if hold_indices:
        settled_path = os.path.join(work_dir, f"{pattern_prefix}{settled_idx:04d}.png")
        for i in hold_indices:
            frame_path = os.path.join(work_dir, f"{pattern_prefix}{i:04d}.png")
            _link_or_copy(settled_path, frame_path)
        log.info(
            "skia_sequence_hold_frames_linked",
            effect=effect,
            n_render=n_render,
            rendered=len(render_indices),
            linked=len(hold_indices),
        )

    return _with_masonry_layer_origin(
        {
            "pattern": os.path.join(work_dir, f"{pattern_prefix}%04d.png"),
            "first_frame": os.path.join(work_dir, f"{pattern_prefix}0000.png"),
            "n_frames": n_render,
            "fps": FPS,
            "start_s": start_s,
            "end_s": end_s,
            "is_animated": True,
        },
        overlay,
    )


def _masonry_layer_origin(overlay: dict) -> float | None:
    origin = overlay.get("masonry_layer_origin_x_px")
    if isinstance(origin, bool) or not isinstance(origin, (int, float)):
        return None
    if math.isfinite(float(origin)) and float(origin) > 0:
        return float(origin)
    return None


def _with_masonry_layer_origin(
    sequence: dict[str, Any],
    overlay: dict,
) -> dict[str, Any]:
    """Carry compositor-only board origin metadata alongside the PNG sequence."""
    origin = _masonry_layer_origin(overlay)
    if origin is not None:
        sequence["layer_origin_x_px"] = origin
    return sequence


def _validate_and_clamp(overlay: dict, slot_duration_s: float) -> dict | None:
    """Run `_validate_overlay` and fold the result back into a copied dict.

    Returns None when the overlay should be skipped (empty text without
    spans). Spans-only overlays keep their original timing fields —
    `_validate_overlay` returns (None, 0, 0, "") for them, and the Skia path
    validates just timing downstream.
    """
    validated_text, start_s, end_s, _position = _validate_overlay(overlay, slot_duration_s)
    if validated_text is None and not overlay.get("spans"):
        return None
    # Apply the validated timing back to the overlay (start clamp etc.)
    # but only if we got a non-None result.
    clamped = dict(overlay)
    if validated_text is not None:
        # `_validate_overlay` is shared with libass and encodes explicit line
        # breaks as ASS's literal `\N`. Skia needs real newlines for wrapping
        # and logical-line animation stages.
        clamped["text"] = validated_text.replace("\\N", "\n")
        clamped["start_s"] = start_s
        clamped["end_s"] = end_s
    return clamped


def _render_overlay_sequences(
    overlays: list[dict],
    slot_duration_s: float,
    work_dir: str,
    *,
    matte: SubjectMatteProvider | None = None,
    render_canvas: Canvas = PORTRAIT,
) -> list[dict[str, Any]]:
    """Validate every overlay against slot_duration_s, then render PNG
    sequences. Returns list of overlay-sequence configs ready for FFmpeg."""
    out: list[dict[str, Any]] = []
    for i, overlay in enumerate(overlays):
        clamped = _validate_and_clamp(overlay, slot_duration_s)
        if clamped is None:
            continue
        seq = _generate_overlay_sequence(
            clamped, work_dir, i, matte=matte, render_canvas=render_canvas
        )
        if seq is not None:
            out.append(seq)
    return out


# -- Sequence composite stream ------------------------------------------------
#
# Editorial sequences (role="generative_sequence") arrive as MANY small blocks
# — a 60s edit can carry 80+. The per-overlay path turns each into its own
# FFmpeg image2 input + overlay filter stage, and the chained filtergraph
# pushes every output frame through every stage: burn cost scales ~linearly
# with input count (~6.5s/input on a 60s canvas), so 80 blocks ≈ 520s — past
# the 300s budget and flirting with the subprocess timeout in
# `_ffmpeg_burn_pngs`. Skia rendering is the cheap part (~1s), so the fix is
# to pre-composite ALL sequence blocks into ONE full-canvas RGBA PNG sequence
# spanning their union window: one input, one overlay stage, per-frame
# visibility carried by the PNGs themselves. Frame economy still holds — a
# composite frame only changes when a block pops in, a fade ramp is active,
# or a block ends; everything between is a hard link to the previous unique
# frame (same mechanics as `_sequence_hold_plan`).


def _sequence_head_settle_s(overlay: dict) -> float | None:
    """Seconds after `start_s` until the block's entrance animation settles.

    Mirrors `_sequence_hold_plan`'s head boundary: fade-in settles at
    `_FADE_IN_SETTLE_S`; static/none blocks pop fully settled. Returns None
    for any other effect — the caller must treat every in-window frame as
    changing (correct, just not frame-economical) because we don't model that
    effect's animation envelope here.
    """
    effect = overlay.get("effect", "none")
    if effect == "fade-in":
        return _FADE_IN_SETTLE_S
    if effect in ("static", "none"):
        return 0.0
    return None


def _sequence_block_changing_at(overlay: dict, t_local: float, duration_s: float) -> bool:
    """True when the block's pixels can differ from the adjacent frame's:
    inside the fade-in head, inside the fade-out tail, or (unknown effect)
    anywhere in the window. Boundary semantics mirror `_sequence_hold_plan`
    (`t < settle_s or t >= fade_out_start_s` renders)."""
    settle_s = _sequence_head_settle_s(overlay)
    if settle_s is None:
        return True
    if t_local < min(settle_s, duration_s):
        return True
    fade_out_s = min(_sequence_fade_out_ms(overlay) / 1000.0, duration_s)
    return fade_out_s > 0 and t_local >= duration_s - fade_out_s


def _render_sequence_composite(
    overlays: list[dict],
    slot_duration_s: float,
    work_dir: str,
    *,
    render_canvas: Canvas = PORTRAIT,
) -> dict[str, Any] | None:
    """Render every sequence overlay into ONE full-canvas PNG sequence.

    The stream spans [min(start_s), max(end_s)] of the validated blocks at
    `FPS`; each frame draws EVERY block visible at that timestamp (inclusive
    bounds — matching the per-overlay `between(t,start,end)` enable) via
    `_draw_overlay_on_canvas`, in input order (later blocks on top). Frames
    where no block is visible are fully transparent but still exist as hard
    links to one blank frame, keeping the image2 sequence contiguous. The
    final +1 frame past `end_s` is the same seam fix as the per-overlay path
    (image2 EOFs at its last frame's PTS).

    Returns a sequence config consumable by `_ffmpeg_burn_pngs` as a single
    animated input, or None when fewer than 2 blocks survive validation —
    the caller falls back to the per-overlay path (no regression for the
    trivial cases).
    """
    blocks: list[tuple[dict, float, float]] = []
    for i, overlay in enumerate(overlays):
        clamped = _validate_and_clamp(overlay, slot_duration_s)
        if clamped is None:
            continue
        start_s = float(clamped.get("start_s", 0.0))
        end_s = float(clamped.get("end_s", 0.0))
        if end_s - start_s <= 0:
            continue
        _record_font_resolved_event(clamped, i)
        blocks.append((clamped, start_s, end_s))
    if len(blocks) < 2:
        return None

    t0 = min(s for _, s, _ in blocks)
    t1 = max(e for _, _, e in blocks)
    duration_s = t1 - t0
    frame_dur = 1.0 / FPS
    wanted = max(1, int(round(duration_s * FPS)))
    if wanted > SEQUENCE_COMPOSITE_FRAME_CEILING:
        log.warning(
            "skia_sequence_composite_duration_clamped",
            duration_s=duration_s,
            wanted_frames=wanted,
            clamped_to=SEQUENCE_COMPOSITE_FRAME_CEILING,
        )
    n_frames = min(SEQUENCE_COMPOSITE_FRAME_CEILING, wanted)
    # +1 seam hold frame, same reasoning as `_generate_overlay_sequence`.
    n_render = min(SEQUENCE_COMPOSITE_FRAME_CEILING, n_frames + 1)

    pattern_prefix = "skia_seq_composite_f"

    # Frame plan: a frame renders uniquely when any visible block is mid-
    # animation (pop/fade window); otherwise frames with the same visible-set
    # key hard-link to the first rendered frame with that key. The empty set
    # keys one shared blank frame for clear gaps.
    render_jobs: list[tuple[int, list[tuple[dict, float, float]]]] = []
    links: list[tuple[int, int]] = []
    first_for_key: dict[tuple, int] = {}
    for i in range(n_render):
        t = t0 + i * frame_dur
        draw_list: list[tuple[dict, float, float]] = []
        key_parts: list[int] = []
        changing = False
        for j, (block, start_s, end_s) in enumerate(blocks):
            if not (start_s <= t <= end_s):
                continue
            t_local = t - start_s
            block_dur = end_s - start_s
            draw_list.append((block, t_local, block_dur))
            key_parts.append(j)
            changing = changing or _sequence_block_changing_at(block, t_local, block_dur)
        if changing:
            render_jobs.append((i, draw_list))
            continue
        key = ("blank",) if not key_parts else ("hold", tuple(key_parts))
        src = first_for_key.get(key)
        if src is None:
            first_for_key[key] = i
            render_jobs.append((i, draw_list))
        else:
            links.append((src, i))

    def _render_one(job: tuple[int, list[tuple[dict, float, float]]]) -> None:
        i, draw_list = job
        surface = skia.Surfaces.MakeRasterN32Premul(render_canvas.width, render_canvas.height)
        canvas = surface.getCanvas()
        canvas.clear(skia.ColorTRANSPARENT)
        for block, t_local, block_dur in draw_list:
            _draw_overlay_on_canvas(canvas, block, t_local, block_dur, render_canvas=render_canvas)
        _write_png_pillow(
            surface.makeImageSnapshot(),
            os.path.join(work_dir, f"{pattern_prefix}{i:05d}.png"),
        )

    with ThreadPoolExecutor(max_workers=_ENCODE_WORKERS) as pool:
        list(pool.map(_render_one, render_jobs))

    for src, dst in links:
        src_path = os.path.join(work_dir, f"{pattern_prefix}{src:05d}.png")
        dst_path = os.path.join(work_dir, f"{pattern_prefix}{dst:05d}.png")
        _link_or_copy(src_path, dst_path)

    log.info(
        "skia_sequence_composite_frames_linked",
        blocks=len(blocks),
        n_frames=n_render,
        unique_frames=len(render_jobs),
        linked_frames=len(links),
        inputs_saved=len(blocks) - 1,
    )

    return {
        "pattern": os.path.join(work_dir, f"{pattern_prefix}%05d.png"),
        "first_frame": os.path.join(work_dir, f"{pattern_prefix}00000.png"),
        "n_frames": n_render,
        "fps": FPS,
        "start_s": t0,
        "end_s": t1,
        "is_animated": True,
    }


# -- FFmpeg compositing: build the burn command and execute ------------------


def _ffmpeg_burn_pngs(
    input_video: str,
    sequences: list[dict[str, Any]],
    output_path: str,
    *,
    canvas: Canvas = PORTRAIT,
) -> None:
    """Build and run the FFmpeg command that overlays PNG sequences onto
    `input_video`. Uses image2 demuxer for animated sequences (one input per
    sequence config — usually one overlay, but the generative_sequence
    composite packs every sequence block into a single config) + setpts shift
    to align each sequence with its absolute start_s.
    """
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    cmd = ["ffmpeg", "-y", "-i", input_video]

    for seq in sequences:
        if seq["is_animated"]:
            cmd.extend(
                [
                    "-framerate",
                    str(seq["fps"]),
                    "-start_number",
                    "0",
                    "-i",
                    seq["pattern"],
                ]
            )
        else:
            cmd.extend(["-loop", "1", "-i", seq["first_frame"]])

    fc_parts = ["[0:v]null[base]"]
    prev = "base"
    for i, seq in enumerate(sequences):
        src = f"{i + 1}:v"
        start = seq["start_s"]
        end = seq["end_s"]
        if seq["is_animated"]:
            shifted = f"shift{i}"
            fc_parts.append(f"[{src}]setpts=PTS+{start:.4f}/TB[{shifted}]")
            ov_src = shifted
        else:
            ov_src = src
        out = f"v{i}"
        fc_parts.append(
            f"[{prev}][{ov_src}]overlay=0:0"
            f":enable='between(t,{start:.4f},{end:.4f})'"
            f":eof_action=pass"
            f"[{out}]"
        )
        prev = out

    cmd.extend(
        [
            "-filter_complex",
            ";".join(fc_parts),
            "-map",
            f"[{prev}]",
            "-map",
            "0:a?",
            *_encoding_args(output_path, preset="fast", canvas=canvas),
        ]
    )

    burn_t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    burn_elapsed_ms = int((time.monotonic() - burn_t0) * 1000)
    if result.returncode != 0:
        log.warning(
            "skia_burn_text_failed",
            rc=result.returncode,
            elapsed_ms=burn_elapsed_ms,
            stderr=result.stderr.decode(errors="replace")[-500:],
        )
        # Mirror the Pillow burn's failure handling: copy through so the rest
        # of the pipeline keeps going. A failed text burn shouldn't kill the
        # whole job.
        shutil.copy2(input_video, output_path)
    else:
        log.info(
            "skia_burn_text_done",
            elapsed_ms=burn_elapsed_ms,
            sequence_count=len(sequences),
            total_frames=sum(s["n_frames"] for s in sequences),
        )


def _strip_behind_subject_for_sequence_role(overlays: list[dict]) -> list[dict]:
    """behind_subject is not supported on role="generative_sequence" overlays
    in v1 — they always route through `_render_sequence_composite` once
    there are >= 2 of them, and that composite path has no matte hook.
    Stripping (rather than raising) matches this module's existing
    "unknown/unsupported field degrades gracefully" posture."""
    out: list[dict] = []
    for overlay in overlays:
        if overlay.get("role") == SEQUENCE_OVERLAY_ROLE and overlay.get("behind_subject"):
            log.warning(
                "text_behind_subject_unsupported_for_sequence_role",
                text=_overlay_text(overlay)[:40],
            )
            overlay = dict(overlay)
            overlay.pop("behind_subject", None)
        out.append(overlay)
    return out


def render_text_overlay_sequences(
    overlays: list[dict],
    tmpdir: str,
    *,
    work_dir_name: str = "skia_text_burn",
    matte: SubjectMatteProvider | None = None,
    canvas: Canvas = PORTRAIT,
) -> tuple[list[dict[str, Any]], str | None]:
    """Render overlay dicts into transparent full-frame PNG streams.

    Most callers should use ``burn_text_overlays_skia``. Masonry collage needs the
    PNG streams but applies its own board-motion overlay expression so the text
    rides with the wall instead of staying pinned to the viewport.

    ``matte`` is an optional `SubjectMatteProvider` (duck-typed, see that
    Protocol) consulted only for overlays carrying `behind_subject: True`.
    """
    if not overlays:
        return [], None

    overlays = _strip_behind_subject_for_sequence_role(overlays)

    # Resolve the timing window: production caller passes ABS_PASS_TIME_S /
    # ABS_PASS_SLOT_INDEX sentinels meaning "use the final-pass duration".
    # The overlays carry absolute timestamps already; we only need a permissive
    # validation window, so pass the max end_s.
    max_end = max((float(o.get("end_s", 0.0)) for o in overlays), default=0.0)
    slot_duration_s = max_end + 1.0
    work_dir = os.path.join(tmpdir, work_dir_name)
    os.makedirs(work_dir, exist_ok=True)

    render_t0 = time.monotonic()
    # Editorial sequence blocks (role="generative_sequence") composite into
    # ONE full-canvas PNG stream when there are >= 2 of them — FFmpeg burn
    # cost scales with input count, and a sequence edit can carry 80+ blocks.
    # Everything else (lyric, intro, legacy) keeps the per-overlay path
    # untouched. 0/1 sequence overlays also stay per-overlay (no regression
    # for the trivial cases). Masonry blocks with board-local layer origins
    # are composited only with blocks sharing the same origin because differently
    # originated full-frame layers cannot share one pre-composited stream.
    sequence_overlays = [o for o in overlays if o.get("role") == SEQUENCE_OVERLAY_ROLE]
    sequence_groups: list[tuple[float | None, list[dict]]] = []
    for overlay in sequence_overlays:
        origin = _masonry_layer_origin(overlay)
        if sequence_groups and sequence_groups[-1][0] == origin:
            sequence_groups[-1][1].append(overlay)
        else:
            sequence_groups.append((origin, [overlay]))

    sequence_layers: list[dict[str, Any]] = []
    composited_sequence_blocks = 0
    for group_index, (origin, group) in enumerate(sequence_groups):
        group_work_dir = work_dir
        if len(sequence_groups) > 1:
            group_work_dir = os.path.join(work_dir, f"sequence_group_{group_index:03d}")
            os.makedirs(group_work_dir, exist_ok=True)
        composite = (
            _render_sequence_composite(
                group,
                slot_duration_s,
                group_work_dir,
                **_render_canvas_kwargs(canvas),
            )
            if len(group) >= 2
            else None
        )
        if composite is None:
            sequence_layers.extend(
                _render_overlay_sequences(
                    group,
                    slot_duration_s,
                    group_work_dir,
                    matte=matte,
                    **_render_canvas_kwargs(canvas),
                )
            )
            continue
        if origin is not None:
            composite["layer_origin_x_px"] = origin
        sequence_layers.append(composite)
        composited_sequence_blocks += len(group)

    non_sequence = [o for o in overlays if o.get("role") != SEQUENCE_OVERLAY_ROLE]
    sequences = _render_overlay_sequences(
        non_sequence,
        slot_duration_s,
        work_dir,
        matte=matte,
        **_render_canvas_kwargs(canvas),
    )
    # Appended last → sequence text draws on top of non-sequence overlays. Contiguous
    # origin runs stay in authored order, preserving sequence z-order across layers.
    sequences.extend(sequence_layers)
    render_ms = int((time.monotonic() - render_t0) * 1000)

    if not sequences:
        return [], work_dir

    log.info(
        "skia_burn_text_overlays",
        overlay_count=len(overlays),
        sequence_count=len(sequences),
        composited_sequence_blocks=composited_sequence_blocks,
        total_frames=sum(s["n_frames"] for s in sequences),
        render_ms=render_ms,
    )
    return sequences, work_dir


# -- Public entry points: drop-in replacements for the Pillow burn functions -


def burn_text_overlays_skia(
    input_path: str,
    overlays: list[dict],
    output_path: str,
    tmpdir: str,
    *,
    matte: SubjectMatteProvider | None = None,
    canvas: Canvas = PORTRAIT,
) -> None:
    """Drop-in replacement for `text_overlay._burn_text_overlays` when the
    job is agentic or music.

    Caller is responsible for honoring `settings.text_renderer_skia_enabled`
    — this function does the Skia render unconditionally. The kill-switch
    check lives in the orchestrator (or in the wrapper that selects between
    Pillow and Skia at the burn site) so this module stays a pure renderer.

    ``matte`` is an optional `SubjectMatteProvider` — overlays carrying
    `behind_subject: True` occlude behind whatever the provider's `mask_at`
    reports at each frame's absolute timestamp. Omitting it degrades any
    `behind_subject` overlay to a normal render (logged, not an error).
    """
    if not overlays:
        shutil.copy2(input_path, output_path)
        return

    sequences, work_dir = render_text_overlay_sequences(
        overlays,
        tmpdir,
        matte=matte,
        **_canvas_kwargs(canvas),
    )
    try:
        if not sequences:
            shutil.copy2(input_path, output_path)
            return
        _ffmpeg_burn_pngs(input_path, sequences, output_path, **_canvas_kwargs(canvas))
    finally:
        # The full-frame 1080x1920 PNG sequences are only needed for the single
        # encode above. Free them the instant it returns so they don't pile up on
        # the worker's RAM-backed scratch across overlays and (serial) variants —
        # this is what was filling /tmp and OOM/timeout-killing renders in prod.
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def pre_burn_curtain_slot_text_skia(
    input_path: str,
    overlays: list[dict],
    output_path: str,
    tmpdir: str,
    slot_duration_s: float,
    slot_index: int,
) -> None:
    """Slot-pre-burn equivalent for agentic jobs. Caller is responsible for
    the kill-switch check — this is a pure Skia renderer."""
    if not overlays:
        shutil.copy2(input_path, output_path)
        return

    work_dir = os.path.join(tmpdir, f"skia_curtain_slot_{slot_index}")
    os.makedirs(work_dir, exist_ok=True)

    render_t0 = time.monotonic()
    sequences = _render_overlay_sequences(overlays, slot_duration_s, work_dir)
    render_ms = int((time.monotonic() - render_t0) * 1000)

    if not sequences:
        shutil.copy2(input_path, output_path)
        return

    log.info(
        "skia_pre_burn_curtain_slot_text",
        slot=slot_index,
        overlay_count=len(overlays),
        sequence_count=len(sequences),
        total_frames=sum(s["n_frames"] for s in sequences),
        render_ms=render_ms,
    )
    try:
        _ffmpeg_burn_pngs(input_path, sequences, output_path)
    finally:
        # Free the per-slot PNG sequence immediately after the encode (see the
        # note in burn_text_overlays_skia — bounds peak scratch to one burn).
        shutil.rmtree(work_dir, ignore_errors=True)
