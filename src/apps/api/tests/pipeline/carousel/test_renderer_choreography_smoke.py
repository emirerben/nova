"""Smoke tests for `render_choreography_frames` — the V2 renderer that adds
ROLLING VIDEO card faces and FOCUS CHOREOGRAPHY (fullscreen zoom) on top of
`render_carousel_frames`'s original still-poster-only path.

Follows `test_renderer_smoke.py`'s conventions: `pytest.importorskip("skia")`
at module scope, a caller-supplied `transform_fn` so these tests never depend
on `effects.transform_for`'s real math, and hand-built `VideoCardAsset`s
(synthetic JPEG frame directories via Pillow) so no ffmpeg call is needed
here — that's covered separately by `test_video_cards.py` and `test_segment.py`.
"""

from __future__ import annotations

import os

import pytest
from PIL import Image

pytest.importorskip("skia")

from app.pipeline.carousel.choreography import FrameState
from app.pipeline.carousel.effects import CardGeometry, CardTransform
from app.pipeline.carousel.renderer import CANVAS_H, CANVAS_W, render_choreography_frames
from app.pipeline.carousel.video_cards import VideoCardAsset

GEO = CardGeometry(card_w=300.0, card_h=400.0, gap=20.0, corner_radius=24.0)
BACKGROUND_RGB = (10, 10, 12)


def _fake_transform(effect, scroll_x, card_index, geo, viewport_w):
    """Three cards side by side, centered vertically — same shape
    `test_renderer_smoke.py` uses, just without the `position_scroll_x`
    kwarg (this test's `transform_fn` opts out of the progress/position
    split, same as that file's escape hatch)."""
    i = card_index
    return CardTransform(
        x=viewport_w / 2 - geo.card_w / 2 + (i - 1) * (geo.card_w + geo.gap) + scroll_x,
        y=(CANVAS_H - geo.card_h) / 2,
        scale=0.9,
        rotate_y_deg=0.0,
        translate_z_px=0.0,
        z_index=0,
        opacity=1.0,
        shadow_alpha=0.2,
    )


def _make_poster(tmp_path, index: int, color: tuple[int, int, int]) -> str:
    path = os.path.join(tmp_path, f"poster_{index:02d}.png")
    Image.new("RGB", (300, 400), color).save(path)
    return path


def _make_frames_dir(
    tmp_path, name: str, n: int, w: int, h: int, colors: list[tuple[int, int, int]]
) -> str:
    """A tiny synthetic JPEG frame sequence: frame `i`'s color cycles through
    `colors`, so consecutive frames are visibly distinguishable."""
    d = os.path.join(tmp_path, name)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        color = colors[i % len(colors)]
        Image.new("RGB", (w, h), color).save(os.path.join(d, f"frame_{i:04d}.jpg"), quality=95)
    return d


@pytest.fixture
def stills_only_cards(tmp_path):
    """No video tier at all — matches `render_carousel_frames`'s delegation
    shape exactly (card_frame_count=0)."""
    return [
        VideoCardAsset(
            index=i,
            card_frames_dir="",
            card_frame_count=0,
            full_frames_dir=None,
            full_frame_count=0,
            poster_path=_make_poster(tmp_path, i, color),
        )
        for i, color in enumerate([(220, 40, 40), (40, 200, 60), (40, 80, 230)])
    ]


@pytest.fixture
def video_cards(tmp_path):
    return [
        VideoCardAsset(
            index=0,
            card_frames_dir=_make_frames_dir(
                tmp_path, "card0", 10, 300, 400, [(220, 40, 40), (250, 120, 40)]
            ),
            card_frame_count=10,
            full_frames_dir=_make_frames_dir(
                tmp_path, "card0_full", 10, CANVAS_W, CANVAS_H, [(220, 40, 40)]
            ),
            full_frame_count=10,
            poster_path=_make_poster(tmp_path, 0, (220, 40, 40)),
        ),
        VideoCardAsset(
            index=1,
            card_frames_dir=_make_frames_dir(tmp_path, "card1", 10, 300, 400, [(40, 200, 60)]),
            card_frame_count=10,
            full_frames_dir=None,
            full_frame_count=0,
            poster_path=_make_poster(tmp_path, 1, (40, 200, 60)),
        ),
        VideoCardAsset(
            index=2,
            card_frames_dir=_make_frames_dir(tmp_path, "card2", 10, 300, 400, [(40, 80, 230)]),
            card_frame_count=10,
            full_frames_dir=None,
            full_frame_count=0,
            poster_path=_make_poster(tmp_path, 2, (40, 80, 230)),
        ),
    ]


def test_stills_only_produces_one_opaque_png_per_frame(tmp_path, stills_only_cards):
    frames = [FrameState(t_s=k / 30, scroll_x=0.0) for k in range(3)]
    out_dir = os.path.join(tmp_path, "out")

    paths = render_choreography_frames(
        "cover_flow", frames, stills_only_cards, GEO, out_dir, transform_fn=_fake_transform
    )

    assert paths == [os.path.join(out_dir, f"frame_{i:04d}.png") for i in range(3)]
    for p in paths:
        with Image.open(p) as im:
            assert im.size == (CANVAS_W, CANVAS_H)
            assert im.mode == "RGB"


