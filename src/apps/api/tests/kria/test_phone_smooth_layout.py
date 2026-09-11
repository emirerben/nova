"""Compiler geometry and native masks reference production line clipping."""

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
from tests.kria.test_phone_smooth_clips import Capture

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_smooth_layout.json"


def reference():
    cases = []
    for order in ["forward", "reverse", "center-out"]:
        for legacy in [False, True]:
            overlay = {
                "text": "İyi günler\n\nCafé hello",
                "start_s": 2,
                "end_s": 6,
                "effect": "smooth-type",
                "text_size_px": 32,
                "font_family": "Inter",
                "shadow_enabled": True,
                "outline_px": 2,
                "rotation_deg": 23,
                "preserve_font_size": True,
            }
            if not legacy:
                overlay["motion"] = {"version": 2, "order": order, "blur_px": 7, "intensity": 0.8}
            canvas = Canvas(300, 200)
            layer, font = compile_text_overlay(overlay, layer_id="smooth", canvas=canvas)
            samples = []
            for time in [0, 0.1, 0.3, 0.7, 1.5, 3.5]:
                capture = Capture()
                calls = []

                def draw(*args, **kwargs):
                    if args[1]:
                        calls.append(
                            {
                                "text": args[1],
                                "clip": capture.clip,
                                "x": args[2],
                                "baseline_y": args[3],
                            }
                        )

                with patch.object(cloud, "_draw_line_with_layers", side_effect=draw):
                    cloud._draw_centered_text(
                        capture,
                        overlay["text"],
                        overlay,
                        shape_text=True,
                        smooth_t_local=None if legacy else time,
                        smooth_motion=overlay.get("motion"),
                        render_canvas=canvas,
                    )
                samples.append({"time": time, "calls": calls})
            cases.append(
                {
                    "layer": layer.model_dump(mode="json"),
                    "font": font.model_dump(mode="json"),
                    "samples": samples,
                }
            )
    return cases


def test_actual_cloud_geometry_matches_fixture():
    assert json.loads(FIXTURE.read_text()) == reference()


@pytest.mark.parametrize("failure", ["index", "bounds", "text", "shape", "effect"])
def test_rejects_invalid_smooth_geometry(failure):
    layer = json.loads(FIXTURE.read_text())[0]["layer"]
    line = layer["smooth_reveal"]["lines"][0]
    if failure == "index":
        line["run_index"] = 99
    elif failure == "bounds":
        line["bounds"] = None
    elif failure == "text":
        line["text"] = "wrong"
    elif failure == "shape":
        layer["runs"][0]["shaped"] = False
    else:
        layer["effect"] = "static"
    with pytest.raises(ValidationError):
        PortableTextLayer.model_validate(layer)


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), ensure_ascii=False, indent=2) + "\n")
