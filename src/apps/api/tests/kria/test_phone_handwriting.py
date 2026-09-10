"""Actual cloud centerline paths are the native handwriting reference."""

import json
import sys
from pathlib import Path

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_handwriting.json"


def reference():
    canvas = Canvas(300, 200)
    overlay = {
        "text": "Hi!\nİyi günler",
        "start_s": 0,
        "end_s": 3,
        "effect": "handwriting",
        "text_size_px": 32,
        "font_family": "Inter",
        "shadow_enabled": False,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "rotation_deg": 23,
    }
    layer, font = compile_text_overlay(overlay, layer_id="pen", canvas=canvas)
    assert font is None

    class Capture:
        def __init__(self):
            self.paths = []

        def save(self):
            pass

        def restore(self):
            pass

        def translate(self, *args):
            pass

        def rotate(self, *args):
            pass

        def drawPath(self, path, paint):
            self.paths.append([{"x": point.x(), "y": point.y()} for point in path.getPoints()])

    samples = []
    for progress in [0, 0.01, 0.25, 0.6, 1]:
        capture = Capture()
        cloud._draw_handwriting_strokes(
            capture, overlay["text"], overlay, progress, render_canvas=canvas
        )
        samples.append({"progress": progress, "paths": capture.paths})
    return {"layer": layer.model_dump(mode="json"), "samples": samples}


def test_reference_tracks_real_cloud_paths():
    assert json.loads(FIXTURE.read_text()) == reference()


def test_handwriting_uses_centerline_content_without_a_font():
    layer = reference()["layer"]
    assert layer["runs"] == []
    assert layer["handwriting"]["strokes"]
    assert layer["handwriting"]["ink_width"] > 0
    assert layer["handwriting"]["blur_layers"] == []


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2) + "\n")
