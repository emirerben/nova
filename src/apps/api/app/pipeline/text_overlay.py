"""Generate text overlay PNGs and animated ASS files for TikTok-style text.

Two rendering paths:
  - Static effects + font-cycle -> PNG images composited via FFmpeg overlay filter
  - Animated effects (fade-in, typewriter, slide-up) -> ASS subtitle files
    burned via FFmpeg subtitles filter

Supports effects:
  - Static effects (pop-in, scale-up, none): single PNG for the duration
  - font-cycle: rapid font switching -- generates one PNG per font frame, each
    with a different font, swapped via timed FFmpeg overlays (~7 changes/sec)
  - Animated effects (fade-in, typewriter, slide-up): ASS subtitle files

Font: Playfair Display Bold (bundled, SIL OFL) for titles/display,
      Playfair Display Regular (bundled) for serif/subtitle text,
      Montserrat ExtraBold (bundled) as sans fallback + font-cycle contrast.
Fallback: system Helvetica -> Pillow default.
"""

import os

import structlog

from app.pipeline.ass_utils import format_ass_time, sanitize_ass_text

log = structlog.get_logger()

# -- Constants ----------------------------------------------------------------

MAX_OVERLAY_TEXT_LEN = 40

# Output dimensions (must match reframe output)
CANVAS_W = 1080
CANVAS_H = 1920

# Font path: look next to assets/ dir relative to the api app root
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "fonts")
FONTS_DIR = os.path.normpath(_ASSETS_DIR)

OVERLAY_FONT_PATH = os.path.normpath(os.path.join(_ASSETS_DIR, "PlayfairDisplay-Bold.ttf"))
OVERLAY_FONT_PATH_REGULAR = os.path.normpath(
    os.path.join(_ASSETS_DIR, "PlayfairDisplay-Regular.ttf")
)
MONTSERRAT_FONT_PATH = os.path.normpath(os.path.join(_ASSETS_DIR, "Montserrat-ExtraBold.ttf"))

# Effects that produce animated .ass files instead of static PNGs
ASS_ANIMATED_EFFECTS = frozenset({"fade-in", "typewriter", "slide-up"})

# Position -> vertical anchor (fraction of canvas height)
_POSITION_Y = {
    "center": 0.5,
    "top": 0.15,
    "bottom": 0.85,
}

# -- Font style mapping -------------------------------------------------------
# Gemini reports font_style per overlay -> we pick the best available font.
# Priority: bundled asset -> macOS system -> fallback.

_FONT_SIZE_MAP = {"small": 48, "medium": 72, "large": 120, "xlarge": 150}

_FONT_STYLE_MAP: dict[str, list[str]] = {
    "serif": [
        OVERLAY_FONT_PATH_REGULAR,  # Playfair Display Regular
        "/System/Library/Fonts/Supplemental/Didot.ttc",
        "/System/Library/Fonts/Supplemental/Baskerville.ttc",
    ],
    "serif_italic": [
        "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf",
        "/System/Library/Fonts/Supplemental/Didot.ttc",
    ],
    "script": [
        "/System/Library/Fonts/Supplemental/SnellRoundhand.ttc",
        "/System/Library/Fonts/Supplemental/Brush Script.ttf",
    ],
    "sans": [
        MONTSERRAT_FONT_PATH,  # Montserrat ExtraBold
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "display": [
        OVERLAY_FONT_PATH,  # Playfair Display Bold
        "/System/Library/Fonts/Supplemental/Didot.ttc",
        MONTSERRAT_FONT_PATH,
    ],
}


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
    "top": (8, 200),
    "bottom": (2, 200),
}


