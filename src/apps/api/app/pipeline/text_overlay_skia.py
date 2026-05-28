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

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import skia
import structlog
from PIL import Image

from app.pipeline.text_overlay import (
    _FONT_REGISTRY,
    _FONT_SIZE_MAP,
    _POSITION_Y,
    CANVAS_H,
    CANVAS_W,
    FONT_CYCLE_FAST_INTERVAL_S,
    FONT_CYCLE_INTERVAL_S,
    FONT_CYCLE_SETTLE_RATIO,
    FONTS_DIR,
    MAX_FONT_CYCLE_FRAMES,
    _emoji_codepoint,
    _emoji_png_path,
    _hex_to_rgba,
    _validate_overlay,
)

log = structlog.get_logger()

# Output framerate for animated overlay sequences. 30 fps is enough for the
# fastest production animation (font-cycle FAST_INTERVAL = 70ms = 14fps native
# cycle rate) while keeping file count bounded. Frame budget per overlay:
# MAX_OVERLAY_FRAMES caps long-running effects (font-cycle) the same way
# Pillow's MAX_FONT_CYCLE_FRAMES does.
FPS = 30
MAX_OVERLAY_FRAMES = max(120, MAX_FONT_CYCLE_FRAMES)

# Encoder thread pool: Pillow PNG encode releases the GIL during compression,
# so threading actually helps. 4 workers matches the production Celery worker
# CPU count (shared-cpu-4x). Skia rendering is also GIL-released inside the
# C++ layer.
_ENCODE_WORKERS = 4

# Text layout constants — mirror Pillow's _TEXT_MAX_LINE_W / _TEXT_LINE_SPACING.
_MAX_LINE_W_FRAC = 0.9
_LINE_SPACING = 1.15
_MIN_FONT_SIZE = 24


# -- Typeface cache (load once at import) -------------------------------------
#
# Loading TTFs via Skia.Typeface.MakeFromFile re-parses the font (~5-15ms for
# a 400KB file). For an animated overlay rendered at 30fps, that's most of the
# per-frame budget. Preload every font in the registry at import time and
# fail-fast if any is missing — better to crash the worker boot than to render
# a job with the wrong font silently.

_TYPEFACE_BY_PATH: dict[str, skia.Typeface] = {}


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
        raise MissingGlyphsError(
            f"Typeface missing {len(missing)} glyph(s): {', '.join(missing)}"
        )


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


def _typeface_for_overlay(overlay: dict, font_name_override: str | None = None) -> skia.Typeface:
    """Resolve a typeface for an overlay following the same priority as
    text_overlay._draw_text_png: font_name_override → overlay.font_family →
    overlay.font_style → final fallback (Playfair Display Bold).
    """
    name = font_name_override or overlay.get("font_family")
    if name:
        entry = _FONT_REGISTRY.get("fonts", {}).get(name)
        if entry:
            p = os.path.join(FONTS_DIR, entry["file"])
            tf = _TYPEFACE_BY_PATH.get(p)
            if tf is not None:
                return tf

    style = overlay.get("font_style", "display")
    style_default = _FONT_REGISTRY.get("style_defaults", {}).get(style)
    if style_default:
        entry = _FONT_REGISTRY.get("fonts", {}).get(style_default)
        if entry:
            p = os.path.join(FONTS_DIR, entry["file"])
            tf = _TYPEFACE_BY_PATH.get(p)
            if tf is not None:
                return tf

    # Last resort: Playfair Display Bold (always in the registry)
    p = os.path.join(FONTS_DIR, "PlayfairDisplay-Bold.ttf")
    tf = _TYPEFACE_BY_PATH.get(p)
    if tf is None:
        # Should never happen — preload would have logged a warning
        tf = skia.Typeface.MakeFromFile(p)
    return tf


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


# -- Layout helpers -----------------------------------------------------------


