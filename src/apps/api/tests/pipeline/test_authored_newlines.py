"""Manual text preserves explicit rows and size across save and render."""

from pathlib import Path
from unittest.mock import patch

import pytest
import skia
from PIL import ImageDraw

from app.agents._schemas.text_element import TextElement
from app.pipeline import text_overlay as pillow
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.portable_text_layout import compile_text_overlay


def test_manual_newline_flag_survives_text_element_burn_contract():
    element = TextElement(text="A very long line\n\nLast", start_s=0, end_s=2, wrap_lines=False)
    assert TextElement.model_validate(element.model_dump()).wrap_lines is False
    assert TextElement(text="Legacy", start_s=0, end_s=2).wrap_lines is True
    (overlay,) = build_overlays_from_text_elements([element], video_duration_s=2)
    assert overlay["wrap_lines"] is False


@pytest.mark.parametrize("size", [12, 72, 400])
@pytest.mark.parametrize("phases", [None, {"entrance": "fade"}])
def test_portable_and_cloud_keep_manual_rows_and_font_size(size, phases):
    text = "An intentionally very long line that must fit the frame\n\nLast line"
    overlay = {
        "text": text,
        "start_s": 0,
        "end_s": 2,
        "font_family": "Inter",
        "text_size_px": size,
        "max_width_frac": 0.2,
        "wrap_lines": False,
        "shadow_enabled": False,
        "animation_phases": phases,
    }
    canvas = Canvas(600, 400)
    layer, _ = compile_text_overlay(overlay, layer_id="manual", canvas=canvas)
    first = text.split("\n")[0]
    # KRI-826: the explicit rows stay hard breaks, and a stale narrow `max_width_frac`
    # (0.2) never re-wraps them. A row that fits the frame's safe width (0.9 x 600)
    # stays one line at the creator's size; a wider row soft-wraps inside it, and only
    # a single word wider than the frame shrinks the text (never below 24px).
    safe = 0.9 * canvas.width
    face = cloud._typeface_for_overlay(overlay)
    if skia.Font(face, size).measureText(first) <= safe:
        assert [run.text for run in layer.runs] == [first, "Last line"]
        assert all(run.font_size == size for run in layer.runs)
    else:
        assert layer.runs[-1].text == "Last line"
        assert " ".join(run.text for run in layer.runs[:-1]).split() == first.split()
        drawn = {run.font_size for run in layer.runs}
        assert len(drawn) == 1
        (drawn_size,) = drawn
        assert min(size, 24) <= drawn_size <= size
        font = skia.Font(face, drawn_size)
        assert all(font.measureText(run.text) <= safe for run in layer.runs)
        if skia.Font(face, size).measureText("intentionally") <= safe:
            assert drawn_size == size  # wrap first; shrink only for an over-wide word
    with patch.object(cloud, "_draw_line_with_layers") as draw:
        cloud._draw_overlay_on_canvas(
            skia.Surface(600, 400).getCanvas(), overlay, 1, 2, render_canvas=canvas
        )
    actual = [call for call in draw.call_args_list if call.args[1]]
    assert len(actual) == len(layer.runs)
    for run, call in zip(layer.runs, actual, strict=True):
        assert (run.text, run.x, run.baseline_y) == call.args[1:4]
        assert call.args[4].getSize() == layer.runs[0].font_size
    # The blank authored row still takes its own baseline before "Last line".
    assert layer.runs[-1].baseline_y - layer.runs[-2].baseline_y > layer.runs[0].font_size * 2


def test_pillow_manual_text_keeps_rows_and_size_inside_the_safe_width(tmp_path):
    """KRI-826: the Pillow renderer (server slide export, classic burns) applies the
    same authored-line fit as Skia: hard breaks kept, a too-wide row soft-wraps at
    the creator's size, a stale narrow ``max_width_frac`` never re-wraps."""
    original = ImageDraw.ImageDraw.text
    drawn = []

    def record(self, xy, text, *args, **kwargs):
        drawn.append((text, kwargs["font"]))
        return original(self, xy, text, *args, **kwargs)

    text = "Long text " * 20 + "\n\nLast"
    with patch.object(ImageDraw.ImageDraw, "text", record):
        pillow._draw_text_png(
            text,
            "center",
            str(tmp_path / "text.png"),
            font_family="Inter",
            text_size_px=96,
            max_width_frac=0.2,
            wrap_lines=False,
            shadow_enabled=False,
        )
    rows = [row for row, _ in drawn]
    assert drawn and all(font.size == 96 for _, font in drawn)
    assert rows[-1] == "Last"
    assert " ".join(rows[:-1]).split() == ("Long text " * 20).split()
    assert len(rows) > 3  # soft-wrapped, not one 2000px line
    assert all(font.getlength(row) <= 0.9 * pillow.CANVAS_W for row, font in drawn)


