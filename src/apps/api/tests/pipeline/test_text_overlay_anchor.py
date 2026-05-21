"""Unit tests for the renderer's text_anchor mode and measure_text_width helper.

text_anchor="left" places the LEFT edge of each rendered line at the anchor x;
"right" places the RIGHT edge there; "center" (default) centers on the anchor.
Used by Layer-2 progressive word-reveal overlays so cumulative text grows
rightward without re-centering as new words appear.

We probe behavior by rendering a PNG and inspecting its pixel column extent,
which is more reliable than mocking PIL internals.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
from PIL import Image

from app.pipeline.text_overlay import (
    CANVAS_W,
    _draw_text_png,
    measure_text_width,
)


def _png_x_extent(png_path: str) -> tuple[int, int]:
    """Return (left_x, right_x) of the bounding box of all non-transparent
    pixels in `png_path`. Used to verify horizontal anchoring."""
    arr = np.array(Image.open(png_path).convert("RGBA"))
    alpha = arr[:, :, 3]
    nonzero_cols = np.where(alpha.sum(axis=0) > 0)[0]
    if nonzero_cols.size == 0:
        return (-1, -1)
    return (int(nonzero_cols.min()), int(nonzero_cols.max()))


# ── measure_text_width ────────────────────────────────────────────────────────


def test_measure_text_width_empty_returns_zero():
    assert measure_text_width("") == 0


def test_measure_text_width_longer_text_is_wider():
    short = measure_text_width("hi", text_size="medium")
    longer = measure_text_width("hello there everyone", text_size="medium")
    assert short > 0
    assert longer > short


def test_measure_text_width_larger_size_is_wider():
    medium = measure_text_width("hello", text_size="medium")
    large = measure_text_width("hello", text_size="large")
    assert large > medium


# ── text_anchor ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_png_path():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_left_anchor_places_text_left_edge_at_anchor_x(tmp_png_path):
    """text_anchor='left' with position_x_frac=0.2 should put the leftmost
    rendered pixel at approximately CANVAS_W * 0.2."""
    anchor_frac = 0.2
    _draw_text_png(
        "hello",
        position="center",
        png_path=tmp_png_path,
        text_size="medium",
        position_x_frac=anchor_frac,
        text_anchor="left",
    )
    left_x, right_x = _png_x_extent(tmp_png_path)
    anchor_x = int(CANVAS_W * anchor_frac)
    # Allow a few pixels of slack for shadow/stroke bleed (the gaussian shadow
    # blurs ~12px outside the glyph; that pushes left_x slightly to the left).
    # The key invariant: the rendered text starts AT or NEAR the anchor, NOT
    # centered on it. With a centered "hello" at anchor_x=216, left_x would be
    # roughly 216 - hello_width/2 ≈ 130; with left anchor it should be ≥ ~200.
    assert left_x >= anchor_x - 25, (
        f"left-anchored text should not start far left of anchor_x={anchor_x}, "
        f"got left_x={left_x}"
    )
    assert right_x > anchor_x, "text should extend rightward from the anchor"


def test_center_anchor_is_unchanged_default(tmp_png_path):
    """Default text_anchor='center' must preserve historical centering: text's
    midpoint sits on position_x_frac. This is a regression lock for every
    existing overlay in prod."""
    anchor_frac = 0.5
    _draw_text_png(
        "hello",
        position="center",
        png_path=tmp_png_path,
        text_size="medium",
        position_x_frac=anchor_frac,
        # text_anchor defaults to "center"
    )
    left_x, right_x = _png_x_extent(tmp_png_path)
    midpoint = (left_x + right_x) // 2
    anchor_x = int(CANVAS_W * anchor_frac)
    # Centered text's pixel midpoint should be within a small slack of anchor_x.
    # Slack covers shadow asymmetry + emoji-prefix logic + sub-pixel rounding.
    assert abs(midpoint - anchor_x) < 20, (
        f"center-anchored text midpoint {midpoint} should align with anchor_x={anchor_x}"
    )


def test_right_anchor_places_text_right_edge_at_anchor_x(tmp_png_path):
    """text_anchor='right' with position_x_frac=0.8 should put the rightmost
    rendered pixel at approximately CANVAS_W * 0.8."""
    anchor_frac = 0.8
    _draw_text_png(
        "hello",
        position="center",
        png_path=tmp_png_path,
        text_size="medium",
        position_x_frac=anchor_frac,
        text_anchor="right",
    )
    left_x, right_x = _png_x_extent(tmp_png_path)
    anchor_x = int(CANVAS_W * anchor_frac)
    # Slack covers the gaussian shadow (radius 12) extending pixels past the
    # glyph bbox on the right.
    assert right_x <= anchor_x + 35, (
        f"right-anchored text should not extend far right of anchor_x={anchor_x}, "
        f"got right_x={right_x}"
    )
    assert left_x < anchor_x, "text should extend leftward from the anchor"


def test_left_anchor_with_emoji_prefix_anchors_emoji_at_anchor_x(tmp_png_path):
    """text_anchor='left' + emoji_prefix: the emoji's left edge is at the
    anchor x, and text follows to its right with a gap. Covers the code path
    at text_overlay.py:1751-1758 where the emoji+line-1 combined block is
    positioned. Without this test the emoji+anchor combination is unverified.

    Uses a common emoji ('🔥' U+1F525) with a known Twemoji asset bundled in
    assets/emoji. If the asset is missing on a test runner, the function
    silently logs and falls back to text-only — in which case the test
    degenerates into the no-emoji left-anchor case, which is fine."""
    anchor_frac = 0.15
    _draw_text_png(
        "hot",
        position="center",
        png_path=tmp_png_path,
        text_size="medium",
        position_x_frac=anchor_frac,
        text_anchor="left",
        emoji_prefix="\U0001f525",  # 🔥
    )
    left_x, right_x = _png_x_extent(tmp_png_path)
    anchor_x = int(CANVAS_W * anchor_frac)
    # Left edge of the whole block (emoji or text) should sit near anchor_x,
    # not far to the left as it would for center-anchor.
    assert left_x >= anchor_x - 25, (
        f"left-anchored emoji+text should not start far left of anchor_x={anchor_x}, "
        f"got left_x={left_x}"
    )
    # Block must extend rightward.
    assert right_x > anchor_x + 30


def test_left_anchor_cumulative_keeps_first_word_position_stable(tmp_png_path):
    """Two left-anchored renders at the same anchor_x — one with 'good' and one
    with 'good morning' — must place 'good' at the same left edge. This is the
    core invariant for progressive word reveal: prior words don't shift when
    a new word is added.

    We render both at the same anchor and compare the left edge. With center
    anchor, adding 'morning' would shift 'good' leftward; with left anchor it
    must NOT shift."""
    anchor_frac = 0.1

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1:
        path_short = f1.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
        path_long = f2.name

    try:
        _draw_text_png(
            "good",
            position="center",
            png_path=path_short,
            text_size="medium",
            position_x_frac=anchor_frac,
            text_anchor="left",
        )
        _draw_text_png(
            "good morning",
            position="center",
            png_path=path_long,
            text_size="medium",
            position_x_frac=anchor_frac,
            text_anchor="left",
        )
        left_short, _ = _png_x_extent(path_short)
        left_long, right_long = _png_x_extent(path_long)
        # Left edges of 'good' must match (within shadow slack) in both renders.
        # This is the invariant that makes the progressive reveal not look like
        # a reflow — the first word stays put as new words enter to the right.
        assert abs(left_short - left_long) < 10, (
            f"left edge should be stable across cumulative stages: "
            f"short={left_short}, long={left_long}"
        )
        # And the longer text should extend further right than the short one.
        _, right_short = _png_x_extent(path_short)
        assert right_long > right_short
    finally:
        for p in (path_short, path_long):
            if os.path.exists(p):
                os.unlink(p)