def _resolve_font_size_px(overlay: dict) -> int:
    px = overlay.get("text_size_px")
    if px:
        return max(_MIN_FONT_SIZE, int(px))
    size_class = overlay.get("text_size", "medium")
    return max(_MIN_FONT_SIZE, _FONT_SIZE_MAP.get(size_class, 72))


def _resolve_anchor(overlay: dict) -> tuple[float, float]:
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
    return float(x_frac) * CANVAS_W, float(y_frac) * CANVAS_H


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


def _vertical_block_top(anchor: str, cy: float, block_h: float) -> float:
    """Top y of a `block_h`-tall text block anchored at cy. Mirrors Pillow's
    _draw_text_png vertical anchoring: left-anchored text treats
    position_y_frac as the block TOP, so wrapped lines grow DOWNWARD — a
    cumulative reveal that wraps from 1 line to 2 keeps the earlier line
    pinned instead of re-centering (the "all previous words re-appear" bug on
    prod template 89cde014). Center/right keep the block vertically centered on
    cy (historical default — changing it would shift existing centered
    templates). Centralized so _draw_centered_text, _draw_pop_in_with_suffix,
    and _draw_karaoke_line all agree on the vertical origin."""
    if anchor == "left":
        return cy
    return cy - block_h / 2.0


def _wrap_text_to_lines(text: str, font: skia.Font, max_width: float) -> list[str]:
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
            if font.measureText(candidate) <= max_width or not current:
                current.append(word)
            else:
                out.append(" ".join(current))
                current = [word]
        if current:
            out.append(" ".join(current))
    return out


def _shrink_to_fit(
    text: str,
    typeface: skia.Typeface,
    initial_size: int,
    max_width: float,
) -> tuple[skia.Font, int, list[str]]:
    """Wrap + iteratively shrink the font until every line fits inside
    max_width. Caps at 6 iterations and a 24px floor, matching Pillow.

    Returns (font, final_size_px, wrapped_lines).
    """
    size = initial_size
    font = skia.Font(typeface, size)
    font.setSubpixel(True)
    lines = _wrap_text_to_lines(text, font, max_width)

    iterations = 0
    while iterations < 6 and size > _MIN_FONT_SIZE:
        widest = max((font.measureText(ln) for ln in lines), default=0)
        if widest <= max_width:
            break
        size = max(_MIN_FONT_SIZE, int(size * 0.85))
        font = skia.Font(typeface, size)
        font.setSubpixel(True)
        lines = _wrap_text_to_lines(text, font, max_width)
        iterations += 1

    return font, size, lines


def _measure_block(font: skia.Font, lines: list[str]) -> dict[str, Any]:
    """Compute per-line widths, max line height, total block height."""
    metrics = font.getMetrics()
    line_height_raw = metrics.fDescent - metrics.fAscent
    line_step = int(line_height_raw * _LINE_SPACING)
    widths = [font.measureText(ln) for ln in lines]
    block_h = line_step * (len(lines) - 1) + int(line_height_raw) if lines else 0
    return {
        "widths": widths,
        "line_step": line_step,
        "block_h": block_h,
        "ascent_offset": -metrics.fAscent,
    }


# -- Per-frame drawing -------------------------------------------------------


