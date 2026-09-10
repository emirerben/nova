"""Capture actual production word placement and color at timestamp boundaries."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import skia
from pydantic import ValidationError

from app.kria.portable_text import PortableTextLayer
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_karaoke_layout.json"


def reference():
    assert (
        Path(cloud.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / "app")
    )
    cases = []
    for anchor in ["left", "center", "right"]:
        overlay = {
            "text": "fallback",
            "display_text": "ignored",
            "effect": "karaoke-line",
            "start_s": 1,
            "end_s": 5,
            "text_size_px": 32,
            "font_family": "Inter",
            "text_color": "#FFFFFF",
            "highlight_color": "#FF3000",
            "text_anchor": anchor,
            "shadow_enabled": True,
            "outline_px": 2,
            "letter_spacing": 0.03,
            "max_width_frac": 0.7,
            "rotation_deg": 0,
            "shape_text": True,
            "motion": {"version": 2, "speed": 0.2},
            "word_timings": [
                {"text": "Hello", "start_s": 0.5, "end_s": 0.7},
                {"text": " ", "start_s": 200},
                {"text": "world", "start_s": 0.2, "end_s": 0.3},
                {"text": "later", "duration_cs": 20},
                {"text": "now", "start_s": -1, "end_s": -2},
                {"text": "last", "start_s": "bad", "end_s": None},
            ],
        }
        canvas = Canvas(300, 200)
        layer, font = compile_text_overlay(overlay, layer_id="karaoke", canvas=canvas)
        samples = []
        for time in [0, 0.199, 0.2, 0.499, 0.5, 0.7, 0.9, 3.9]:
            calls = []

            def draw(*args, **kwargs):
                color = args[5]
                calls.append(
                    {
                        "text": args[1],
                        "x": args[2],
                        "baseline_y": args[3],
                        "font_size": args[4].getSize(),
                        "stroke": args[6],
                        "color": [
                            skia.ColorGetR(color),
                            skia.ColorGetG(color),
                            skia.ColorGetB(color),
                            skia.ColorGetA(color),
                        ],
                    }
                )

            with patch.object(cloud, "_draw_line_with_layers", side_effect=draw):
                cloud._draw_karaoke_line(None, overlay, time, 4, render_canvas=canvas)
            for run, start, call in zip(layer.runs, layer.karaoke.starts, calls, strict=True):
                assert (run.text, run.x, run.baseline_y, run.font_size) == (
                    call["text"],
                    call["x"],
                    call["baseline_y"],
                    call["font_size"],
                )
                color = layer.karaoke.highlight if start <= time else run.fill
                assert [
                    round(v * 255) for v in [color.red, color.green, color.blue, color.alpha]
                ] == call["color"]
                assert run.stroke_width == call["stroke"] * 2
            samples.append({"time": time, "calls": calls})
        assert layer.motion is None
        cases.append(
            {
                "layer": layer.model_dump(mode="json"),
                "font": font.model_dump(mode="json"),
                "samples": samples,
            }
        )
    return cases


def test_production_word_geometry_and_color_match_fixture():
    assert reference() == json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("timings", [[], [{"text": " "}]])
def test_empty_karaoke_uses_raw_text_static_fallback(timings):
    overlay = {
        "text": "Raw text",
        "display_text": "ignored",
        "start_s": 0,
        "end_s": 4,
        "effect": "karaoke-line",
        "word_timings": timings,
        "font_family": "Inter",
    }
    layer, _ = compile_text_overlay(overlay, layer_id="fallback", canvas=Canvas(300, 200))
    assert layer.effect == "static"
    assert " ".join(run.text for run in layer.runs) == "Raw text"


@pytest.mark.parametrize("bad", ["missing", "count", "negative", "nan", "shape", "wrong_effect"])
def test_rejects_malformed_karaoke(bad):
    layer = json.loads(FIXTURE.read_text())[0]["layer"]
    if bad == "missing":
        layer["karaoke"] = None
    elif bad == "count":
        layer["karaoke"]["starts"].pop()
    elif bad in {"negative", "nan"}:
        layer["karaoke"]["starts"][0] = -1 if bad == "negative" else float("nan")
    elif bad == "shape":
        layer["runs"][0]["shaped"] = True
    else:
        layer["effect"] = "static"
    with pytest.raises(ValidationError):
        PortableTextLayer.model_validate(layer)


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), ensure_ascii=False, indent=2) + "\n")
