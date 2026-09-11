"""Suffix positions are captured from the production cloud handler."""

from unittest.mock import patch

import pytest
import skia

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay


@pytest.mark.parametrize("anchor", ["left", "center", "right"])
@pytest.mark.parametrize("preserve", [True, False])
@pytest.mark.parametrize(
    "text,suffix",
    [
        ("Every little moment matters", "matters"),
        ("Hello world", "world"),
        ("word", "word"),
    ],
)
def test_suffix_positions_match_real_cloud_draw_at_every_time(anchor, preserve, text, suffix):
    overlay = dict(
        text=text,
        pop_animated_suffix=suffix,
        effect="pop-in",
        start_s=0,
        end_s=4,
        font_family="Inter",
        text_size_px=32,
        text_anchor=anchor,
        preserve_font_size=preserve,
        max_width_frac=0.65,
        letter_spacing=0.1,
        line_spacing=2,
        shape_text=True,
        motion={"version": 2, "speed": 0.5, "intensity": 0.4},
    )
    canvas = Canvas(300, 200)
    layer, _ = compile_text_overlay(overlay, layer_id="suffix", canvas=canvas)
    assert layer.effect == "static"
    assert layer.motion is None
    surface = skia.Surface(300, 200)
    for time in [0, 0.1, 0.4, 2, 3.9]:
        calls = []

        def capture(_canvas, text, x, baseline, font, *_args, **_kwargs):
            calls.append((text, x, baseline, font.getSize()))

        with patch.object(cloud, "_draw_line_with_layers", side_effect=capture):
            cloud._draw_pop_in_with_suffix(
                surface.getCanvas(), overlay, time, 4, render_canvas=canvas
            )
        assert [(run.text, run.x, run.baseline_y, run.font_size) for run in layer.runs] == calls
        assert all(not run.shaped and run.letter_spacing == 0 for run in layer.runs)


def test_invalid_suffix_keeps_regular_pop_animation():
    layer, _ = compile_text_overlay(
        dict(
            text="Hello world",
            pop_animated_suffix="missing",
            effect="pop-in",
            start_s=0,
            end_s=4,
            font_family="Inter",
        ),
        layer_id="fallback",
        canvas=Canvas(300, 200),
    )
    assert layer.effect == "pop-in"


@pytest.mark.parametrize("effect", ["static", "pop-in", "fade-in"])
def test_informational_highlight_word_does_not_change_native_or_cloud_pixels(effect):
    overlay = dict(
        text="Hello world", effect=effect, start_s=0, end_s=4, font_family="Inter", text_size_px=32
    )
    decorated = {**overlay, "highlight_word": "world"}
    canvas = Canvas(300, 200)
    base, _ = compile_text_overlay(overlay, layer_id="metadata", canvas=canvas)
    marked, _ = compile_text_overlay(decorated, layer_id="metadata", canvas=canvas)
    assert base == marked
    for time in [0, 0.2, 2, 3.9]:
        surfaces = [skia.Surface(300, 200), skia.Surface(300, 200)]
        for surface, value in zip(surfaces, [overlay, decorated]):
            cloud._draw_overlay_on_canvas(surface.getCanvas(), value, time, 4, render_canvas=canvas)
        assert (
            surfaces[0].makeImageSnapshot().tobytes() == surfaces[1].makeImageSnapshot().tobytes()
        )