def _draw_centered_text(
    canvas: skia.Canvas,
    text: str,
    overlay: dict,
    *,
    font_override: skia.Font | None = None,
    color_override: int | None = None,
    alpha: float = 1.0,
    scale: float = 1.0,
    y_translate: float = 0.0,
) -> None:
    """Draw `text` centered horizontally at the overlay's anchor, with
    shadow + optional stroke + fill. Mirrors Pillow's _draw_text_png layout:
    multi-line vertical centering, 0.9*CANVAS_W word-wrap, auto-shrink.

    Effect transforms (alpha, scale, y_translate) are applied via a canvas
    matrix so glyph metrics are not perturbed.
    """
    if not text:
        return

    typeface = _typeface_for_overlay(overlay)
    if font_override is not None:
        font = font_override
        size = int(font.getSize())
        lines = _wrap_text_to_lines(text, font, CANVAS_W * _MAX_LINE_W_FRAC)
    else:
        initial_size = _resolve_font_size_px(overlay)
        font, size, lines = _shrink_to_fit(
            text, typeface, initial_size, CANVAS_W * _MAX_LINE_W_FRAC
        )

    if not lines:
        return

    block = _measure_block(font, lines)
    cx, cy = _resolve_anchor(overlay)
    anchor = _resolve_text_anchor(overlay)

    block_top = _vertical_block_top(anchor, cy, block["block_h"])
    first_baseline = block_top + block["ascent_offset"]

    # Emoji prefix: composite to the left of line 0
    first_w = block["widths"][0] if block["widths"] else 0
    emoji_metrics = _resolve_emoji_metrics(overlay, first_w, font)

    base_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"), int(255 * alpha))
    fill_color = color_override if color_override is not None else base_color
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    shadow_alpha = int(160 * alpha)

    canvas.save()
    # Center transform on anchor for scale + translate
    canvas.translate(cx, cy + y_translate)
    canvas.scale(scale, scale)
    canvas.translate(-cx, -cy)

    for i, line in enumerate(lines):
        baseline_y = first_baseline + i * block["line_step"]
        line_w = block["widths"][i]
        if i == 0 and emoji_metrics is not None:
            combined_w = emoji_metrics["size"] + emoji_metrics["gap"] + line_w
            block_left = _anchored_left_x(anchor, cx, combined_w)
            line_x = block_left + emoji_metrics["size"] + emoji_metrics["gap"]
        else:
            line_x = _anchored_left_x(anchor, cx, line_w)

        _draw_line_with_layers(
            canvas,
            line,
            line_x,
            baseline_y,
            font,
            fill_color,
            stroke_px,
            shadow_alpha,
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
) -> None:
    """Shadow → stroke → fill, in that order, matching Pillow's compositing."""
    # Shadow: soft black blur, offset 6px down (Pillow uses y+6, alpha 160).
    if shadow_alpha > 0:
        shadow_paint = skia.Paint(
            AntiAlias=True,
            Color=skia.ColorSetARGB(shadow_alpha, 0, 0, 0),
            MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 12.0),
        )
        canvas.drawString(line, x, baseline_y + 6.0, font, shadow_paint)

    # Crisp black stroke (TikTok caption look)
    if stroke_px > 0:
        stroke_paint = skia.Paint(
            AntiAlias=True,
            Color=skia.ColorSetARGB(230, 0, 0, 0),
            Style=skia.Paint.kStroke_Style,
            StrokeWidth=stroke_px * 2.0,
            StrokeJoin=skia.Paint.kRound_Join,
        )
        canvas.drawString(line, x, baseline_y, font, stroke_paint)

    # Fill
    fill_paint = skia.Paint(AntiAlias=True, Color=fill_color)
    canvas.drawString(line, x, baseline_y, font, fill_paint)


# -- Emoji compositing -------------------------------------------------------


