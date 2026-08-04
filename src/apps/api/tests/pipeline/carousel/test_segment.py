"""End-to-end test for `render_carousel_moment` — real ffmpeg + real skia.

Builds two tiny synthetic source clips with ffmpeg (`lavfi` color sources,
no checked-in fixtures), then drives the full Lane D pipeline: frame
extraction (`cards.py`, Lane C) -> spring simulation (Lane A) -> Skia render
(Lane B) -> ffmpeg encode (this lane). Skipped when skia or ffmpeg are
unavailable, matching `test_renderer_smoke.py`'s and
`tests/services/test_video_frames.py`'s conventions respectively.
"""

from __future__ import annotations

import os
import subprocess

import pytest

pytest.importorskip("skia")

from app.pipeline.carousel.choreography import FocusMoment
from app.pipeline.carousel.segment import (
    CarouselMomentSpec,
    render_carousel_moment,
)
from app.pipeline.probe import probe_video
from app.services.video_frames import have_ffmpeg

pytestmark = pytest.mark.skipif(
    not have_ffmpeg(), reason="ffmpeg not on PATH — segment tests require it"
)

FPS = 30


def _make_color_clip(path: str, color: str) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=320x240:d=1",
        "-pix_fmt",
        "yuv420p",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture
def two_clips(tmp_path):
    a = os.path.join(tmp_path, "red.mp4")
    b = os.path.join(tmp_path, "blue.mp4")
    _make_color_clip(a, "red")
    _make_color_clip(b, "blue")
    return a, b


def test_render_carousel_moment_returns_valid_mp4(tmp_path, two_clips):
    a, b = two_clips
    spec = CarouselMomentSpec(effect="scale_sweep", clip_paths=(a, b), duration_s=1.0)

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is not None
    assert os.path.exists(result)
    assert os.path.getsize(result) > 0

    probe = probe_video(result)
    assert probe.width == 1080
    assert probe.height == 1920
    assert probe.fps == pytest.approx(FPS, abs=0.5)
    # duration_s=1.0 at 30fps -> target_n=30. The canonical flick's settle
    # trace for a 2-card carousel is ~112 frames (~3.7s) — far more than 30 —
    # so this exercises the TRUNCATION branch of _fit_duration, not padding.
    # Assert against the requested 1.0s, not the untruncated ~3.7s trace.
    assert probe.duration_s == pytest.approx(1.0, abs=0.15)


def test_render_carousel_moment_invalid_effect_returns_none(tmp_path, two_clips):
    a, b = two_clips
    spec = CarouselMomentSpec(effect="not_a_real_effect", clip_paths=(a, b), duration_s=1.0)

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is None


def test_render_carousel_moment_nonexistent_clip_returns_none(tmp_path, two_clips):
    a, _b = two_clips
    spec = CarouselMomentSpec(
        effect="scale_sweep",
        clip_paths=(a, "/tmp/does/not/exist.mp4"),
        duration_s=1.0,
    )

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is None


def test_render_carousel_moment_too_few_clips_returns_none(tmp_path, two_clips):
    a, _b = two_clips
    spec = CarouselMomentSpec(effect="scale_sweep", clip_paths=(a,), duration_s=1.0)

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is None


# -- V2: mode="rolling" / mode="focus" e2e -------------------------------------
#
# `spec.mode` defaults to "stills" — every test above this line omits `mode`
# entirely and must keep passing UNMODIFIED (regression pin for byte-identical
# V1 behavior; see `CarouselMomentSpec.mode`'s docstring in segment.py).


def test_render_carousel_moment_defaults_mode_to_stills(tmp_path, two_clips):
    """Explicit regression check: an un-set `mode` behaves exactly like
    `mode="stills"` — same output shape as the pre-V2 default-arg tests
    above, just spelled out explicitly for clarity."""
    a, b = two_clips
    spec = CarouselMomentSpec(effect="scale_sweep", clip_paths=(a, b), duration_s=1.0)
    assert spec.mode == "stills"

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is not None
    probe = probe_video(result)
    assert probe.width == 1080
    assert probe.height == 1920
    assert probe.duration_s == pytest.approx(1.0, abs=0.15)


def test_render_carousel_moment_rolling_mode_returns_valid_mp4(tmp_path, two_clips):
    a, b = two_clips
    spec = CarouselMomentSpec(
        effect="scale_sweep", clip_paths=(a, b), duration_s=1.0, mode="rolling"
    )

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is not None
    assert os.path.exists(result)
    assert os.path.getsize(result) > 0

    probe = probe_video(result)
    assert probe.width == 1080
    assert probe.height == 1920
    assert probe.fps == pytest.approx(FPS, abs=0.5)
    # rolling_timeline trims/pads to exactly round(duration_s * fps).
    assert probe.duration_s == pytest.approx(1.0, abs=0.15)


def test_render_carousel_moment_focus_mode_returns_valid_mp4(tmp_path, two_clips):
    a, b = two_clips
    spec = CarouselMomentSpec(
        effect="cover_flow",
        clip_paths=(a, b),
        mode="focus",
        focus_moments=(FocusMoment(card_index=1, hold_s=0.3, zoom_s=0.2),),
        seed=1,
    )

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is not None
    assert os.path.exists(result)
    assert os.path.getsize(result) > 0

    probe = probe_video(result)
    assert probe.width == 1080
    assert probe.height == 1920
    assert probe.fps == pytest.approx(FPS, abs=0.5)
    # duration_s is IGNORED in focus mode — the natural choreography length
    # (well under MAX_FOCUS_TOTAL_S for this short a moment) governs instead.
    assert 0.5 < probe.duration_s < 15.5


def test_render_carousel_moment_focus_mode_clamps_out_of_range_card_index(tmp_path, two_clips):
    """A focus_moments entry naming a card beyond the (2-clip) pool must be
    clamped into range rather than crashing — 2 clips means valid indices
    are {0, 1}."""
    a, b = two_clips
    spec = CarouselMomentSpec(
        effect="scale_sweep",
        clip_paths=(a, b),
        mode="focus",
        focus_moments=(FocusMoment(card_index=99, hold_s=0.2, zoom_s=0.2),),
        seed=0,
    )

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is not None


def test_render_carousel_moment_invalid_mode_returns_none(tmp_path, two_clips):
    a, b = two_clips
    spec = CarouselMomentSpec(
        effect="scale_sweep", clip_paths=(a, b), duration_s=1.0, mode="not_a_real_mode"
    )

    result = render_carousel_moment(spec, str(tmp_path))

    assert result is None
