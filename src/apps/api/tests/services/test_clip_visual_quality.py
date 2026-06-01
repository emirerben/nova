"""Tests for deterministic clip visual-quality sampling."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.services.clip_visual_quality import (
    LumaSample,
    VisualQualityError,
    best_quality_window,
    sample_luma_timeline,
    summarize_window,
)


def _run_result(stderr: bytes, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    result.stdout = b""
    return result


def _metadata(*values: tuple[float, float]) -> bytes:
    lines = []
    for idx, (t_s, yavg) in enumerate(values):
        lines.append(f"[Parsed_metadata_2 @ 0x123] frame:{idx} pts:{idx} pts_time:{t_s}")
        lines.append(f"[Parsed_metadata_2 @ 0x123] lavfi.signalstats.YAVG={yavg}")
    return "\n".join(lines).encode()


def test_sample_luma_timeline_parses_signalstats_metadata() -> None:
    with patch(
        "app.services.clip_visual_quality.subprocess.run",
        return_value=_run_result(_metadata((0.0, 20.0), (1.0, 90.0))),
    ) as run:
        samples = sample_luma_timeline("/tmp/clip.mp4")

    assert samples == [
        LumaSample(t_s=0.0, luma_mean=20.0),
        LumaSample(t_s=1.0, luma_mean=90.0),
    ]
    cmd = run.call_args.args[0]
    assert "signalstats" in " ".join(cmd)
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_sample_luma_timeline_raises_on_ffmpeg_failure() -> None:
    with patch(
        "app.services.clip_visual_quality.subprocess.run",
        return_value=_run_result(b"bad input", returncode=1),
    ):
        with pytest.raises(VisualQualityError, match="ffmpeg returned 1"):
            sample_luma_timeline("/tmp/clip.mp4")


def test_sample_luma_timeline_raises_on_timeout() -> None:
    with patch(
        "app.services.clip_visual_quality.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
    ):
        with pytest.raises(VisualQualityError, match="timed out"):
            sample_luma_timeline("/tmp/clip.mp4")


def test_sample_luma_timeline_wraps_ffmpeg_spawn_failure() -> None:
    with patch(
        "app.services.clip_visual_quality.subprocess.run",
        side_effect=OSError("ffmpeg missing"),
    ):
        with pytest.raises(VisualQualityError, match="failed to start"):
            sample_luma_timeline("/tmp/clip.mp4")


def test_summarize_window_classifies_dark_and_usable_windows() -> None:
    samples = [
        LumaSample(t_s=0.0, luma_mean=25.0),
        LumaSample(t_s=1.0, luma_mean=30.0),
        LumaSample(t_s=6.0, luma_mean=120.0),
        LumaSample(t_s=7.0, luma_mean=140.0),
    ]

    assert summarize_window(samples, 0.0, 2.0).classification == "very_dark"
    assert summarize_window(samples, 6.0, 8.0).classification == "usable"


def test_best_quality_window_shifts_off_dark_intro() -> None:
    samples = [
        LumaSample(t_s=0.0, luma_mean=20.0),
        LumaSample(t_s=1.0, luma_mean=25.0),
        LumaSample(t_s=2.0, luma_mean=30.0),
        LumaSample(t_s=6.0, luma_mean=130.0),
        LumaSample(t_s=7.0, luma_mean=135.0),
    ]

    start_s, quality = best_quality_window(samples, clip_dur=12.0, window_dur=5.0)

    assert start_s >= 6.0
    assert quality.classification == "usable"
