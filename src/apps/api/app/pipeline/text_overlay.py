"""Generate text overlay PNGs and animated ASS files for TikTok-style text.

Two rendering paths:
  - Static effects + font-cycle -> PNG images composited via FFmpeg overlay filter
  - Animated effects (fade-in, typewriter, slide-up) -> ASS subtitle files
    burned via FFmpeg subtitles filter

Supports effects:
  - Static effects (scale-up, none): single PNG for the duration
  - font-cycle: rapid font switching -- generates one PNG per font frame, each
    with a different font, swapped via timed FFmpeg overlays (~7 changes/sec)
  - Animated effects (fade-in, typewriter, slide-up, slide-down, pop-in, bounce): ASS subtitle files

Fonts loaded from font-registry.json (shared with frontend).
Fallback: Pillow default if all resolution fails.
"""

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

import structlog

from app.pipeline.ass_utils import format_ass_time, sanitize_ass_text

log = structlog.get_logger()

# -- Constants ----------------------------------------------------------------

# Hard char cap as a safety net only — the renderer now word-wraps long text
# to multi-line and shrinks fonts on single-word overflow, so this limit
# should never trigger for real captions. 300 = far above any sane caption
# length but still bounds Pillow measurement time on adversarial input.
MAX_OVERLAY_TEXT_LEN = 300

# Output dimensions (must match reframe output)
CANVAS_W = 1080
CANVAS_H = 1920

# Font path: look next to assets/ dir relative to the api app root
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "fonts")
FONTS_DIR = os.path.normpath(_ASSETS_DIR)

# Pre-rendered emoji PNGs (Twemoji 72x72 transparent). Pillow can't render
# color emoji glyphs without raqm/HarfBuzz, so we composite a static PNG
# to the left of caption text instead. Filename = lowercase hex codepoint
# of the BASE emoji (variation selectors stripped).
_EMOJI_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "assets", "emoji")
)


def _emoji_codepoint(emoji: str) -> str:
    """Return lowercase hex codepoint of the base char in `emoji`.

    Strips ZWJ (U+200D) and variation selectors (U+FE0F/U+FE0E). For our
    use case we only handle single-char emoji like 🗣️ -> "1f5e3". Multi-
    glyph compositions (skin tones, family combos) just use the leading
    base codepoint, which usually has its own Twemoji asset.
    """
    if not emoji:
        return ""
    for ch in emoji:
        cp = ord(ch)
        # Skip variation selectors and ZWJ
        if cp in (0xFE0F, 0xFE0E, 0x200D):
            continue
        return f"{cp:x}"
    return ""


def _emoji_png_path(emoji: str) -> str | None:
    """Return absolute path to the emoji PNG asset, or None if missing."""
    cp = _emoji_codepoint(emoji)
    if not cp:
        return None
    path = os.path.join(_EMOJI_DIR, f"{cp}.png")
    return path if os.path.exists(path) else None


OVERLAY_FONT_PATH = os.path.normpath(os.path.join(_ASSETS_DIR, "PlayfairDisplay-Bold.ttf"))
OVERLAY_FONT_PATH_REGULAR = os.path.normpath(
    os.path.join(_ASSETS_DIR, "PlayfairDisplay-Regular.ttf")
)
MONTSERRAT_FONT_PATH = os.path.normpath(os.path.join(_ASSETS_DIR, "Montserrat-ExtraBold.ttf"))

# Effects that produce animated .ass files instead of static PNGs
ASS_ANIMATED_EFFECTS = frozenset(
    {
        "fade-in",
        "typewriter",
        "slide-up",
        "slide-down",
        "pop-in",
        "bounce",
        "karaoke-line",
        "lyric-line",
    }
)

# Animation keyframe timings (ms from overlay start). At runtime, each timestamp
# is clamped to a fraction of the overlay's actual duration so very short
# overlays don't get a truncated animation tail (e.g. a 200ms label with a
# 500ms bounce would otherwise just freeze mid-bounce).
_POP_IN_KEYFRAMES_MS = (0, 150, 250)  # scale 30 -> 115 -> 100
_POP_IN_SCALES = (30, 115, 100)
_BOUNCE_KEYFRAMES_MS = (0, 180, 360, 500)  # scale 100 -> 125 -> 90 -> 100
_BOUNCE_SCALES = (100, 125, 90, 100)


def _clamp_keyframes(keyframes_ms: tuple[int, ...], duration_ms: int) -> tuple[int, ...]:
    """Compress keyframes proportionally if they overrun the overlay window.

    The last keyframe is the animation end; if it overruns duration_ms, scale
    every keyframe by duration_ms / last so the animation completes before the
    overlay disappears. Clamp ratio is capped at 1.0 so short keyframes don't
    get stretched on long overlays.
    """
    last = keyframes_ms[-1]
    if last <= duration_ms:
        return keyframes_ms
    ratio = duration_ms / last
    return tuple(max(0, int(kf * ratio)) for kf in keyframes_ms)


# Position -> vertical anchor (fraction of canvas height)
_POSITION_Y = {
    "center": 0.45,
    "center-above": 0.42,
    "center-label": 0.4720,
    "center-below": 0.55,
    "top": 0.15,
    "bottom": 0.85,
}

# -- Font registry ------------------------------------------------------------
# Single source of truth for both Python and TypeScript font resolution.

_REGISTRY_PATH = os.path.join(FONTS_DIR, "font-registry.json")

# Hardcoded fallback if font-registry.json fails to parse (critical gap mitigation)
_FALLBACK_STYLE_MAP: dict[str, str] = {
    "display": OVERLAY_FONT_PATH,
    "sans": MONTSERRAT_FONT_PATH,
    "serif": OVERLAY_FONT_PATH_REGULAR,
    "serif_italic": OVERLAY_FONT_PATH_REGULAR,
    "script": OVERLAY_FONT_PATH,
}

try:
    with open(_REGISTRY_PATH) as _f:
        _FONT_REGISTRY: dict = json.load(_f)
except Exception as _exc:
    log.error("font_registry_load_failed", error=str(_exc), path=_REGISTRY_PATH)
    _FONT_REGISTRY = {"fonts": {}, "style_defaults": {}}


def _registry_font_path(font_name: str) -> str | None:
    """Look up the .ttf file path for a font name in the registry."""
    entry = _FONT_REGISTRY.get("fonts", {}).get(font_name)
    if not entry:
        return None
    path = os.path.join(FONTS_DIR, entry["file"])
    return path if os.path.exists(path) else None


def _registry_ass_name(font_name: str) -> str:
    """Look up the ASS font name for a font_family from the registry."""
    entry = _FONT_REGISTRY.get("fonts", {}).get(font_name)
    if entry:
        return entry.get("ass_name", font_name)
    return font_name


def _registry_ass_bold(font_name: str) -> int:
    """Look up the ASS Bold flag for a registry font."""
    entry = _FONT_REGISTRY.get("fonts", {}).get(font_name)
    if entry and "ass_bold" in entry:
        return int(entry["ass_bold"])
    if entry and int(entry.get("weight", 400)) >= 700:
        return 0
    return -1


# -- Font size mapping --------------------------------------------------------

_FONT_SIZE_MAP = {
    "small": 36,
    "medium": 72,
    "large": 120,
    "xlarge": 150,
    "xxlarge": 250,
    "jumbo": 199,
}

# -- Font style mapping (backwards compat, now backed by registry) ------------

_FONT_STYLE_MAP: dict[str, list[str]] = {}


def _build_style_map() -> dict[str, list[str]]:
    """Build font style -> file path list from registry, with hardcoded fallback."""
    style_defaults = _FONT_REGISTRY.get("style_defaults", {})
    result: dict[str, list[str]] = {}

    for style, font_name in style_defaults.items():
        path = _registry_font_path(font_name)
        if path:
            result[style] = [path]
        elif style in _FALLBACK_STYLE_MAP:
            result[style] = [_FALLBACK_STYLE_MAP[style]]

    # Ensure all 5 styles exist even if registry is incomplete
    for style, fallback_path in _FALLBACK_STYLE_MAP.items():
        if style not in result:
            result[style] = [fallback_path]

    return result


_FONT_STYLE_MAP = _build_style_map()


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    """Convert '#D4A843' or 'D4A843' to (R, G, B, 255)."""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    return (255, 255, 255, 255)  # default white


def _hex_to_ass_bgr(hex_color: str) -> str:
    """Convert '#RRGGBB' to ASS's BGR hex literal (no '&H' wrapper).

    ASS color tags use `&HBBGGRR&` byte order (little-endian within the
    24-bit value). This helper does only the byte-swap; the caller wraps
    with `&H...&`.
    """
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return "FFFFFF"
    try:
        r, g, b = h[0:2], h[2:4], h[4:6]
    except ValueError:
        return "FFFFFF"
    return f"{b}{g}{r}".upper()


def _finite_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


# Position -> ASS alignment + MarginV
# ASS alignment: 2=bottom-center, 5=center, 8=top-center
_ASS_POSITION = {
    "center": (5, 0),
    "center-above": (5, 150),
    "center-below": (5, -100),
    "top": (8, 200),
    "bottom": (2, 200),
}


def _ass_explicit_pos_alignment(text_anchor: str | None) -> int:
    r"""ASS alignment for explicit \pos/\move coordinates.

    Keep the vertical anchor on ASS's middle row so existing y coordinates keep
    their meaning: left=\an4, center=\an5, right=\an6.
    """
    if text_anchor == "left":
        return 4
    if text_anchor == "right":
        return 6
    return 5


def _build_ass_header(
    fontname: str = "Playfair Display",
    *,
    style_name: str = "Overlay",
    bold: int = -1,
    outline: str = "0",
    shadow: str = "2",
    outline_colour: str = "&H00000000",
    back_colour: str = "&H40000000",
) -> str:
    """Build ASS header with dynamic font name."""
    return f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {style_name},{fontname},90,&H00FFFFFF,&H00FFFFFF,{outline_colour},{back_colour},{bold},0,0,0,100,100,0,0,1,{outline},{shadow},5,50,50,0,1
