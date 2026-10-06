"""The narrated opening title stays inside the top band of the frame.

`narrated_title.narrated_title_placement` is the one place the cloud and the
phone narrated title take their look from. The cloud preset (120 px, centred on
y 0.15) only shrinks a line wider than the frame, so a long title wrapped to four
or more lines and its first line ran off the top (a 44-character title's ink
started at y -31 on 1080x1920). These tests compile the title through the real
renderers: the phone's `portable_text_layout` and the cloud's Skia frame draw.
"""

import pytest
from PIL import ImageChops

skia = pytest.importorskip("skia")

from app.agents._schemas.text_element import TextElement  # noqa: E402
from app.kria.recipes import Canvas  # noqa: E402
from app.pipeline import overlay_verify  # noqa: E402
from app.pipeline import text_overlay_skia as cloud  # noqa: E402
from app.pipeline.canvas import canvas_for_orientation  # noqa: E402
from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: E402
from app.pipeline.narrated_title import (  # noqa: E402
    TITLE_FONT_FAMILY,
    TITLE_SIZE_PX,
    TITLE_Y_FRAC,
    fit_narrated_title,
    narrated_title_placement,
)
from app.pipeline.portable_text_layout import compile_text_overlay  # noqa: E402

_SAFE_TOP_FRAC = 0.06
_BAND_BOTTOM_FRAC = 0.30

# Titles the cloud preset already keeps in the band, per orientation.
_FITTING = {
    "portrait": ["Cacio e pepe in 10 minutes", "Pastéis de nata at sunrise in Belém"],
    "landscape": ["Cacio e pepe in 10 minutes"],
}
_LONG = [
    # 44 characters: four lines at 120 px, first-line ink at y -31 (the report).
    "Çılbır: the Turkish eggs everyone gets wrong",
    "The one Lisbon pastry shop locals queue for before sunrise",
    "Everything I ate in 48 hours in Barcelona, ranked from worst to best ever",
    # The 80-character cap.
    "Wait until you see what this tiny Kyoto café does with matcha, sesame and yuzu!!",
    # Glyphs Playfair lacks: the phone lays this out shaped, the cloud unshaped.
    "Best ramen in Tokyo 🍜🔥 you have to try this one before you leave",
]


def _canvas(orientation: str) -> Canvas:
    pipeline_canvas = canvas_for_orientation(orientation)
    return Canvas(width=pipeline_canvas.width, height=pipeline_canvas.height)


def _element(text: str, canvas: Canvas, **look) -> TextElement:
    return TextElement(
        id="narrated-title",
        text=text,
        start_s=0.0,
        end_s=1.6,
        role="generative_intro",
        effect="fade-in",
        **(look or narrated_title_placement(text, canvas=canvas, explicit=False)),
    )


def _overlay(element: TextElement) -> dict:
    [overlay] = build_overlays_from_text_elements(
        [element], video_duration_s=12.0, independent_box_alignment=True
    )
    return overlay


def _phone_block(overlay: dict, canvas: Canvas) -> tuple[float, float, int, float]:
    """(top, bottom, line count, font size) of the layout box the phone exports."""
    layer, _ = compile_text_overlay(overlay, layer_id="title-0", canvas=canvas)
    size = layer.runs[0].font_size
    metrics = skia.Font(cloud._resolve_typeface_for_overlay(overlay).typeface, size).getMetrics()
    baselines = sorted({round(run.baseline_y, 3) for run in layer.runs})
    return baselines[0] + metrics.fAscent, baselines[-1] + metrics.fDescent, len(baselines), size


def _cloud_fill_box(overlay: dict) -> tuple[int, int, int, int]:
    """Bounding box of the white title fill in the real Skia frame (portrait),
    leaving out the soft drop shadow."""
    frame = overlay_verify.render_overlay_frame(overlay)
    bright = [channel.point(lambda value: 255 if value > 200 else 0) for channel in frame.split()]
    mask = bright[0]
    for channel in bright[1:]:
        mask = ImageChops.multiply(mask, channel)
    box = mask.getbbox()
    assert box is not None, "no title fill rendered"
    return box