_ASS_OVERLAY_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Overlay,Playfair Display,90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H40000000,-1,0,0,0,100,100,0,0,1,0,2,5,50,50,0,1
"""  # noqa: E501

# -- Font-cycle configuration -------------------------------------------------
# Contrasting font styles for rapid cycling. Uses system fonts with fallbacks.
# Each entry is a list of candidate paths -- first found is used.

_CYCLE_FONT_CANDIDATES = [
    # 0: Elegant serif (primary -- Playfair Display Bold, settle font)
    [OVERLAY_FONT_PATH],
    # 1: Bold serif
    [
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ],
    # 2: Condensed impact
    [
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    # 3: Script / handwriting
    [
        "/System/Library/Fonts/Supplemental/Brush Script.ttf",
        "/System/Library/Fonts/Supplemental/Papyrus.ttc",
    ],
    # 4: Bold sans-serif (contrast during cycling)
    [MONTSERRAT_FONT_PATH],
]

# How often the font switches (seconds per frame)
FONT_CYCLE_INTERVAL_S = 0.15

# Fraction of duration where cycling "settles" on the primary font
FONT_CYCLE_SETTLE_RATIO = 0.30

OVERLAY_FONT_SIZE = 90


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

        if effect == "font-cycle":
            configs = _render_font_cycle(
                overlay, slot_duration_s, output_dir, slot_index, i,
            )
            results.extend(configs)
        else:
            text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
            if text is None:
                continue

            font_style = overlay.get("font_style", "display")
            text_size = overlay.get("text_size", "medium")
            hex_color = overlay.get("text_color", "#FFFFFF")
            text_color = _hex_to_rgba(hex_color)

            png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{i}.png")
            _draw_text_png(text, font_style, text_size, position, png_path, text_color=text_color)
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

        ass_path = os.path.join(output_dir, f"slot_{slot_index}_anim_{i}.ass")
        try:
            _write_animated_ass(text, start_s, end_s, position, effect, ass_path)
            if _validate_ass_file(ass_path):
                ass_paths.append(ass_path)
            else:
                log.warning("ass_validation_failed", slot=slot_index, overlay=i)
        except Exception as exc:
            log.warning("animated_overlay_failed", slot=slot_index, error=str(exc))

    return ass_paths if ass_paths else None


# -- ASS Animation Rendering --------------------------------------------------


def _write_animated_ass(
    text: str,
    start_s: float,
    end_s: float,
    position: str,
    effect: str,
    output_path: str,
) -> None:
    """Write an ASS file with animation tags for the given effect."""
    alignment, margin_v = _ASS_POSITION.get(position, (5, 0))
    start_str = format_ass_time(start_s)
    end_str = format_ass_time(end_s)

    if effect == "fade-in":
        dialogue_text = f"{{\\an{alignment}\\fad(500,0)}}{text}"

    elif effect == "typewriter":
        total_dur_cs = int((end_s - start_s) * 100)
        char_count = max(len(text), 1)
        per_char_cs = max(1, total_dur_cs // char_count)
        parts = [f"{{\\an{alignment}}}"]
        for ch in text:
            parts.append(f"{{\\k{per_char_cs}}}{ch}")
        dialogue_text = "".join(parts)

    elif effect == "slide-up":
        y_frac = _POSITION_Y.get(position, 0.5)
        target_y = int(CANVAS_H * y_frac)
        start_y = CANVAS_H + 100
        x = CANVAS_W // 2
        dialogue_text = (
            f"{{\\an5\\move({x},{start_y},{x},{target_y},0,500)}}{text}"
        )

    else:
        dialogue_text = f"{{\\an{alignment}}}{text}"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_ASS_OVERLAY_HEADER)
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
    "settles" on the primary font (Playfair Display Bold).

    Returns a list of {png_path, start_s, end_s} configs for FFmpeg overlay.
    """
    text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
    if text is None:
        return []

    # Resolve available cycle fonts
    cycle_fonts = _resolve_cycle_fonts()
    if len(cycle_fonts) < 2:
        # Not enough fonts for cycling -- fall back to static
        log.warning("font_cycle_insufficient_fonts", count=len(cycle_fonts))
        font_style = overlay.get("font_style", "display")
        text_size = overlay.get("text_size", "medium")
        hex_color = overlay.get("text_color", "#FFFFFF")
        text_color = _hex_to_rgba(hex_color)
        png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{overlay_index}.png")
        _draw_text_png(text, font_style, text_size, position, png_path, text_color=text_color)
        return [{"png_path": png_path, "start_s": start_s, "end_s": end_s}]

    duration = end_s - start_s
    settle_start = start_s + duration * (1.0 - FONT_CYCLE_SETTLE_RATIO)
    cycle_end = settle_start  # cycling stops here, settle begins

    results: list[dict] = []
    frame_idx = 0
    t = start_s

    # Phase 1: rapid font cycling
    font_idx = 0
    while t < cycle_end:
        frame_end = min(t + FONT_CYCLE_INTERVAL_S, cycle_end)
        font = cycle_fonts[font_idx % len(cycle_fonts)]

        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_fontcycle_{overlay_index}_{frame_idx}.png",
        )
        _draw_text_png_with_font(text, font, position, png_path)
        results.append({"png_path": png_path, "start_s": t, "end_s": frame_end})

        t = frame_end
        frame_idx += 1
        font_idx += 1

    # Phase 2: settle on the primary font for the remaining duration
    if settle_start < end_s:
        primary_font = cycle_fonts[0]  # Playfair Display Bold
        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_fontcycle_{overlay_index}_settle.png",
        )
        _draw_text_png_with_font(text, primary_font, position, png_path)
        results.append({"png_path": png_path, "start_s": settle_start, "end_s": end_s})

    log.info(
        "font_cycle_rendered",
        slot=slot_index,
        text=text[:20],
        frames=len(results),
        fonts_available=len(cycle_fonts),
        duration=round(duration, 2),
    )

    return results


# -- Shared helpers -----------------------------------------------------------


def _validate_overlay(
    overlay: dict, slot_duration_s: float,
) -> tuple[str | None, float, float, str]:
    """Validate and sanitize overlay fields. Returns (text, start_s, end_s, position).

    Returns (None, 0, 0, "") if the overlay should be skipped.
    """
    text = overlay.get("text", "")
    start_s = float(overlay.get("start_s", 0.0))
    end_s = float(overlay.get("end_s", 0.0))
    position = overlay.get("position", "center")

    if start_s >= end_s:
        return None, 0.0, 0.0, ""

    if end_s > slot_duration_s:
        end_s = slot_duration_s
    if start_s >= end_s:
        return None, 0.0, 0.0, ""

    text = sanitize_ass_text(text)
    if not text:
        return None, 0.0, 0.0, ""
    if len(text) > MAX_OVERLAY_TEXT_LEN:
        text = text[: MAX_OVERLAY_TEXT_LEN - 1] + "\u2026"

    return text, start_s, end_s, position


