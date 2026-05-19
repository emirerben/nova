"""Lightweight unit tests for scripts/overlay_forensics/frames.py.

End-to-end behavior (real ffmpeg invocations) is covered by the analyzer's
integration test. These tests assert input-validation and error paths.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.overlay_forensics import frames as frames_mod


def test_check_ffmpeg_raises_when_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(frames_mod.FFmpegNotInstalledError):
        frames_mod.check_ffmpeg_installed()


def test_sample_frames_rejects_invalid_step():
    with pytest.raises(ValueError):
        list(frames_mod.sample_frames(Path("/nonexistent"), 0.0, 1.0, step_s=0.0))


def test_sample_frames_rejects_window_inversion():
    with pytest.raises(ValueError):
        list(frames_mod.sample_frames(Path("/nonexistent"), 1.0, 0.5, step_s=0.05))


def test_probe_video_raises_on_missing_file(monkeypatch):
    # Ensure ffprobe is on PATH for the test to reach probe_video's
    # subprocess invocation; skip if it isn't.
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not on PATH")
    with pytest.raises(frames_mod.VideoReadError):
        frames_mod.probe_video(Path("/nonexistent/path/no.mp4"))
