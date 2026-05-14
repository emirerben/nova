"""Tests for the image → looping mp4 conversion service."""

from __future__ import annotations

import io
import shutil
import subprocess
from unittest.mock import patch

import pytest
from PIL import Image

from app.services import image_to_video
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


def _ffprobe_color_metadata(path: str) -> tuple[str, str, str]:
    """Return (color_primaries, color_transfer, color_space) as read from
    the H.264 SPS by ffprobe. Used to verify -x264-params propagation."""
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=color_primaries,color_transfer,color_space",
            "-of", "csv=p=0:s=,",
            path,
        ],
        text=True,
    ).strip()
    parts = out.split(",")
    while len(parts) < 3:
        parts.append("unknown")
    return parts[0] or "unknown", parts[1] or "unknown", parts[2] or "unknown"


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

    def test_truncated_png_raises_clean_error(self):
        """PIL decodes lazily — Image.open() succeeds on header alone, then
        exif_transpose/resize raise OSError when they hit the truncated body.
        Regression for the 'failed to fetch' bug where the OSError bubbled
        past the route's ImageConversionError catch and produced a 500
        without CORS headers."""
        raw = _make_image_bytes(800, 800, "PNG")
        # Keep the header (~50 bytes) but lose most of the IDAT chunk
        truncated = raw[:80]
        with pytest.raises(ImageConversionError):
            _normalize_to_9x16(truncated)


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

    def test_ffmpeg_command_uses_quiet_logging(self, tmp_path):
        """ffmpeg must be invoked with -loglevel error -nostats so stderr
        contains only real warnings/errors. Without this, the per-frame
        progress stream dominates stderr and our error tail surfaces
        progress noise instead of the actual failure reason."""
        captured_argv: list[list[str]] = []

        real_run = subprocess.run

        def spy_run(cmd, *args, **kwargs):
            captured_argv.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        out = tmp_path / "out.mp4"
        raw = _make_image_bytes(1080, 1920, "PNG")
        with patch.object(image_to_video.subprocess, "run", side_effect=spy_run):
            image_bytes_to_mp4(raw, str(out), duration_s=1.0)

        assert captured_argv, "expected subprocess.run to be called"
        argv = captured_argv[0]
        assert argv[0] == "ffmpeg"
        assert "-loglevel" in argv, f"missing -loglevel in {argv!r}"
        assert argv[argv.index("-loglevel") + 1] == "error"
        assert "-nostats" in argv, f"missing -nostats in {argv!r}"

    def test_ffmpeg_failure_surfaces_real_error_not_progress(self, tmp_path):
        """When ffmpeg exits non-zero, the user-facing ImageConversionError
        message must contain the actual error line — not the trailing
        progress chatter that previously dominated proc.stderr[-200:].

        Regression for: 'FFmpeg failed converting image to mp4: ate=N/A
        speed=N/A frame= 2 fps=2.0 q=26.0 size= 0KiB time=00:00:00.00 …'
        (May 2026, Impressing Myself template).
        """
        real_error = (
            "[libx264 @ 0x55] Error initializing output stream 0:0 -- "
            "Error while opening encoder for output stream #0:0"
        )
        fake_completed = subprocess.CompletedProcess(
            args=["ffmpeg"],
            returncode=1,
            stdout="",
            stderr=real_error + "\n",
        )

        out = tmp_path / "out.mp4"
        raw = _make_image_bytes(1080, 1920, "PNG")
        with patch.object(image_to_video.subprocess, "run", return_value=fake_completed):
            with pytest.raises(ImageConversionError) as excinfo:
                image_bytes_to_mp4(raw, str(out), duration_s=1.0)

        msg = str(excinfo.value)
        assert "Error initializing output stream" in msg, (
            f"expected real ffmpeg error in message, got {msg!r}"
        )
        # Must NOT contain the kind of progress-noise the old behavior surfaced
        assert "frame=" not in msg
        assert "fps=" not in msg
        assert "size=" not in msg

    def test_color_metadata_tagged_bt709(self, tmp_path):
        """The converted mp4 MUST advertise bt709 primaries/matrix/transfer
        in its H.264 SPS. Without -x264-params colorprim=bt709:..., libx264
        ignores FFmpeg-level -color_primaries and the file ships with
        primaries=unknown — which crashes the downstream reframe colorspace
        filter (rc=234, EINVAL). Observed in prod on 2026-05-12, job 2795fa69.
        """
        out = tmp_path / "out.mp4"
        raw = _make_image_bytes(1080, 1920, "PNG")
        image_bytes_to_mp4(raw, str(out), duration_s=2.0)
        primaries, transfer, space = _ffprobe_color_metadata(str(out))
        assert primaries == "bt709", (
            f"color_primaries must be bt709 (was {primaries!r}); "
            f"missing -x264-params propagation"
        )
        assert transfer == "bt709", f"color_transfer must be bt709 (was {transfer!r})"
        assert space == "bt709", f"color_space must be bt709 (was {space!r})"