def _resolve_emoji_metrics(
    overlay: dict, first_line_width: float, font: skia.Font
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
    max_combined = int(CANVAS_W * 0.95)
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


def _draw_pop_in_with_suffix(
    canvas: skia.Canvas, overlay: dict, t_local: float, duration_s: float
) -> None:
    """pop-in with `pop_animated_suffix`: prefix renders static at 100% scale,
    suffix scales through 30→115→100 envelope over the first 250ms.

    Used by music's per-word-pop lyric style. Mirrors text_overlay's libass
    `\\fscx`/`\\fscy` keyframes (production keyframes_ms = (0, 150, 250),
    scales = (30, 115, 100)). Without this, every new lyric word re-pops the
    full accumulated line — visible flicker.
    """
    text = _overlay_text(overlay)
    suffix = overlay.get("pop_animated_suffix") or ""
    if not text:
        return

    # If the suffix doesn't actually trail the text, fall back to a regular
    # pop-in (animate the whole line). Mirrors production's null-suffix path.
    if not suffix or not text.endswith(suffix):
        _draw_with_animation(canvas, overlay, t_local, duration_s, effect="pop-in")
        return

    prefix = text[: -len(suffix)].rstrip()

    typeface = _typeface_for_overlay(overlay)
    initial_size = _resolve_font_size_px(overlay)
    # Lay out the FULL line so prefix + suffix stay in their original positions
    full_font, full_size, full_lines = _shrink_to_fit(
        text, typeface, initial_size, CANVAS_W * _MAX_LINE_W_FRAC
    )
    # The suffix-pop layout puts prefix + suffix on ONE baseline. If the line
    # is too wide to fit on a single line (a manually-edited phrase grew past
    # ~90% canvas, or the analysis-time line-split didn't apply), that layout
    # would clip off the right edge ("combination of hard work" → "combination
    # of har"). Fall back to the wrapping/shrinking path, which stacks lines
    # and honors text_anchor. Because pop-in no longer scales (scale=1.0, see
    # below) and _draw_centered_text now top-anchors left-anchored blocks
    # (_vertical_block_top), the wrapped fallback renders the prior words in
    # the SAME positions as the previous stage — no whole-line re-pop, no
    # vertical re-center. The cumulative reveal stays spatially stable.
    if len(full_lines) > 1:
        _draw_with_animation(canvas, overlay, t_local, duration_s, effect="pop-in")
        return
    full_w = full_font.measureText(text)
    prefix_w = full_font.measureText((prefix + " ") if prefix else "")
    suffix_w = full_font.measureText(suffix)

    cx, cy = _resolve_anchor(overlay)
    anchor = _resolve_text_anchor(overlay)
    line_start_x = _anchored_left_x(anchor, cx, full_w)

    metrics = full_font.getMetrics()
    line_height_raw = metrics.fDescent - metrics.fAscent
    # Top-anchor the single-line baseline for left/right (matching
    # _vertical_block_top + _measure_block's single-line block_h) so a phrase's
    # first stage (one line, here) sits at the SAME vertical position as the
    # later stages that wrap to multiple lines via the bail above. Without
    # this, stage 1 vertically centers and the line visibly jumps up when
    # stage 2 wraps and top-anchors.
    baseline_y = _vertical_block_top(anchor, cy, line_height_raw) + (-metrics.fAscent)

    fill_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"))
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)

    # Draw prefix statically
    if prefix:
        _draw_line_with_layers(
            canvas,
            prefix,
            line_start_x,
            baseline_y,
            full_font,
            fill_color,
            stroke_px,
            shadow_alpha=160,
        )

    # No bounce: the revealed word appears at full size. The word-by-word
    # reveal comes from the per-stage timing (each stage adds one word), not
    # from a scale animation — so dropping the 30→115→100 overshoot keeps the
    # progressive reveal but removes the springy pop the words used to do as
    # they showed up.
    scale = 1.0

    # Suffix is centered on its slot; scale around that center so the suffix
    # doesn't visibly shift its baseline x while it pops.
    suffix_x = line_start_x + prefix_w
    suffix_cx = suffix_x + suffix_w / 2.0

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
    )
    canvas.restore()


