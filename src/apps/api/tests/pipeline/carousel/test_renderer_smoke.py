"""Smoke tests for the Blossom-carousel Skia frame renderer.

These tests call into `render_carousel_frames`, which lazy-imports skia (see
the module docstring in `renderer.py` — the tests/evals CI job lacks libEGL).
`test_text_overlay_skia.py` gets away with an unguarded top-level
`import skia` because it never runs in that job; this file can't assume that,
so it importorskips at module scope.

Deliberately independent of Lane B (`effects.py`): every render call passes
its own `transform_fn` (the renderer's one intentional signature deviation —
see `renderer.render_carousel_frames`'s docstring) so this test suite never
depends on `effects.transform_for`'s math landing first. `cards.py` (ffmpeg
frame extraction) is exercised separately by Lane D's segment test — these
tests build `CardAsset`s directly from Pillow-generated PNGs, so no ffmpeg
call is needed here.
"""

from __future__ import annotations

import math
import os
from dataclasses import replace

import pytest
from PIL import Image

pytest.importorskip("skia")

from app.pipeline.carousel.cards import CardAsset
from app.pipeline.carousel.effects import CardGeometry, CardTransform
from app.pipeline.carousel.renderer import (
    CANVAS_H,
    CANVAS_W,
    PERSPECTIVE_PX,
    project_card_corners,
    render_carousel_frames,
)
from app.pipeline.carousel.spring import SpringFrame

BACKGROUND_RGB = (10, 10, 12)
GEO = CardGeometry(card_w=300.0, card_h=400.0, gap=20.0)


def _make_card(tmp_path, index: int, color: tuple[int, int, int]) -> CardAsset:
    # Odd, non-16:9-ish source size to exercise the cover-crop path.
    path = os.path.join(tmp_path, f"card_src_{index:02d}.png")
    Image.new("RGB", (800, 600), color).save(path)
    return CardAsset(index=index, image_path=str(path))


@pytest.fixture
def cards(tmp_path):
    return [
        _make_card(tmp_path, 0, (220, 40, 40)),  # red
        _make_card(tmp_path, 1, (40, 200, 60)),  # green
        _make_card(tmp_path, 2, (40, 80, 230)),  # blue
    ]


def _fake_transform(effect, scroll_x, card_index, geo, viewport_w):
    """Stand-in for `effects.transform_for`, in the shape the real cover-flow
    style dispatcher is expected to produce (see the CardTransform contract in
    effects.py) — three cards laid out side by side, centered vertically, with
    off-axis cards rotated/pushed back and scroll_x shifting all of them
    horizontally so frames actually differ across a spring trace."""
    i = card_index
    return CardTransform(
        x=viewport_w / 2 - geo.card_w / 2 + (i - 1) * (geo.card_w + geo.gap) + scroll_x,
        y=(CANVAS_H - geo.card_h) / 2,
        scale=0.8 + 0.1 * i,
        rotate_y_deg=(i - 1) * 25,
        translate_z_px=-100 * abs(i - 1),
        z_index=-abs(i - 1),
        opacity=1.0,
        shadow_alpha=0.3,
    )


def _spring_frames(scrolls: list[float]) -> list[SpringFrame]:
    return [
        SpringFrame(t_s=k / 30, virtual_scroll=scroll, velocity=0.0, target=scroll)
        for k, scroll in enumerate(scrolls)
    ]


# -- render_carousel_frames ----------------------------------------------------


def test_renders_one_opaque_png_per_spring_frame(tmp_path, cards):
    frames = _spring_frames([0.0, 120.0, -90.0])
    out_dir = os.path.join(tmp_path, "out")

    paths = render_carousel_frames(
        "cover_flow", frames, cards, GEO, out_dir, transform_fn=_fake_transform
    )

    assert paths == [
        os.path.join(out_dir, "frame_0000.png"),
        os.path.join(out_dir, "frame_0001.png"),
        os.path.join(out_dir, "frame_0002.png"),
    ]
    for path in paths:
        assert os.path.exists(path)
        with Image.open(path) as im:
            assert im.size == (CANVAS_W, CANVAS_H)
            assert im.mode == "RGB"  # opaque — no alpha channel written


