"""Actual cloud giant-title frames and resolved origins for native comparison."""

import json
import os
import platform
import sys
from itertools import groupby
from pathlib import Path

import pytest
import skia

from app.kria.portable_text import PortableTextLayer
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay
from tests.kria.reference_assertions import assert_reference_matches, write_linux_reference

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_giant_title.json"


def reference():
    assert (
        Path(cloud.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / "app")
    )
    canvas = Canvas(300, 200)
    cases = []
    for effect in [
        "static",
        "none",
        "fade-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "slide-in",
        "pop-in",
        "bounce",
        "ink-reveal",
        "lyric-line",
        "typewriter",
        "stream-in",
    ]:
        overlay = dict(
            text="GO ON",
            effect=effect,
            start_s=0,
            end_s=4,
            font_family="Inter",
            text_size_px=62,
            preserve_font_size=True,
            shadow_enabled=False,
            outline_px=0,
            theme_transition={"type": "giant-title-wipe", "target_glyph": "O"},
        )
        layer, font = compile_text_overlay(overlay, layer_id=effect, canvas=canvas)
        cases.append(frame_case(effect, overlay, layer, font, canvas))
    base_overlay = dict(overlay)
    for index, values in enumerate(
        [
            dict(
                text="GO ON",
                rotation_deg=25,
                text_anchor="left",
                x_frac=0.15,
                shadow_enabled=True,
                outline_px=2,
                motion={"version": 2, "speed": 0.5, "intensity": 0.7},
            ),
            dict(
                text="GO ON",
                text_anchor="right",
                x_frac=0.85,
                shape_text=True,
                theme_transition={"type": "giant-title-wipe"},
            ),
            dict(text="GO ON", theme_transition={"type": "giant-title-wipe", "target_glyph": "G"}),
            dict(
                text="GO ON",
                shadow_enabled=True,
                outline_px=2,
                glow_color="#9C40FF",
                glow_strength=0.7,
            ),
            dict(
                text="GO ON",
                effect="static",
                motion={"version": 2, "speed": 0.5, "intensity": 0.7, "exit_s": 0.8},
            ),
        ]
    ):
        overlay = {**base_overlay, "effect": "fade-in", **values}
        layer, font = compile_text_overlay(overlay, layer_id=f"styled-{index}", canvas=canvas)
        cases.append(frame_case(f"styled-{index}", overlay, layer, font, canvas))
    for name, values in [
        (
            "scheduled-typewriter",
            dict(effect="typewriter", reveal_schedule_s=[0, 2.9, 3.15, 3.6, 3.8]),
        ),
        (
            "slow-stream",
            dict(
                effect="stream-in",
                text="GO ON GO ON GO ON GO ON GO ON GO ON",
                text_size_px=32,
                shape_text=True,
                rotation_deg=12,
                motion={"version": 2, "speed": 0.25, "cursor_style": "underscore"},
            ),
        ),
    ]:
        overlay = {**base_overlay, **values}
        layer, font = compile_text_overlay(overlay, layer_id=name, canvas=canvas)
        cases.append(frame_case(name, overlay, layer, font, canvas))
    overlay = {
        **base_overlay,
        "effect": "karaoke-line",
        "text_color": "#FFFFFF",
        "highlight_color": "#FF4D00",
        "word_timings": [
            {"text": "GO", "start_s": 0, "end_s": 3.1},
            {"text": "ON", "start_s": 3.1, "end_s": 4},
        ],
    }
    layer, font = compile_text_overlay(overlay, layer_id="late-karaoke", canvas=canvas)
    assert layer.effect == "karaoke-line"
    cases.append(frame_case("late-karaoke", overlay, layer, font, canvas))
    for name, values in [
        ("smooth-legacy", {}),
        (
            "styled-smooth-gradient",
            {
                "text": "GO ON GO ON",
                "text_size_px": 32,
                "rotation_deg": -18,
                "text_gradient": {"colors": ["#FF0000", "#0000FF"], "angle_deg": 135},
                "motion": {
                    "version": 2,
                    "speed": 0.25,
                    "blur_px": 0,
                    "order": "center-out",
                    "direction": "down",
                    "travel_px": 40,
                },
            },
        ),
        (
            "styled-smooth-clear",
            {
                "text": "GO ON GO ON",
                "text_size_px": 32,
                "rotation_deg": -18,
                "text_gradient": {"colors": ["#FF0000", "#0000FF"], "angle_deg": 135},
                "shadow_enabled": True,
                "glow_color": "#40FF90",
                "glow_strength": 0.5,
                "motion": {
                    "version": 2,
                    "speed": 0.25,
                    "blur_px": 0,
                    "order": "center-out",
                    "direction": "down",
                    "travel_px": 40,
                },
            },
        ),
        (
            "styled-smooth",
            {
                "text": "GO ON GO ON",
                "text_size_px": 32,
                "rotation_deg": -18,
                "text_gradient": {"colors": ["#FF0000", "#0000FF"], "angle_deg": 135},
                "shadow_enabled": True,
                "glow_color": "#40FF90",
                "glow_strength": 0.5,
                "motion": {
                    "version": 2,
                    "speed": 0.25,
                    "blur_px": 8,
                    "order": "center-out",
                    "direction": "down",
                    "travel_px": 40,
                },
            },
        ),
        ("smooth-motion", {"motion": {"version": 2, "blur_px": 8, "travel_px": 30}}),
        (
            "smooth-late",
            {
                "text": "GO ON GO ON GO ON",
                "text_size_px": 32,
                "motion": {"version": 2, "speed": 0.25, "blur_px": 8, "order": "reverse"},
            },
        ),
    ]:
        overlay = {**base_overlay, "effect": "smooth-type", **values}
        layer, font = compile_text_overlay(overlay, layer_id=name, canvas=canvas)
        cases.append(frame_case(name, overlay, layer, font, canvas))
    for name, values in [
        ("staggered-legacy", {}),
        (
            "staggered-late",
            {
                "text": "GO ON GO ON",
                "text_size_px": 32,
                "rotation_deg": 23,
                "shadow_enabled": True,
                "outline_px": 2,
                "motion": {"version": 2, "speed": 0.25},
            },
        ),
    ]:
        overlay = {**base_overlay, "effect": "staggered-slice", **values}
        layer, font = compile_text_overlay(overlay, layer_id=name, canvas=canvas)
        cases.append(frame_case(name, overlay, layer, font, canvas))
    for name, values in [
        ("handwriting-legacy", {}),
        (
            "handwriting-glow",
            {
                "glow": True,
                "glow_color": "#40FF80",
                "glow_strength": 0.8,
                "shadow_enabled": True,
                "outline_px": 2,
                "rotation_deg": 23,
            },
        ),
        (
            "handwriting-fading-glow",
            {
                "glow": True,
                "glow_color": "#C040FF",
                "glow_strength": 0.8,
                "motion": {"version": 2, "speed": 0.25, "exit_s": 1.2},
            },
        ),
        (
            "handwriting-gradient",
            {
                "text_gradient": {"colors": ["#FF0000", "#0000FF"], "angle_deg": 135},
                "rotation_deg": -18,
                "motion": {"version": 2, "speed": 0.25},
            },
        ),
    ]:
        overlay = {**base_overlay, "effect": "handwriting", **values}
        layer, font = compile_text_overlay(overlay, layer_id=name, canvas=canvas)
        cases.append(frame_case(name, overlay, layer, font, canvas))
    samples = []
    for duration in [0.02, 0.2, 1, 4, 17]:
        for fraction in [
            -0.1,
            0,
            0.2,
            0.68,
            0.7,
            0.72,
            0.76,
            0.8,
            0.84,
            0.88,
            0.9,
            0.92,
            0.94,
            0.96,
            0.98,
            1,
            1.1,
        ]:
            time = duration * fraction
            samples.append(
                dict(
                    time=time,
                    duration=duration,
                    scale=cloud._giant_title_wipe_scale_at(time, duration),
                    alpha=cloud._clamp_byte(255 * cloud._giant_title_wipe_alpha_at(time, duration))
                    / 255,
                )
            )
    return dict(width=300, height=200, cases=cases, timing=samples)


