"""Where the narrated opening title sits on the frame: one rule for cloud and phone.

Both narrated renders draw a confirmed opening title as one ``generative_intro``
TextElement in the cloud ``top`` / ``large`` look: a block of Playfair Display
lines at 120 px, wrapped to 0.9 of the frame width and centred on y 0.15. The
renderer only shrinks a line wider than the frame, never a block taller than
its room, so a title of more than ~40 characters wraps to four or more lines and
its centred block runs off the top of the frame (KRI-455 follow-up).

`narrated_title_placement` is the one place both builders take the title's
look from (`generative_build._narrated_storyboard_text_elements` for the cloud,
`phone_narrated_plan.narrated_title_element` for the phone), so a cloud and a
phone render of the same title stay byte-identical. The title keeps to the top
band of the frame: its block starts below the top safe margin (the TikTok/Reels
header chrome; same 6% as `overlay_constraints`), ends above
``_TITLE_BAND_BOTTOM_FRAC`` (well clear of the bottom caption band and of the
middle of the frame) and has at most ``_TITLE_MAX_LINES`` lines:

* A title the preset already keeps in the band is left exactly as it is.
* Otherwise the block keeps its centre on y 0.15 if its top clears the margin,
  else its top sits on the margin, and the font steps down from 120 px until the
  wrapped block fits the band.

Geometry is measured with the Skia renderer's own wrap and block metrics
(`text_overlay_skia._shrink_to_fit` / `_measure_block`) on the overlay the
element compiles to. The cloud burn and the phone compiler
(`portable_text_layout`) lay text out with those same functions, and the fit is
expressed only in fields every renderer already honours (``size_px``,
``y_frac``), so the result is the same on every path, including the iOS editor
preview, which draws the phone title from its element.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

# The cloud intro's "top" / "large" / default face, resolved
# (`text_overlay._POSITION_Y`, `_FONT_SIZE_MAP`, the intro role's typeface).
TITLE_Y_FRAC = 0.15
TITLE_SIZE_PX = 120
TITLE_FONT_FAMILY = "Playfair Display"

_TITLE_SAFE_MARGIN_FRAC = 0.06
_TITLE_BAND_BOTTOM_FRAC = 0.30
_TITLE_MAX_LINES = 3
# y_frac is persisted on the element; four decimals is under 0.2 px on any canvas.
_Y_FRAC_DECIMALS = 4


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@dataclass(frozen=True)
class NarratedTitleFit:
    """A title's fitted font size and block centre (fraction of canvas height)."""

    size_px: int
    y_frac: float


@dataclass(frozen=True)
class _Block:
    lines: int
    height: float