"""  # noqa: E501


# Keep static header for backwards compat (used when no font_family set)
_ASS_OVERLAY_HEADER = _build_ass_header("Playfair Display")

# -- Font-cycle configuration -------------------------------------------------
# Fonts with cycle_role="contrast" are used during rapid cycling.
# The settle font (index 0) is either the overlay's font_family or Playfair Display Bold.

FONT_CYCLE_INTERVAL_S = 0.15
FONT_CYCLE_FAST_INTERVAL_S = 0.07
FONT_CYCLE_SETTLE_RATIO = 0.30
# Safety cap on PNG frames generated for a single font-cycle overlay. The cap
# exists to prevent runaway PNG generation if a slot duration / interval pair
# requests thousands of frames. Raised from 60 to 100 on 2026-05-10 so the
# Dimples Passport Travel Vlog template's 5.5s title slot (which needs ~76
# frames of fast-cycle PNGs to animate through the full curtain-close phase)
# no longer hits the cap mid-curtain and freezes to a gap-fill static frame.
# Other templates' font-cycle slots are all short enough (<4.2s of fast
# cycling) that they never reach 60 frames — they'll continue to use far
# fewer than 100 and see zero behavior change.
MAX_FONT_CYCLE_FRAMES = 100
OVERLAY_FONT_SIZE = 90

# Lyric-line auto-fit. The LyricLine ASS Style hardcodes Fontsize=90 in
# `_build_ass_header`; we mirror it here so the wrap+shrink helper knows what
# size libass would otherwise draw. The 24px floor mirrors Skia's
# `_shrink_to_fit` MIN_FONT_SIZE so a stupendously long line still renders
# (small but readable) instead of hard-failing.
_LYRIC_LINE_STYLE_FONT_SIZE = 90
_LYRIC_LINE_MIN_FONT_PX = 24
_LYRIC_LINE_SHRINK_RATIO = 0.85
_LYRIC_LINE_SHRINK_MAX_ITERS = 6


def _build_cycle_contrast_names() -> list[str]:
    """Get font names with cycle_role='contrast' from the registry."""
    names: list[str] = []
    for font_name, entry in _FONT_REGISTRY.get("fonts", {}).items():
        if entry.get("cycle_role") == "contrast":
            path = os.path.join(FONTS_DIR, entry["file"])
            if os.path.exists(path):
                names.append(font_name)
    return names


_CYCLE_CONTRAST_NAMES = _build_cycle_contrast_names()


# -- Public API ---------------------------------------------------------------


def generate_text_overlay_png(
    overlays: list[dict],
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
) -> list[dict] | None:
    """Render text overlay PNG images and return overlay configs for FFmpeg.

    Handles static effects and font-cycle. Overlays whose effect is in
    ASS_ANIMATED_EFFECTS are also rendered as PNG fallback (ASS is preferred
    but PNG fallback ensures overlays always appear).

    Returns list of {png_path, start_s, end_s} or None if nothing rendered.
    """
    results: list[dict] = []

    for i, overlay in enumerate(overlays):
        effect = overlay.get("effect", "none")
        spans = overlay.get("spans")

        if effect == "font-cycle":
            configs = _render_font_cycle(
                overlay,
                slot_duration_s,
                output_dir,
                slot_index,
                i,
            )
            results.extend(configs)
        elif effect == "player-card":
            cfg = _render_player_card(
                overlay,
                slot_duration_s,
                output_dir,
                slot_index,
                i,
            )
            if cfg:
                results.extend(cfg)
        elif spans:
            # Rich span rendering — per-word font/color/size
            text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
            if text is None:
                # _validate_overlay returned None — either bad timing or empty text.
                # Re-extract timing with clamping (spans carry content, so empty text is OK).
                start_s = float(overlay.get("start_s", 0.0))
                end_s = float(overlay.get("end_s", 0.0))
                if end_s > slot_duration_s:
                    end_s = slot_duration_s
                if start_s >= end_s:
                    continue  # genuinely bad timing — skip even with spans
                position = overlay.get("position", "center")
            png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{i}.png")
            _draw_spans_png(
                overlay, spans, position, png_path, outline_px=overlay.get("outline_px")
            )
            if os.path.exists(png_path):
                results.append({"png_path": png_path, "start_s": start_s, "end_s": end_s})
        else:
            text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
            if text is None:
                continue

            font_family = overlay.get("font_family")
            font_style = overlay.get("font_style", "display")
            text_size = overlay.get("text_size", "medium")
            text_size_px_val = overlay.get("text_size_px")
            hex_color = overlay.get("text_color", "#FFFFFF")
            text_color = _hex_to_rgba(hex_color)

            png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{i}.png")
            _draw_text_png(
                text,
                position,
                png_path,
                font_family=font_family,
                font_style=font_style,
                text_size=text_size,
                text_size_px=text_size_px_val,
                text_color=text_color,
                position_x_frac=overlay.get("position_x_frac"),
                position_y_frac=overlay.get("position_y_frac"),
                text_anchor=overlay.get("text_anchor", "center"),
                stroke_width=int(overlay.get("outline_px") or overlay.get("stroke_width") or 0),
                emoji_prefix=overlay.get("emoji_prefix", ""),
            )
            results.append({"png_path": png_path, "start_s": start_s, "end_s": end_s})

    return results if results else None


def generate_animated_overlay_ass(
    overlays: list[dict],
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
) -> list[str] | None:
    """Generate ASS subtitle files for animated text effects.

    Processes overlays whose effect IS in ASS_ANIMATED_EFFECTS.
    Each overlay produces a separate .ass file with timing and animation tags.
    Returns list of ASS file paths, or None if no animated overlays.
    """
    ass_paths: list[str] = []

    for i, overlay in enumerate(overlays):
        effect = overlay.get("effect", "none")
        if effect not in ASS_ANIMATED_EFFECTS:
            continue

        text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
        if text is None:
            continue

        font_family = overlay.get("font_family")
        ass_path = os.path.join(output_dir, f"slot_{slot_index}_anim_{i}.ass")
        try:
            _write_animated_ass(
                text,
                start_s,
                end_s,
                position,
                effect,
                ass_path,
                font_family=font_family,
                position_x_frac=overlay.get("position_x_frac"),
                position_y_frac=overlay.get("position_y_frac"),
                text_anchor=overlay.get("text_anchor", "center"),
                outline_px=overlay.get("outline_px"),
                # Karaoke needs per-word timings (relative to overlay start)
                # plus the highlight color used for "already sung" words.
                word_timings=overlay.get("word_timings"),
                highlight_color=overlay.get("highlight_color"),
                text_color=overlay.get("text_color"),
                # Per-word-pop cumulative stages set this to the newly added
                # word; the renderer animates only that suffix and keeps the
                # accumulated prefix static. Without it, every new word
                # re-pops the full line — visible flicker.
                pop_animated_suffix=overlay.get("pop_animated_suffix"),
                # `lyric-line` effect only — alpha fade in/out durations (ms).
                # The injector (`_inject_line`) sets these per-overlay; falls
                # back to the function defaults for any other call site.
                fade_in_ms=_overlay_int(overlay, "fade_in_ms", 150),
                fade_out_ms=_overlay_int(overlay, "fade_out_ms", 250),
                # `lyric-line` effect only — optional fade-out curve override.
                # Set by the dynamic-crossfade scheduler in lyric_injector to
                # "sqrt" for inter-line crossfades (mirror-symmetric with the
                # sqrt fade-in → unit-partition crossfade, no readable stacking).
                # None means default lingering `1−p²`.
                fade_out_curve=overlay.get("fade_out_curve"),
                # `lyric-line` effect only — px font-size override that the
                # wrap+shrink helper uses as its starting base. Without this
                # the helper starts from the Style's Fontsize=90 and never
                # honours the injector's narrower default.
                text_size_px=overlay.get("text_size_px"),
            )
            if _validate_ass_file(ass_path):
                ass_paths.append(ass_path)
            else:
                # ASS validation failed — same class of silent-drop as raise below.
                log.error("ass_validation_failed", slot=slot_index, overlay=i)
                raise RuntimeError(
                    f"ASS validation failed for animated overlay {i} on slot "
                    f"{slot_index}; refusing to ship a video missing recipe-defined text"
                )
        except Exception as exc:
            # Re-raise: silent skip drops the recipe-defined animated text from
            # the final video. The slot still renders but the overlay vanishes
            # with no surface — same class as the curtain-close silent-fallback.
            log.error("animated_overlay_failed", slot=slot_index, overlay=i, error=str(exc))
            raise RuntimeError(
                f"animated text overlay {i} on slot {slot_index} failed; "
                f"refusing to ship a video missing recipe-defined text: {exc}"
            ) from exc

    return ass_paths if ass_paths else None


def _overlay_int(overlay: dict, key: str, default: int) -> int:
    value = overlay.get(key)
    return default if value is None else int(value)


def render_overlays_at_time(
    overlays: list[dict],
    slot_duration_s: float,
    time_in_slot_s: float,
    output_path: str,
) -> None:
    """Render the overlay layer at one moment in time as a transparent PNG.

    Used by the admin editor to show a WYSIWYG preview of the exported video.
    Reuses the same draw helpers as the production pipeline so the preview is
    pixel-identical to the exported overlay layer.

    Behavior:
      - Filters overlays to those visible at time_in_slot_s (start_s <= t < end_s).
      - For ``font-cycle`` overlays, picks the active frame at T using
        _compute_font_cycle_frame_specs and renders only that frame.
      - For ``player-card`` overlays, walks _render_player_card's PNG list and
        picks the frame whose window contains T.
      - For ASS-animated effects (fade-in, typewriter, slide-up), renders the
        steady-state PNG (the same fallback the export uses if ASS fails);
        animation phase is not previewed in v1.
      - For static and span overlays, renders normally.
    Composites all visible overlay PNGs onto a 1080x1920 transparent canvas
    and writes to output_path.
    """
    import tempfile

    from PIL import Image  # noqa: PLC0415

    base = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))

    if not overlays:
        base.save(output_path, "PNG")
        return

    with tempfile.TemporaryDirectory(prefix="overlay_preview_") as tmp_dir:
        for i, overlay in enumerate(overlays):
            try:
                layer_path = _render_single_overlay_at_time(
                    overlay,
                    slot_duration_s,
                    time_in_slot_s,
                    tmp_dir,
                    i,
                )
            except Exception as exc:
                log.warning(
                    "overlay_preview_render_failed",
                    overlay_index=i,
                    font_family=overlay.get("font_family"),
                    font_style=overlay.get("font_style"),
                    effect=overlay.get("effect"),
                    has_spans=bool(overlay.get("spans")),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            if not layer_path or not os.path.exists(layer_path):
                continue
            try:
                with Image.open(layer_path) as layer:
                    layer = layer.convert("RGBA")
                    if layer.size != (CANVAS_W, CANVAS_H):
                        canvas = Image.new(
                            "RGBA",
                            (CANVAS_W, CANVAS_H),
                            (0, 0, 0, 0),
                        )
                        canvas.paste(layer, (0, 0))
                        layer = canvas
                    base = Image.alpha_composite(base, layer)
            except Exception as exc:
                log.warning(
                    "overlay_preview_composite_failed",
                    overlay_index=i,
                    error=str(exc),
                )

    base.save(output_path, "PNG")


def _render_single_overlay_at_time(
    overlay: dict,
    slot_duration_s: float,
    t: float,
    output_dir: str,
    overlay_index: int,
) -> str | None:
    """Render a single overlay at time T into output_dir, return PNG path or None."""
    text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
    has_spans = bool(overlay.get("spans"))
    if text is None and not has_spans:
        return None
    if text is None and has_spans:
        start_s = float(overlay.get("start_s", 0.0))
        end_s = float(overlay.get("end_s", 0.0))
        if end_s > slot_duration_s:
            end_s = slot_duration_s
        if start_s >= end_s:
            return None
        position = overlay.get("position", "center")

    if not (start_s <= t < end_s):
        return None

    effect = overlay.get("effect", "none")
    spans = overlay.get("spans")
    png_path = os.path.join(output_dir, f"overlay_{overlay_index}.png")

    if effect == "font-cycle":
        text_size = overlay.get("text_size", "medium")
        pixel_size = overlay.get("text_size_px") or _FONT_SIZE_MAP.get(text_size, 72)
        font_family = overlay.get("font_family")
        settle_name = font_family or "_default"
        custom_cycle = overlay.get("cycle_fonts")
        cycle_fonts = _resolve_cycle_fonts(
            pixel_size,
            settle_font_name=settle_name,
            custom_fonts=custom_cycle,
        )
        # A 2-font curated cycle (settle + one custom entry) still has just
        # 2 entries — that's intentional. The <2 guard catches degraded
        # registry state (e.g. all fonts failed to load), not curated lists.
        if len(cycle_fonts) < 2:
            return _render_static_overlay_layer(overlay, text, position, png_path)
        accel_at = overlay.get("font_cycle_accel_at_s")
        specs = _compute_font_cycle_frame_specs(start_s, end_s, cycle_fonts, accel_at)
        active_font = _pick_font_cycle_font_at(specs, t)
        if active_font is None:
            return None
        text_color = _hex_to_rgba(overlay.get("text_color", "#FFFFFF"))
        cycling_span_indices: list[int] = []
        if spans:
            cycling_span_indices = [idx for idx, s in enumerate(spans) if not s.get("font_family")]
        _draw_frame(
            overlay,
            spans,
            text,
            position,
            png_path,
            font=active_font,
            text_color=text_color,
            cycling_span_indices=cycling_span_indices,
            position_x_frac=overlay.get("position_x_frac"),
            position_y_frac=overlay.get("position_y_frac"),
        )
        return png_path

    if effect == "player-card":
        sub_dir = os.path.join(output_dir, f"player_card_{overlay_index}")
        os.makedirs(sub_dir, exist_ok=True)
        configs = _render_player_card(
            overlay,
            slot_duration_s,
            sub_dir,
            slot_index=0,
            overlay_index=overlay_index,
        )
        if not configs:
            return None
        for cfg in configs:
            if cfg["start_s"] <= t < cfg["end_s"]:
                return cfg["png_path"]
        return None

    return _render_static_overlay_layer(overlay, text, position, png_path)


def _render_static_overlay_layer(
    overlay: dict,
    text: str | None,
    position: str,
    png_path: str,
) -> str:
    """Static / spans / ASS-animated steady-state PNG render."""
    spans = overlay.get("spans")
    if spans:
        _draw_spans_png(
            overlay,
            spans,
            position,
            png_path,
            outline_px=overlay.get("outline_px"),
        )
        return png_path

    font_family = overlay.get("font_family")
    font_style = overlay.get("font_style", "display")
    text_size = overlay.get("text_size", "medium")
    text_size_px_val = overlay.get("text_size_px")
    text_color = _hex_to_rgba(overlay.get("text_color", "#FFFFFF"))
    _draw_text_png(
        text,
        position,
        png_path,
        font_family=font_family,
        font_style=font_style,
        text_size=text_size,
        text_size_px=text_size_px_val,
        text_color=text_color,
        position_x_frac=overlay.get("position_x_frac"),
        position_y_frac=overlay.get("position_y_frac"),
        text_anchor=overlay.get("text_anchor", "center"),
        stroke_width=int(overlay.get("outline_px") or overlay.get("stroke_width") or 0),
        emoji_prefix=overlay.get("emoji_prefix", ""),
    )
    return png_path


def _pick_font_cycle_font_at(specs: list[tuple], t: float):
    """Return the font whose [start_s, end_s) window contains t, or None."""
    for font, frame_start, frame_end, _kind, _idx in specs:
        if frame_start <= t < frame_end:
            return font
    if specs:
        last = specs[-1]
        if abs(t - last[2]) < 1e-6:
            return last[0]
    return None


# -- ASS Animation Rendering --------------------------------------------------


def _pre_wrap_for_scale_animation(
    text: str,
    font_family: str | None,
    *,
    max_scale_pct: int = 100,
) -> str:
    """Insert ASS line breaks (`\\N`) so libass doesn't re-wrap during scale.

    libass decides line breaks against the *scaled* glyph widths. For effects
    that animate `\\fscx`/`\\fscy` (pop-in, bounce), the wrap point shifts as
    the scale crosses the canvas-width threshold — long text appears on one
    line at \\fscx30 and rewraps to two lines at \\fscx100, which reads as a
    jitter at the entrance. Pre-wrapping against the animation's largest scale
    and setting WrapStyle 2 via the inline `\\q2` tag freezes the layout so
    only glyph size animates.

    Wrap budget mirrors the PNG path's `_TEXT_MAX_LINE_W * CANVAS_W`, divided
    by the largest animation overshoot, so both the settled frame and the
    115%/125% peak stay inside the safe width. If the font cannot be resolved
    (missing entry, registry load failure), returns the original text —
    animation still plays correctly, only the wrap stays at libass's auto-wrap
    default.
    """
    if not text:
        return text
    from PIL import Image, ImageDraw  # noqa: PLC0415

    family = font_family or "Playfair Display"
    font = _resolve_font_family(family, OVERLAY_FONT_SIZE)
    if font is None:
        return text
    scale = max(1.0, max_scale_pct / 100.0)
    max_width = int((CANVAS_W * _TEXT_MAX_LINE_W) / scale)
    dummy = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)
    lines = _wrap_text_to_lines(text, font, max_width, draw)
    return "\\N".join(lines)


def _wrap_and_shrink_for_lyric_line(
    text: str,
    font_family: str | None,
    base_size_px: int,
) -> tuple[str, int]:
    """Wrap with `\\N` at word boundaries + iteratively shrink the font so every
    line fits in `CANVAS_W * _TEXT_MAX_LINE_W` (= 972px on the 1080-wide canvas).

    Mirrors `text_overlay_skia._shrink_to_fit` so the libass `lyric-line` path
    has the same fit-to-screen guarantee as the Skia renderer. Returns
    `(wrapped_text_with_N_breaks, final_px_size)`. The caller emits `\\fs<px>`
    inside the override block when `final_px_size != _LYRIC_LINE_STYLE_FONT_SIZE`.

    Falls back to `(text, base_size_px)` when the font can't be resolved — the
    line still renders, just without measurement-driven layout (the same
    fail-soft contract as `_pre_wrap_for_scale_animation`).
    """
    if not text:
        return text, base_size_px
    from PIL import Image, ImageDraw  # noqa: PLC0415

    family = font_family or "Playfair Display"
    max_width = int(CANVAS_W * _TEXT_MAX_LINE_W)
    dummy = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)

    size = max(_LYRIC_LINE_MIN_FONT_PX, int(base_size_px))
    lines: list[str] = [text]
    for _ in range(_LYRIC_LINE_SHRINK_MAX_ITERS + 1):
        font = _resolve_font_family(family, size)
        if font is None:
            return text, base_size_px
        lines = _wrap_text_to_lines(text, font, max_width, draw)
        widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if widest <= max_width or size <= _LYRIC_LINE_MIN_FONT_PX:
            break
        size = max(_LYRIC_LINE_MIN_FONT_PX, int(size * _LYRIC_LINE_SHRINK_RATIO))

    return "\\N".join(lines), size


def _wrap_karaoke_timed_words_for_ass(
    timed_words: list[dict],
    font_family: str | None,
    base_size_px: int,
) -> list[list[dict]]:
    """Wrap karaoke timing payloads while preserving per-word ASS timing tags."""
    if not timed_words:
        return []

    from PIL import Image, ImageDraw  # noqa: PLC0415

    family = font_family or "Playfair Display"
    font = _resolve_font_family(family, base_size_px)
    if font is None:
        return [timed_words]

    max_width = int(CANVAS_W * _TEXT_MAX_LINE_W)
    dummy = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)

    lines: list[list[dict]] = []
    current: list[dict] = []
    for word in timed_words:
        candidate = [*current, word]
        candidate_text = " ".join(str(w["text"]) for w in candidate)
        bbox = draw.textbbox((0, 0), candidate_text, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current.append(word)
        else:
            lines.append(current)
            current = [word]
    if current:
        lines.append(current)
    return lines


def _emit_lyric_line_alpha_tags(
    section_start_s: float,
    section_end_s: float,
    fade_in_ms: int,
    fade_out_ms: int,
    *,
    fade_out_curve: str | None = None,
) -> str:
    """Emit clamped eased opacity tags for lyric-line ASS events.

    `fade_out_curve="sqrt"` switches the fade-out accel from 2.0 (default
    lingering `1−p²`) to 0.5 (mirror-symmetric `1−√p`). Set by the dynamic
    crossfade scheduler in `lyric_injector.py` for inter-line crossfades
    only; over a matched-duration window paired with the sqrt fade-in on
    the incoming line, α_outgoing + α_incoming = 1 at every t — no
    readable stacked text. See plan §2 / Mirea fix.
    """
    duration_ms = max(0, int(round((section_end_s - section_start_s) * 1000)))
    fade_in = max(0, min(fade_in_ms, duration_ms))
    fade_out = max(0, min(fade_out_ms, max(0, duration_ms - fade_in)))

    tags: list[str] = []
    if fade_in > 0:
        tags.append(r"\alpha&HFF&")
        tags.append(rf"\t(0,{fade_in},0.5,\alpha&H00&)")
    else:
        tags.append(r"\alpha&H00&")

    if fade_out > 0:
        fade_out_start = max(fade_in, duration_ms - fade_out)
        fade_out_accel = "0.5" if fade_out_curve == "sqrt" else "2.0"
        tags.append(rf"\t({fade_out_start},{duration_ms},{fade_out_accel},\alpha&HFF&)")

    return "{" + "".join(tags) + "}"


def _write_animated_ass(
    text: str,
    start_s: float,
    end_s: float,
    position: str,
    effect: str,
    output_path: str,
    *,
    font_family: str | None = None,
    position_x_frac: float | None = None,
    position_y_frac: float | None = None,
    text_anchor: str = "center",
    outline_px: int | None = None,
    # Karaoke-only: per-word timings relative to overlay start, and the
    # highlight color used for "already sung" words. None for non-karaoke
    # effects.
    word_timings: list[dict] | None = None,
    highlight_color: str | None = None,
    text_color: str | None = None,
    # Per-word-pop cumulative-stage hint. When set on a "pop-in" effect, only
    # this trailing suffix of `text` scales in; the prefix renders statically
    # at 100%. Used by the lyrics injector to avoid re-popping the entire
    # accumulated line each time a new word is added. None for any other
    # effect or for non-cumulative pop-in usage (the full text pops as before).
    pop_animated_suffix: str | None = None,
    # Lyric-line only: alpha fade durations in milliseconds. Threaded as
    # scalar kwargs (not via an overlay dict) to match the existing pattern
    # for effect-specific parameters in this function.
    fade_in_ms: int = 150,
    fade_out_ms: int = 250,
    # Lyric-line only: optional fade-out curve override. When set to "sqrt",
    # the libass fade-out accel switches from 2.0 (default `1−p²` lingering)
    # to 0.5 (mirror-symmetric `1−√p`). Set by the dynamic crossfade
    # scheduler in `lyric_injector.py` for inter-line crossfades only — see
    # _emit_lyric_line_alpha_tags and plan §2. None for every non-crossfade
    # case (solo last line, sparse pair, hard-cut, override, kill-switch off).
    fade_out_curve: str | None = None,
    # Lyric-line only: pixel font size override. When set, the dialogue emits
    # `\fs<px>` to override the LyricLine Style's hardcoded Fontsize=90. The
    # wrap+shrink helper then further reduces this size if even the wrapped
    # text would overflow the 90%-canvas safe width. None means "use the
    # Style's Fontsize as-is, and only shrink if needed."
    text_size_px: int | None = None,
) -> None:
    """Write an ASS file with animation tags for the given effect.

    `position_x_frac` (0.0=left edge, 0.5=center, 1.0=right edge) and
    `position_y_frac` (0.0=top, 1.0=bottom) override the position-keyword
    based default anchor. When either is set, libass renders via `\\pos`
    or `\\move` tags at the explicit pixel coordinate instead of relying
    on the `\\an<alignment>` margins. Used by Waka Waka's "This" / "is"
    overlays which sit at lower-left / lower-right (off-center).
    `text_anchor` selects the horizontal side of explicit coordinates.
    """
    alignment, margin_v = _ASS_POSITION.get(position, (5, 0))
    start_str = format_ass_time(start_s)
    end_str = format_ass_time(end_s)

    # Resolve horizontal anchor — explicit x_frac wins, otherwise center.
    if position_x_frac is not None:
        target_x = int(CANVAS_W * max(0.0, min(1.0, position_x_frac)))
    else:
        target_x = CANVAS_W // 2

    # Custom Y position override via \pos tag. When either x_frac or y_frac
    # is explicit, emit a \pos so libass anchors there. y_frac alone uses the
    # canvas-center x. x_frac alone resolves y from the position-keyword.
    pos_tag = ""
    if position_y_frac is not None or position_x_frac is not None:
        y_frac_for_pos = (
            position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
        )
        target_y = int(CANVAS_H * y_frac_for_pos)
        pos_tag = f"\\pos({target_x},{target_y})"
    explicit_alignment = _ass_explicit_pos_alignment(text_anchor)
    pos_or_align = f"\\an{explicit_alignment}{pos_tag}" if pos_tag else f"\\an{alignment}"

    # Optional black outline override via \bord + \3c (outline colour) tags
    outline_tag = ""
    if outline_px and outline_px > 0:
        outline_tag = f"\\bord{outline_px}\\3c&H000000&"

    if effect == "fade-in":
        dialogue_text = f"{{{pos_or_align}{outline_tag}\\fad(500,0)}}{text}"

    elif effect == "typewriter":
        total_dur_cs = int((end_s - start_s) * 100)
        char_count = max(len(text), 1)
        per_char_cs = max(1, total_dur_cs // char_count)
        parts = [f"{{{pos_or_align}{outline_tag}}}"]
        for ch in text:
            parts.append(f"{{\\k{per_char_cs}}}{ch}")
        dialogue_text = "".join(parts)

    elif effect == "slide-up":
        y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
        target_y = int(CANVAS_H * y_frac)
        start_y = CANVAS_H + 100
        dialogue_text = (
            f"{{\\an{explicit_alignment}"
            f"\\move({target_x},{start_y},{target_x},{target_y},0,500)"
            f"{outline_tag}}}{text}"
        )

    elif effect == "slide-down":
        # Mirror of slide-up: text falls in from above the visible canvas down
        # to its target y. Used for Waka Waka's "This is" opening title where
        # the user described it as "yukarıdan aşağı gelen" (coming top-to-bottom).
        y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
        target_y = int(CANVAS_H * y_frac)
        start_y = -200  # 200 px above the top edge of the 1920 canvas
        dialogue_text = (
            f"{{\\an{explicit_alignment}"
            f"\\move({target_x},{start_y},{target_x},{target_y},0,500)"
            f"{outline_tag}}}{text}"
        )

    elif effect == "pop-in":
        # Snap-scale from 30% to 115% (overshoot) then settle to 100%. The libass
        # \t(t1,t2,tags) tag linearly interpolates the inner tags between t1 and
        # t2 (ms relative to dialogue start). Initial \fscx/\fscy sets the t=0
        # state; each \t() animates to its target.
        # Pre-wrap with \N + \q2: libass re-lays out lines per frame based on the
        # scaled glyph widths, so without a fixed line structure the text jumps
        # from 1 line at \fscx30 to 2 lines once \fscx crosses the wrap threshold.
        # See test_pop_in_prewraps_long_text_into_fixed_lines for the regression.
        duration_ms = int((end_s - start_s) * 1000)
        k0, k1, k2 = _clamp_keyframes(_POP_IN_KEYFRAMES_MS, duration_ms)
        s0, s1, s2 = _POP_IN_SCALES
        scale_anim = (
            f"\\fscx{s0}\\fscy{s0}"
            f"\\t({k0},{k1},\\fscx{s1}\\fscy{s1})"
            f"\\t({k1},{k2},\\fscx{s2}\\fscy{s2})"
        )

        # Cumulative-stage path (used by the lyrics per-word-pop injector):
        # the suffix is the newly added word, the prefix is the accumulated
        # line up to but not including it. Pre-wrap the full phrase before
        # splicing so the static prefix and animated suffix share the same
        # fixed layout under \q2; otherwise long cumulative lyric lines clip
        # off-screen instead of wrapping.
        suffix_stripped = (pop_animated_suffix or "").strip()
        if suffix_stripped and text.rstrip().endswith(suffix_stripped):
            stripped_text = text.rstrip()
            wrapped_text = _pre_wrap_for_scale_animation(
                stripped_text,
                font_family,
                max_scale_pct=max(_POP_IN_SCALES),
            ).rstrip()
            # Trim the suffix from the right; whatever's left (minus its
            # trailing whitespace) is the static prefix. If text is "I got
            # room" and suffix is "room", prefix becomes "I got" and the
            # rendered dialogue reads "I got <animated>room</animated>".
            prefix_source = (
                wrapped_text if wrapped_text.endswith(suffix_stripped) else stripped_text
            )
            prefix_text = prefix_source[: -len(suffix_stripped)].rstrip()
            if prefix_text:
                separator = "" if prefix_text.endswith(r"\N") else " "
                dialogue_text = (
                    f"{{{pos_or_align}\\q2{outline_tag}}}"
                    f"{prefix_text}{separator}"
                    f"{{{scale_anim}}}{suffix_stripped}"
                )
            else:
                # First stage of the line — only the suffix exists. Animate it
                # the same way the legacy single-word pop did.
                dialogue_text = f"{{{pos_or_align}\\q2{outline_tag}{scale_anim}}}{suffix_stripped}"
        else:
            wrapped = _pre_wrap_for_scale_animation(
                text,
                font_family,
                max_scale_pct=max(_POP_IN_SCALES),
            )
            dialogue_text = f"{{{pos_or_align}\\q2{outline_tag}{scale_anim}}}{wrapped}"

    elif effect == "karaoke-line":
        # Sing-along: the full line is visible for the overlay duration. Each
        # word is wrapped in ASS karaoke timing tags `{\kt<start>\kf<duration>}`,
        # which fade the word's fill from SecondaryColour to PrimaryColour
        # over the word's duration. `\kt` anchors the word to the real
        # overlay-relative vocal onset; without it, lyric alignment gaps get
        # flattened into the following word and the highlight drifts. The
        # result is a smooth left-to-right color sweep that follows the vocal.
        # `\kf` (fill) is preferred over `\k` (sharp swap) because it looks
        # less jarring at typical sub-second word durations.
        timings = word_timings or []
        base_size = int(text_size_px) if text_size_px else OVERLAY_FONT_SIZE
        fs_tag = f"\\fs{base_size}" if base_size != OVERLAY_FONT_SIZE else ""
        if not timings:
            # No per-word data — fall back to a simple no-animation render so
            # the line at least appears on screen. The caller already logs
            # this as a degraded path.
            font = _resolve_font_family(font_family or "Playfair Display", base_size)
            if font is not None:
                from PIL import Image, ImageDraw  # noqa: PLC0415

                dummy = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
                draw = ImageDraw.Draw(dummy)
                lines = _wrap_text_to_lines(text, font, int(CANVAS_W * _TEXT_MAX_LINE_W), draw)
                text = "\\N".join(lines)
            dialogue_text = f"{{{pos_or_align}\\q2{outline_tag}{fs_tag}}}{text}"
        else:
            # ASS color encoding is &HBBGGRR& (note byte order). With \kf,
            # SecondaryColour is the unsung baseline and PrimaryColour is the
            # active/sung highlight after the fill completes.
            primary_bgr = _hex_to_ass_bgr(highlight_color or "#FFFF00")
            secondary_bgr = _hex_to_ass_bgr(text_color or "#FFFFFF")
            parts = [
                f"{{{pos_or_align}\\q2{outline_tag}{fs_tag}"
                f"\\1c&H{primary_bgr}&\\2c&H{secondary_bgr}&}}"
            ]
            # `\kt` / `\kf` payloads are in centiseconds. We clamp each word
            # to at least 5cs (50ms) so karaoke never emits a zero-length
            # token which libass renders as an immediate primary-color flash.
            cursor_s = 0.0
            timed_words: list[dict] = []
            for w in timings:
                word_text = str(w.get("text", "")).strip()
                if not word_text:
                    continue
                start_word_s = max(0.0, _finite_float(w.get("start_s"), cursor_s))
                fallback_dur_s = max(
                    0.05, _finite_float(w.get("duration_cs"), 30.0) / 100.0
                )
                end_word_s = _finite_float(w.get("end_s"), start_word_s + fallback_dur_s)
                dur_s = (
                    end_word_s - start_word_s
                    if end_word_s > start_word_s
                    else fallback_dur_s
                )
                dur_cs = max(5, int(round(dur_s * 100.0)))
                start_cs = max(0, int(round(start_word_s * 100.0)))
                timed_words.append({"text": word_text, "start_cs": start_cs, "dur_cs": dur_cs})
                cursor_s = max(cursor_s, start_word_s + dur_cs / 100.0)
            wrapped_words = _wrap_karaoke_timed_words_for_ass(timed_words, font_family, base_size)
            for line_idx, line in enumerate(wrapped_words):
                if line_idx > 0:
                    parts.append(r"\N")
                for word in line:
                    parts.append(
                        f"{{\\kt{word['start_cs']}\\kf{word['dur_cs']}}}{word['text']} "
                    )
            dialogue_text = "".join(parts).rstrip()

    elif effect == "lyric-line":
        # YouTube-lyric-video style: one static line of plain text with a
        # thin outline, hard libass offset shadow, and eased opacity transforms.
        # The transforms use libass's accel exponent, not cubic-bezier curves,
        # and the shadow is hard-edged rather than blurred.
        color_tag = ""
        if text_color:
            bgr = _hex_to_ass_bgr(text_color)
            color_tag = f"\\1c&H{bgr}&"
        alpha_tags = _emit_lyric_line_alpha_tags(
            start_s, end_s, fade_in_ms, fade_out_ms, fade_out_curve=fade_out_curve
        )

        # Wrap + shrink to keep lyrics inside the 90%-canvas safe width.
        # libass uses \q2 below (explicit \N breaks only), so we MUST insert
        # \N ourselves — otherwise long lines render as a single row that
        # overflows the 1080px frame. Shrinking the font via \fs is the
        # second line of defence when even the wrapped longest line is wider
        # than the safe budget.
        base_size = int(text_size_px) if text_size_px else _LYRIC_LINE_STYLE_FONT_SIZE
        wrapped_text, fit_size = _wrap_and_shrink_for_lyric_line(text, font_family, base_size)
        fs_tag = f"\\fs{fit_size}" if fit_size != _LYRIC_LINE_STYLE_FONT_SIZE else ""

        inline = alpha_tags[:-1] + pos_or_align + r"\q2" + fs_tag + outline_tag + color_tag + "}"
        dialogue_text = f"{inline}{wrapped_text}"

    elif effect == "bounce":
        # Squash-and-stretch: 100 → 125 (stretch) → 90 (squash) → 100 (settle).
        # Used for hero reactions like the "shukran Morocco!" outro label.
        # Pre-wrap with \N + \q2: same rationale as pop-in — fixed line layout
        # so the squash/stretch doesn't drag libass through a wrap threshold.
        wrapped = _pre_wrap_for_scale_animation(
            text,
            font_family,
            max_scale_pct=max(_BOUNCE_SCALES),
        )
        duration_ms = int((end_s - start_s) * 1000)
        k0, k1, k2, k3 = _clamp_keyframes(_BOUNCE_KEYFRAMES_MS, duration_ms)
        s0, s1, s2, s3 = _BOUNCE_SCALES
        dialogue_text = (
            f"{{{pos_or_align}\\q2{outline_tag}"
            f"\\fscx{s0}\\fscy{s0}"
            f"\\t({k0},{k1},\\fscx{s1}\\fscy{s1})"
            f"\\t({k1},{k2},\\fscx{s2}\\fscy{s2})"
            f"\\t({k2},{k3},\\fscx{s3}\\fscy{s3})"
            f"}}{wrapped}"
        )

    else:
        dialogue_text = f"{{{pos_or_align}{outline_tag}}}{text}"

    style_name = "LyricLine" if effect == "lyric-line" else "Overlay"

    # Use dynamic ASS header when font_family is set
    if font_family:
        ass_name = _registry_ass_name(font_family)
        if effect == "lyric-line":
            header = _build_ass_header(
                ass_name,
                style_name=style_name,
                bold=_registry_ass_bold(font_family),
                outline="1.5",
                shadow="2",
                outline_colour="&H00000000",
                back_colour="&H99000000",
            )
        else:
            header = _build_ass_header(ass_name)
    elif effect == "lyric-line":
        header = _build_ass_header(
            "Playfair Display",
            style_name=style_name,
            bold=0,
            outline="1.5",
            shadow="2",
            outline_colour="&H00000000",
            back_colour="&H99000000",
        )
    else:
        header = _ASS_OVERLAY_HEADER

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        f.write(
            f"Dialogue: 0,{start_str},{end_str},{style_name},,0,0,{margin_v},,{dialogue_text}\n"
        )


def _validate_ass_file(path: str) -> bool:
    """Basic validation that the ASS file has required sections."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        return "[Script Info]" in content and "[V4+ Styles]" in content and "[Events]" in content
    except Exception:
        return False


