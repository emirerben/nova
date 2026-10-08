"""Pixel-level proof for KRI-524's compiled text sequence renderer lane.

This is an offline render check: the editor compiler produces the child text
elements, the production generative-overlay adapter converts them to Skia burn
dictionaries, and the production Skia frame function renders each child.  It
does not render a full video or claim FFmpeg compositing coverage.
"""

from __future__ import annotations

import io

from PIL import Image

from app.agents._schemas.text_element import TextElement
from app.pipeline import generative_overlays
from app.pipeline import text_overlay_skia as skia_renderer
from app.services.kria_editor_ops import compile_editor_ops
from tests.agents.test_editor_text_sequence import _job, _op, _variant


def _rendered_sequence() -> tuple[list[dict], list[dict]]:
    variant = _variant("Morning coffee afternoon walk evening lights", start=0.0, end=6.0)
    op = {
        **_op(["Morning coffee", "afternoon walk", "evening lights"]),
        "expected_source_text": variant["text_elements"][0]["text"],
        "target_ids": ["title"],
        "expected_count": 1,
        "patch": {
            "animation_phases": {
                "entrance": "fade",
                "exit": "fade",
                "loop": "none",
                "speed": 1,
            }
        },
    }
    compiled = compile_editor_ops(_job(variant), variant, [op])
    children = compiled.payload.text_elements[:3]
    elements = [TextElement.model_validate(row) for row in children]
    overlays = generative_overlays.build_overlays_from_text_elements(
        elements,
        video_duration_s=6.0,
    )
    return children, overlays


def _alpha_stats(overlay: dict, local_time: float) -> tuple[int, int]:
    image = skia_renderer._draw_frame(
        overlay,
        local_time,
        float(overlay["end_s"] - overlay["start_s"]),
    )
    pil = Image.open(io.BytesIO(bytes(image.encodeToData()))).convert("RGBA")
    alpha = pil.getchannel("A")
    extrema = alpha.getextrema()
    return extrema[1], sum(1 for value in alpha.getdata() if value > 0)


def test_compiled_sequence_renders_fade_edges_and_opaque_holds() -> None:
    children, overlays = _rendered_sequence()
    assert len(children) == len(overlays) == 3
    assert [row["start_s"] for row in children] == [0.0, 2.0, 4.0]
    assert [row["end_s"] for row in children] == [2.0, 4.0, 6.0]
    assert all(overlay["animation_phases"]["entrance"] == "fade" for overlay in overlays)
    assert all(overlay["animation_phases"]["exit"] == "fade" for overlay in overlays)

    for overlay in overlays:
        duration = float(overlay["end_s"] - overlay["start_s"])
        at_start, start_pixels = _alpha_stats(overlay, 0.0)
        at_hold, hold_pixels = _alpha_stats(overlay, duration / 2)
        at_end, end_pixels = _alpha_stats(overlay, duration)
        assert at_start == 0 and start_pixels == 0
        assert at_hold == 255 and hold_pixels > 0
        assert at_end == 0 and end_pixels == 0


def test_compiled_sequence_has_contiguous_non_overlapping_pixel_windows() -> None:
    children, overlays = _rendered_sequence()
    intervals = [(float(row["start_s"]), float(row["end_s"])) for row in children]
    assert all(end > start for start, end in intervals)
    assert all(left[1] == right[0] for left, right in zip(intervals, intervals[1:]))
    assert all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:]))

    # At each shared boundary, both production-rendered children are transparent
    # at their local edge. This guards against an accidental overlap/crossfade
    # introduced by the adapter rather than relying only on arithmetic windows.
    for left, right in zip(overlays, overlays[1:]):
        left_max, left_pixels = _alpha_stats(left, float(left["end_s"] - left["start_s"]))
        right_max, right_pixels = _alpha_stats(right, 0.0)
        assert (left_max, left_pixels) == (0, 0)
        assert (right_max, right_pixels) == (0, 0)