def _preset_overlay(text: str) -> dict:
    """The burn dict the cloud preset title compiles to."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415

    element = TextElement(
        id="narrated-title-fit",
        text=text,
        start_s=0.0,
        end_s=1.0,
        role="generative_intro",
        position="top",
        size_class="large",
        effect="fade-in",
    )
    [overlay] = build_overlays_from_text_elements(
        [element], video_duration_s=1.0, independent_box_alignment=True
    )
    return overlay


def _measure(overlay: dict, canvas: _Canvas) -> tuple[Any, ...]:
    """Inputs to the renderer's wrap: text, typeface, width budget, tracking,
    line spacing, and every shaping mode a renderer may lay the text out with."""
    import skia  # noqa: PLC0415

    from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415
    from app.pipeline.portable_text_layout import legacy_glyphs_resolvable  # noqa: PLC0415

    text = cloud._overlay_text(overlay)
    typeface = cloud._resolve_typeface_for_overlay(overlay).typeface
    shaped = bool(overlay.get("shape_text"))
    # The cloud burn lays out unshaped; the phone compiler falls back to shaped
    # layout for a character the font has no glyph for. Fit both.
    modes = (
        (shaped,)
        if shaped or legacy_glyphs_resolvable(skia.Font(typeface, 40), text)
        else (False, True)
    )
    return (
        text,
        typeface,
        cloud._overlay_max_width_px(overlay, canvas),
        cloud.resolve_letter_spacing_em(overlay.get("letter_spacing")),
        cloud.resolve_line_spacing(overlay.get("line_spacing")),
        modes,
    )


def _block(measure: tuple[Any, ...], size_px: int) -> _Block:
    """The tallest block the renderer would draw at ``size_px``."""
    from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415

    text, typeface, max_width, spacing_em, line_spacing, modes = measure
    blocks = []
    for shaped in modes:
        font, size, lines = cloud._shrink_to_fit(
            text, typeface, size_px, max_width, spacing_em, shape_text=shaped
        )
        metrics = cloud._measure_block(
            font,
            lines,
            line_spacing=line_spacing,
            letter_spacing_px=spacing_em * size,
            shape_text=shaped,
        )
        blocks.append(_Block(lines=len(lines), height=float(metrics["block_h"])))
    return max(blocks, key=lambda block: (block.height, block.lines))


def fit_narrated_title(text: str, *, canvas: _Canvas) -> NarratedTitleFit | None:
    """The size and centre that keep ``text`` in the title band, or ``None``
    when the cloud preset (120 px centred on y 0.15) already does."""
    from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415

    if not text.strip():
        return None
    height = float(canvas.height)
    top_limit = _TITLE_SAFE_MARGIN_FRAC * height
    bottom_limit = _TITLE_BAND_BOTTOM_FRAC * height
    preset_centre = TITLE_Y_FRAC * height
    measure = _measure(_preset_overlay(text), canvas)

    def centre_y_frac(block: _Block) -> float:
        """The preset centre, or lower so the block's top sits on the margin.
        Rounded up so the block never ends up above the margin."""
        centre = max(preset_centre, top_limit + block.height / 2)
        scale = 10**_Y_FRAC_DECIMALS
        return math.ceil(centre / height * scale - 1e-9) / scale

    def fits(block: _Block, y_frac: float) -> bool:
        centre = y_frac * height
        return (
            block.lines <= _TITLE_MAX_LINES
            and centre - block.height / 2 >= top_limit - 1e-6
            and centre + block.height / 2 <= bottom_limit
        )

    if fits(_block(measure, TITLE_SIZE_PX), TITLE_Y_FRAC):
        return None
    for size_px in range(TITLE_SIZE_PX, cloud._MIN_FONT_SIZE - 1, -1):
        block = _block(measure, size_px)
        y_frac = centre_y_frac(block)
        if fits(block, y_frac):
            return NarratedTitleFit(size_px=size_px, y_frac=y_frac)
    # Nothing fits the band (only an unbreakable run of characters can do
    # that): the smallest size with its top on the margin.
    return NarratedTitleFit(
        size_px=cloud._MIN_FONT_SIZE,
        y_frac=centre_y_frac(_block(measure, cloud._MIN_FONT_SIZE)),
    )


def narrated_title_placement(text: str, *, canvas: _Canvas, explicit: bool) -> dict[str, Any]:
    """TextElement position/size fields for the narrated opening title.

    ``explicit`` spells out the cloud preset (custom y 0.15, 120 px, Playfair
    Display) for a reader with no cloud defaults (the iOS editor preview draws
    a bare "top" lower, in Inter). Without it, a title the preset keeps in the
    band keeps the preset's own ``top`` / ``large`` fields, so existing cloud
    elements are unchanged. Either way a title the preset would push out of the
    band gets its fitted size and centre.
    """
    fit = fit_narrated_title(text, canvas=canvas)
    if fit is None and not explicit:
        return {"position": "top", "size_class": "large"}
    return {
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": fit.y_frac if fit is not None else TITLE_Y_FRAC,
        "size_class": "large",
        "size_px": fit.size_px if fit is not None else TITLE_SIZE_PX,
        **({"font_family": TITLE_FONT_FAMILY} if explicit else {}),
    }
