"""Generate text overlay PNGs and animated ASS files for TikTok-style text.

Two rendering paths:
  - Static effects → PNG images composited via FFmpeg overlay filter
  - Animated effects (fade-in, typewriter, slide-up) → ASS subtitle files
    burned via FFmpeg subtitles filter

Font: Montserrat ExtraBold (bundled at assets/fonts/).
Fallback: system Helvetica → Pillow default.
"""

import os

import structlog

from app.pipeline.ass_utils import format_ass_time, sanitize_ass_text

log = structlog.get_logger()

# ── Constants ────────────────────────────────────────────────────────────────

OVERLAY_FONT_SIZE = 90
MAX_OVERLAY_TEXT_LEN = 40

CANVAS_W = 1080
CANVAS_H = 1920

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "fonts")
OVERLAY_FONT_PATH = os.path.normpath(
    os.path.join(_ASSETS_DIR, "Montserrat-ExtraBold.ttf")
)

# Effects that produce animated .ass files instead of static PNGs
ASS_ANIMATED_EFFECTS = frozenset({"fade-in", "typewriter", "slide-up"})

# Position → vertical anchor (fraction of canvas height)
_POSITION_Y = {
    "center": 0.5,
    "top": 0.15,
    "bottom": 0.85,
}

# Position → ASS alignment + MarginV
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
Style: Overlay,Montserrat ExtraBold,90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,5,50,50,0,1
"""  # noqa: E501


# ── Public API ───────────────────────────────────────────────────────────────


def generate_text_overlay_png(
    overlays: list[dict],
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
) -> list[dict] | None:
    """Render static text overlays as PNG images for FFmpeg overlay filter.

    Only processes overlays whose effect is NOT in ASS_ANIMATED_EFFECTS.
    Returns list of {png_path, start_s, end_s} or None if nothing rendered.
    """
    results: list[dict] = []

    for i, overlay in enumerate(overlays):
        effect = overlay.get("effect", "none")
        if effect in ASS_ANIMATED_EFFECTS:
            continue

        text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
        if text is None:
            continue

        font = _load_font(OVERLAY_FONT_PATH)
        png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{i}.png")
        _draw_text_png(text, font, position, png_path)
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


# ── ASS Animation Rendering ─────────────────────────────────────────────────


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


# ── Static Overlay Helpers ───────────────────────────────────────────────────


def _validate_overlay(
    overlay: dict, slot_duration_s: float
) -> tuple[str | None, float, float, str]:
    """Validate and sanitize overlay fields.

    Returns (text, start_s, end_s, position) or (None, 0, 0, "") to skip.
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


def _load_font(font_path: str, size: int = OVERLAY_FONT_SIZE):
    """Load a font from path with fallback chain."""
    from PIL import ImageFont  # noqa: PLC0415

    try:
        return ImageFont.truetype(font_path, size)
    except (OSError, IOError):
        pass
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except (OSError, IOError):
        pass
    return ImageFont.load_default()


def _draw_text_png(text: str, font, position: str, png_path: str) -> None:
    """Draw centered text with black outline on a transparent 1080x1920 canvas."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (CANVAS_W - text_w) // 2
    y_frac = _POSITION_Y.get(position, 0.5)
    y = int(CANVAS_H * y_frac - text_h / 2)

    outline_width = 4
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx * dx + dy * dy <= outline_width * outline_width:
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255))

    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    img.save(png_path, "PNG")
