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
        f"left-anchored text should not start far left of anchor_x={anchor_x}, got left_x={left_x}"
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


# ── Vertical anchor: text_anchor=left → top-anchored ──────────────────────────


def _png_y_extent(png_path: str) -> tuple[int, int]:
    """Return (top_y, bottom_y) of the bounding box of all non-transparent
    pixels. Mirror of _png_x_extent for vertical-anchor checks."""
    arr = np.array(Image.open(png_path).convert("RGBA"))
    alpha = arr[:, :, 3]
    nonzero_rows = np.where(alpha.sum(axis=1) > 0)[0]
    if nonzero_rows.size == 0:
        return (-1, -1)
    return (int(nonzero_rows.min()), int(nonzero_rows.max()))


def test_left_anchor_stays_top_anchored_when_text_wraps():
    """`text_anchor="left"` treats position_y_frac as the TOP of the first
    line. Adding words that wrap to additional lines must NOT shift the
    earlier (already-visible) text upward.

    The historical center-anchor semantic kept the BLOCK center stable, so a
    cumulative reveal that wrapped from 1 line → 2 lines moved the first
    line upward (the block grew, but its center stayed put). For progressive
    word-reveal that reads as the earlier text "jumping" upward as new words
    arrive — visually wrong. This test pins the corrected semantic for the
    left-anchor mode.
    """
    y_frac = 0.5
    with (
        tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1,
        tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2,
    ):
        path_short = f1.name
        path_long = f2.name
    try:
        # Short text — fits on one line at 120 px / 5% left margin.
        _draw_text_png(
            "hello",
            position="center",
            png_path=path_short,
            text_size="large",
            text_size_px=120,
            position_x_frac=0.05,
            position_y_frac=y_frac,
            text_anchor="left",
        )
        # Long text — wraps to multiple lines at 120 px / 5% left margin.
        _draw_text_png(
            "hello there everyone this is a long line that absolutely must wrap",
            position="center",
            png_path=path_long,
            text_size="large",
            text_size_px=120,
            position_x_frac=0.05,
            position_y_frac=y_frac,
            text_anchor="left",
        )
        top_short, bottom_short = _png_y_extent(path_short)
        top_long, bottom_long = _png_y_extent(path_long)
        # Top edges of the first line must match within shadow slack across
        # the two renders — adding more words below cannot pull earlier text
        # upward.
        assert abs(top_short - top_long) < 30, (
            f"top edge of first line should stay anchored across cumulative "
            f"stages: short={top_short}, long={top_long}"
        )
        # The wrapped (longer) version must extend further down than the
        # single-line short version.
        assert bottom_long > bottom_short
    finally:
        for p in (path_short, path_long):
            if os.path.exists(p):
                os.unlink(p)


def test_center_anchor_keeps_block_center_when_text_wraps():
    """Center-anchor mode (default) is unchanged: the block center stays at
    position_y_frac, so a long wrapped block extends both up and down from
    the anchor. Locks that the new top-anchor branch doesn't regress
    historical center behavior.
    """
    y_frac = 0.5
    with (
        tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f1,
        tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2,
    ):
        path_short = f1.name
        path_long = f2.name
    try:
        _draw_text_png(
            "hello",
            position="center",
            png_path=path_short,
            text_size="large",
            text_size_px=120,
            position_y_frac=y_frac,
            text_anchor="center",
        )
        _draw_text_png(
            "hello there everyone this is a long line that absolutely must wrap",
            position="center",
            png_path=path_long,
            text_size="large",
            text_size_px=120,
            position_y_frac=y_frac,
            text_anchor="center",
        )
        top_short, bottom_short = _png_y_extent(path_short)
        top_long, bottom_long = _png_y_extent(path_long)
        # In center-anchor mode the long block's top is ABOVE the short
        # block's top (block grew up + down from its center).
        assert top_long < top_short, (
            f"center-anchor: long block should extend upward from anchor: "
            f"short_top={top_short}, long_top={top_long}"
        )
        assert bottom_long > bottom_short
    finally:
        for p in (path_short, path_long):
            if os.path.exists(p):
                os.unlink(p)


def test_left_anchor_baseline_stable_across_cumulative_stages():
    """As a cumulative reveal grows N→N+1 words on line 2, the BASELINE of
    line 2 must NOT drift — that's what determines whether earlier letters
    (like the 'y' in "you") appear to move on screen.

    PIL's default text anchor ('la') positions y at the font's ascent line,
    but the rendered bbox top varies with which glyphs appear (top of "you"
    sits at x-height; top of "you put in" sits at ascender height because
    of the 't' / 'i'). With 'la', adding 'put' to "you" pushes earlier
    letters DOWN — exactly what the user reported on template 89cde014.
    The renderer uses anchor='ls' (left, baseline) instead, which is
    font-intrinsic and stable regardless of glyph mix.

    We probe baseline stability via the BOTTOM of the line — bottom =
    baseline + descender_extent, both font-intrinsic constants — so a
    stable baseline means a stable bottom across stages. The TOP can still
    vary (new ascenders extend higher) but that's correct typography: new
    letters appear above the x-height line without disturbing existing
    letters below.
    """
    stages = [
        "It's not if luck you",
        "It's not if luck you put",
        "It's not if luck you put in",
    ]
    paths: list[str] = []
    try:
        for stage_text in stages:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                p = f.name
            paths.append(p)
            _draw_text_png(
                stage_text,
                position="center",
                png_path=p,
                text_size="large",
                text_size_px=120,
                position_x_frac=0.05,
                position_y_frac=0.44,
                text_anchor="left",
            )
        line2_bottoms: list[int] = []
        for p in paths[1:]:  # stage 0 fits on 1 line, no line 2 to measure
            arr = np.array(Image.open(p).convert("RGBA"))
            alpha = arr[:, :, 3]
            row_density = (alpha > 30).sum(axis=1)
            nonzero = np.where(row_density > 5)[0]
            if nonzero.size == 0:
                continue
            # Find lines by row-gap detection; line 2's bottom is the last
            # contiguous non-empty row in the last detected line.
            gaps = np.where(np.diff(nonzero) > 10)[0]
            if gaps.size == 0:
                # Single line — shouldn't happen for these stages.
                continue
            line_ends = [nonzero[g] for g in gaps] + [nonzero[-1]]
            line2_bottoms.append(int(line_ends[-1]))
        assert len(line2_bottoms) >= 2
        assert max(line2_bottoms) - min(line2_bottoms) < 5, (
            f"line-2 baseline (proxied by bottom) must stay stable across "
            f"cumulative stages; got line2_bottoms={line2_bottoms}. The 'y' in "
            "'you' must not move when 'put' / 'in' are added."
        )
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)
