"""Tests for reframe.py — assert FFmpeg command is a list (no shell=True), correct filters."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.reframe import (
    MAX_OUTPUT_BYTES,
    ReframeError,
    _build_video_filter,
    reframe_and_export,
)


class TestBuildVideoFilter:
    def test_16_9_contains_scale_and_crop(self):
        filters = _build_video_filter("16:9", None)
        joined = ",".join(filters)
        assert "scale" in joined
        assert "crop" in joined

    def test_9_16_contains_scale_and_pad(self):
        filters = _build_video_filter("9:16", None)
        joined = ",".join(filters)
        assert "scale" in joined
        assert "pad" in joined

    def test_ass_path_appended_when_provided(self):
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as f:
            ass_path = f.name
        try:
            filters = _build_video_filter("9:16", ass_path)
            joined = ",".join(filters)
            assert "ass=" in joined
        finally:
            os.unlink(ass_path)

    def test_no_ass_filter_when_path_is_none(self):
        filters = _build_video_filter("9:16", None)
        joined = ",".join(filters)
        assert "ass=" not in joined

    def test_no_ass_filter_when_file_missing(self):
        filters = _build_video_filter("9:16", "/nonexistent/captions.ass")
        joined = ",".join(filters)
        assert "ass=" not in joined


class TestReframeAndExport:
    def _make_successful_run(self, output_path: str):
        """Simulate ffmpeg writing a valid small output file."""
        def side_effect(cmd, **kwargs):
            # Write a fake output file
            with open(output_path, "wb") as f:
                f.write(b"\x00" * 1024)
            return MagicMock(returncode=0, stderr=b"")

        return side_effect

    def test_command_is_list_not_string(self):
        """Critical security assertion: FFmpeg must be called with a list, never shell=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = self._make_successful_run(output)
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=50.0,
                    aspect_ratio="16:9",
                    ass_subtitle_path=None,
                    output_path=output,
                )
                call_args = mock_run.call_args
                # First positional arg must be a list
                assert isinstance(call_args[0][0], list)
                # shell=True must never be set
                assert call_args[1].get("shell") is not True

    def test_ffmpeg_command_contains_no_shell_strings(self):
        """Assert no f-string user data in command that could cause injection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")
            injected_path = "/fake/input.mp4; rm -rf /"
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = self._make_successful_run(output)
                reframe_and_export(
                    input_path=injected_path,
                    start_s=0.0,
                    end_s=50.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                )
                cmd = mock_run.call_args[0][0]
                # The injected path is in the list as a single element — not executed
                assert injected_path in cmd
                assert isinstance(cmd, list)

    def test_ffmpeg_failure_raises_reframe_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1, stderr=b"ffmpeg: error"
                )
                with pytest.raises(ReframeError, match="FFmpeg failed"):
                    reframe_and_export(
                        input_path="/fake/input.mp4",
                        start_s=0.0,
                        end_s=50.0,
                        aspect_ratio="16:9",
                        ass_subtitle_path=None,
                        output_path=output,
                    )

    def test_oversized_output_raises_reframe_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")

            def make_huge_file(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * (MAX_OUTPUT_BYTES + 1))
                return MagicMock(returncode=0, stderr=b"")

            with patch("subprocess.run", side_effect=make_huge_file):
                with pytest.raises(ReframeError, match="exceeds 500MB"):
                    reframe_and_export(
                        input_path="/fake/input.mp4",
                        start_s=0.0,
                        end_s=50.0,
                        aspect_ratio="16:9",
                        ass_subtitle_path=None,
                        output_path=output,
                    )

    def test_invalid_duration_raises_reframe_error(self):
        with pytest.raises(ReframeError, match="Invalid duration"):
            reframe_and_export(
                input_path="/fake/input.mp4",
                start_s=50.0,
                end_s=10.0,  # end < start
                aspect_ratio="16:9",
                ass_subtitle_path=None,
                output_path="/fake/out.mp4",
            )
