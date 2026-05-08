"""Tests for the image → looping mp4 conversion service."""

from __future__ import annotations

import io
import shutil
import subprocess

import pytest
from PIL import Image

from app.services.image_to_video import (
    ImageConversionError,
    _normalize_to_9x16,
    image_bytes_to_mp4,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _make_image_bytes(width: int, height: int, fmt: str = "JPEG") -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _ffprobe_duration_s(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return float(out)


def _ffprobe_resolution(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=,",
            path,
        ],
        text=True,
    ).strip()
    w, h = out.split(",")
    return int(w), int(h)


class TestNormalize:
    def test_landscape_image_is_cropped_to_9x16(self):
        raw = _make_image_bytes(1920, 1080, "PNG")
        png = _normalize_to_9x16(raw)
        img = Image.open(io.BytesIO(png))
        assert img.size == (1080, 1920)

    def test_portrait_image_taller_than_9x16_is_cropped(self):
        raw = _make_image_bytes(1080, 2400, "PNG")
        png = _normalize_to_9x16(raw)
        img = Image.open(io.BytesIO(png))
        assert img.size == (1080, 1920)

    def test_already_9x16_is_unchanged_size(self):
        raw = _make_image_bytes(1080, 1920, "PNG")
        png = _normalize_to_9x16(raw)
        img = Image.open(io.BytesIO(png))
        assert img.size == (1080, 1920)

    def test_invalid_bytes_raises(self):
        with pytest.raises(ImageConversionError):
            _normalize_to_9x16(b"not an image")


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestEndToEnd:
    def test_produces_mp4_at_target_duration(self, tmp_path):
        out = tmp_path / "out.mp4"
        raw = _make_image_bytes(1920, 1080, "JPEG")
        image_bytes_to_mp4(raw, str(out), duration_s=3.0)
        assert out.exists()
        # Duration tolerance: FFmpeg may snap to nearest frame boundary
        assert abs(_ffprobe_duration_s(str(out)) - 3.0) < 0.2
        assert _ffprobe_resolution(str(out)) == (1080, 1920)

    def test_minimum_duration_clamped(self, tmp_path):
        out = tmp_path / "out.mp4"
        raw = _make_image_bytes(1080, 1920, "PNG")
        image_bytes_to_mp4(raw, str(out), duration_s=0.1)
        # _normalize clamps to 0.5s minimum
        assert _ffprobe_duration_s(str(out)) >= 0.4