def test_frame_is_not_all_background(tmp_path, cards):
    """At scroll=0 the middle card (index 1, green) is centered on the canvas."""
    frames = _spring_frames([0.0])
    out_dir = os.path.join(tmp_path, "out")

    [path] = render_carousel_frames(
        "cover_flow", frames, cards, GEO, out_dir, transform_fn=_fake_transform
    )

    with Image.open(path) as im:
        center_pixel = im.getpixel((CANVAS_W // 2, CANVAS_H // 2))
        assert center_pixel != BACKGROUND_RGB


def test_frames_differ_across_varying_scroll(tmp_path, cards):
    frames = _spring_frames([0.0, 180.0, -140.0])
    out_dir = os.path.join(tmp_path, "out")

    paths = render_carousel_frames(
        "cover_flow", frames, cards, GEO, out_dir, transform_fn=_fake_transform
    )

    contents = []
    for path in paths:
        with open(path, "rb") as f:
            contents.append(f.read())

    # render_carousel_frames drives each frame's transform from
    # `renderer.lagged_virtual_scroll` (the PRECEDING frame's virtual_scroll —
    # see that function's docstring), not the frame's own: frame 0 has no
    # predecessor so it uses its own value (0.0); frame 1's predecessor IS
    # frame 0 (0.0) — same value, so frames 0 and 1 render IDENTICALLY here.
    # Frame 2's predecessor is frame 1's actual virtual_scroll (180.0),
    # genuinely different, so it diverges from both.
    assert contents[0] == contents[1]
    assert contents[1] != contents[2]
    assert contents[0] != contents[2]


def test_zero_opacity_card_is_not_drawn(tmp_path, cards):
    """A card with opacity<=0 must be skipped entirely. Hide the centered
    middle card (index 1) and confirm the canvas center reverts to background
    — the two side cards are offset away from center at scroll=0."""

    def transform_hide_middle(effect, scroll_x, card_index, geo, viewport_w):
        t = _fake_transform(effect, scroll_x, card_index, geo, viewport_w)
        return replace(t, opacity=0.0) if card_index == 1 else t

    frames = _spring_frames([0.0])
    out_dir = os.path.join(tmp_path, "out")

    [path] = render_carousel_frames(
        "cover_flow", frames, cards, GEO, out_dir, transform_fn=transform_hide_middle
    )

    with Image.open(path) as im:
        assert im.getpixel((CANVAS_W // 2, CANVAS_H // 2)) == BACKGROUND_RGB


# -- project_card_corners (pure, no skia) --------------------------------------


def test_project_card_corners_axis_aligned_when_no_rotation():
    """rotate_y=0, translate_z=0 must reduce to the plain (scaled) rect — no
    3D effects in play."""
    t = CardTransform(x=100.0, y=200.0, scale=1.0, rotate_y_deg=0.0, translate_z_px=0.0)

    corners = project_card_corners(t, GEO)

    expected = [
        (100.0, 200.0),
        (100.0 + GEO.card_w, 200.0),
        (100.0 + GEO.card_w, 200.0 + GEO.card_h),
        (100.0, 200.0 + GEO.card_h),
    ]
    for (actual_x, actual_y), (expected_x, expected_y) in zip(corners, expected, strict=True):
        assert actual_x == pytest.approx(expected_x, abs=1e-6)
        assert actual_y == pytest.approx(expected_y, abs=1e-6)


def test_project_card_corners_matches_hand_computed_corner():
    """Regression pin: independently re-derive the top-right corner for
    rotate_y=35deg, translate_z=-200px straight from the documented formula
    (not by calling `project_card_corners` on itself) and compare."""
    t = CardTransform(x=0.0, y=0.0, scale=1.0, rotate_y_deg=35.0, translate_z_px=-200.0)

    corners = project_card_corners(t, GEO)
    _, top_right, _, _ = corners

    cx, cy = GEO.card_w / 2.0, GEO.card_h / 2.0
    origin_x, origin_y = CANVAS_W / 2.0, CANVAS_H / 2.0
    theta = math.radians(35.0)
    x0, y0, z0 = GEO.card_w / 2.0, -GEO.card_h / 2.0, -200.0
    x1 = x0 * math.cos(theta) + z0 * math.sin(theta)
    y1 = y0
    z1 = -x0 * math.sin(theta) + z0 * math.cos(theta)
    big_x = (cx - origin_x) + x1
    big_y = (cy - origin_y) + y1
    f = PERSPECTIVE_PX / max(PERSPECTIVE_PX - z1, 1.0)
    expected = (origin_x + big_x * f, origin_y + big_y * f)

    assert top_right[0] == pytest.approx(expected[0], abs=1e-6)
    assert top_right[1] == pytest.approx(expected[1], abs=1e-6)


def test_project_card_corners_foreshortens_far_side():
    """rotate_y=35deg (positive) with translate_z=-200px pushes the +x
    (right-hand) corners further from the camera than the -x (left-hand)
    corners: for x0>0, z0<0, z1 = -x0*sin(theta) + z0*cos(theta) is more
    negative than for x0<0 (both terms compound negative on the right,
    partially cancel on the left) — so f = d/(d-z1) is SMALLER on the right,
    meaning the right edge projects shorter than the left edge."""
    t = CardTransform(x=0.0, y=0.0, scale=1.0, rotate_y_deg=35.0, translate_z_px=-200.0)

    top_left, top_right, bottom_right, bottom_left = project_card_corners(t, GEO)

    left_edge_height = bottom_left[1] - top_left[1]
    right_edge_height = bottom_right[1] - top_right[1]

    assert left_edge_height > 0
    assert right_edge_height > 0
    assert right_edge_height < left_edge_height
