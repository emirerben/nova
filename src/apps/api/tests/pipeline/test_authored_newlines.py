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
    text = "An intentionally very long line that must not wrap\n\nLast line"
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
    assert [run.text for run in layer.runs] == [text.split("\n")[0], "Last line"]
    assert all(run.font_size == size for run in layer.runs)
    with patch.object(cloud, "_draw_line_with_layers") as draw:
        cloud._draw_overlay_on_canvas(
            skia.Surface(600, 400).getCanvas(), overlay, 1, 2, render_canvas=canvas
        )
    actual = [call for call in draw.call_args_list if call.args[1]]
    assert len(actual) == len(layer.runs)
    for run, call in zip(layer.runs, actual, strict=True):
        assert (run.text, run.x, run.baseline_y) == call.args[1:4]
        assert call.args[4].getSize() == size
    assert layer.runs[1].baseline_y - layer.runs[0].baseline_y > size * 2


def test_pillow_manual_text_never_wraps_or_resolves_a_smaller_font(tmp_path):
    original = ImageDraw.ImageDraw.text
    drawn = []

    def record(self, xy, text, *args, **kwargs):
        drawn.append((text, kwargs["font"].size))
        return original(self, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", record):
        pillow._draw_text_png(
            "Long text " * 20 + "\n\nLast",
            "center",
            str(tmp_path / "text.png"),
            font_family="Inter",
            text_size_px=96,
            max_width_frac=0.2,
            wrap_lines=False,
            shadow_enabled=False,
        )
    assert drawn and all(size == 96 for _, size in drawn)
    assert any(text == "Long text " * 20 for text, _ in drawn)


def test_classic_animation_disables_ass_wrap_and_keeps_explicit_rows(tmp_path):
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
    assert dialogue.count(r"\N") == 2


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
