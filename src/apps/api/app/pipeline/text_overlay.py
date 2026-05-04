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
Style: Overlay,{fontname},90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H20000000,-1,0,0,0,100,100,0,0,1,0,6,5,50,50,0,1
"""  # noqa: E501


# Keep static header for backwards compat (used when no font_family set)
_ASS_OVERLAY_HEADER = _build_ass_header("Playfair Display")

# -- Font-cycle configuration -------------------------------------------------
# Fonts with cycle_role="contrast" are used during rapid cycling.
# The settle font (index 0) is either the overlay's font_family or Playfair Display Bold.

FONT_CYCLE_INTERVAL_S = 0.15
FONT_CYCLE_FAST_INTERVAL_S = 0.07
FONT_CYCLE_SETTLE_RATIO = 0.30
MAX_FONT_CYCLE_FRAMES = 60
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
            _draw_spans_png(overlay, spans, position, png_path)
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
        # Resolve effective pixel size: explicit text_size_px wins, else map
        # the named text_size; default matches the ASS Style fallback (90).
        text_size_px = overlay.get("text_size_px")
        if text_size_px is None:
            text_size_px = _FONT_SIZE_MAP.get(overlay.get("text_size", "medium"), 90)
        ass_path = os.path.join(output_dir, f"slot_{slot_index}_anim_{i}.ass")
        try:
            _write_animated_ass(
                text, start_s, end_s, position, effect, ass_path,
                font_family=font_family,
                position_y_frac=overlay.get("position_y_frac"),
                text_size_px=int(text_size_px),
                text_color=overlay.get("text_color"),
            )
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
    *,
    font_family: str | None = None,
    position_y_frac: float | None = None,
    text_size_px: int | None = None,
    text_color: str | None = None,
) -> None:
    """Write an ASS file with animation tags for the given effect.

    text_size_px and text_color, when provided, are injected as ASS dialogue
    overrides (\\fs and \\c) so per-overlay sizing and color flow through to
    libass — the ASS Style line is a fallback (fontsize 90, color white).
    Without these overrides, every animated overlay rendered at the Style's
    hardcoded size regardless of recipe values.
    """
    alignment, margin_v = _ASS_POSITION.get(position, (5, 0))
    start_str = format_ass_time(start_s)
    end_str = format_ass_time(end_s)

    # Custom Y position override via \pos tag
    pos_tag = ""
    if position_y_frac is not None:
        target_y = int(CANVAS_H * position_y_frac)
        pos_tag = f"\\pos({CANVAS_W // 2},{target_y})"

    # Per-overlay size + color overrides applied to every effect branch.
    style_tag = ""
    if text_size_px is not None:
        style_tag += f"\\fs{int(text_size_px)}"
    if text_color:
        # ASS color: &H00BBGGRR — ASS is BGR not RGB, alpha 00 = opaque.
        hex6 = text_color.lstrip("#")
        if len(hex6) == 6:
            r, g, b = hex6[0:2], hex6[2:4], hex6[4:6]
            style_tag += f"\\c&H00{b}{g}{r}&"

    if effect == "fade-in":
        dialogue_text = f"{{\\an5{pos_tag}{style_tag}\\fad(500,0)}}{text}" if pos_tag else f"{{\\an{alignment}{style_tag}\\fad(500,0)}}{text}"

    elif effect == "typewriter":
        total_dur_cs = int((end_s - start_s) * 100)
        char_count = max(len(text), 1)
        per_char_cs = max(1, total_dur_cs // char_count)
        parts = [f"{{\\an{alignment}{style_tag}}}"]
        for ch in text:
            parts.append(f"{{\\k{per_char_cs}}}{ch}")
        dialogue_text = "".join(parts)

    elif effect == "slide-up":
        y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
        target_y = int(CANVAS_H * y_frac)
        start_y = CANVAS_H + 100
        x = CANVAS_W // 2
        dialogue_text = (
            f"{{\\an5{style_tag}\\move({x},{start_y},{x},{target_y},0,500)}}{text}"
        )

    else:
        dialogue_text = f"{{\\an5{pos_tag}{style_tag}}}{text}" if pos_tag else f"{{\\an{alignment}{style_tag}}}{text}"

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
        )
    else:
        _draw_text_png(
            text, position, png_path,
            font=font, font_family=font_family, font_style=font_style,
            text_size=text_size, text_color=text_color,
            position_y_frac=position_y_frac,
        )


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

    duration = end_s - start_s

    # Acceleration: switch to faster interval after this absolute timestamp
    accel_at = overlay.get("font_cycle_accel_at_s")

    # When curtain-close acceleration is active, skip the settle phase
    # entirely — cycling runs to the end, reinforcing the kinetic energy
    # of the closing bars. Otherwise, settle on the primary font for the
    # last FONT_CYCLE_SETTLE_RATIO of the duration.
    if accel_at is not None:
        settle_start = end_s
        cycle_end = end_s
    else:
        settle_start = start_s + duration * (1.0 - FONT_CYCLE_SETTLE_RATIO)
        cycle_end = settle_start  # cycling stops here, settle begins

    # Determine which spans participate in cycling (no explicit font_family)
    cycling_span_indices: list[int] = []
    if spans:
        cycling_span_indices = [
            idx for idx, s in enumerate(spans) if not s.get("font_family")
        ]

    # ── Pre-compute all frame specs (sequential, pure math) ─────────────
    frame_specs: list[tuple] = []  # (png_path, font, start_s, end_s)
    frame_idx = 0
    t = start_s

    # Phase 1: rapid font cycling (with optional acceleration)
    font_idx = 0
    while t < cycle_end and frame_idx < MAX_FONT_CYCLE_FRAMES:
        interval = FONT_CYCLE_INTERVAL_S
        if accel_at is not None and t >= accel_at:
            interval = FONT_CYCLE_FAST_INTERVAL_S

        frame_end = min(t + interval, cycle_end)
        font = cycle_fonts[font_idx % len(cycle_fonts)]

        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_fontcycle_{overlay_index}_{frame_idx}.png",
        )
        frame_specs.append((png_path, font, t, frame_end))

        t = frame_end
        frame_idx += 1
        font_idx += 1

    # Gap-fill: if the loop exited early (frame cap hit) with t < cycle_end,
    # render one more PNG with the last-used font covering (t, cycle_end)
    if t < cycle_end - 0.001:
        last_font = cycle_fonts[(font_idx - 1) % len(cycle_fonts)]
        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_fontcycle_{overlay_index}_{frame_idx}_gapfill.png",
        )
        frame_specs.append((png_path, last_font, t, cycle_end))
        t = cycle_end

    # Phase 2: settle on the primary font for the remaining duration
    if settle_start < end_s:
        primary_font = cycle_fonts[0]  # settle font (font_family or Playfair Display Bold)
        png_path = os.path.join(
            output_dir,
            f"slot_{slot_index}_fontcycle_{overlay_index}_settle.png",
        )
        frame_specs.append((png_path, primary_font, settle_start, end_s))

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
) -> None:
    """Draw styled text on a transparent 1080x1920 canvas.

    Font resolution priority:
      1. ``font`` — pre-loaded ImageFont (used by font-cycle, skip resolution)
      2. ``font_family`` — look up in registry by name
      3. ``font_style`` — legacy style-based lookup

    Uses subtle drop shadow instead of outline for a clean TikTok look.
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Resolve font: pre-loaded > font_family > font_style
    if font is None:
        if font_family:
            size = text_size_px or _FONT_SIZE_MAP.get(text_size, 72)
            font = _resolve_font_family(font_family, size)
        if font is None:
            font = _load_styled_font(font_style, text_size, text_size_px=text_size_px)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (CANVAS_W - text_w) // 2
    y_frac = position_y_frac if position_y_frac is not None else _POSITION_Y.get(position, 0.5)
    y = int(CANVAS_H * y_frac - text_h / 2)

    # Soft gaussian shadow -- cinematic depth, no hard edges
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
            # Foreground
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
