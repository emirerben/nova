"""Actual cloud giant-title frames and resolved origins for native comparison."""

import json
import os
import sys
from itertools import groupby
from pathlib import Path

import pytest
import skia

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

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
                motion={"version": 2, "speed": 3, "cursor_style": "underscore"},
            ),
        ),
    ]:
        overlay = {**base_overlay, **values}
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
        font=font.model_dump(mode="json"),
        frames=frames,
    )


def test_giant_fixture_matches_actual_cloud():
    assert json.loads(FIXTURE.read_text()) == reference()


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), separators=(",", ":")) + "\n")


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
