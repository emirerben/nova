"""Generate text overlay PNGs and animated ASS files for TikTok-style text.

Two rendering paths:
  - Static effects + font-cycle -> PNG images composited via FFmpeg overlay filter
  - Animated effects (fade-in, typewriter, slide-up) -> ASS subtitle files
    burned via FFmpeg subtitles filter

Supports effects:
  - Static effects (scale-up, none): single PNG for the duration
  - font-cycle: rapid font switching -- generates one PNG per font frame, each
    with a different font, swapped via timed FFmpeg overlays (~7 changes/sec)
  - Animated effects (fade-in, typewriter, slide-up, pop-in, bounce): ASS subtitle files

Fonts loaded from font-registry.json (shared with frontend).
Fallback: Pillow default if all resolution fails.
"""

import json
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
    {"fade-in", "typewriter", "slide-up", "pop-in", "bounce"}
)

# Animation keyframe timings (ms from overlay start). At runtime, each timestamp
# is clamped to a fraction of the overlay's actual duration so very short
# overlays don't get a truncated animation tail (e.g. a 200ms label with a
# 500ms bounce would otherwise just freeze mid-bounce).
_POP_IN_KEYFRAMES_MS = (0, 150, 250)        # scale 30 -> 115 -> 100
_POP_IN_SCALES = (30, 115, 100)
_BOUNCE_KEYFRAMES_MS = (0, 180, 360, 500)   # scale 100 -> 125 -> 90 -> 100
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


# -- Font size mapping --------------------------------------------------------

