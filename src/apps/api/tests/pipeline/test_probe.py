"""Unit tests for pipeline/probe.py"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.probe import ProbeError, ProbeTimeout, VideoProbe, _classify_aspect, probe_video


class TestClassifyAspect:
    def test_landscape_16_9(self):
        assert _classify_aspect(1920, 1080) == "16:9"

    def test_vertical_9_16(self):
        assert _classify_aspect(1080, 1920) == "9:16"

    def test_square_is_other(self):
        assert _classify_aspect(1080, 1080) == "other"

    def test_zero_dimensions(self):
        assert _classify_aspect(0, 0) == "other"


class TestProbeVideo:
    def _make_ffprobe_output(
        self,
        duration="120.5",
        width=1920,
        height=1080,
        codec="h264",
        fps="30/1",
        file_size="500000000",
        has_audio=True,
    ) -> str:
        streams = [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": width,
                "height": height,
                "r_frame_rate": fps,
                "duration": duration,
            }
        ]
        if has_audio:
            streams.append({"codec_type": "audio", "codec_name": "aac"})
        return json.dumps({
            "streams": streams,
            "format": {"duration": duration, "size": file_size},
        })

    def test_happy_path_16_9(self):
        output = self._make_ffprobe_output()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=output, stderr=""
            )
            result = probe_video("/fake/video.mp4")

        assert isinstance(result, VideoProbe)
        assert result.width == 1920
        assert result.height == 1080
        assert result.aspect_ratio == "16:9"
        assert result.duration_s == pytest.approx(120.5)
        assert result.fps == pytest.approx(30.0)
        assert result.has_audio is True
        assert result.codec == "h264"

    def test_happy_path_9_16(self):
        output = self._make_ffprobe_output(width=1080, height=1920)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
            result = probe_video("/fake/video.mp4")

        assert result.aspect_ratio == "9:16"

    def test_no_audio_stream(self):
        output = self._make_ffprobe_output(has_audio=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
            result = probe_video("/fake/video.mp4")

        assert result.has_audio is False

    def test_nonzero_returncode_raises_probe_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
            with pytest.raises(ProbeError, match="ffprobe failed"):
                probe_video("/fake/bad.mp4")

    def test_timeout_raises_probe_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=60)):  # noqa: E501
            with pytest.raises(ProbeTimeout):
                probe_video("/fake/video.mp4")

    def test_invalid_json_raises_probe_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
            with pytest.raises(ProbeError, match="not valid JSON"):
                probe_video("/fake/video.mp4")

    def test_no_video_stream_raises_probe_error(self):
        output = json.dumps({"streams": [], "format": {}})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
            with pytest.raises(ProbeError, match="No video stream"):
                probe_video("/fake/video.mp4")

    def test_fractional_fps(self):
        output = self._make_ffprobe_output(fps="24000/1001")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
            result = probe_video("/fake/video.mp4")

        assert result.fps == pytest.approx(23.976, rel=1e-3)

    def test_ffprobe_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(ProbeError, match="ffprobe not found"):
                probe_video("/fake/video.mp4")