# -- Font-cycle rendering -----------------------------------------------------


def _draw_frame(
    overlay: dict,
    spans: list[dict] | None,
    text: str | None,
    position: str,
    png_path: str,
    *,
    font=None,
    text_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    font_family: str | None = None,
    font_style: str = "display",
    text_size: str = "medium",
    cycling_span_indices: list[int] | None = None,
    position_x_frac: float | None = None,
    position_y_frac: float | None = None,
) -> None:
    """Dispatch to spans or flat text rendering. Used by font-cycle to avoid repetition."""
    if spans:
        overrides = (
            {idx: font for idx in cycling_span_indices} if font and cycling_span_indices else None
        )
        _draw_spans_png(
            overlay,
            spans,
            position,
            png_path,
            font_overrides=overrides,
            position_y_frac=position_y_frac,
            outline_px=overlay.get("outline_px"),
        )
    else:
        _draw_text_png(
            text,
            position,
            png_path,
            font=font,
            font_family=font_family,
            font_style=font_style,
            text_size=text_size,
            text_color=text_color,
            position_x_frac=position_x_frac,
            position_y_frac=position_y_frac,
            text_anchor=overlay.get("text_anchor", "center"),
            stroke_width=int(overlay.get("outline_px") or overlay.get("stroke_width") or 0),
            emoji_prefix=overlay.get("emoji_prefix", ""),
        )