def _draw_karaoke_line(
    canvas: skia.Canvas, overlay: dict, t_local: float, duration_s: float
) -> None:
    """karaoke-line: each word switches from primary color to highlight color
    when t_local crosses its end timestamp. word_timings is a list of
    {text, duration_cs} with cumulative timing from overlay start.

    Karaoke does NOT consume `display_text` — its per-word highlight order
    is keyed off the original `text`. Layer 2's finalizer never sets
    `display_text` on karaoke overlays (it only fires for effect="lyric-line").
    """
    text = overlay.get("text", "") or ""
    word_timings = overlay.get("word_timings") or []
    if not word_timings:
        _draw_centered_text(canvas, text, overlay)
        return

    words: list[str] = []
    ends: list[float] = []
    acc = 0.0
    for w in word_timings:
        words.append(w.get("text", ""))
        acc += float(w.get("duration_cs", 0)) / 100.0
        ends.append(acc)

    typeface = _typeface_for_overlay(overlay)
    initial_size = _resolve_font_size_px(overlay)
    # Use full text for layout sizing so a long lyric line shrinks once at the
    # start instead of per-word.
    font, _size, _lines = _shrink_to_fit(
        " ".join(words), typeface, initial_size, CANVAS_W * _MAX_LINE_W_FRAC
    )

    space_w = font.measureText(" ")
    word_widths = [font.measureText(w) for w in words]
    total_w = sum(word_widths) + space_w * max(0, len(words) - 1)

    cx, cy = _resolve_anchor(overlay)
    # Mirror _draw_centered_text: left/right-anchored overlays pin the line's
    # left/right edge at position_x_frac; centered overlays center on cx.
    anchor = _resolve_text_anchor(overlay)
    x = _anchored_left_x(anchor, cx, total_w)
    metrics = font.getMetrics()
    line_height_raw = metrics.fDescent - metrics.fAscent
    # Match _vertical_block_top so a karaoke line shares the same vertical
    # origin convention as the centered/pop-in paths (top for left/right,
    # centered for center).
    baseline_y = _vertical_block_top(anchor, cy, line_height_raw) + (-metrics.fAscent)

    primary_color = _skia_color_from_hex(overlay.get("text_color", "#FFFFFF"))
    highlight_color = _skia_color_from_hex(overlay.get("highlight_color") or "#FFD24A")
    stroke_px = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)

    for i, word in enumerate(words):
        sung = t_local >= ends[i]
        color = highlight_color if sung else primary_color
        _draw_line_with_layers(
            canvas, word, x, baseline_y, font, color, stroke_px, shadow_alpha=160
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
) -> None:
    """Apply animation transforms (scale, alpha, position, reveal length) for
    `effect` at time `t_local`, then draw the (possibly transformed) text.
    """
    effect = effect or overlay.get("effect", "none")
    text = _overlay_text(overlay)

    scale = 1.0
    alpha = 1.0
    y_translate = 0.0
    visible_text = text

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
        chars_per_s = 12.0
        visible_chars = max(1, int(t_local * chars_per_s) + 1)
        visible_text = text[:visible_chars]
    elif effect == "stream-in":
        # "How an AI returns an answer" — reveal WORD by word (not char) with a
        # blinking cursor while streaming. Pairs with text_anchor="left" so the
        # answer grows rightward from a fixed margin like a chat response.
        words = text.split()
        words_per_s = 6.0
        n = max(1, int(t_local * words_per_s) + 1)
        visible_text = " ".join(words[:n])
        if n < len(words) and int(t_local * 2) % 2 == 0:
            visible_text = f"{visible_text} |"  # blink cursor at ~2 Hz
    elif effect in ("slide-up", "slide-down"):
        animate_for = min(0.35, duration_s * 0.5)
        progress = min(1.0, t_local / animate_for) if animate_for > 0 else 1.0
        eased = _ease_out_cubic(progress)
        direction = -1.0 if effect == "slide-up" else 1.0
        y_translate = direction * 220.0 * (1.0 - eased)
    elif effect == "pop-in":
        # No bounce: word appears at full size (see _draw_pop_in_with_suffix).
        scale = 1.0
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

    _draw_centered_text(
        canvas,
        visible_text,
        overlay,
        alpha=alpha,
        scale=scale,
        y_translate=y_translate,
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
    # pop-in with pop_animated_suffix is animated even if base effect is "pop-in";
    # plain pop-in without suffix is also animated.
    return effect in _ANIMATED_EFFECTS_SKIA


def _draw_frame(overlay: dict, t_local: float, duration_s: float) -> skia.Image:
    """Render one frame of `overlay` at time `t_local`. Returns a Skia Image
    that the caller writes to disk via Pillow."""
    surface = skia.Surfaces.MakeRasterN32Premul(CANVAS_W, CANVAS_H)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)

    effect = overlay.get("effect", "none")

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
        _draw_centered_text(canvas, _overlay_text(overlay), overlay, font_override=font)
    elif effect == "karaoke-line":
        _draw_karaoke_line(canvas, overlay, t_local, duration_s)
    elif effect == "pop-in" and overlay.get("pop_animated_suffix"):
        _draw_pop_in_with_suffix(canvas, overlay, t_local, duration_s)
    elif _is_animated(overlay):
        # `lyric-line` is in this branch because its fade_in_ms / fade_out_ms
        # are honored per-frame in `_draw_with_animation`. `_overlay_text`
        # still resolves `display_text` (the Layer 2 finalizer's truncated
        # partial line) when present — the alpha multiplies on top of that.
        _draw_with_animation(canvas, overlay, t_local, duration_s)
    else:
        # Static effects render at full opacity throughout [start_s, end_s].
        _draw_centered_text(canvas, _overlay_text(overlay), overlay)

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


