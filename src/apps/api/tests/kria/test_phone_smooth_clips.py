"""Reference native per-line masks against actual cloud canvas clipping calls."""

import json
import sys
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay
from tests.kria.reference_assertions import assert_reference_matches, write_linux_reference

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_smooth_clips.json"


class Capture:
    def __init__(self):
        self.clip = None
        self.stack = []

    def save(self):
        self.stack.append(self.clip)

    def restore(self):
        self.clip = self.stack.pop()

    def clipRect(self, rect, **kwargs):
        self.clip = [rect.left(), rect.top(), rect.right(), rect.bottom()]

    def translate(self, *args):
        pass

    def rotate(self, *args):
        pass

    def scale(self, *args):
        pass


def reference():
    result = []
    for anchor in ["left", "center", "right"]:
        for order in ["forward", "reverse", "center-out"]:
            for styled in [False, True]:
                overlay = {
                    "text_size_px": 32,
                    "font_family": "Inter",
                    "text_anchor": anchor,
                    "shadow_enabled": styled,
                    "outline_px": 3 if styled else 0,
                    "glow_strength": 0.4 if styled else 0,
                    "rotation_deg": 23,
                    "preserve_font_size": True,
                }
                text = "Hi!\nשלום\n123 مرحبا\n\nİyi"
                canvas = Canvas(500, 400)
                face = cloud._resolve_typeface_for_overlay(overlay)
                font, size, lines = cloud._wrap_at_fixed_size(
                    text,
                    face.typeface,
                    32,
                    cloud._overlay_max_width_px(overlay, canvas),
                    0,
                    shape_text=True,
                )
                block = cloud._measure_block(
                    font,
                    lines,
                    line_spacing=cloud.resolve_line_spacing(None),
                    letter_spacing_px=0,
                    shape_text=True,
                )
                cx, cy = cloud._resolve_anchor(overlay, canvas)
                top = cloud._vertical_block_top(
                    cloud._resolve_vertical_anchor(overlay), cy, block["block_h"]
                )
                shadow = cloud._text_shadow_bleed_px(cloud._text_shadow_style(overlay))
                bleed = [max(5 if styled else 2, part, 62 if styled else 0) for part in shadow]
                geometry = []
                for index, line in enumerate(lines):
                    if not line:
                        geometry.append(None)
                        continue
                    width = block["widths"][index]
                    left = cloud._anchored_left_x(anchor, cx, width)
                    first = next(
                        (
                            unicodedata.bidirectional(c)
                            for c in line
                            if unicodedata.bidirectional(c) in {"R", "AL", "AN", "L"}
                        ),
                        "L",
                    )
                    geometry.append(
                        {
                            "text": line,
                            "rtl": first in {"R", "AL", "AN"},
                            "bounds": [
                                left - bleed[0],
                                top + index * block["line_step"] - bleed[1],
                                left + width + bleed[2],
                                top + (index + 1) * block["line_step"] + bleed[3],
                            ],
                        }
                    )
                compiled, _ = compile_text_overlay(
                    {
                        **overlay,
                        "text": text,
                        "start_s": 0,
                        "end_s": 4,
                        "effect": "smooth-type",
                        "motion": {"version": 2, "order": order},
                    },
                    layer_id="smooth",
                    canvas=canvas,
                )
                for actual, expected in zip(compiled.smooth_reveal.lines, geometry, strict=True):
                    if expected is None:
                        assert actual.text == "" and actual.bounds is None
                    else:
                        assert actual.text == expected["text"]
                        assert actual.first_strong_rtl == expected["rtl"]
                        assert list(actual.bounds.model_dump().values()) == expected["bounds"]
                samples = []
                for progress in [0, 0.01, 0.4, 0.99, 1]:
                    capture = Capture()
                    calls = []

                    def draw(*args, **kwargs):
                        if args[1]:
                            calls.append({"text": args[1], "clip": capture.clip})

                    with (
                        patch.object(
                            cloud,
                            "smooth_type_line_progresses",
                            return_value=[progress] * len(lines),
                        ),
                        patch.object(cloud, "_draw_line_with_layers", side_effect=draw),
                    ):
                        cloud._draw_centered_text(
                            capture,
                            text,
                            overlay,
                            shape_text=True,
                            smooth_t_local=0.1,
                            smooth_motion={"version": 2, "order": order},
                            render_canvas=canvas,
                        )
                    samples.append({"progress": progress, "calls": calls})
                result.append(
                    {
                        "order": order,
                        "lines": [line for line in geometry if line],
                        "samples": samples,
                    }
                )
    return result


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Canonical Skia font fixtures use Linux/FreeType; verify in the production Docker image",
)
def test_reference_matches_actual_cloud_masks():
    assert_reference_matches(reference(), json.loads(FIXTURE.read_text()))


if __name__ == "__main__" and "--write" in sys.argv:
    write_linux_reference(FIXTURE, reference())
