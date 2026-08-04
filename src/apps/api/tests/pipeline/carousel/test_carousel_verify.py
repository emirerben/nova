"""Self-test for the carousel parity gate (`app.pipeline.carousel_verify`)
that proves the compare machinery works BEFORE any browser capture exists.

Two things are verified:
  (a) `generate_test_cards` produces the expected card assets — 5 distinct
      540x720 PNGs, replicating the reference HTML pages' visuals.
  (b) A SYNTHETIC round-trip: render our own side once, then feed OUR OWN
      frames/trace as BOTH "reference" and "ours". `compute_ssim` of a video
      against itself must be ~1.0; `compare_motion_traces` of a trace against
      itself must have zero delta and pass. This exercises every function in
      the gate (render, encode, ssim, trace-compare, montage) without a
      browser reference, so a regression in the compare machinery itself is
      caught independent of whether `tools/carousel_reference/` captures
      exist yet.

Skipped when skia or ffmpeg are unavailable, matching
`test_renderer_smoke.py` and `test_segment.py`'s conventions respectively.
"""

from __future__ import annotations

import os

import pytest
from PIL import Image

pytest.importorskip("skia")

from app.pipeline.carousel.segment import DEFAULT_GEOMETRY
from app.pipeline.carousel_verify import (
    build_side_by_side_montage,
    compare_motion_traces,
    compute_ssim,
    encode_frames_to_mp4,
    generate_test_cards,
    render_our_side,
)
from app.services.video_frames import have_ffmpeg

pytestmark = pytest.mark.skipif(
    not have_ffmpeg(), reason="ffmpeg not on PATH — carousel_verify tests require it"
)


# -- generate_test_cards --------------------------------------------------------


def test_generate_test_cards_produces_five_distinct_png_cards(tmp_path):
    cards = generate_test_cards(5, str(tmp_path))

    assert len(cards) == 5
    assert [c.index for c in cards] == [0, 1, 2, 3, 4]

    colors = []
    for card in cards:
        assert os.path.exists(card.image_path)
        with Image.open(card.image_path) as im:
            assert im.size == (round(DEFAULT_GEOMETRY.card_w), round(DEFAULT_GEOMETRY.card_h))
            assert im.mode == "RGB"
            # Sample a corner pixel, well away from every card's marker rect
            # (markers start at y>=40 and are all inset from the left/right
            # edges — (2, 2) is background on every card).
            colors.append(im.getpixel((2, 2)))

    assert len(set(colors)) == 5, f"expected 5 distinct card colors, got {colors}"


def test_generate_test_cards_marker_visible_on_first_card(tmp_path):
    """Card 0's marker sits at top=40..72px, white border inset 12px from
    each side — sample a pixel on that border and confirm it's near-white,
    distinct from the background fill."""
    [card] = generate_test_cards(1, str(tmp_path))

    with Image.open(card.image_path) as im:
        bg_pixel = im.getpixel((2, 2))
        border_pixel = im.getpixel((12, 40))  # top-left corner of the marker's border

    assert border_pixel != bg_pixel
    assert sum(border_pixel) > sum(bg_pixel)  # border is white — brighter than any hsl(*, 70%, 55%)


# -- synthetic round-trip --------------------------------------------------------


def test_synthetic_round_trip_scale_sweep(tmp_path):
    """Feed our own render as both sides of the comparison: proves the
    compare machinery (ssim + trace diff + montage) before any browser
    capture exists."""
    work_dir = os.path.join(tmp_path, "work")

    frame_paths, our_trace = render_our_side("scale_sweep", n_frames=20, work_dir=work_dir)

    assert len(frame_paths) == 20
    assert len(our_trace) == 20
    assert len(our_trace[0]["cards"]) == 5
    for card_entry in our_trace[0]["cards"]:
        assert {"left", "top", "width", "height", "opacity"} <= card_entry.keys()

    frames_dir = os.path.join(work_dir, "frames")
    ours_mp4 = os.path.join(work_dir, "ours.mp4")
    encode_frames_to_mp4(frames_dir, ours_mp4, fps=30)
    assert os.path.exists(ours_mp4)
    assert os.path.getsize(ours_mp4) > 0

    ssim = compute_ssim(ours_mp4, ours_mp4, work_dir)
    assert ssim["global"] == pytest.approx(1.0, abs=1e-3)
    assert ssim["min_frame"] == pytest.approx(1.0, abs=1e-3)

    trace_cmp = compare_motion_traces(our_trace, our_trace, tol_px=2.0)
    assert trace_cmp["max_delta_px"] == 0.0
    assert trace_cmp["mean_delta_px"] == 0.0
    assert trace_cmp["max_opacity_delta"] == 0.0
    assert trace_cmp["worst"] is None or trace_cmp["worst"]["delta"] == 0.0
    assert trace_cmp["frame_count_mismatch"] is False
    assert trace_cmp["pass"] is True

    montage_path = os.path.join(work_dir, "montage.png")
    build_side_by_side_montage(frames_dir, frames_dir, montage_path, sample_every=5)
    assert os.path.exists(montage_path)
    with Image.open(montage_path) as im:
        assert im.width > 0 and im.height > 0


def test_compare_motion_traces_reports_frame_count_mismatch():
    """Truncating one side must be reported explicitly, not silently
    ignored, and the comparison should still complete over the common
    prefix."""
    frame = {
        "i": 0,
        "scrollLeft": 0.0,
        "cards": [{"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0, "opacity": 1.0}],
    }
    full_trace = [frame, frame, frame]
    short_trace = [frame]

    result = compare_motion_traces(full_trace, short_trace, tol_px=2.0)

    assert result["frame_count_mismatch"] is True
    assert result["browser_frame_count"] == 3
    assert result["our_frame_count"] == 1
    assert result["compared_frames"] == 1
    assert result["pass"] is True  # the one compared frame is identical


def test_compare_motion_traces_flags_delta_over_tolerance():
    browser_frame = {
        "i": 0,
        "scrollLeft": 0.0,
        "cards": [{"left": 0.0, "top": 0.0, "width": 100.0, "height": 100.0, "opacity": 1.0}],
    }
    our_frame = {
        "i": 0,
        "scrollLeft": 0.0,
        "cards": [{"left": 5.0, "top": 0.0, "width": 100.0, "height": 100.0, "opacity": 1.0}],
    }

    result = compare_motion_traces([browser_frame], [our_frame], tol_px=2.0)

    assert result["max_delta_px"] == pytest.approx(5.0)
    assert result["pass"] is False
    assert result["worst"]["field"] == "left"
    assert result["worst"]["delta"] == pytest.approx(5.0)