# -- Public API: render overlays to FFmpeg-ready PNG sequences ---------------


def _generate_overlay_sequence(overlay: dict, work_dir: str, idx: int) -> dict[str, Any] | None:
    """Render every frame for one overlay, return a single config describing
    the sequence (or single PNG for static effects)."""
    start_s = float(overlay.get("start_s", 0.0))
    end_s = float(overlay.get("end_s", 0.0))
    duration_s = max(0.0, end_s - start_s)
    if duration_s <= 0:
        return None

    pattern_prefix = f"skia_overlay_{idx:03d}_f"

    if not _is_animated(overlay):
        out_path = os.path.join(work_dir, f"{pattern_prefix}0000.png")
        img = _draw_frame(overlay, 0.0, duration_s)
        _write_png_pillow(img, out_path)
        return {
            "pattern": os.path.join(work_dir, f"{pattern_prefix}%04d.png"),
            "first_frame": out_path,
            "n_frames": 1,
            "fps": FPS,
            "start_s": start_s,
            "end_s": end_s,
            "is_animated": False,
        }

    # MAX_OVERLAY_FRAMES protects font-cycle (rapid font swaps where every
    # frame is unique) from runaway PNG counts. `lyric-line` is slow alpha
    # ramps over a long-held middle and would lose its fade-out + tail to
    # the cap for any line longer than MAX_OVERLAY_FRAMES / FPS (~4s) — the
    # prod regression for the user-reported job's Line 2 (4.26s). lyric-line
    # opts out of the font-cycle cap but uses a generous sanity ceiling so a
    # malformed transcript with `end_s = 240.0` (last-line-of-section has no
    # gap_cap in lyric_injector) cannot blow scratch disk on the encode
    # worker: 30s × 30fps = 900 frames × ~1MB PNG ≈ 1GB worst case, vs
    # 7200 frames × 1MB ≈ 7GB unbounded.
    effect = overlay.get("effect", "none")
    wanted = max(1, int(round(duration_s * FPS)))
    if effect == "lyric-line":
        ceiling = int(FPS * 30)
        if wanted > ceiling:
            log.warning(
                "skia_lyric_line_duration_clamped",
                duration_s=duration_s,
                wanted_frames=wanted,
                clamped_to=ceiling,
            )
        n_frames = min(ceiling, wanted)
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
        min(int(FPS * 30), n_frames + 1)
        if effect == "lyric-line"
        else min(MAX_OVERLAY_FRAMES, n_frames + 1)
    )

    def _render_one(i: int) -> None:
        t_local = i * frame_dur
        img = _draw_frame(overlay, t_local, duration_s)
        _write_png_pillow(img, os.path.join(work_dir, f"{pattern_prefix}{i:04d}.png"))

    with ThreadPoolExecutor(max_workers=_ENCODE_WORKERS) as pool:
        list(pool.map(_render_one, range(n_render)))

    return {
        "pattern": os.path.join(work_dir, f"{pattern_prefix}%04d.png"),
        "first_frame": os.path.join(work_dir, f"{pattern_prefix}0000.png"),
        "n_frames": n_render,
        "fps": FPS,
        "start_s": start_s,
        "end_s": end_s,
        "is_animated": True,
    }


