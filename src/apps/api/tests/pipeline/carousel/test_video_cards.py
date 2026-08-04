"""Tests for `app.pipeline.carousel.video_cards` — real ffmpeg, tiny lavfi
clips, no checked-in fixtures. Skipped when ffmpeg is unavailable, matching
`test_segment.py`'s convention."""

from __future__ import annotations

import os
import subprocess

import pytest

from app.pipeline.carousel.video_cards import (
    CARD_TIER_H,
    CARD_TIER_W,
    FULL_TIER_H,
    FULL_TIER_W,
    resolve_video_card,
)
from app.services.video_frames import have_ffmpeg

pytestmark = pytest.mark.skipif(
    not have_ffmpeg(), reason="ffmpeg not on PATH — video_cards tests require it"
)


def _make_color_clip(path: str, color: str, duration: float) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=320x240:d={duration}",
        "-pix_fmt",
        "yuv420p",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _jpeg_count(dir_path: str) -> int:
    return len([n for n in os.listdir(dir_path) if n.startswith("frame_") and n.endswith(".jpg")])


@pytest.fixture
def clip(tmp_path) -> str:
    path = os.path.join(tmp_path, "clip.mp4")
    _make_color_clip(path, "orange", duration=3.0)
    return path


def test_card_tier_frame_count_and_dims(tmp_path, clip):
    asset = resolve_video_card(clip, str(tmp_path), 0, card_seconds=1.0)

    assert asset.index == 0
    assert os.path.isdir(asset.card_frames_dir)
    assert asset.card_frame_count == pytest.approx(30, abs=3)  # ~1.0s @ 30fps
    assert _jpeg_count(asset.card_frames_dir) == asset.card_frame_count

    from PIL import Image  # noqa: PLC0415

    first = os.path.join(asset.card_frames_dir, "frame_0000.jpg")
    assert os.path.exists(first)
    with Image.open(first) as im:
        assert im.size == (CARD_TIER_W, CARD_TIER_H)

    # No full tier requested -> not extracted.
    assert asset.full_frames_dir is None
    assert asset.full_frame_count == 0

    # Poster fallback still resolved (existing resolve_card_media output).
    assert os.path.exists(asset.poster_path)


def test_full_tier_extracted_only_when_requested(tmp_path, clip):
    asset = resolve_video_card(clip, str(tmp_path), 1, card_seconds=1.0, full_seconds=0.5)

    assert asset.full_frames_dir is not None
    assert os.path.isdir(asset.full_frames_dir)
    assert asset.full_frame_count == pytest.approx(15, abs=3)  # ~0.5s @ 30fps

    from PIL import Image  # noqa: PLC0415

    first = os.path.join(asset.full_frames_dir, "frame_0000.jpg")
    with Image.open(first) as im:
        assert im.size == (FULL_TIER_W, FULL_TIER_H)


def test_frames_start_numbered_from_zero(tmp_path, clip):
    asset = resolve_video_card(clip, str(tmp_path), 0, card_seconds=0.5)
    assert os.path.exists(os.path.join(asset.card_frames_dir, "frame_0000.jpg"))


def test_clip_shorter_than_requested_window_yields_fewer_frames_not_a_crash(tmp_path):
    short_clip = os.path.join(tmp_path, "short.mp4")
    _make_color_clip(short_clip, "purple", duration=0.5)

    asset = resolve_video_card(short_clip, str(tmp_path), 0, card_seconds=3.0)

    # Extraction never raises even though the clip can't fill the requested
    # window; it just produces fewer frames than 3.0s*30fps would imply.
    assert asset.card_frame_count > 0
    assert asset.card_frame_count < round(3.0 * 30)


def test_nonexistent_clip_raises_runtime_error(tmp_path):
    with pytest.raises(RuntimeError):
        resolve_video_card(
            os.path.join(tmp_path, "does_not_exist.mp4"), str(tmp_path), 0, card_seconds=1.0
        )


def test_indices_produce_distinct_directories(tmp_path, clip):
    asset0 = resolve_video_card(clip, str(tmp_path), 0, card_seconds=0.5)
    asset1 = resolve_video_card(clip, str(tmp_path), 1, card_seconds=0.5)
    assert asset0.card_frames_dir != asset1.card_frames_dir