def test_video_card_face_advances_across_frames(tmp_path, video_cards):
    """Card 0's face cycles red/orange every frame — successive rendered
    frames must differ (proves the renderer is actually indexing into the
    JPEG sequence per frame, not freezing on one)."""
    frames = [FrameState(t_s=k / 30, scroll_x=0.0) for k in range(4)]
    out_dir = os.path.join(tmp_path, "out")

    paths = render_choreography_frames(
        "cover_flow", frames, video_cards, GEO, out_dir, transform_fn=_fake_transform
    )

    contents = [open(p, "rb").read() for p in paths]
    assert contents[0] != contents[1]
    assert contents[1] != contents[2]


def test_focused_card_center_pixel_differs_from_non_focus_frame(tmp_path, video_cards):
    """A focus ramp on card 0: the canvas center pixel during the fullscreen
    hold must differ from the same pixel in a frame where nothing is
    focused (the whole canvas is covered by the focused card's face, not
    the background/other cards)."""
    frames = [
        FrameState(t_s=0.0, scroll_x=0.0),  # no focus
        FrameState(t_s=1 / 30, scroll_x=0.0, focus_card=0, focus_t=1.0, dim=0.55),  # full focus
    ]
    out_dir = os.path.join(tmp_path, "out")

    paths = render_choreography_frames(
        "cover_flow", frames, video_cards, GEO, out_dir, transform_fn=_fake_transform
    )

    with Image.open(paths[0]) as im0, Image.open(paths[1]) as im1:
        center0 = im0.getpixel((CANVAS_W // 2, CANVAS_H // 2))
        center1 = im1.getpixel((CANVAS_W // 2, CANVAS_H // 2))
    assert center0 != center1


def test_fullscreen_frame_has_no_background_pixels_at_corners(tmp_path, video_cards):
    """At focus_t=1.0, the focused card's quad must cover the ENTIRE canvas
    — none of the four corners should still show `background_rgb`."""
    frames = [FrameState(t_s=0.0, scroll_x=0.0, focus_card=0, focus_t=1.0, dim=0.55)]
    out_dir = os.path.join(tmp_path, "out")

    [path] = render_choreography_frames(
        "cover_flow", frames, video_cards, GEO, out_dir, transform_fn=_fake_transform
    )

    with Image.open(path) as im:
        corners = [
            im.getpixel((0, 0)),
            im.getpixel((CANVAS_W - 1, 0)),
            im.getpixel((0, CANVAS_H - 1)),
            im.getpixel((CANVAS_W - 1, CANVAS_H - 1)),
        ]
    for corner in corners:
        assert corner[:3] != BACKGROUND_RGB


def test_non_focused_cards_are_dimmed(tmp_path, video_cards):
    """A high `dim` on a non-focused-card frame must darken its rendered
    pixel relative to `dim=0`."""
    frames_dim0 = [FrameState(t_s=0.0, scroll_x=0.0, dim=0.0)]
    frames_dim_high = [FrameState(t_s=0.0, scroll_x=0.0, dim=0.9)]

    out0 = os.path.join(tmp_path, "out0")
    out1 = os.path.join(tmp_path, "out1")

    [path0] = render_choreography_frames(
        "cover_flow", frames_dim0, video_cards, GEO, out0, transform_fn=_fake_transform
    )
    [path1] = render_choreography_frames(
        "cover_flow", frames_dim_high, video_cards, GEO, out1, transform_fn=_fake_transform
    )

    with Image.open(path0) as im0, Image.open(path1) as im1:
        # Middle card (index 1, green) is centered at scroll=0.
        p0 = im0.getpixel((CANVAS_W // 2, CANVAS_H // 2))
        p1 = im1.getpixel((CANVAS_W // 2, CANVAS_H // 2))
    assert sum(p1[:3]) < sum(p0[:3])


def test_focused_card_always_drawn_last_on_top(tmp_path, video_cards):
    """Even if the focused card's z_index would normally sort it BEHIND
    another card, focus must force it to paint last. Use a transform_fn that
    gives every card the SAME on-screen position (full overlap) but assigns
    the focused card (index 0) the LOWEST z_index — if focus-on-top wins,
    the shared center pixel matches card 0's face color, not whichever card
    would otherwise win the z-index sort."""

    def _overlapping_transform(effect, scroll_x, card_index, geo, viewport_w):
        z = -10 if card_index == 0 else 0  # card 0 would normally paint FIRST (bottom)
        return CardTransform(
            x=viewport_w / 2 - geo.card_w / 2,
            y=(CANVAS_H - geo.card_h) / 2,
            scale=1.0,
            rotate_y_deg=0.0,
            translate_z_px=0.0,
            z_index=z,
            opacity=1.0,
            shadow_alpha=0.0,
        )

    frames = [FrameState(t_s=0.0, scroll_x=0.0, focus_card=0, focus_t=0.5, dim=0.3)]
    out_dir = os.path.join(tmp_path, "out")

    [path] = render_choreography_frames(
        "cover_flow", frames, video_cards, GEO, out_dir, transform_fn=_overlapping_transform
    )

    with Image.open(path) as im:
        center = im.getpixel((CANVAS_W // 2, CANVAS_H // 2))
    # Card 0's face cycles (220,40,40)/(250,120,40) — reddish either way;
    # card 1 (green) or card 2 (blue) would look nothing like this if they'd
    # painted on top instead.
    assert center[0] > center[1] and center[0] > center[2]