@pytest.mark.parametrize(
    ("orientation", "text"),
    [(orientation, text) for orientation, texts in _FITTING.items() for text in texts],
)
def test_a_title_the_preset_keeps_in_the_band_keeps_the_preset(orientation, text):
    canvas = _canvas(orientation)

    assert fit_narrated_title(text, canvas=canvas) is None
    # The cloud element is byte-identical to before; the phone spells it out.
    assert narrated_title_placement(text, canvas=canvas, explicit=False) == {
        "position": "top",
        "size_class": "large",
    }
    assert narrated_title_placement(text, canvas=canvas, explicit=True) == {
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": TITLE_Y_FRAC,
        "size_class": "large",
        "size_px": TITLE_SIZE_PX,
        "font_family": TITLE_FONT_FAMILY,
    }


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
@pytest.mark.parametrize("text", _LONG)
def test_a_long_title_stays_inside_the_top_band(orientation, text):
    canvas = _canvas(orientation)
    preset = _element(text, canvas, position="top", size_class="large")
    preset_top, *_ = _phone_block(_overlay(preset), canvas)
    assert preset_top < _SAFE_TOP_FRAC * canvas.height, "the preset alone would cross the margin"

    fit = fit_narrated_title(text, canvas=canvas)
    top, bottom, lines, size = _phone_block(_overlay(_element(text, canvas)), canvas)

    assert fit is not None
    assert size == fit.size_px < TITLE_SIZE_PX, "laid out at the fitted size, no further shrink"
    assert lines <= 3
    assert top >= _SAFE_TOP_FRAC * canvas.height - 0.5
    assert bottom <= _BAND_BOTTOM_FRAC * canvas.height + 0.5
    # The centre only moves down, and only as far as the margin needs.
    assert fit.y_frac >= TITLE_Y_FRAC
    if fit.y_frac > TITLE_Y_FRAC:
        assert top == pytest.approx(_SAFE_TOP_FRAC * canvas.height, abs=0.5)


@pytest.mark.parametrize("text", _LONG)
def test_a_long_title_is_the_largest_that_fits(text):
    canvas = _canvas("portrait")
    fit = fit_narrated_title(text, canvas=canvas)
    assert fit is not None
    bigger = fit.size_px + 1
    top, bottom, lines, _ = _phone_block(
        _overlay(
            _element(
                text,
                canvas,
                position="custom",
                x_frac=0.5,
                y_frac=TITLE_Y_FRAC,
                size_class="large",
                size_px=bigger,
            )
        ),
        canvas,
    )
    height = bottom - top
    # One pixel bigger can't fit the band at any centre.
    assert lines > 3 or height > (_BAND_BOTTOM_FRAC - _SAFE_TOP_FRAC) * canvas.height


@pytest.mark.parametrize("text", _LONG[:4])
def test_the_cloud_burn_draws_a_long_title_inside_the_band(text):
    """Through the real Skia frame draw: the white fill (shadow aside) clears
    the top margin and ends above the band bottom. The preset ran off the top."""
    canvas = _canvas("portrait")

    left, top, right, bottom = _cloud_fill_box(_overlay(_element(text, canvas)))

    assert top >= _SAFE_TOP_FRAC * canvas.height
    assert bottom <= _BAND_BOTTOM_FRAC * canvas.height
    assert 0 < left and right < canvas.width


def test_the_preset_burn_cut_off_the_reported_title():
    """Negative control for the check above: the old look clips at the top."""
    canvas = _canvas("portrait")
    preset = _element(_LONG[0], canvas, position="top", size_class="large")

    frame = overlay_verify.render_overlay_frame(_overlay(preset))
    verdict, reason = overlay_verify.check_clipping(
        overlay_verify.text_bbox(frame), frame.width, frame.height
    )

    assert verdict == "FAIL" and "TOP" in reason


def test_an_unbreakable_title_keeps_the_renderer_shrink():
    """One run wider than the frame: the renderer's own width shrink already
    puts it on one small line inside the band, so nothing changes."""
    text = "W" * 80
    canvas = _canvas("portrait")

    assert fit_narrated_title(text, canvas=canvas) is None
    top, bottom, lines, _ = _phone_block(_overlay(_element(text, canvas)), canvas)
    assert lines == 1
    assert _SAFE_TOP_FRAC * canvas.height <= top < bottom <= _BAND_BOTTOM_FRAC * canvas.height


def test_blank_title_has_no_fit():
    assert fit_narrated_title("   ", canvas=_canvas("portrait")) is None