@pytest.mark.parametrize(
    ("x_frac", "anchor"), [(0.5, "center"), (0.7, "center"), (0.3, "left"), (0.8, "right")]
)
def test_pillow_authored_line_never_leaves_the_safe_area(tmp_path, x_frac, anchor):
    """Off-centre or edge-anchored authored text wraps to the room it has (KRI-826)."""
    import numpy as np
    from PIL import Image

    out = tmp_path / "text.png"
    pillow._draw_text_png(
        "İstanbul'da ğüşıöç sabahları kahvaltı ve çay keyfi harika bir gün",
        "center",
        str(out),
        font_family="Inter",
        text_size_px=72,
        position_x_frac=x_frac,
        text_anchor=anchor,
        wrap_lines=False,
        shadow_enabled=False,
    )
    alpha = np.array(Image.open(out))[..., 3]
    xs = np.nonzero(alpha > 0)[1]
    margin = 0.05 * pillow.CANVAS_W
    assert xs.min() >= margin - 4
    assert xs.max() <= pillow.CANVAS_W - margin + 4


def test_pillow_inline_emoji_draws_in_color_not_as_a_box(tmp_path):
    import numpy as np
    from PIL import Image

    if pillow._emoji_font() is None:
        pytest.skip("no color emoji face on this machine")
    out = tmp_path / "text.png"
    pillow._draw_text_png(
        "sabahları 🔥 kahvaltı",
        "center",
        str(out),
        font_family="Inter",
        text_size_px=96,
        text_color=(255, 255, 255, 255),
        wrap_lines=False,
        shadow_enabled=False,
    )
    rgba = np.array(Image.open(out)).astype(int)
    opaque = rgba[rgba[..., 3] > 200]
    # White text only has r == g == b; the flame has saturated pixels.
    assert ((opaque[:, 0] - opaque[:, 2]) > 80).sum() > 200


def test_classic_animation_disables_ass_wrap_and_fits_explicit_rows(tmp_path):
    (path,) = pillow.generate_animated_overlay_ass(
        [
            {
                "text": "Long text " * 20 + "\n\nLast",
                "start_s": 0,
                "end_s": 2,
                "effect": "pop-in",
                "font_family": "Inter",
                "text_size_px": 96,
                "wrap_lines": False,
            }
        ],
        2,
        str(tmp_path),
        0,
    )
    dialogue = next(
        row for row in Path(path).read_text().splitlines() if row.startswith("Dialogue:")
    )
    assert r"{\q2\fs96}" in dialogue
    rows = dialogue.split("}")[-1].split(r"\N")
    # KRI-826: libass wrapping stays off (\q2), so the rows are fitted up front: the
    # blank row and "Last" survive, the long line soft-wraps inside 0.9 x W.
    assert rows[-2:] == ["", "Last"]
    font = pillow._resolve_font_family("Inter", 96)
    assert len(rows) > 4
    assert all(font.getlength(row) <= 0.9 * pillow.CANVAS_W for row in rows)


def test_blank_rows_survive_build_and_read_adapter():
    from app.agents._schemas.text_element import _burn_dict_to_text_element

    text = "\nFirst\n\nLast\n"
    (overlay,) = build_overlays_from_text_elements(
        [TextElement(text=text, start_s=0, end_s=2, wrap_lines=False)], video_duration_s=2
    )
    assert overlay["text"] == text
    restored = _burn_dict_to_text_element(overlay)
    assert restored.text == text
    assert restored.wrap_lines is False


def test_classic_png_gets_real_newlines_after_validation(tmp_path):
    with patch.object(pillow, "_draw_text_png") as draw:
        pillow.generate_text_overlay_png(
            [{"text": "First\n\nLast", "start_s": 0, "end_s": 2, "wrap_lines": False}],
            2,
            str(tmp_path),
            0,
        )
    assert draw.call_args.args[0] == "First\n\nLast"
    assert draw.call_args.kwargs["wrap_lines"] is False


def test_manual_text_bypasses_pre_render_size_and_position_constraints():
    from app.pipeline.overlay_constraints import apply_overlay_constraints

    overlay = {
        "text": "A long line " * 20,
        "text_size_px": 800,
        "position_x_frac": 1.2,
        "wrap_lines": False,
    }
    expected = overlay.copy()
    assert apply_overlay_constraints([overlay]) == [expected]