def frame_case(name, overlay, layer, font, canvas):
    assert PortableTextLayer.model_validate(layer.model_dump()) == layer
    frames = []
    for time in [0, 0.08, 0.2, 1, 2.72, 2.9, 3.05, 3.15, 3.3, 3.6, 3.8, 3.95]:
        image = cloud._draw_frame(overlay, time, 4, render_canvas=canvas)
        pixels = image.toarray(
            colorType=skia.ColorType.kRGBA_8888_ColorType,
            alphaType=skia.AlphaType.kPremul_AlphaType,
        )
        packed = pixels.astype("uint32")
        packed = (
            packed[:, :, 0]
            | (packed[:, :, 1] << 8)
            | (packed[:, :, 2] << 16)
            | (packed[:, :, 3] << 24)
        )
        frames.append(
            dict(
                time=time,
                pixels=[
                    [int(value), sum(1 for _ in values)]
                    for value, values in groupby(packed.ravel())
                ],
            )
        )
        if directory := os.environ.get("KRIA_GIANT_DEBUG_DIR"):
            from PIL import Image

            straight = image.toarray(
                colorType=skia.ColorType.kRGBA_8888_ColorType,
                alphaType=skia.AlphaType.kUnpremul_AlphaType,
            )
            Image.fromarray(straight).save(Path(directory) / f"cloud-{name}-{time}.png")
    return dict(
        id=name,
        layer=layer.model_dump(mode="json"),
        font=font.model_dump(mode="json") if font else None,
        frames=frames,
    )


