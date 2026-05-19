"""Integration tests for stage A: ffmpeg-based frame extraction.

These tests generate a small synthetic video at runtime (silent black frames)
so the suite has a deterministic, redistributable input — no checked-in
fixture videos. Tests auto-skip when ffmpeg is not on PATH, matching how the
rest of the suite handles optional external tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.video_frames import (
    DEFAULT_FPS,
    ExtractedFrame,
    extract_frames,
    frames_workdir,
    have_ffmpeg,
)

pytestmark = pytest.mark.skipif(
    not have_ffmpeg(), reason="ffmpeg not on PATH — synthetic-video tests require it"
)


def _make_silent_black_video(out_path: Path, duration_s: float) -> None:
    """Generate a 320x240 black video at 30fps, no audio, given duration.

    Uses ffmpeg's `lavfi` source so no input file is needed. Cheap to
    generate (< 200ms even for 10s clips) so each test gets its own.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=320x240:r=30:d={duration_s}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def test_extract_frames_at_2fps_on_3s_video_produces_6_frames(tmp_path):
    video = tmp_path / "src.mp4"
    _make_silent_black_video(video, duration_s=3.0)

    out_dir = tmp_path / "frames"
    frames = extract_frames(video, out_dir=out_dir, fps=2.0)

    assert len(frames) == 6
    assert all(isinstance(f, ExtractedFrame) for f in frames)
    assert all(Path(f.path).exists() for f in frames)


def test_extracted_frame_timestamps_are_at_uniform_1_over_fps_intervals(tmp_path):
    video = tmp_path / "src.mp4"
    _make_silent_black_video(video, duration_s=2.5)

    frames = extract_frames(video, out_dir=tmp_path / "frames", fps=2.0)

    # 2.5s at 2fps = 5 frames at t=0.0, 0.5, 1.0, 1.5, 2.0
    expected = [0.0, 0.5, 1.0, 1.5, 2.0]
    actual = [f.t_s for f in frames]
    assert actual == expected


def test_higher_fps_yields_proportionally_more_frames(tmp_path):
    video = tmp_path / "src.mp4"
    _make_silent_black_video(video, duration_s=2.0)

    frames_2fps = extract_frames(video, out_dir=tmp_path / "f2", fps=2.0)
    frames_5fps = extract_frames(video, out_dir=tmp_path / "f5", fps=5.0)

    # 2s × 2fps = 4 frames; 2s × 5fps = 10 frames
    assert len(frames_2fps) == 4
    assert len(frames_5fps) == 10
    # 5fps yields intervals of 0.2s
    assert frames_5fps[0].t_s == 0.0
    assert frames_5fps[1].t_s == pytest.approx(0.2)


def test_extract_creates_out_dir_if_missing(tmp_path):
    video = tmp_path / "src.mp4"
    _make_silent_black_video(video, duration_s=1.0)
    out_dir = tmp_path / "does" / "not" / "exist" / "yet"

    frames = extract_frames(video, out_dir=out_dir, fps=2.0)

    assert out_dir.is_dir()
    assert len(frames) == 2


def test_fps_must_be_positive(tmp_path):
    video = tmp_path / "src.mp4"
    _make_silent_black_video(video, duration_s=1.0)

    with pytest.raises(ValueError, match="fps must be positive"):
        extract_frames(video, out_dir=tmp_path / "f", fps=0.0)
    with pytest.raises(ValueError, match="fps must be positive"):
        extract_frames(video, out_dir=tmp_path / "f", fps=-1.0)


def test_extract_raises_on_unreadable_video(tmp_path):
    # File exists but is not a valid video.
    bogus = tmp_path / "not-a-video.mp4"
    bogus.write_bytes(b"definitely not a video")

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        extract_frames(bogus, out_dir=tmp_path / "f", fps=2.0)


def test_extract_raises_on_missing_file(tmp_path):
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        extract_frames(tmp_path / "missing.mp4", out_dir=tmp_path / "f", fps=2.0)


def test_default_fps_is_2():
    # Pinning the default — design-doc / cost-budget contract.
    assert DEFAULT_FPS == 2.0


def test_frames_workdir_tempdir_path_is_cleaned_up():
    with frames_workdir(None) as work_dir:
        assert work_dir.is_dir()
        (work_dir / "marker").write_text("hi")
        captured = work_dir
    assert not captured.exists()


def test_frames_workdir_caller_provided_path_is_not_cleaned_up(tmp_path):
    target = tmp_path / "keep-me"
    with frames_workdir(target) as work_dir:
        assert work_dir == target
        assert work_dir.is_dir()
        (work_dir / "marker").write_text("hi")
    # Caller owns cleanup → still exists after context exit
    assert target.exists()
    assert (target / "marker").exists()