@pytest.mark.parametrize("explicit_default", [False, True])
def test_default_wrap_is_omitted_from_legacy_serialized_payloads(explicit_default):
    import json

    values = {"text": "Legacy", "start_s": 0, "end_s": 2}
    if explicit_default:
        values["wrap_lines"] = True
    legacy = TextElement.model_validate(values)
    assert "wrap_lines" not in legacy.model_dump()
    assert "wrap_lines" not in json.loads(legacy.model_dump_json())
    assert TextElement.model_validate_json(legacy.model_dump_json()).wrap_lines is True
    manual = legacy.model_copy(update={"wrap_lines": False})
    assert manual.model_dump()["wrap_lines"] is False
    assert json.loads(manual.model_dump_json())["wrap_lines"] is False
    assert TextElement.model_validate_json(manual.model_dump_json()).wrap_lines is False
    assert TextElement.model_json_schema()["properties"]["wrap_lines"]["default"] is True


@pytest.mark.parametrize("text", ["FIRST LINE\n\nTHIRD LINE", "\nFIRST LINE\n\nTHIRD LINE\n"])
def test_classic_manual_blank_rows_advance_by_font_metrics(tmp_path, text):
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, value, *args, **kwargs):
        if value:
            drawn.append((xy[1], kwargs["font"].getmetrics()))
        return original(self, xy, value, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", record):
        pillow.generate_text_overlay_png(
            [
                {
                    "text": text,
                    "start_s": 0,
                    "end_s": 2,
                    "font_family": "Inter",
                    "text_size_px": 84,
                    "position_y_frac": 0.5,
                    "wrap_lines": False,
                    "shadow_enabled": False,
                }
            ],
            2,
            str(tmp_path),
            0,
        )
    assert len(drawn) == 2
    (first_y, (ascent, descent)), (last_y, _) = drawn
    line_step = int((ascent + descent) * 1.15)
    assert last_y - first_y == 2 * line_step
    # Font-metric block bounds include the authored leading/trailing blank rows.
    lines = text.split("\n")
    block_height = (len(lines) - 1) * line_step + ascent + descent
    expected_top = int(pillow.CANVAS_H * 0.5 - block_height / 2)
    assert first_y == expected_top + ascent + lines.index("FIRST LINE") * line_step
    typeface = cloud._typeface_for_overlay({"font_family": "Inter"})
    metrics = cloud._measure_block(skia.Font(typeface, 84), lines)
    # Pillow rounds font metrics to integers; Skia retains fractional metrics.
    assert abs((last_y - first_y) - 2 * metrics["line_step"]) <= 4


@pytest.mark.parametrize(
    ("x_frac", "alignment"), [(None, "center"), (0.7, "center"), (0.3, "left"), (0.8, "right")]
)
def test_server_slide_export_keeps_long_turkish_emoji_text_inside_the_frame(
    tmp_path, x_frac, alignment
):
    """KRI-826 E2E on the real slide renderer: the iOS Slider sets ``wrap_lines=False``
    on a hand-edited text; the server export (Pillow) must wrap it inside the safe area
    on a 4:5 slide, in color for the emoji, exactly like the iOS canvas does."""
    import numpy as np
    from PIL import Image

    from app.pipeline.slide_post.build import render_text_element_png
    from app.schemas.slide_post import SlideTextElement

    text = "Bugün İstanbul’da ılık bir akşam 🌅 şimdi Kadıköy’de çay içiyoruz ☕️ 50% {off}: a\\b"
    element = SlideTextElement(
        id="t1",
        text=text,
        font_family="Inter",
        size_px=72,
        alignment=alignment,
        position="custom",
        x_frac=x_frac,
        y_frac=0.5,
        max_width_frac=0.3,
        wrap_lines=False,
    )
    out = tmp_path / "slide_text.png"
    render_text_element_png(element, str(out), canvas=(1080, 1350))
    rgba = np.array(Image.open(out)).astype(int)
    assert rgba.shape[:2] == (1350, 1080)
    xs = np.nonzero(rgba[..., 3] > 40)[1]
    assert xs.min() >= 0.05 * 1080 - 6 and xs.max() <= 0.95 * 1080 + 6, (xs.min(), xs.max())
    if pillow._emoji_font() is not None:
        opaque = rgba[rgba[..., 3] > 200]
        assert ((opaque[:, 0] - opaque[:, 2]) > 80).sum() > 100  # the sunset/coffee in color