# ── Player-card overlay (effect="player-card") ─────────────────────────────
# Renders a translucent player highlight overlay over an existing slot:
#   - Giant kit number (default Outfit Bold @ ~720px) centered on canvas,
#     white with thick black outline + drop-shadow.
#   - Italic-styled red serif player name (Fraunces Bold sheared) overlapping
#     the lower middle of the number, large (~160px).
# The PNG is transparent except for the number+name; the slot's footage shows
# through, matching the "Rayan Cherki / 10" beat in the reference TikTok.

PLAYER_CARD_NUMBER_PX = 720
PLAYER_CARD_NAME_PX = 200  # bumped — Pacifico script needs more size to read
PLAYER_CARD_NAME_COLOR = "#C81E32"  # crimson red
PLAYER_CARD_NUMBER_COLOR = "#FFFFFF"
PLAYER_CARD_NUMBER_OPACITY = 0.85  # slight transparency so footage bleeds through
# Animation timing (1.0s window): fade-in + hold + fade-out
PLAYER_CARD_FADE_IN_S = 0.15
PLAYER_CARD_FADE_OUT_S = 0.15
PLAYER_CARD_FADE_STEPS = 4  # smoothness per fade direction


def _render_player_card(
    overlay: dict,
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
    overlay_index: int,
) -> list[dict] | None:
    """Render the player-highlight card overlay with fade-in/hold/fade-out.

    Returns a list of {png_path, start_s, end_s} time-windowed PNGs that the
    caller composites in sequence. The card always animates as a SINGLE unit:
    kit number + script name share the exact same alpha across all frames so
    the viewer reads them as one element, not two staggered text bursts.

    Reads ``jersey_no`` and ``player_name`` from the overlay dict; both must
    be non-empty or the overlay is silently skipped.
    """
    from PIL import Image  # noqa: PLC0415

    jersey_no = str(overlay.get("jersey_no") or "").strip()
    player_name = str(overlay.get("player_name") or "").strip()
    if not jersey_no or not player_name:
        log.info(
            "player_card_skipped_missing_fields",
            slot=slot_index,
            has_no=bool(jersey_no),
            has_name=bool(player_name),
        )
        return None

    start_s = float(overlay.get("start_s", 0.0))
    end_s = float(overlay.get("end_s", min(slot_duration_s, start_s + 1.0)))
    end_s = min(end_s, slot_duration_s)
    if start_s >= end_s:
        return None

    duration = end_s - start_s
    fade_in = min(PLAYER_CARD_FADE_IN_S, duration / 3.0)
    fade_out = min(PLAYER_CARD_FADE_OUT_S, duration / 3.0)
    hold_start = start_s + fade_in
    hold_end = end_s - fade_out

    # ── Build the master player-card frame (alpha=1.0) ──────────────────
    master = _draw_player_card_master(jersey_no, player_name)

    # ── Multi-frame fade: render N stepped alphas for in + hold + out ──
    configs: list[dict] = []
    steps = max(1, PLAYER_CARD_FADE_STEPS)

    def _emit(label: str, alpha: float, t0: float, t1: float) -> None:
        if t1 <= t0:
            return
        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_player_card_{overlay_index}_{label}.png",
        )
        if alpha >= 0.999:
            master.save(png_path, "PNG")
        else:
            faded = Image.eval(master.split()[3], lambda v: int(v * alpha))
            framed = master.copy()
            framed.putalpha(faded)
            framed.save(png_path, "PNG")
        configs.append({"png_path": png_path, "start_s": t0, "end_s": t1})

    # fade-in stepped frames
    if fade_in > 0:
        step_dur = fade_in / steps
        for i in range(steps):
            alpha = (i + 1) / steps  # 0.25, 0.50, 0.75, 1.00 for steps=4
            t0 = start_s + i * step_dur
            t1 = start_s + (i + 1) * step_dur
            _emit(f"in{i}", alpha, t0, t1)
    # hold (full opacity)
    _emit("hold", 1.0, hold_start, hold_end)
    # fade-out stepped frames
    if fade_out > 0:
        step_dur = fade_out / steps
        for i in range(steps):
            alpha = 1.0 - ((i + 1) / steps)  # 0.75, 0.50, 0.25, 0.00
            t0 = hold_end + i * step_dur
            t1 = hold_end + (i + 1) * step_dur
            if alpha > 0.01:  # skip the alpha=0 frame, hard cut off instead
                _emit(f"out{i}", alpha, t0, t1)

    log.info(
        "player_card_rendered",
        slot=slot_index,
        jersey_no=jersey_no,
        player_name=player_name,
        start_s=start_s,
        end_s=end_s,
        frames=len(configs),
    )
    return configs