@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() not in {"x86_64", "AMD64"},
    reason="Exact pixels require production Linux/x86_64; ARM uses different SIMD rounding",
)
@pytest.mark.timeout(300)
def test_giant_fixture_matches_actual_cloud():
    assert_reference_matches(reference(), json.loads(FIXTURE.read_text()))


if __name__ == "__main__" and "--write" in sys.argv:
    if platform.machine() not in {"x86_64", "AMD64"}:
        raise RuntimeError("Generate exact pixel references on production Linux/x86_64")
    write_linux_reference(FIXTURE, reference(), compact=True)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -30001, 30001])
def test_giant_origin_rejects_nonfinite_and_out_of_bounds(value):
    from app.kria.portable_text import GiantTitleTransition

    with pytest.raises(ValueError):
        GiantTitleTransition(origin_x=value, origin_y=0)
    with pytest.raises(ValueError):
        GiantTitleTransition(origin_x=0, origin_y=value)


def test_giant_origin_rejects_unknown_fields():
    from app.kria.portable_text import GiantTitleTransition

    with pytest.raises(ValueError):
        GiantTitleTransition(origin_x=0, origin_y=0, zoom=60)


def test_dissolve_theme_matches_production_static_source(monkeypatch, tmp_path):
    import numpy as np

    canvas = Canvas(300, 200)
    overlay = dict(
        text="GO ON",
        effect="dissolve-out",
        start_s=0,
        end_s=4,
        font_family="Inter",
        text_size_px=62,
        preserve_font_size=True,
        shadow_enabled=False,
        outline_px=0,
    )
    captured = []

    def capture(source, time, duration, **kwargs):
        captured.append((time, duration, kwargs, source.toarray().copy()))
        return source

    monkeypatch.setattr(cloud, "render_dissolve_skia_image", capture)
    monkeypatch.setattr(cloud, "_write_png_pillow", lambda *args: None)
    results = []
    layers = []
    for theme in (None, {"type": "giant-title-wipe", "target_glyph": "O"}):
        candidate = {**overlay, "theme_transition": theme}
        captured.clear()
        cloud._generate_overlay_sequence(candidate, str(tmp_path), 1, render_canvas=canvas)
        results.append(sorted(captured, key=lambda row: row[0]))
        layers.append(
            compile_text_overlay(candidate, layer_id="dissolve", canvas=canvas, dissolve_seed=138)[
                0
            ]
        )
    assert len(results[0]) > 100
    for plain, themed in zip(*results, strict=True):
        assert plain[:3] == themed[:3]
        assert plain[2] == {"seed": 138, "cap_to_webkit": True}
        np.testing.assert_array_equal(plain[3], themed[3])
    assert layers[0] == layers[1]
    assert layers[1].giant_title is None
