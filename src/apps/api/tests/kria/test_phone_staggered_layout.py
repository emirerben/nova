"""Portable glyph geometry is captured against the real staggered draw function."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.kria.portable_text import PortableTextLayer
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_staggered_layout.json"


class Capture:
    def __init__(self):
        self.alpha = 255
        self.translations = []
        self.rotation = 0

    def saveLayerAlpha(self, _, alpha):
        self.alpha = alpha
        self.translations = []
        self.rotation = 0

    def translate(self, x, y):
        self.translations.append([x, y])

    def rotate(self, angle):
        self.rotation = angle

    def restore(self):
        pass


def reference():
    cases = []
    for anchor in ["left", "center", "right"]:
        for legacy in [False, True]:
            overlay = {
                "text": "Hello world rest\nLater line",
                "start_s": 2,
                "end_s": 6,
                "effect": "staggered-slice",
                "text_size_px": 32,
                "font_family": "Inter",
                "shadow_enabled": True,
                "outline_px": 2,
                "rotation_deg": 23,
                "text_anchor": anchor,
                "max_width_frac": 0.65,
                "preserve_font_size": True,
                "letter_spacing": 0.03,
            }
            if not legacy:
                overlay["motion"] = {"version": 2, "speed": 0.8, "intensity": 0.8}
            canvas = Canvas(300, 200)
            layer, font = compile_text_overlay(overlay, layer_id="staggered", canvas=canvas)
            samples = []
            for time in [0, 0.1, 0.3, 0.7, 1.2, 1.5, 2, 3.5]:
                capture = Capture()
                calls = []

                def draw(*args, **kwargs):
                    calls.append(
                        {
                            "text": args[1],
                            "x": args[2],
                            "baseline_y": args[3],
                            "alpha": capture.alpha,
                            "rotation": capture.rotation,
                            "translations": capture.translations[:],
                        }
                    )

                with (
                    patch.object(cloud, "_draw_line_with_layers", side_effect=draw),
                    patch.object(cloud, "_draw_centered_text") as settled,
                ):
                    cloud._draw_staggered_slice(capture, overlay, time, 4, render_canvas=canvas)
                samples.append({"time": time, "settled": settled.called, "calls": calls})
            cases.append(
                {
                    "layer": layer.model_dump(mode="json"),
                    "font": font.model_dump(mode="json"),
                    "samples": samples,
                }
            )
    return cases


def test_actual_cloud_glyph_draw_matches_fixture():
    assert json.loads(FIXTURE.read_text()) == reference()


@pytest.mark.parametrize("failure", ["index", "order", "text", "shape", "unnormalized"])
def test_rejects_invalid_staggered_geometry(failure):
    layer = json.loads(FIXTURE.read_text())[0]["layer"]
    content = layer["staggered"]
    glyph = content["glyphs"][0]
    if failure == "index":
        glyph["glyph_index"] = 4999
    elif failure == "order":
        content["glyphs"] = list(reversed(content["glyphs"]))
    elif failure == "text":
        glyph["run"]["text"] = "wrong"
    elif failure == "shape":
        glyph["run"]["shaped"] = True
    else:
        content["text"] = " " + content["text"]
    with pytest.raises(ValidationError):
        PortableTextLayer.model_validate(layer)


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), ensure_ascii=False, indent=2) + "\n")
