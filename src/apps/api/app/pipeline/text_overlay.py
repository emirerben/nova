"""Generate text overlay PNG images for TikTok-style editorial text.

Renders styled text as transparent PNG images using Pillow. These are composited
onto video frames via FFmpeg's `overlay` filter with `enable` timing — no
libass/libfreetype required in FFmpeg.

Supports effects:
  - Static effects (pop-in, fade-in, scale-up, none): single PNG for the duration
  - font-cycle: rapid font switching — generates one PNG per font frame, each
    with a different font, swapped via timed FFmpeg overlays (~7 changes/sec)

Font: Montserrat ExtraBold (bundled, SIL OFL) + system fonts for cycling.
"""

import os

import structlog

from app.pipeline.ass_utils import sanitize_ass_text

log = structlog.get_logger()

# ── Constants ────────────────────────────────────────────────────────────────

OVERLAY_FONT_SIZE = 90
MAX_OVERLAY_TEXT_LEN = 40

# Output dimensions (must match reframe output)
CANVAS_W = 1080
CANVAS_H = 1920

# Font path: look next to assets/ dir relative to the api app root
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "fonts")
OVERLAY_FONT_PATH = os.path.normpath(os.path.join(_ASSETS_DIR, "Montserrat-ExtraBold.ttf"))

# Position → vertical anchor (fraction of canvas height)
_POSITION_Y = {
    "center": 0.5,
    "top": 0.15,
    "bottom": 0.85,
}

# ── Font-cycle configuration ────────────────────────────────────────────────
# Contrasting font styles for rapid cycling. Uses system fonts with fallbacks.
# Each entry is a list of candidate paths — first found is used.

_CYCLE_FONT_CANDIDATES = [
    # 0: Bold sans-serif (primary — Montserrat ExtraBold)
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
    # 4: Elegant serif
    [
        "/System/Library/Fonts/Supplemental/Didot.ttc",
        "/System/Library/Fonts/Supplemental/Copperplate.ttc",
    ],
]

# How often the font switches (seconds per frame)
FONT_CYCLE_INTERVAL_S = 0.15

# Fraction of duration where cycling "settles" on the primary font
FONT_CYCLE_SETTLE_RATIO = 0.30


# ── Public API ───────────────────────────────────────────────────────────────


def generate_text_overlay_png(
    overlays: list[dict],
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
) -> list[dict] | None:
    """Render text overlay PNG images and return overlay configs for FFmpeg.

    Args:
        overlays: List of overlay dicts with keys:
            text (str), start_s (float), end_s (float),
            position ("center"|"top"|"bottom"), effect (str)
        slot_duration_s: Duration of the slot (for clamping end_s)
        output_dir: Directory to write PNG files
        slot_index: Slot index (for unique filenames)

    Returns:
        List of dicts with {png_path, start_s, end_s} for each rendered overlay,
        or None if no overlays were rendered.
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
            config = _render_overlay(
                overlay, slot_duration_s, output_dir, slot_index, i,
            )
            if config is not None:
                results.append(config)

    return results if results else None


# ── Static overlay rendering ─────────────────────────────────────────────────


def _render_overlay(
    overlay: dict,
    slot_duration_s: float,
    output_dir: str,
    slot_index: int,
    overlay_index: int,
) -> dict | None:
    """Render a single static overlay to PNG. Returns config dict or None to skip."""
    text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
    if text is None:
        return None

    font = _load_font(OVERLAY_FONT_PATH)
    png_path = os.path.join(output_dir, f"slot_{slot_index}_overlay_{overlay_index}.png")
    _draw_text_png(text, font, position, png_path)

    return {"png_path": png_path, "start_s": start_s, "end_s": end_s}


# ── Font-cycle rendering ────────────────────────────────────────────────────


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
    "settles" on the primary font (Montserrat ExtraBold).

    Returns a list of {png_path, start_s, end_s} configs for FFmpeg overlay.
    """
    text, start_s, end_s, position = _validate_overlay(overlay, slot_duration_s)
    if text is None:
        return []

    # Resolve available cycle fonts
    cycle_fonts = _resolve_cycle_fonts()
    if len(cycle_fonts) < 2:
        # Not enough fonts for cycling — fall back to static
        log.warning("font_cycle_insufficient_fonts", count=len(cycle_fonts))
        config = _render_overlay(overlay, slot_duration_s, output_dir, slot_index, overlay_index)
        return [config] if config else []

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
        _draw_text_png(text, font, position, png_path)
        results.append({"png_path": png_path, "start_s": t, "end_s": frame_end})

        t = frame_end
        frame_idx += 1
        font_idx += 1

    # Phase 2: settle on the primary font for the remaining duration
    if settle_start < end_s:
        primary_font = cycle_fonts[0]  # Montserrat ExtraBold
        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_fontcycle_{overlay_index}_settle.png",
        )
        _draw_text_png(text, primary_font, position, png_path)
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


# ── Shared helpers ───────────────────────────────────────────────────────────


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


def _load_font(font_path: str, size: int = OVERLAY_FONT_SIZE):
    """Load a font from path with fallback chain."""
    from PIL import ImageFont  # noqa: PLC0415

    try:
        return ImageFont.truetype(font_path, size)
    except (OSError, IOError):
        pass

    # Fallback: system Helvetica
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except (OSError, IOError):
        pass

    return ImageFont.load_default()


def _draw_text_png(text: str, font, position: str, png_path: str) -> None:
    """Draw centered text with black outline on a transparent canvas and save as PNG."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (CANVAS_W - text_w) // 2
    y_frac = _POSITION_Y.get(position, 0.5)
    y = int(CANVAS_H * y_frac - text_h / 2)

    # Black outline (stroke)
    outline_width = 4
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx * dx + dy * dy <= outline_width * outline_width:
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255))

    # White text on top
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    img.save(png_path, "PNG")


_cycle_fonts_cache: list | None = None


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