def _render_overlay_sequences(
    overlays: list[dict], slot_duration_s: float, work_dir: str
) -> list[dict[str, Any]]:
    """Validate every overlay against slot_duration_s, then render PNG
    sequences. Returns list of overlay-sequence configs ready for FFmpeg."""
    out: list[dict[str, Any]] = []
    for i, overlay in enumerate(overlays):
        validated_text, start_s, end_s, _position = _validate_overlay(overlay, slot_duration_s)
        # `_validate_overlay` returns None when the text is empty AND spans
        # are absent. Spans-only overlays are not yet supported by the Skia
        # path — fall back to validating just timing.
        if validated_text is None and not overlay.get("spans"):
            continue
        # Apply the validated timing back to the overlay (start clamp etc.)
        # but only if we got a non-None result.
        clamped = dict(overlay)
        if validated_text is not None:
            clamped["text"] = validated_text
            clamped["start_s"] = start_s
            clamped["end_s"] = end_s

        seq = _generate_overlay_sequence(clamped, work_dir, i)
        if seq is not None:
            out.append(seq)
    return out


# -- FFmpeg compositing: build the burn command and execute ------------------


def _ffmpeg_burn_pngs(
    input_video: str,
    sequences: list[dict[str, Any]],
    output_path: str,
) -> None:
    """Build and run the FFmpeg command that overlays per-overlay PNG
    sequences onto `input_video`. Uses image2 demuxer for animated sequences
    (one input per overlay) + setpts shift to align each sequence with its
    absolute start_s.
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
            *_encoding_args(output_path, preset="fast"),
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


# -- Public entry points: drop-in replacements for the Pillow burn functions -


def burn_text_overlays_skia(
    input_path: str,
    overlays: list[dict],
    output_path: str,
    tmpdir: str,
) -> None:
    """Drop-in replacement for `text_overlay._burn_text_overlays` when the
    job is agentic or music.

    Caller is responsible for honoring `settings.text_renderer_skia_enabled`
    — this function does the Skia render unconditionally. The kill-switch
    check lives in the orchestrator (or in the wrapper that selects between
    Pillow and Skia at the burn site) so this module stays a pure renderer.
    """
    if not overlays:
        shutil.copy2(input_path, output_path)
        return

    # Resolve the timing window: production caller passes ABS_PASS_TIME_S /
    # ABS_PASS_SLOT_INDEX sentinels meaning "use the final-pass duration".
    # The overlays carry absolute timestamps already; we only need a permissive
    # validation window, so pass the max end_s.
    max_end = max((float(o.get("end_s", 0.0)) for o in overlays), default=0.0)
    work_dir = os.path.join(tmpdir, "skia_text_burn")
    os.makedirs(work_dir, exist_ok=True)

    render_t0 = time.monotonic()
    sequences = _render_overlay_sequences(overlays, max_end + 1.0, work_dir)
    render_ms = int((time.monotonic() - render_t0) * 1000)

    if not sequences:
        shutil.copy2(input_path, output_path)
        return

    log.info(
        "skia_burn_text_overlays",
        overlay_count=len(overlays),
        sequence_count=len(sequences),
        total_frames=sum(s["n_frames"] for s in sequences),
        render_ms=render_ms,
    )
    _ffmpeg_burn_pngs(input_path, sequences, output_path)


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
    _ffmpeg_burn_pngs(input_path, sequences, output_path)