_FONT_SIZE_MAP = {
    "small": 36, "medium": 72, "large": 120,
    "xlarge": 150, "xxlarge": 250, "jumbo": 199,
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


# Position -> ASS alignment + MarginV
# ASS alignment: 2=bottom-center, 5=center, 8=top-center
_ASS_POSITION = {
    "center": (5, 0),
    "center-above": (5, 150),
    "center-below": (5, -100),
    "top": (8, 200),
    "bottom": (2, 200),
}


def _build_ass_header(fontname: str = "Playfair Display") -> str:
    """Build ASS header with dynamic font name."""
    return f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Overlay,{fontname},90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H40000000,-1,0,0,0,100,100,0,0,1,0,2,5,50,50,0,1
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
                overlay, slot_duration_s, output_dir, slot_index, i,
            )
            results.extend(configs)
        elif effect == "player-card":
            cfg = _render_player_card(
                overlay, slot_duration_s, output_dir, slot_index, i,
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
            _draw_spans_png(overlay, spans, position, png_path,
                            outline_px=overlay.get("outline_px"))
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
                text, position, png_path,
                font_family=font_family, font_style=font_style,
                text_size=text_size, text_size_px=text_size_px_val,
                text_color=text_color,
                position_y_frac=overlay.get("position_y_frac"),
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
                text, start_s, end_s, position, effect, ass_path,
                font_family=font_family,
                position_y_frac=overlay.get("position_y_frac"),
                outline_px=overlay.get("outline_px"),
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
                    overlay, slot_duration_s, time_in_slot_s, tmp_dir, i,
                )
            except Exception as exc:
                log.warning(
                    "overlay_preview_render_failed",
                    overlay_index=i,
                    error=str(exc),
                )
                continue
            if not layer_path or not os.path.exists(layer_path):
                continue
            try:
                with Image.open(layer_path) as layer:
                    layer = layer.convert("RGBA")
                    if layer.size != (CANVAS_W, CANVAS_H):
                        canvas = Image.new(
                            "RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0),
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
        cycle_fonts = _resolve_cycle_fonts(pixel_size, settle_font_name=settle_name)
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
            cycling_span_indices = [
                idx for idx, s in enumerate(spans) if not s.get("font_family")
            ]
        _draw_frame(
            overlay, spans, text, position, png_path,
            font=active_font, text_color=text_color,
            cycling_span_indices=cycling_span_indices,
            position_y_frac=overlay.get("position_y_frac"),
        )
        return png_path

    if effect == "player-card":
        sub_dir = os.path.join(output_dir, f"player_card_{overlay_index}")
        os.makedirs(sub_dir, exist_ok=True)
        configs = _render_player_card(
            overlay, slot_duration_s, sub_dir, slot_index=0,
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
            overlay, spans, position, png_path,
            outline_px=overlay.get("outline_px"),
        )
        return png_path

    font_family = overlay.get("font_family")
    font_style = overlay.get("font_style", "display")
    text_size = overlay.get("text_size", "medium")
    text_size_px_val = overlay.get("text_size_px")
    text_color = _hex_to_rgba(overlay.get("text_color", "#FFFFFF"))
    _draw_text_png(
        text, position, png_path,
        font_family=font_family, font_style=font_style,
        text_size=text_size, text_size_px=text_size_px_val,
        text_color=text_color,
        position_y_frac=overlay.get("position_y_frac"),
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


def _write_animated_ass(
    text: str,
    start_s: float,
    end_s: float,
    position: str,
    effect: str,
    output_path: str,
    *,
    font_family: str | None = None,
    position_y_frac: float | None = None,
    outline_px: int | None = None,
) -> None:
    """Write an ASS file with animation tags for the given effect."""
    alignment, margin_v = _ASS_POSITION.get(position, (5, 0))
    start_str = format_ass_time(start_s)
    end_str = format_ass_time(end_s)

    # Custom Y position override via \pos tag
    pos_tag = ""
    if position_y_frac is not None:
        target_y = int(CANVAS_H * position_y_frac)
        pos_tag = f"\\pos({CANVAS_W // 2},{target_y})"

    # Optional black outline override via \bord + \3c (outline colour) tags
    outline_tag = ""
    if outline_px and outline_px > 0:
        outline_tag = f"\\bord{outline_px}\\3c&H000000&"

    if effect == "fade-in":
        if pos_tag:
            dialogue_text = f"{{\\an5{pos_tag}{outline_tag}\\fad(500,0)}}{text}"
        else:
            dialogue_text = f"{{\\an{alignment}{outline_tag}\\fad(500,0)}}{text}"

    elif effect == "typewriter":
        total_dur_cs = int((end_s - start_s) * 100)
        char_count = max(len(text), 1)
        per_char_cs = max(1, total_dur_cs // char_count)
        parts = [f"{{\\an{alignment}{outline_tag}}}"]
        for ch in text:
            parts.append(f"{{\\k{per_char_cs}}}{ch}")
        dialogue_text = "".join(parts)

    elif effect == "slide-up":
        y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
        target_y = int(CANVAS_H * y_frac)
        start_y = CANVAS_H + 100
        x = CANVAS_W // 2
        dialogue_text = (
            f"{{\\an5\\move({x},{start_y},{x},{target_y},0,500){outline_tag}}}{text}"
        )

    elif effect == "pop-in":
        # Snap-scale from 30% to 115% (overshoot) then settle to 100%. The libass
        # \t(t1,t2,tags) tag linearly interpolates the inner tags between t1 and
        # t2 (ms relative to dialogue start). Initial \fscx/\fscy sets the t=0
        # state; each \t() animates to its target.
        duration_ms = int((end_s - start_s) * 1000)
        k0, k1, k2 = _clamp_keyframes(_POP_IN_KEYFRAMES_MS, duration_ms)
        s0, s1, s2 = _POP_IN_SCALES
        pos_or_align = f"\\an5{pos_tag}" if pos_tag else f"\\an{alignment}"
        dialogue_text = (
            f"{{{pos_or_align}{outline_tag}"
            f"\\fscx{s0}\\fscy{s0}"
            f"\\t({k0},{k1},\\fscx{s1}\\fscy{s1})"
            f"\\t({k1},{k2},\\fscx{s2}\\fscy{s2})"
            f"}}{text}"
        )

    elif effect == "bounce":
        # Squash-and-stretch: 100 → 125 (stretch) → 90 (squash) → 100 (settle).
        # Used for hero reactions like the "shukran Morocco!" outro label.
        duration_ms = int((end_s - start_s) * 1000)
        k0, k1, k2, k3 = _clamp_keyframes(_BOUNCE_KEYFRAMES_MS, duration_ms)
        s0, s1, s2, s3 = _BOUNCE_SCALES
        pos_or_align = f"\\an5{pos_tag}" if pos_tag else f"\\an{alignment}"
        dialogue_text = (
            f"{{{pos_or_align}{outline_tag}"
            f"\\fscx{s0}\\fscy{s0}"
            f"\\t({k0},{k1},\\fscx{s1}\\fscy{s1})"
            f"\\t({k1},{k2},\\fscx{s2}\\fscy{s2})"
            f"\\t({k2},{k3},\\fscx{s3}\\fscy{s3})"
            f"}}{text}"
        )

    else:
        dialogue_text = (
            f"{{\\an5{pos_tag}{outline_tag}}}{text}"
            if pos_tag
            else f"{{\\an{alignment}{outline_tag}}}{text}"
        )

    # Use dynamic ASS header when font_family is set
    if font_family:
        ass_name = _registry_ass_name(font_family)
        header = _build_ass_header(ass_name)
    else:
        header = _ASS_OVERLAY_HEADER

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n[Events]\n")
        f.write(
            "Format: Layer, Start, End, Style, Name, "
            "MarginL, MarginR, MarginV, Effect, Text\n"
        )
        f.write(
            f"Dialogue: 0,{start_str},{end_str},Overlay,,"
            f"0,0,{margin_v},,{dialogue_text}\n"
        )


def _validate_ass_file(path: str) -> bool:
    """Basic validation that the ASS file has required sections."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        return (
            "[Script Info]" in content
            and "[V4+ Styles]" in content
            and "[Events]" in content
        )
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
    position_y_frac: float | None = None,
) -> None:
    """Dispatch to spans or flat text rendering. Used by font-cycle to avoid repetition."""
    if spans:
        overrides = (
            {idx: font for idx in cycling_span_indices}
            if font and cycling_span_indices
            else None
        )
        _draw_spans_png(
            overlay, spans, position, png_path,
            font_overrides=overrides, position_y_frac=position_y_frac,
            outline_px=overlay.get("outline_px"),
        )
    else:
        _draw_text_png(
            text, position, png_path,
            font=font, font_family=font_family, font_style=font_style,
            text_size=text_size, text_color=text_color,
            position_y_frac=position_y_frac,
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
PLAYER_CARD_NAME_PX = 200            # bumped — Pacifico script needs more size to read
PLAYER_CARD_NAME_COLOR = "#C81E32"   # crimson red
PLAYER_CARD_NUMBER_COLOR = "#FFFFFF"
PLAYER_CARD_NUMBER_OPACITY = 0.85    # slight transparency so footage bleeds through
# Animation timing (1.0s window): fade-in + hold + fade-out
PLAYER_CARD_FADE_IN_S = 0.15
PLAYER_CARD_FADE_OUT_S = 0.15
PLAYER_CARD_FADE_STEPS = 4           # smoothness per fade direction


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
        slot=slot_index, jersey_no=jersey_no, player_name=player_name,
        start_s=start_s, end_s=end_s, frames=len(configs),
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
        _registry_font_path("Outfit")
        or _registry_font_path("Montserrat")
        or MONTSERRAT_FONT_PATH
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
    ImageDraw.Draw(shadow).text(
        (n_x, n_y + 12), jersey_no, font=number_font, fill=(0, 0, 0, 200)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=24))
    img = Image.alpha_composite(img, shadow)

    number_alpha = int(round(255 * PLAYER_CARD_NUMBER_OPACITY))
    number_color = _hex_to_rgba(PLAYER_CARD_NUMBER_COLOR)[:3] + (number_alpha,)
    fg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    ImageDraw.Draw(fg).text(
        (n_x, n_y), jersey_no, font=number_font,
        fill=number_color, stroke_width=12, stroke_fill=(0, 0, 0, 230),
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
    name_layer = Image.new(
        "RGBA", (base_w + pad * 2, base_h + pad * 2), (0, 0, 0, 0)
    )
    ImageDraw.Draw(name_layer).text(
        (pad - nm_bbox[0], pad - nm_bbox[1]),
        player_name, font=name_font, fill=name_color,
        stroke_width=4, stroke_fill=(0, 0, 0, 220),
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

    # Resolve available cycle fonts at the requested size, keyed by settle font
    settle_name = font_family or "_default"
    cycle_fonts = _resolve_cycle_fonts(pixel_size, settle_font_name=settle_name)
    if len(cycle_fonts) < 2:
        # Not enough fonts for cycling -- fall back to static
        log.warning("font_cycle_insufficient_fonts", count=len(cycle_fonts))
        png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{overlay_index}.png")
        _draw_frame(
            overlay, spans, text, position, png_path,
            font_family=font_family, font_style=overlay.get("font_style", "display"),
            text_size=text_size, text_color=text_color,
            position_y_frac=y_override,
        )
        return [{"png_path": png_path, "start_s": start_s, "end_s": end_s}]

    # Acceleration: switch to faster interval after this absolute timestamp
    accel_at = overlay.get("font_cycle_accel_at_s")

    # Determine which spans participate in cycling (no explicit font_family)
    cycling_span_indices: list[int] = []
    if spans:
        cycling_span_indices = [
            idx for idx, s in enumerate(spans) if not s.get("font_family")
        ]

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
            overlay, spans, text, position, png_p,
            font=fnt, text_color=text_color,
            cycling_span_indices=cycling_span_indices,
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
    overlay: dict, slot_duration_s: float,
) -> tuple[str | None, float, float, str]:
    """Validate and sanitize overlay fields. Returns (text, start_s, end_s, position).

    Returns (None, 0, 0, "") if the overlay should be skipped.
    When spans are present, skip text truncation (validate per-span instead).
    """
    text = overlay.get("text", "")
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
    position_y_frac: float | None = None,
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
                emoji_img = emoji_img.resize(
                    (emoji_size, emoji_size), Image.Resampling.LANCZOS
                )
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

    for i, ln in enumerate(lines):
        if i == 0 and emoji_metrics:
            # Center emoji + line 1 together as a single unit
            combined_w = emoji_metrics["combined_w"]
            combined_x = max(0, (CANVAS_W - combined_w) // 2)
            x = combined_x + emoji_metrics["size"] + emoji_metrics["gap"]
        else:
            x = (CANVAS_W - line_widths[i]) // 2
        y = block_top + i * line_step
        # Soft drop shadow (always on — gives depth on busy backgrounds)
        shadow_draw.text((x, y + 6), ln, font=font, fill=(0, 0, 0, 160))
        # Foreground: optional crisp stroke (TikTok caption look) + fill.
        # Pillow renders stroke + fill in a single pass when stroke_width > 0,
        # so glyph anti-aliasing stays clean.
        if stroke_width > 0:
            fg_draw.text(
                (x, y), ln, font=font, fill=text_color,
                stroke_width=stroke_width, stroke_fill=stroke_color,
            )
        else:
            fg_draw.text((x, y), ln, font=font, fill=text_color)

    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=12))
    img = Image.alpha_composite(img, shadow_layer)
    img = Image.alpha_composite(img, fg_layer)

    # Paste emoji to the left of line 1's NEW (combined-centered) position.
    if emoji_metrics:
        first_line_h = line_heights[0]
        combined_w = emoji_metrics["combined_w"]
        combined_x = max(0, (CANVAS_W - combined_w) // 2)
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

        span_data.append({
            "text": text,
            "font": font,
            "color": color,
            "w": w,
            "h": h,
            "ascender": ascender,
        })

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
                fg_draw.text((x, draw_y), sd["text"], font=sd["font"], fill=sd["color"],
                             stroke_width=outline_px, stroke_fill=(0, 0, 0, 255))
            else:
                fg_draw.text((x, draw_y), sd["text"], font=sd["font"], fill=sd["color"])

            x += sd["w"] + _SPAN_WORD_GAP

        cur_y += int(lm["max_h"] * _SPAN_LINE_SPACING)

    # Apply shadow blur
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=12))
    img = Image.alpha_composite(img, shadow_layer)
    img = Image.alpha_composite(img, fg_layer)
    img.save(png_path, "PNG")


# Cache key is (size, settle_font_name) to avoid cross-overlay pollution
_cycle_fonts_cache: dict[tuple[int, str], list] = {}


def _reset_cycle_cache() -> None:
    """Reset the font-cycle cache. For testing only."""
    global _cycle_fonts_cache
    _cycle_fonts_cache = {}


def _resolve_cycle_fonts(
    size: int = OVERLAY_FONT_SIZE,
    settle_font_name: str = "_default",
) -> list:
    """Resolve available fonts for cycling at the given pixel size.

    The settle font (index 0) is determined by settle_font_name:
    - "_default" -> Playfair Display Bold (classic behavior)
    - Any other value -> looked up in the registry by font_family name

    Remaining fonts are all registry fonts with cycle_role="contrast".

    Cached per (size, settle_font_name) so different overlays with different
    font_family get different cycle font lists.
    """
    global _cycle_fonts_cache
    cache_key = (size, settle_font_name)
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

    # Remaining: contrast fonts from registry (with proper weight axis)
    for name in _CYCLE_CONTRAST_NAMES:
        font = _resolve_font_family(name, size)
        if font:
            fonts.append(font)
        else:
            # Fallback: Montserrat is always available as a static font
            fonts.append(_load_font(MONTSERRAT_FONT_PATH, size))

    _cycle_fonts_cache[cache_key] = fonts
    log.info("font_cycle_fonts_resolved", count=len(fonts), size=size, settle=settle_font_name)
    return fonts