def _load_styled_font(
    font_style: str = "display",
    text_size: str = "medium",
):
    """Load a font matching the requested style and size.

    font_style: "serif", "serif_italic", "script", "sans", "display"
    text_size:  "small" (48), "medium" (72), "large" (120), "xlarge" (150)
    """
    from PIL import ImageFont  # noqa: PLC0415

    size = _FONT_SIZE_MAP.get(text_size, 72)
    candidates = _FONT_STYLE_MAP.get(font_style, _FONT_STYLE_MAP["sans"])

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    # Last resort
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except OSError:
        return ImageFont.load_default()


def _load_font(font_path: str, size: int = OVERLAY_FONT_SIZE):
    """Load a font from path with fallback chain."""
    from PIL import ImageFont  # noqa: PLC0415

    try:
        return ImageFont.truetype(font_path, size)
    except OSError:
        pass

    # Fallback: system Helvetica
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except OSError:
        pass

    return ImageFont.load_default()


def _draw_text_png(
    text: str,
    font_style: str,
    text_size: str,
    position: str,
    png_path: str,
    text_color: tuple[int, int, int, int] = (255, 255, 255, 255),
) -> None:
    """Draw styled text on a transparent 1080x1920 canvas.

    Auto-scales the font so text fills ~80% of canvas width for large/xlarge sizes.
    Uses subtle drop shadow instead of outline for a clean TikTok look.
    """
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Target width: large/xlarge text should fill most of the frame
    if text_size in ("large", "xlarge"):
        target_w = int(CANVAS_W * 0.85)
    else:
        target_w = int(CANVAS_W * 0.65)

    # Auto-scale: start from the nominal size, scale up/down to fit target width
    nominal_size = _FONT_SIZE_MAP.get(text_size, 72)
    font = _load_styled_font(font_style, text_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]

    if text_w > 0 and abs(text_w - target_w) > 20:
        # Scale font size to fit target width
        scaled_size = int(nominal_size * target_w / text_w)
        scaled_size = max(32, min(scaled_size, 300))  # clamp
        candidates = _FONT_STYLE_MAP.get(font_style, _FONT_STYLE_MAP["sans"])
        for path in candidates:
            try:
                font = ImageFont.truetype(path, scaled_size)
                break
            except OSError:
                continue

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (CANVAS_W - text_w) // 2
    y_frac = _POSITION_Y.get(position, 0.5)
    y = int(CANVAS_H * y_frac - text_h / 2)

    # Soft gaussian shadow -- cinematic depth, no hard edges
    from PIL import ImageFilter  # noqa: PLC0415

    shadow_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text((x, y + 6), text, font=font, fill=(0, 0, 0, 160))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=12))
    img = Image.alpha_composite(img, shadow_layer)

    # Clean foreground text -- no outline, no stroke
    fg_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    fg_draw = ImageDraw.Draw(fg_layer)
    fg_draw.text((x, y), text, font=font, fill=text_color)
    img = Image.alpha_composite(img, fg_layer)

    img.save(png_path, "PNG")


def _draw_text_png_with_font(text: str, font, position: str, png_path: str) -> None:
    """Draw centered text with soft gaussian shadow on a transparent canvas.

    Used by font-cycle rendering where the font object is already resolved.
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (CANVAS_W - text_w) // 2
    y_frac = _POSITION_Y.get(position, 0.5)
    y = int(CANVAS_H * y_frac - text_h / 2)

    # Soft gaussian shadow -- cinematic depth
    shadow_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text((x, y + 6), text, font=font, fill=(0, 0, 0, 160))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=12))
    img = Image.alpha_composite(img, shadow_layer)

    # Clean white text -- no outline, no stroke
    fg_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    fg_draw = ImageDraw.Draw(fg_layer)
    fg_draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    img = Image.alpha_composite(img, fg_layer)

    img.save(png_path, "PNG")


_cycle_fonts_cache: list | None = None


def _reset_cycle_cache() -> None:
    """Reset the font-cycle cache. For testing only."""
    global _cycle_fonts_cache
    _cycle_fonts_cache = None


def _resolve_cycle_fonts() -> list:
    """Resolve available fonts for cycling. Cached after first call."""
    global _cycle_fonts_cache
    if _cycle_fonts_cache is not None:
        return _cycle_fonts_cache

    fonts = []
    for candidates in _CYCLE_FONT_CANDIDATES:
        for path in candidates:
            if os.path.exists(path):
                font = _load_font(path)
                fonts.append(font)
                break
        # If no candidate found for this style, skip it

    _cycle_fonts_cache = fonts
    log.info("font_cycle_fonts_resolved", count=len(fonts))
    return fonts
