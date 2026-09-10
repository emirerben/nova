"""Native reveal run geometry is pinned to real cloud line drawing calls."""

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

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_reveal_layout.json"


def reference():
    cases = []
    for effect in ["typewriter", "stream-in"]:
        for anchor in ["left", "center", "right"]:
            for shaped in [False, True]:
                overlay = {
                    "text": "  İyi günler\n\nCafé hello world",
                    "start_s": 2,
                    "end_s": 6,
                    "effect": effect,
                    "text_size_px": 32,
                    "font_family": "Inter",
                    "shape_text": shaped,
                    "text_anchor": anchor,
                    "shadow_enabled": False,
                    "letter_spacing": 0.03,
                    "rotation_deg": 23,
                    "motion": {"version": 2, "cursor_style": "underscore", "intensity": 0.8},
                }
                canvas = Canvas(300, 200)
                layer, font = compile_text_overlay(overlay, layer_id="reveal", canvas=canvas)
                samples = []
                for time in [0, 0.1, 0.3, 0.5, 0.9, 1.2, 2, 3.5]:
                    surface = skia.Surface(300, 200)
                    with patch("app.pipeline.text_overlay_skia._draw_line_with_layers") as draw:
                        cloud._draw_with_animation(
                            surface.getCanvas(), overlay, time, 4, render_canvas=canvas
                        )
                    runs = [
                        {"text": call.args[1], "x": call.args[2], "baseline_y": call.args[3]}
                        for call in draw.call_args_list
                    ]
                    samples.append({"time": time, "runs": runs})
                cases.append(
                    {
                        "layer": layer.model_dump(mode="json"),
                        "font": font.model_dump(mode="json"),
                        "samples": samples,
                    }
                )
    return cases


def test_reference_matches_actual_cloud_line_positions():
    assert json.loads(FIXTURE.read_text()) == reference()


@pytest.mark.parametrize("failure", ["offset", "index", "cursor"])
def test_reveal_rejects_geometry_that_cannot_be_sampled(failure):
    layer = json.loads(FIXTURE.read_text())[0]["layer"]
    line = layer["discrete_reveal"]["lines"][0]
    if failure == "offset":
        line["cursor_offsets"].pop()
    elif failure == "index":
        line["run_index"] = 99
    else:
        line["cursor_run"]["text"] = "unexpected text"
    with pytest.raises(ValidationError):
        PortableTextLayer.model_validate(layer)


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2, ensure_ascii=False) + "\n")
