from unittest.mock import patch

import pytest

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import UnsupportedPortableText, compile_text_overlay


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("anchor", ["left", "center", "right"])
@pytest.mark.parametrize("fixed", [False, True])
def test_compiled_runs_match_actual_cloud_layout(shaped, anchor, fixed):
    canvas = Canvas(600, 400)
    overlay = {
        "text": "AV fi İstanbul çok güzel\nA second line of text",
        "start_s": 0.2,
        "end_s": 2,
        "font_family": "Inter",
        "text_size_px": 36,
        "text_anchor": anchor,
        "vertical_anchor": "center",
        "position_x_frac": 0.4,
        "position_y_frac": 0.6,
        "letter_spacing": 0.04,
        "line_spacing": 1.2,
        "shape_text": shaped,
        "preserve_font_size": fixed,
        "text_color": "#E84A8A",
        "stroke_width": 2,
        "shadow_style": "high_visibility",
        "glow_color": "#123456",
        "glow_strength": 0.6,
        "text_gradient": {"colors": ["#FF0000", "#0000FF"], "angle_deg": 135},
    }
    layer, asset = compile_text_overlay(overlay, layer_id="test", canvas=canvas)
    with patch.object(cloud, "_draw_line_with_layers") as draw:
        import skia

        cloud._draw_overlay_on_canvas(
            skia.Surface(600, 400).getCanvas(), overlay, 0, 1.8, render_canvas=canvas
        )
    assert len(layer.runs) == draw.call_count
    assert asset.catalog == "font"
    for run, call in zip(layer.runs, draw.call_args_list, strict=True):
        _, text, x, baseline, font, color, stroke, shadow = call.args
        assert (run.text, run.x, run.baseline_y) == (text, x, baseline)
        assert run.font_size == font.getSize()
        assert run.stroke_width == stroke * 2
        assert run.letter_spacing == call.kwargs["letter_spacing_px"]
        assert run.shaped == call.kwargs["shape_text"]
        assert (run.glyphs is None) == shaped
        assert run.gradient is not None
        assert len(run.blur_layers) == 4  # two glow layers, then ambient/contact shadows
        assert [blur.sigma for blur in run.blur_layers] == [8, 20, 14, 3]
        assert run.font_asset_id == asset.id


@pytest.mark.parametrize(
    "extra",
    [
        {"effect": "dissolve-out"},
        {"emoji_prefix": "🙂"},
        {"behind_subject": True},
        {"theme_transition": {"type": "unknown"}},
        {"effect": "handwriting", "theme_transition": {"type": "giant-title-wipe"}},
        {"spans": [{"text": "hello"}]},
    ],
)
def test_compiler_refuses_treatments_it_cannot_preserve(extra):
    with pytest.raises(UnsupportedPortableText):
        compile_text_overlay(
            {"text": "Hello", "start_s": 0, "end_s": 2, **extra},
            layer_id="test",
            canvas=Canvas(600, 400),
        )


def test_normalized_motion_is_preserved_but_static_dispatch_ignores_it():
    overlay = {
        "text": "Hello",
        "start_s": 0,
        "end_s": 2,
        "effect": "pop-in",
        "motion": {"version": 2, "speed": 0.53},
    }
    layer, _ = compile_text_overlay(overlay, layer_id="test", canvas=Canvas(600, 400))
    assert layer.motion.speed == 0.53
    layer, _ = compile_text_overlay(
        {**overlay, "effect": "static"}, layer_id="test", canvas=Canvas(600, 400)
    )
    assert layer.motion is None


@pytest.mark.parametrize(
    "effect", ["fade-in", "scale-up", "slide-up", "slide-down", "pop-in", "bounce"]
)
def test_legacy_animation_keeps_its_duration_and_effect(effect):
    layer, _ = compile_text_overlay(
        {"text": "Hello", "start_s": 0.2, "end_s": 0.32, "effect": effect},
        layer_id="legacy",
        canvas=Canvas(600, 400),
    )
    assert layer.effect == effect
    assert layer.motion is None
    assert layer.start == 0.2
    assert layer.end == 0.32


@pytest.mark.parametrize("rotation", [0, 23])
@pytest.mark.parametrize("glow", [0, 1])
def test_ink_reveal_bounds_match_actual_styled_cloud_clip(rotation, glow):
    overlay = {
        "text": "Hello\nA second line",
        "start_s": 0,
        "end_s": 2,
        "font_family": "Inter",
        "text_size_px": 36,
        "effect": "ink-reveal",
        "rotation_deg": rotation,
        "shadow_style": "high_visibility",
        "stroke_width": 2,
        "glow_color": "#123456",
        "glow_strength": glow,
    }
    canvas = Canvas(600, 400)
    layer, _ = compile_text_overlay(overlay, layer_id="ink", canvas=canvas)

    class Capture:
        clips = []

        def save(self):
            pass

        def restore(self):
            pass

        def translate(self, *args):
            pass

        def scale(self, *args):
            pass

        def rotate(self, *args):
            pass

        def clipRect(self, rect, **kwargs):
            self.clips.append((rect.left(), rect.top(), rect.right(), rect.bottom()))

    capture = Capture()
    with patch("app.pipeline.text_overlay_skia._draw_line_with_layers"):
        cloud._draw_centered_text(
            capture, overlay["text"], overlay, render_canvas=canvas, reveal_progress=0.5
        )
    bounds = layer.reveal_bounds
    assert bounds is not None
    assert capture.clips == [
        pytest.approx(
            (bounds.left, bounds.top, (bounds.left + bounds.right) / 2, bounds.bottom), abs=0.0001
        )
    ]


def test_dissolve_compiles_settled_text_with_explicit_seed_and_ignores_motion():
    overlay = {
        "text": "Dissolve",
        "start_s": 0,
        "end_s": 4,
        "font_family": "Inter",
        "text_size_px": 36,
    }
    static, font = compile_text_overlay(overlay, layer_id="test", canvas=Canvas(600, 400))
    dissolve, same_font = compile_text_overlay(
        {**overlay, "effect": "dissolve-out", "motion": {"version": 2, "speed": 4}},
        layer_id="test",
        canvas=Canvas(600, 400),
        dissolve_seed=138,
    )
    assert dissolve.runs == static.runs
    assert dissolve.motion is None
    assert dissolve.dissolve_seed == 138
    assert same_font == font


def test_slide_in_matches_actual_cloud_static_hold():
    import numpy as np

    canvas = Canvas(300, 200)
    overlay = {
        "text": "Hello",
        "font_family": "Inter",
        "text_size_px": 32,
        "start_s": 0,
        "end_s": 4,
        "effect": "slide-in",
        "motion": {"version": 2, "speed": 4, "exit_s": 1},
    }
    assert not cloud._is_animated(overlay)
    expected = (
        cloud._draw_frame({**overlay, "effect": "static"}, 0, 4, render_canvas=canvas)
        .toarray()
        .copy()
    )
    for time in [0, 0.2, 1, 3.9]:
        actual = cloud._draw_frame(overlay, time, 4, render_canvas=canvas).toarray().copy()
        np.testing.assert_array_equal(actual, expected)
    layer, _ = compile_text_overlay(overlay, layer_id="slide-in", canvas=canvas)
    assert layer.effect == "slide-in"
    assert layer.motion is None