def _draw_player_card_master(jersey_no: str, player_name: str):
    """Build the canonical player-card frame (alpha=1.0) on a 1080x1920 canvas.

    Layout:
      • Giant kit number (Outfit Bold) centered, white with thick black outline
        and gaussian drop-shadow.
      • Script-style player name (Pacifico, no shear — already cursive)
        overlapping the lower portion of the number, crimson red with thin
        outline + soft shadow for legibility.
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))

    # ── Giant kit number ────────────────────────────────────────────────
    number_font_path = (
        _registry_font_path("Outfit") or _registry_font_path("Montserrat") or MONTSERRAT_FONT_PATH
    )
    try:
        number_font = ImageFont.truetype(number_font_path, PLAYER_CARD_NUMBER_PX)
    except OSError:
        number_font = ImageFont.load_default()

    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    n_bbox = measure.textbbox((0, 0), jersey_no, font=number_font)
    n_w = n_bbox[2] - n_bbox[0]
    n_h = n_bbox[3] - n_bbox[1]
    n_x = (CANVAS_W - n_w) // 2 - n_bbox[0]
    n_y = (CANVAS_H - n_h) // 2 - n_bbox[1]

    shadow = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((n_x, n_y + 12), jersey_no, font=number_font, fill=(0, 0, 0, 200))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=24))
    img = Image.alpha_composite(img, shadow)

    number_alpha = int(round(255 * PLAYER_CARD_NUMBER_OPACITY))
    number_color = _hex_to_rgba(PLAYER_CARD_NUMBER_COLOR)[:3] + (number_alpha,)
    fg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    ImageDraw.Draw(fg).text(
        (n_x, n_y),
        jersey_no,
        font=number_font,
        fill=number_color,
        stroke_width=12,
        stroke_fill=(0, 0, 0, 230),
    )
    img = Image.alpha_composite(img, fg)

    # ── Script-style player name overlapping the lower portion of the "10" ──
    # Pacifico is a built-in cursive script — no italic shear needed; matches
    # the reference TikTok's signature-style "Rayan Cherki" overlay.
    name_font_path = (
        _registry_font_path("Pacifico")
        or _registry_font_path("Permanent Marker")
        or os.path.join(_ASSETS_DIR, "Pacifico-Regular.ttf")
    )
    # Auto-shrink the script font so the rendered name fits within ~88% of canvas
    # width (Pacifico has wide glyphs; "Rayan Cherki" at 200px exceeds 1080px).
    name_size = PLAYER_CARD_NAME_PX
    max_name_width = int(CANVAS_W * 0.88)
    while name_size > 80:
        try:
            test_font = ImageFont.truetype(name_font_path, name_size)
        except OSError:
            test_font = ImageFont.load_default()
            break
        bbox = measure.textbbox((0, 0), player_name, font=test_font)
        if (bbox[2] - bbox[0]) <= max_name_width:
            break
        name_size -= 8
    try:
        name_font = ImageFont.truetype(name_font_path, name_size)
    except OSError:
        name_font = ImageFont.load_default()

    name_color = _hex_to_rgba(PLAYER_CARD_NAME_COLOR)
    nm_bbox = measure.textbbox((0, 0), player_name, font=name_font)
    base_w = nm_bbox[2] - nm_bbox[0]
    base_h = nm_bbox[3] - nm_bbox[1]
    pad = 32
    name_layer = Image.new("RGBA", (base_w + pad * 2, base_h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(name_layer).text(
        (pad - nm_bbox[0], pad - nm_bbox[1]),
        player_name,
        font=name_font,
        fill=name_color,
        stroke_width=4,
        stroke_fill=(0, 0, 0, 220),
    )

    name_x = (CANVAS_W - name_layer.width) // 2
    name_y = n_y + int(n_h * 0.62) - name_layer.height // 2
    overlay_canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    overlay_canvas.alpha_composite(name_layer, (name_x, name_y))

    name_shadow = overlay_canvas.filter(ImageFilter.GaussianBlur(radius=10))
    img = Image.alpha_composite(img, name_shadow)
    img = Image.alpha_composite(img, overlay_canvas)

    return img


def _compute_font_cycle_frame_specs(
    start_s: float,
    end_s: float,
    cycle_fonts: list,
    accel_at: float | None,
) -> list[tuple]:
    """Pure computation of font-cycle frame specs.

    Returns (font, frame_start_s, frame_end_s, kind, frame_idx) tuples where
    kind is "cycle", "gapfill", or "settle". Production path adds output paths
    and renders all frames; the admin preview path picks the spec whose
    [frame_start_s, frame_end_s) window contains time T and renders just
    that one. Sharing this math keeps the preview's font selection identical
    to the exported video.
    """
    if not cycle_fonts:
        return []

    duration = end_s - start_s
    if accel_at is not None:
        settle_start = end_s
        cycle_end = end_s
    else:
        settle_start = start_s + duration * (1.0 - FONT_CYCLE_SETTLE_RATIO)
        cycle_end = settle_start

    specs: list[tuple] = []
    frame_idx = 0
    t = start_s
    font_idx = 0

    while t < cycle_end and frame_idx < MAX_FONT_CYCLE_FRAMES:
        interval = FONT_CYCLE_INTERVAL_S
        if accel_at is not None and t >= accel_at:
            interval = FONT_CYCLE_FAST_INTERVAL_S
        frame_end = min(t + interval, cycle_end)
        font = cycle_fonts[font_idx % len(cycle_fonts)]
        specs.append((font, t, frame_end, "cycle", frame_idx))
        t = frame_end
        frame_idx += 1
        font_idx += 1

    if t < cycle_end - 0.001:
        last_font = cycle_fonts[(font_idx - 1) % len(cycle_fonts)]
        specs.append((last_font, t, cycle_end, "gapfill", frame_idx))

    if settle_start < end_s:
        primary_font = cycle_fonts[0]
        specs.append((primary_font, settle_start, end_s, "settle", -1))

    return specs


def _render_font_cycle(
    overlay: dict,
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
    overlay_index: int,
) -> list[dict]:
    """Render a font-cycling overlay: multiple PNGs, one per font frame.

    The text stays centered but the font rapidly switches between contrasting
    styles (bold, serif, script, condensed). The last ~30% of the duration
    "settles" on the primary font.

    When ``font_family`` is set on the overlay, that font becomes the settle
    font (index 0). Otherwise, falls back to Playfair Display Bold.

    Supports acceleration: if ``font_cycle_accel_at_s`` is set in the overlay
    dict, the cycling interval drops to FONT_CYCLE_FAST_INTERVAL_S after that
    absolute timestamp (used to speed up during curtain-close).

    Spans-aware: when ``spans`` is present, spans WITH explicit ``font_family``
    stay fixed during cycling; spans WITHOUT ``font_family`` get the cycling font.
    Baseline is locked to max ascender across ALL cycling fonts to prevent jitter.

    Returns a list of {png_path, start_s, end_s} configs for FFmpeg overlay.
    """
    text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
    spans = overlay.get("spans")
    if text is None and not spans:
        return []

    hex_color = overlay.get("text_color", "#FFFFFF")
    text_color = _hex_to_rgba(hex_color)
    text_size = overlay.get("text_size", "medium")
    pixel_size = overlay.get("text_size_px") or _FONT_SIZE_MAP.get(text_size, 72)
    font_family = overlay.get("font_family")
    y_override = overlay.get("position_y_frac")
    x_override = overlay.get("position_x_frac")

    # Resolve available cycle fonts at the requested size, keyed by settle
    # font and any per-overlay custom font list.
    settle_name = font_family or "_default"
    custom_cycle = overlay.get("cycle_fonts")
    cycle_fonts = _resolve_cycle_fonts(
        pixel_size,
        settle_font_name=settle_name,
        custom_fonts=custom_cycle,
    )
    if len(cycle_fonts) < 2:
        # Not enough fonts for cycling -- fall back to static
        log.warning("font_cycle_insufficient_fonts", count=len(cycle_fonts))
        png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{overlay_index}.png")
        _draw_frame(
            overlay,
            spans,
            text,
            position,
            png_path,
            font_family=font_family,
            font_style=overlay.get("font_style", "display"),
            text_size=text_size,
            text_color=text_color,
            position_x_frac=x_override,
            position_y_frac=y_override,
        )
        return [{"png_path": png_path, "start_s": start_s, "end_s": end_s}]

    # Acceleration: switch to faster interval after this absolute timestamp
    accel_at = overlay.get("font_cycle_accel_at_s")

    # Determine which spans participate in cycling (no explicit font_family)
    cycling_span_indices: list[int] = []
    if spans:
        cycling_span_indices = [idx for idx, s in enumerate(spans) if not s.get("font_family")]

    # Pure spec computation lives in _compute_font_cycle_frame_specs so the
    # admin preview endpoint can pick the active frame at time T using
    # exactly the same math.
    pure_specs = _compute_font_cycle_frame_specs(start_s, end_s, cycle_fonts, accel_at)
    frame_specs: list[tuple] = []  # (png_path, font, start_s, end_s)
    for font, frame_start, frame_end, kind, idx in pure_specs:
        if kind == "settle":
            png_path = os.path.join(
                output_dir,
                f"slot_{slot_index}_fontcycle_{overlay_index}_settle.png",
            )
        elif kind == "gapfill":
            png_path = os.path.join(
                output_dir,
                f"slot_{slot_index}_fontcycle_{overlay_index}_{idx}_gapfill.png",
            )
        else:
            png_path = os.path.join(
                output_dir,
                f"slot_{slot_index}_fontcycle_{overlay_index}_{idx}.png",
            )
        frame_specs.append((png_path, font, frame_start, frame_end))

    duration = end_s - start_s

    # ── Render all frames in parallel (Pillow releases GIL) ───────────
    def _render_one(spec: tuple) -> dict:
        png_p, fnt, s, e = spec
        _draw_frame(
            overlay,
            spans,
            text,
            position,
            png_p,
            font=fnt,
            text_color=text_color,
            cycling_span_indices=cycling_span_indices,
            position_x_frac=x_override,
            position_y_frac=y_override,
        )
        return {"png_path": png_p, "start_s": s, "end_s": e}

    if len(frame_specs) > 1:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_render_one, frame_specs))
    else:
        results = [_render_one(s) for s in frame_specs]

    log.info(
        "font_cycle_rendered",
        slot=slot_index,
        text=text[:20] if text else "(spans)",
        frames=len(results),
        fonts_available=len(cycle_fonts),
        duration=round(duration, 2),
        accelerated=accel_at is not None,
        has_spans=bool(spans),
    )

    return results


# -- Shared helpers -----------------------------------------------------------


def _validate_overlay(
    overlay: dict,
    slot_duration_s: float,
) -> tuple[str | None, float, float, str]:
    """Validate and sanitize overlay fields. Returns (text, start_s, end_s, position).

    Returns (None, 0, 0, "") if the overlay should be skipped.
    When spans are present, skip text truncation (validate per-span instead).

    Prefers `display_text` when set (Layer 2 line-style finalization writes
    this for partial lyric lines it truncated to the audible-word substring).
    Falls back to `text` for every other case — byte-identical to pre-PR for
    non-music callers. See plans/geli-me-var-ama-hatalar-robust-reddy.md §2c.
    """
    text = overlay.get("display_text") or overlay.get("text", "")
    start_s = float(overlay.get("start_s", 0.0))
    end_s = float(overlay.get("end_s", 0.0))
    position = overlay.get("position", "center")
    has_spans = bool(overlay.get("spans"))

    if start_s >= end_s:
        return None, 0.0, 0.0, ""

    if end_s > slot_duration_s:
        end_s = slot_duration_s
    if start_s >= end_s:
        return None, 0.0, 0.0, ""

    text = sanitize_ass_text(text)
    if not text and not has_spans:
        return None, 0.0, 0.0, ""
    # Skip truncation when spans are present — each span is short individually
    if not has_spans and len(text) > MAX_OVERLAY_TEXT_LEN:
        text = text[: MAX_OVERLAY_TEXT_LEN - 1] + "\u2026"

    return text, start_s, end_s, position


def _resolve_font_family(font_family: str, size: int):
    """Load a font by font_family name from the registry.

    For variable fonts, sets the weight axis to the registry's declared weight
    so the render matches the CSS font-weight in the admin preview.

    Returns a Pillow ImageFont, or None if the font_family is not found.
    """
    from PIL import ImageFont  # noqa: PLC0415

    entry = _FONT_REGISTRY.get("fonts", {}).get(font_family)
    if not entry:
        return None
    path = os.path.join(FONTS_DIR, entry["file"])
    if not os.path.exists(path):
        return None
    try:
        font = ImageFont.truetype(path, size)
        # Variable fonts default to their lightest weight. Set the weight
        # axis to match the registry's declared weight for WYSIWYG fidelity.
        try:
            axes = font.get_variation_axes()
            if axes:
                weight = entry.get("weight", 400)
                font.set_variation_by_axes([weight])
        except (AttributeError, OSError):
            pass  # Static font or old Pillow — no axis to set
        return font
    except OSError:
        log.warning("font_family_load_failed", font_family=font_family, path=path)
    return None


def _load_styled_font(
    font_style: str = "display",
    text_size: str = "medium",
    text_size_px: int | None = None,
):
    """Load a font matching the requested style and size.

    font_style: "serif", "serif_italic", "script", "sans", "display"
    text_size:  "small" (48), "medium" (72), "large" (120), "xlarge" (150)
    text_size_px: exact pixel override (takes priority over text_size name)
    """
    from PIL import ImageFont  # noqa: PLC0415

    size = text_size_px or _FONT_SIZE_MAP.get(text_size, 72)
    candidates = _FONT_STYLE_MAP.get(font_style, _FONT_STYLE_MAP["sans"])

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    # Last resort — no macOS system fonts, just use Pillow default
    return ImageFont.load_default()


def _load_font(font_path: str, size: int = OVERLAY_FONT_SIZE):
    """Load a font from path with fallback chain."""
    from PIL import ImageFont  # noqa: PLC0415

    try:
        return ImageFont.truetype(font_path, size)
    except OSError:
        pass

    return ImageFont.load_default()


# Maximum text width as fraction of canvas width (matches _SPAN_MAX_LINE_W).
# Text wider than this is wrapped to multiple lines, then if a SINGLE word is
# still too wide the font is shrunk to fit.
_TEXT_MAX_LINE_W = 0.9
# Line spacing as multiplier of max line height for multi-line wrapping.
_TEXT_LINE_SPACING = 1.15


def _wrap_text_to_lines(text: str, font, max_width: int, draw) -> list[str]:
    """Greedy word-wrap: returns list of lines that each fit within max_width.

    If a single word exceeds max_width on its own (e.g. very long URL),
    it stays on its own line — caller should shrink the font.
    """
    words = text.split()
    if not words:
        return [text]

    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word]) if current else word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def measure_text_width(
    text: str,
    *,
    font=None,
    font_family: str | None = None,
    font_style: str = "display",
    text_size: str = "medium",
    text_size_px: int | None = None,
) -> int:
    """Return the rendered pixel width of `text` for the chosen font.

    Pure measurement: resolves the same font that `_draw_text_png` would, then
    asks Pillow for the textbbox. Used by Stage G of the Layer-2 pipeline to
    decide whether a cumulative reveal line will fit within `_TEXT_MAX_LINE_W`
    before emitting overlays — if not, the line is split at the overflow word
    into a new LineGroup that starts at its own transcript timestamp.

    Returns 0 for empty text. Returns 0 if the font cannot be resolved (caller
    treats that as "don't split" — failing open is safer than failing closed
    here, since the renderer still has its own shrink-to-fit safety net).
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    if not text:
        return 0
    if font is None:
        size = text_size_px or _FONT_SIZE_MAP.get(text_size, 72)
        if font_family:
            font = _resolve_font_family(font_family, size)
        if font is None:
            font = _load_styled_font(font_style, text_size, text_size_px=text_size_px)
    if font is None:
        return 0
    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def wrap_and_measure(
    text: str,
    *,
    font_family: str | None = None,
    font_style: str = "display",
    text_size: str = "medium",
    text_size_px: int | None = None,
    max_width_px: int | None = None,
) -> tuple[list[str], int]:
    """Wrap `text` and return (lines, widest_line_px) for the resolved font.

    Single source of truth for "does this text fit?" used by the universal
    constraint pass (`app.pipeline.overlay_constraints`). It resolves the same
    font `_draw_text_png` would, wraps with the same greedy `_wrap_text_to_lines`
    both renderers mirror (Pillow `_wrap_text_to_lines` / Skia
    `_wrap_text_to_lines`), and measures with Pillow. Pillow metrics are the
    conservative reference: the constraint pass adds a small safety margin on top
    so a fit it approves is also safe under Skia's HarfBuzz shaping.

    `max_width_px` defaults to `_TEXT_MAX_LINE_W * CANVAS_W` (the wrap budget both
    renderers use). Returns ([text], 0) when the font can't be resolved — the
    caller treats width 0 as "don't shrink" (the renderer keeps its own
    shrink-to-fit safety net).
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    if not text:
        return ([text], 0)
    size = text_size_px or _FONT_SIZE_MAP.get(text_size, 72)
    font = None
    if font_family:
        font = _resolve_font_family(font_family, size)
    if font is None:
        font = _load_styled_font(font_style, text_size, text_size_px=size)
    if font is None:
        return ([text], 0)

    width_budget = int(max_width_px if max_width_px is not None else _TEXT_MAX_LINE_W * CANVAS_W)
    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        lines.extend(_wrap_text_to_lines(raw_line, font, width_budget, draw) if raw_line else [""])
    widest = 0
    for line in lines:
        if not line:
            continue
        bbox = draw.textbbox((0, 0), line, font=font)
        widest = max(widest, bbox[2] - bbox[0])
    return (lines, widest)


def _draw_text_png(
    text: str,
    position: str,
    png_path: str,
    *,
    font=None,
    font_family: str | None = None,
    font_style: str = "display",
    text_size: str = "medium",
    text_size_px: int | None = None,
    text_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    position_x_frac: float | None = None,
    position_y_frac: float | None = None,
    text_anchor: str = "center",
    stroke_width: int = 0,
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 230),
    emoji_prefix: str = "",
) -> None:
    """Draw styled text on a transparent 1080x1920 canvas.

    Font resolution priority:
      1. ``font`` — pre-loaded ImageFont (used by font-cycle, skip resolution)
      2. ``font_family`` — look up in registry by name
      3. ``font_style`` — legacy style-based lookup

    Wraps long text by words to fit within 90% of canvas width. If a single
    word still exceeds that width (rare — long URL, no spaces), shrinks the
    font enough to make it fit.

    `text_anchor` controls the horizontal alignment of each rendered line
    against `position_x_frac` (default: canvas center):
      - "center" — line is centered on the anchor x (historical default).
      - "left"   — line's LEFT edge is at the anchor x. Used by progressive
                   word-reveal overlays so adding a word grows the line to the
                   right without shifting earlier words leftward.
      - "right"  — line's RIGHT edge is at the anchor x.
    `position_x_frac` semantics change with `text_anchor`: for "center" it is
    the centerline; for "left"/"right" it is the corresponding edge.

    Two compositing layers: a soft drop shadow for depth, and an optional
    crisp black stroke (Pillow's stroke_width) for the TikTok caption look.
    Stroke is opt-in — old behaviour (shadow only) when stroke_width=0.
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Resolve font: pre-loaded > font_family > font_style
    # Track whether we own font resolution — only then do we shrink on overflow.
    # Pre-loaded fonts come from font-cycle which renders many frames with
    # different fonts at the same size; shrinking would make text jump.
    own_font = font is None
    if font is None:
        size = text_size_px or _FONT_SIZE_MAP.get(text_size, 72)
        if font_family:
            font = _resolve_font_family(font_family, size)
        if font is None:
            font = _load_styled_font(font_style, text_size, text_size_px=text_size_px)
    else:
        size = getattr(font, "size", text_size_px or _FONT_SIZE_MAP.get(text_size, 72))

    max_width = int(CANVAS_W * _TEXT_MAX_LINE_W)
    lines = _wrap_text_to_lines(text, font, max_width, draw)

    # Safety: if a single word can't be word-wrapped to fit (no spaces, very
    # long URL), shrink the font until the widest line fits. Caps iterations
    # to avoid pathological inputs blowing render time. Skip when we don't
    # own the font (font-cycle case: caller is responsible for sizing).
    if own_font:

        def _max_line_w(ls: list[str]) -> int:
            widest = 0
            for ln in ls:
                b = draw.textbbox((0, 0), ln, font=font)
                widest = max(widest, b[2] - b[0])
            return widest

        iterations = 0
        while _max_line_w(lines) > max_width and iterations < 6 and size > 24:
            size = int(size * 0.85)
            if font_family:
                new_font = _resolve_font_family(font_family, size)
            else:
                new_font = _load_styled_font(font_style, "medium", text_size_px=size)
            if new_font is None:
                break
            font = new_font
            lines = _wrap_text_to_lines(text, font, max_width, draw)
            iterations += 1

    # Compute per-line metrics + total block height for vertical centering.
    line_heights: list[int] = []
    line_widths: list[int] = []
    for ln in lines:
        b = draw.textbbox((0, 0), ln, font=font)
        line_widths.append(b[2] - b[0])
        line_heights.append(b[3] - b[1])
    # Use the font's INTRINSIC line metrics (ascent + descent) for line_step
    # when left-anchored. Per-text bbox heights vary with line content — line 2
    # of a cumulative reveal goes "you" → "you put" → "you put in", each with a
    # slightly different bbox height because the measured descender depth /
    # ascender height changes with glyph mix. That feeds back into
    # `max(line_heights) * 1.15` and shifts line 2's y position by a few px per
    # cumulative stage, producing the "'you' drifts down when 'put' is added"
    # visual on template 89cde014. Font metrics (`ascent + descent`) are a
    # constant per-font-size so the cumulative reveal lines stay locked.
    # Center-anchor mode keeps the historical bbox-based line_step — changing
    # it could regress existing centered templates and the drift isn't visible
    # there (the block center stays at position_y_frac).
    if text_anchor == "left" and line_heights:
        try:
            ascent, descent = font.getmetrics()
            line_step = int((ascent + descent) * _TEXT_LINE_SPACING)
        except AttributeError:
            # Bitmap fonts / default fonts may not expose getmetrics; fall back.
            line_step = int(max(line_heights) * _TEXT_LINE_SPACING)
    else:
        line_step = int(max(line_heights) * _TEXT_LINE_SPACING) if line_heights else 0
    block_h = (
        sum(line_heights[:-1])
        + (line_step - max(line_heights)) * (len(lines) - 1)
        + line_heights[-1]
        if lines
        else 0
    )
    # Simpler/predictable: stack each line at top + i*line_step.
    block_h = line_step * (len(lines) - 1) + (line_heights[-1] if line_heights else 0)

    y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
    # Vertical anchor semantics mirror horizontal:
    #   text_anchor="left"   → position_y_frac is the TOP of the first line.
    #                          Subsequent wrapped lines extend DOWNWARD, so a
    #                          cumulative reveal that wraps from 1 line to 2
    #                          doesn't shift the first line upward.
    #   text_anchor="center" → position_y_frac is the vertical CENTER of the
    #                          block (historical default — preserved).
    # Without the left-branch, a progressive reveal that wraps mid-sequence
    # moves the earlier (already-visible) line upward — visually wrong since
    # the eye expects revealed text to stay anchored as new words append.
    if text_anchor == "left":
        block_top = int(CANVAS_H * y_frac)
    else:
        block_top = int(CANVAS_H * y_frac - block_h / 2)

    # Soft gaussian shadow layer (drawn once, all lines).
    shadow_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    fg_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    fg_draw = ImageDraw.Draw(fg_layer)

    # Pre-compute emoji metrics so we can offset the FIRST line's x position
    # to keep emoji + line 1 visually centered as one block. Without this
    # the emoji gets pasted to the left of an already-centered text and
    # overflows the canvas edge.
    emoji_metrics: dict | None = None
    if emoji_prefix and lines:
        emoji_path = _emoji_png_path(emoji_prefix)
        if emoji_path:
            try:
                emoji_img = Image.open(emoji_path).convert("RGBA")
                first_line_h = line_heights[0]
                emoji_size = max(24, int(first_line_h * 0.95))
                gap = max(8, emoji_size // 6)
                # Combined width: emoji + gap + line 1 text. If this exceeds
                # 95% canvas width, shrink the emoji proportionally so it
                # still fits without cropping.
                combined = emoji_size + gap + line_widths[0]
                max_combined = int(CANVAS_W * 0.95)
                if combined > max_combined:
                    overshoot = combined - max_combined
                    new_emoji_size = max(20, emoji_size - overshoot)
                    if new_emoji_size != emoji_size:
                        emoji_size = new_emoji_size
                        gap = max(6, emoji_size // 6)
                        combined = emoji_size + gap + line_widths[0]
                emoji_img = emoji_img.resize((emoji_size, emoji_size), Image.Resampling.LANCZOS)
                emoji_metrics = {
                    "img": emoji_img,
                    "size": emoji_size,
                    "gap": gap,
                    "combined_w": combined,
                }
            except Exception as exc:
                log.warning("emoji_load_failed", emoji=emoji_prefix, error=str(exc))
        else:
            log.warning(
                "emoji_asset_missing",
                emoji=emoji_prefix,
                codepoint=_emoji_codepoint(emoji_prefix),
            )

    # Resolve horizontal anchor: when position_x_frac is set, lines center on
    # that x pixel instead of canvas-center. Used by Waka Waka "This"/"is"
    # which sit at lower-left / lower-right (x_frac=0.18 / 0.82).
    if position_x_frac is not None:
        anchor_x = int(CANVAS_W * max(0.0, min(1.0, position_x_frac)))
    else:
        anchor_x = CANVAS_W // 2

    # PIL's default text anchor ('la') positions y at the font's ascent line,
    # but the ACTUAL rendered top of a string varies with which glyphs appear
    # (top of "you" sits at x-height; top of "you put in" sits at ascender
    # height because of the 't' / 'i'). Across cumulative reveal stages where
    # a new word like "put" enters line 2, the line's measured top shifts by
    # 10-20 px every stage — visually the line "drifts" up or down as words
    # are added. Anchor 'ls' (left, baseline) pins each line by its baseline,
    # which is a font-intrinsic reference — stable regardless of glyph mix.
    # Locked by `test_left_anchor_line_step_stable_across_cumulative_stages`.
    try:
        font_ascent, _ = font.getmetrics()
    except AttributeError:
        font_ascent = 0  # bitmap/default font — anchor handling falls back to default
    use_baseline_anchor = font_ascent > 0

    for i, ln in enumerate(lines):
        if i == 0 and emoji_metrics:
            # Position emoji + line 1 as a single unit. For center anchor we
            # center the combined block; for left/right anchor we align the
            # block's edge to anchor_x just like a plain line.
            combined_w = emoji_metrics["combined_w"]
            if text_anchor == "left":
                combined_x = max(0, anchor_x)
            elif text_anchor == "right":
                combined_x = max(0, anchor_x - combined_w)
            else:
                combined_x = max(0, anchor_x - combined_w // 2)
            x = combined_x + emoji_metrics["size"] + emoji_metrics["gap"]
        else:
            if text_anchor == "left":
                x = anchor_x
            elif text_anchor == "right":
                x = anchor_x - line_widths[i]
            else:
                x = anchor_x - line_widths[i] // 2
        # Baseline-anchored y: line i's baseline sits at block_top + ascent +
        # i * line_step. Anchor 'ls' tells PIL to interpret (x, y) as the
        # left-baseline anchor point. Without `use_baseline_anchor` we fall
        # back to the historical top-anchored draw — bitmap fonts don't expose
        # `getmetrics`, so this is the safe-by-default path.
        if use_baseline_anchor:
            y = block_top + font_ascent + i * line_step
            text_anchor_arg = "ls"
            shadow_xy = (x, y + 6)
            fg_xy = (x, y)
        else:
            y = block_top + i * line_step
            text_anchor_arg = None  # PIL default
            shadow_xy = (x, y + 6)
            fg_xy = (x, y)
        # Soft drop shadow (always on — gives depth on busy backgrounds)
        if text_anchor_arg:
            shadow_draw.text(shadow_xy, ln, font=font, fill=(0, 0, 0, 160), anchor=text_anchor_arg)
        else:
            shadow_draw.text(shadow_xy, ln, font=font, fill=(0, 0, 0, 160))
        # Foreground: optional crisp stroke (TikTok caption look) + fill.
        # Pillow renders stroke + fill in a single pass when stroke_width > 0,
        # so glyph anti-aliasing stays clean.
        if stroke_width > 0:
            if text_anchor_arg:
                fg_draw.text(
                    fg_xy,
                    ln,
                    font=font,
                    fill=text_color,
                    stroke_width=stroke_width,
                    stroke_fill=stroke_color,
                    anchor=text_anchor_arg,
                )
            else:
                fg_draw.text(
                    fg_xy,
                    ln,
                    font=font,
                    fill=text_color,
                    stroke_width=stroke_width,
                    stroke_fill=stroke_color,
                )
        else:
            if text_anchor_arg:
                fg_draw.text(fg_xy, ln, font=font, fill=text_color, anchor=text_anchor_arg)
            else:
                fg_draw.text(fg_xy, ln, font=font, fill=text_color)

    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=12))
    img = Image.alpha_composite(img, shadow_layer)
    img = Image.alpha_composite(img, fg_layer)

    # Paste emoji to the left of line 1's NEW (combined-centered) position.
    if emoji_metrics:
        first_line_h = line_heights[0]
        combined_w = emoji_metrics["combined_w"]
        combined_x = max(0, anchor_x - combined_w // 2)
        emoji_x = combined_x
        emoji_y = block_top + (first_line_h - emoji_metrics["size"]) // 2
        img.alpha_composite(emoji_metrics["img"], dest=(emoji_x, emoji_y))

    img.save(png_path, "PNG")


# -- Span-aware rendering ----------------------------------------------------

# Maximum line width as fraction of canvas width
_SPAN_MAX_LINE_W = 0.9
# Gap between words within a line
_SPAN_WORD_GAP = 16
# Line spacing (fraction of max line height)
_SPAN_LINE_SPACING = 1.3


def _resolve_span_font(span: dict, overlay: dict):
    """Resolve a Pillow font for a single span.

    Priority: span.font_family → overlay.font_family → overlay.font_style fallback.
    """
    size_key = span.get("text_size") or overlay.get("text_size", "medium")
    pixel_size = _FONT_SIZE_MAP.get(size_key, 72)

    # 1. Span-level font_family
    span_family = span.get("font_family")
    if span_family:
        font = _resolve_font_family(span_family, pixel_size)
        if font:
            return font

    # 2. Overlay-level font_family
    overlay_family = overlay.get("font_family")
    if overlay_family:
        font = _resolve_font_family(overlay_family, pixel_size)
        if font:
            return font

    # 3. Overlay font_style fallback
    font_style = overlay.get("font_style", "display")
    return _load_styled_font(font_style, size_key)


def _draw_spans_png(
    overlay: dict,
    spans: list[dict],
    position: str,
    png_path: str,
    *,
    font_overrides: dict[int, object] | None = None,
    position_y_frac: float | None = None,
    outline_px: int | None = None,
) -> None:
    """Draw multiple text spans with per-word font/color/size on a 1080x1920 canvas.

    Each span can have its own font_family, text_color, text_size.
    Spans are laid out left-to-right, wrapped to new lines when exceeding
    90% of canvas width, baseline-aligned per line, and horizontally centered.

    font_overrides: optional dict mapping span index → pre-loaded Pillow font.
    Used by font-cycle to override specific spans with the cycling font.
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    if not spans:
        return

    max_line_w = int(CANVAS_W * _SPAN_MAX_LINE_W)

    # Shared measurement surface (avoid per-span allocation)
    _measure_img = Image.new("RGBA", (1, 1))
    _measure_draw = ImageDraw.Draw(_measure_img)

    # 1. Resolve font, color, and measure each span
    span_data = []
    for idx, span in enumerate(spans):
        text = sanitize_ass_text(span.get("text", ""))
        if not text:
            continue
        # Per-span length cap (prevent unbounded Pillow measurement)
        if len(text) > MAX_OVERLAY_TEXT_LEN:
            text = text[: MAX_OVERLAY_TEXT_LEN - 1] + "\u2026"

        # Font: override (from font-cycle) > span-level > overlay-level
        if font_overrides and idx in font_overrides:
            font = font_overrides[idx]
        else:
            font = _resolve_span_font(span, overlay)

        hex_color = span.get("text_color") or overlay.get("text_color", "#FFFFFF")
        color = _hex_to_rgba(hex_color)

        # Measure
        bbox = _measure_draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        ascender = -bbox[1]  # bbox[1] is negative for ascent
        h = bbox[3] - bbox[1]

        # Overflow fallback: if a single span is wider than canvas, scale down
        if w > max_line_w:
            scale = max_line_w / w
            size_key = span.get("text_size") or overlay.get("text_size", "medium")
            pixel_size = _FONT_SIZE_MAP.get(size_key, 72)
            new_size = max(24, int(pixel_size * scale))
            # Try to scale the current font (preserves font_overrides from cycling)
            scaled = None
            try:
                scaled = font.font_variant(size=new_size)
            except (AttributeError, OSError):
                pass
            if scaled:
                font = scaled
            else:
                # Re-resolve at smaller size from registry
                span_family = span.get("font_family") or overlay.get("font_family")
                if span_family:
                    font = _resolve_font_family(span_family, new_size) or _load_styled_font(
                        overlay.get("font_style", "display"), "small"
                    )
                else:
                    font = _load_styled_font(overlay.get("font_style", "display"), "small")
            bbox = _measure_draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]
            ascender = -bbox[1]
            h = bbox[3] - bbox[1]

        span_data.append(
            {
                "text": text,
                "font": font,
                "color": color,
                "w": w,
                "h": h,
                "ascender": ascender,
            }
        )

    if not span_data:
        return

    # 2. Line wrapping — break between spans when line exceeds max width
    lines: list[list[dict]] = [[]]
    line_w = 0
    for sd in span_data:
        gap = _SPAN_WORD_GAP if lines[-1] else 0
        if lines[-1] and line_w + gap + sd["w"] > max_line_w:
            lines.append([])
            line_w = 0
            gap = 0
        lines[-1].append(sd)
        line_w += gap + sd["w"]

    # 3. Compute per-line metrics (max ascender for baseline alignment)
    line_metrics = []
    for line in lines:
        max_asc = max(sd["ascender"] for sd in line)
        max_h = max(sd["h"] for sd in line)
        total_w = sum(sd["w"] for sd in line) + _SPAN_WORD_GAP * (len(line) - 1)
        line_metrics.append({"max_asc": max_asc, "max_h": max_h, "total_w": total_w})

    # 4. Total block height
    total_block_h = sum(int(lm["max_h"] * _SPAN_LINE_SPACING) for lm in line_metrics)

    # 5. Vertical anchor
    y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
    block_top = int(CANVAS_H * y_frac - total_block_h / 2)

    # 6. Render
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    shadow_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    fg_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    fg_draw = ImageDraw.Draw(fg_layer)

    cur_y = block_top
    for line, lm in zip(lines, line_metrics):
        # Horizontal centering
        line_x = (CANVAS_W - lm["total_w"]) // 2
        x = line_x

        for sd in line:
            # Baseline alignment: offset from top of line by (max_ascender - this span's ascender)
            baseline_offset = lm["max_asc"] - sd["ascender"]
            draw_y = cur_y + baseline_offset

            # Shadow
            shadow_draw.text((x, draw_y + 6), sd["text"], font=sd["font"], fill=(0, 0, 0, 160))
            # Foreground — optional black stroke when overlay sets outline_px
            if outline_px and outline_px > 0:
                fg_draw.text(
                    (x, draw_y),
                    sd["text"],
                    font=sd["font"],
                    fill=sd["color"],
                    stroke_width=outline_px,
                    stroke_fill=(0, 0, 0, 255),
                )
            else:
                fg_draw.text((x, draw_y), sd["text"], font=sd["font"], fill=sd["color"])

            x += sd["w"] + _SPAN_WORD_GAP

        cur_y += int(lm["max_h"] * _SPAN_LINE_SPACING)

    # Apply shadow blur
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=12))
    img = Image.alpha_composite(img, shadow_layer)
    img = Image.alpha_composite(img, fg_layer)
    img.save(png_path, "PNG")


# Cache key is (size, settle_font_name, custom_fonts_key) to avoid
# cross-overlay pollution. custom_fonts_key is a tuple of font names (or
# None) so two overlays with different curated cycle lists don't share
# entries.
_cycle_fonts_cache: dict[tuple, list] = {}


def _reset_cycle_cache() -> None:
    """Reset the font-cycle cache. For testing only."""
    global _cycle_fonts_cache
    _cycle_fonts_cache = {}


def _resolve_cycle_fonts(
    size: int = OVERLAY_FONT_SIZE,
    settle_font_name: str = "_default",
    custom_fonts: list[str] | None = None,
) -> list:
    """Resolve available fonts for cycling at the given pixel size.

    The settle font (index 0) is determined by settle_font_name:
    - "_default" -> Playfair Display Bold (classic behavior)
    - Any other value -> looked up in the registry by font_family name

    If `custom_fonts` is provided, the cycle uses ONLY those font names
    (looked up in the registry) — the global cycle_role="contrast" set is
    bypassed entirely. This lets a single overlay opt into a tight cycle
    (e.g. Waka Waka AFRICA wants Montserrat-only) without affecting other
    templates that rely on the default cross-category cycle.

    If `custom_fonts` is None, the cycle is settle + every registry font
    with cycle_role="contrast" (the existing Dimples Passport behavior).

    Cached per (size, settle_font_name, custom_fonts tuple) so different
    overlays get the right cycle list.
    """
    global _cycle_fonts_cache
    custom_key = tuple(custom_fonts) if custom_fonts else None
    cache_key = (size, settle_font_name, custom_key)
    if cache_key in _cycle_fonts_cache:
        return _cycle_fonts_cache[cache_key]

    fonts = []

    # Index 0: settle font
    if settle_font_name != "_default":
        settle = _resolve_font_family(settle_font_name, size)
        if settle:
            fonts.append(settle)
    if not fonts:
        # Default settle: Playfair Display Bold
        settle = _load_font(OVERLAY_FONT_PATH, size)
        fonts.append(settle)

    # Cycle bodies: either the curated per-overlay list or the global
    # cycle_role=contrast set from the registry.
    cycle_names = custom_fonts if custom_fonts else _CYCLE_CONTRAST_NAMES
    for name in cycle_names:
        font = _resolve_font_family(name, size)
        if font:
            fonts.append(font)
        else:
            # Fallback: Montserrat is always available as a static font
            fonts.append(_load_font(MONTSERRAT_FONT_PATH, size))

    _cycle_fonts_cache[cache_key] = fonts
    log.info(
        "font_cycle_fonts_resolved",
        count=len(fonts),
        size=size,
        settle=settle_font_name,
        custom=custom_key,
    )
    return fonts
