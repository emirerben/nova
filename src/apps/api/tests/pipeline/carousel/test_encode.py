"""Integration test for `encode_carousel_segment` — real ffmpeg, no skia.

Generates a small sequence of solid-color PNGs with Pillow (no renderer/skia
dependency), encodes them at 30fps, then probes the output to confirm the
encoder-policy contract: h264, 1080x1920, yuv420p, and a silent audio track
(the body-slot layout — see `encode.py`'s module docstring for why a
carousel segment, which has no inherent audio, still ships one).

Skipped when ffmpeg is not on PATH, matching the rest of the suite's
convention for tests that shell out to real ffmpeg (see
`tests/services/test_video_frames.py`).
"""

from __future__ import annotations

import os

import pytest
from PIL import Image

from app.pipeline.carousel.encode import encode_carousel_segment
from app.pipeline.probe import probe_video
from app.services.video_frames import have_ffmpeg

pytestmark = pytest.mark.skipif(
    not have_ffmpeg(), reason="ffmpeg not on PATH — encode tests require it"
)

FPS = 30
N_FRAMES = 15  # 0.5s at 30fps
CANVAS_W = 1080
CANVAS_H = 1920


def _make_frames(tmp_path) -> str:
    png_dir = os.path.join(tmp_path, "frames")
    os.makedirs(png_dir, exist_ok=True)
    for i in range(N_FRAMES):
        # Vary the color per frame so the sequence isn't degenerate — not
        # asserted on directly, just avoids an all-identical-frame edge case.
        color = (10 + i, 10, 12)
        Image.new("RGB", (CANVAS_W, CANVAS_H), color).save(
            os.path.join(png_dir, f"frame_{i:04d}.png")
        )
    return png_dir


def test_encode_produces_h264_1080x1920_yuv420p_with_silent_audio(tmp_path):
    png_dir = _make_frames(tmp_path)
    output_path = os.path.join(tmp_path, "out.mp4")

    encode_carousel_segment(
        png_dir=png_dir,
        pattern="frame_%04d.png",
        n_frames=N_FRAMES,
        fps=FPS,
        output_path=output_path,
    )

    assert os.path.exists(output_path)
    assert os.path.getsize(output_path) > 0

    probe = probe_video(output_path)
    assert probe.codec == "h264"
    assert probe.width == CANVAS_W
    assert probe.height == CANVAS_H
    assert probe.pix_fmt == "yuv420p"
    # Explicit assertion on the audio decision made in encode.py: a carousel
    # segment carries a silent AAC track at the body-slot layout, matching
    # interstitials.render_color_hold's precedent for locally-generated
    # (non-user-sourced) video segments.
    assert probe.has_audio is True
    # N_FRAMES / FPS = 0.5s. Generous tolerance for encoder frame-boundary
    # rounding.
    assert probe.duration_s == pytest.approx(N_FRAMES / FPS, abs=0.1)


def test_encode_raises_runtime_error_on_missing_input(tmp_path):
    output_path = os.path.join(tmp_path, "out.mp4")

    with pytest.raises(RuntimeError):
        encode_carousel_segment(
            png_dir=os.path.join(tmp_path, "does_not_exist"),
            pattern="frame_%04d.png",
            n_frames=N_FRAMES,
            fps=FPS,
            output_path=output_path,
        )
