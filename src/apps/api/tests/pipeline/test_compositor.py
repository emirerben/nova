"""Tests for reframe.py — assert FFmpeg command is a list (no shell=True), correct filters."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.reframe import (
    MAX_OUTPUT_BYTES,
    ReframeError,
    _build_video_filter,
    _color_hint_filters,
    _encoding_args,
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

    def test_speed_factor_2x_adds_setpts(self):
        """speed_factor=2.0 → setpts=PTS/2.0 in filter chain."""
        filters = _build_video_filter("16:9", None, speed_factor=2.0)
        joined = ",".join(filters)
        assert "setpts=PTS/2.0" in joined

    def test_speed_factor_1_no_setpts(self):
        """speed_factor=1.0 → no setpts filter (avoid rounding artifacts)."""
        filters = _build_video_filter("16:9", None, speed_factor=1.0)
        joined = ",".join(filters)
        assert "setpts" not in joined

    def test_speed_factor_half_adds_setpts(self):
        """speed_factor=0.5 → setpts=PTS/0.5 in filter chain."""
        filters = _build_video_filter("16:9", None, speed_factor=0.5)
        joined = ",".join(filters)
        assert "setpts=PTS/0.5" in joined

    def test_setpts_is_after_colorspace(self):
        """colorspace is first (HDR→SDR), then setpts before scale/crop."""
        filters = _build_video_filter("16:9", None, speed_factor=2.0)
        assert filters[0] == "colorspace=all=bt709:iall=bt2020"
        assert filters[1] == "setpts=PTS/2.0"
        assert "scale" in filters[2]

    def test_color_hint_warm(self):
        """Color hint 'warm' adds colorbalance filter."""
        filters = _build_video_filter("16:9", None, color_hint="warm")
        joined = ",".join(filters)
        assert "colorbalance" in joined

    def test_color_hint_none_no_extra_filters(self):
        """Color hint 'none' adds no color grading."""
        filters = _build_video_filter("16:9", None, color_hint="none")
        joined = ",".join(filters)
        assert "colorbalance" not in joined
        assert "eq=" not in joined

    def test_darkening_window_adds_colorlevels(self):
        filters = _build_video_filter(
            "16:9", None, darkening_windows=[(0.0, 2.0)]
        )
        joined = ",".join(filters)
        assert "colorlevels" in joined
        assert "between(t,0.000,2.000)" in joined

    def test_grid_overlay_disabled_by_default(self):
        """has_grid=False (default) adds no drawgrid filter."""
        filters = _build_video_filter("9:16", None)
        assert not any("drawgrid" in f for f in filters)

    def test_grid_overlay_adds_drawgrid_filter(self):
        """has_grid=True appends a drawgrid filter with rule-of-thirds geometry."""
        filters = _build_video_filter("9:16", None, has_grid=True)
        grid_filter = next((f for f in filters if "drawgrid" in f), None)
        assert grid_filter is not None
        assert "drawgrid=w=iw/3:h=ih/3" in grid_filter
        # Default white color @ 0.6 opacity, thickness 3
        assert ":t=3:" in grid_filter
        assert "c=0xFFFFFF@0.60" in grid_filter

    def test_grid_overlay_custom_color_opacity_thickness(self):
        """Grid respects custom color/opacity/thickness."""
        filters = _build_video_filter(
            "9:16", None,
            has_grid=True,
            grid_color="#E63946",
            grid_opacity=0.8,
            grid_thickness=5,
        )
        joined = ",".join(filters)
        assert "c=0xE63946@0.80" in joined
        assert "t=5" in joined

    def test_grid_color_validator_rejects_non_hex(self):
        """RecipeSlotSchema rejects grid_color values that aren't 6-digit hex."""
        from pydantic import ValidationError

        from app.routes.admin import RecipeSlotSchema

        for bad in ("#XXXXXX", "#GGGGGG", "FFFFFF", "#FF", "#FFFFFFFF", "red", "#;rm -r"):
            with pytest.raises(ValidationError):
                RecipeSlotSchema(
                    position=1, target_duration_s=2.0, slot_type="b-roll",
                    has_grid=True, grid_color=bad,
                )

    def test_grid_color_validator_rejects_rgba(self):
        """9-char RGBA form is rejected (conflicts with @opacity spec)."""
        from pydantic import ValidationError

        from app.routes.admin import RecipeSlotSchema

        with pytest.raises(ValidationError):
            RecipeSlotSchema(
                position=1, target_duration_s=2.0, slot_type="b-roll",
                has_grid=True, grid_color="#FFFFFFAA",
            )

    def test_grid_overlay_before_caption_ass(self):
        """Grid filter must come before ass subtitle so captions render on top."""
        import os
        import tempfile
        # Create a temp file that exists so ass filter is appended
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tmp:
            tmp.write(b"[Script Info]\n")
            ass_path = tmp.name
        try:
            filters = _build_video_filter(
                "9:16", ass_path, has_grid=True,
            )
            grid_idx = next(i for i, f in enumerate(filters) if "drawgrid" in f)
            ass_idx = next(i for i, f in enumerate(filters) if f.startswith("ass="))
            assert grid_idx < ass_idx
        finally:
            os.unlink(ass_path)

    def test_narrowing_window_adds_drawbox(self):
        filters = _build_video_filter(
            "16:9", None, narrowing_windows=[(1.0, 3.0)]
        )
        joined = ",".join(filters)
        assert "drawbox" in joined


class TestEncodingArgs:
    def test_returns_correct_number_of_args(self):
        args = _encoding_args("/tmp/out.mp4")
        assert isinstance(args, list)
        assert len(args) >= 14
        assert args[-1] == "/tmp/out.mp4"
        assert "-c:v" in args
        assert "libx264" in args

    def test_contains_output_path(self):
        args = _encoding_args("/my/output.mp4")
        assert "/my/output.mp4" in args


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


class TestBuildVideoFilterOverlays:
    """Tests for darkening and narrowing in _build_video_filter."""

    def test_darkening_window_produces_colorlevels(self):
        filters = _build_video_filter(
            "9:16", None,
            darkening_windows=[(0.5, 2.5)],
        )
        joined = ",".join(filters)
        assert "colorlevels" in joined
        assert "enable=" in joined
        assert "rimax=0.45" in joined

    def test_multiple_darkening_windows(self):
        filters = _build_video_filter(
            "9:16", None,
            darkening_windows=[(0.5, 2.5), (4.0, 6.0)],
        )
        joined = ",".join(filters)
        assert joined.count("colorlevels") == 2

    def test_narrowing_window_produces_drawbox(self):
        filters = _build_video_filter(
            "9:16", None,
            narrowing_windows=[(0.5, 2.5)],
        )
        joined = ",".join(filters)
        assert joined.count("drawbox") == 2
        assert "y=0" in joined
        assert "y=ih-120" in joined

    def test_filter_ordering(self):
        """scale before colorlevels before drawbox."""
        filters = _build_video_filter(
            "9:16", None,
            darkening_windows=[(0.5, 2.5)],
            narrowing_windows=[(0.5, 2.5)],
        )
        joined = ",".join(filters)
        scale_pos = joined.index("scale")
        color_pos = joined.index("colorlevels")
        drawbox_pos = joined.index("drawbox")
        assert scale_pos < color_pos < drawbox_pos

    def test_no_new_params_backward_compat(self):
        """Calling with None defaults produces same filters as before."""
        filters_old = _build_video_filter("16:9", None)
        filters_new = _build_video_filter(
            "16:9", None,
            darkening_windows=None,
            narrowing_windows=None,
        )
        assert filters_old == filters_new

    def test_reframe_passes_new_params(self):
        """Verify reframe_and_export passes darkening/narrowing to _build_video_filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")

            def side_effect(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * 1024)
                return MagicMock(returncode=0, stderr=b"")

            with (
                patch("subprocess.run", side_effect=side_effect),
                patch(
                    "app.pipeline.reframe._build_video_filter",
                    wraps=_build_video_filter,
                ) as mock_bvf,
            ):
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=5.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                    darkening_windows=[(0.5, 2.0)],
                    narrowing_windows=[(0.5, 2.0)],
                )
                call_kwargs = mock_bvf.call_args.kwargs
                assert call_kwargs["darkening_windows"] == [(0.5, 2.0)]
                assert call_kwargs["narrowing_windows"] == [(0.5, 2.0)]

    def test_darkening_enable_timing_format(self):
        """enable='between(t,0.5,2.5)' correctly formatted."""
        filters = _build_video_filter(
            "9:16", None,
            darkening_windows=[(0.5, 2.5)],
        )
        joined = ",".join(filters)
        assert "between(t,0.500,2.500)" in joined


class TestOverlayCommand:
    """Tests for PNG overlay compositing via filter_complex."""

    def test_overlay_pngs_uses_filter_complex(self):
        """When text_overlay_pngs is provided, command uses -filter_complex."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dummy PNG
            png_path = os.path.join(tmpdir, "overlay.png")
            from PIL import Image
            Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(png_path)

            output = os.path.join(tmpdir, "out.mp4")

            def side_effect(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * 1024)
                return MagicMock(returncode=0, stderr=b"")

            with patch("subprocess.run", side_effect=side_effect) as mock_run:
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=5.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                    text_overlay_pngs=[{
                        "png_path": png_path,
                        "start_s": 0.5,
                        "end_s": 2.5,
                    }],
                )
                cmd = mock_run.call_args[0][0]
                assert "-filter_complex" in cmd
                assert "-vf" not in cmd
                # PNG should be an input
                assert png_path in cmd

    def test_no_overlay_uses_vf(self):
        """Without overlays, command uses simple -vf (no filter_complex)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")

            def side_effect(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * 1024)
                return MagicMock(returncode=0, stderr=b"")

            with patch("subprocess.run", side_effect=side_effect) as mock_run:
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=5.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                )
                cmd = mock_run.call_args[0][0]
                assert "-vf" in cmd
                assert "-filter_complex" not in cmd

    def test_overlay_enable_timing_in_filter_complex(self):
        """The overlay filter has correct enable timing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            png_path = os.path.join(tmpdir, "overlay.png")
            from PIL import Image
            Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(png_path)

            output = os.path.join(tmpdir, "out.mp4")

            def side_effect(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * 1024)
                return MagicMock(returncode=0, stderr=b"")

            with patch("subprocess.run", side_effect=side_effect) as mock_run:
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=5.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                    text_overlay_pngs=[{
                        "png_path": png_path,
                        "start_s": 0.5,
                        "end_s": 2.5,
                    }],
                )
                cmd = mock_run.call_args[0][0]
                fc_idx = cmd.index("-filter_complex")
                fc_string = cmd[fc_idx + 1]
                assert "overlay=0:0" in fc_string
                assert "between(t,0.500,2.500)" in fc_string


class TestColorGrading:
    """Tests for color grading via _build_video_filter and _color_hint_filters."""

    def test_color_grade_warm_produces_colorbalance(self):
        filters = _build_video_filter("9:16", None, color_hint="warm")
        joined = ",".join(filters)
        assert "colorbalance" in joined
        assert "rs=0.15" in joined

    def test_color_grade_cool_produces_colorbalance(self):
        filters = _build_video_filter("9:16", None, color_hint="cool")
        joined = ",".join(filters)
        assert "colorbalance" in joined
        assert "bs=0.15" in joined

    def test_color_grade_high_contrast_produces_eq(self):
        filters = _build_video_filter("9:16", None, color_hint="high-contrast")
        joined = ",".join(filters)
        assert "eq=contrast=1.3" in joined

    def test_color_grade_desaturated_produces_eq(self):
        filters = _build_video_filter("9:16", None, color_hint="desaturated")
        joined = ",".join(filters)
        assert "eq=saturation=0.6" in joined

    def test_color_grade_none_no_filter(self):
        filters_none = _build_video_filter("9:16", None, color_hint="none")
        filters_default = _build_video_filter("9:16", None)
        assert filters_none == filters_default

    def test_color_grade_invalid_no_filter(self):
        """Unrecognized color_hint produces no color filter."""
        filters = _build_video_filter("9:16", None, color_hint="banana")
        # Should be identical to no color hint
        filters_none = _build_video_filter("9:16", None, color_hint="none")
        assert filters == filters_none

    def test_color_grade_vintage_two_filters(self):
        """Vintage produces two separate filter items (colorbalance + eq)."""
        result = _color_hint_filters("vintage")
        assert len(result) == 2
        assert "colorbalance" in result[0]
        assert "eq=saturation" in result[1]

        # Verify they appear as separate items in the full filter chain
        filters = _build_video_filter("9:16", None, color_hint="vintage")
        joined = ",".join(filters)
        assert "colorbalance" in joined
        assert "eq=saturation=0.8" in joined

    def test_filter_ordering_color_before_darkening(self):
        """Color grading filters appear before darkening (colorlevels)."""
        filters = _build_video_filter(
            "9:16", None,
            color_hint="warm",
            darkening_windows=[(0.5, 2.5)],
        )
        joined = ",".join(filters)
        color_pos = joined.index("colorbalance")
        dark_pos = joined.index("colorlevels")
        assert color_pos < dark_pos

    def test_color_hint_threading_through_reframe(self):
        """Verify reframe_and_export passes color_hint to _build_video_filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")

            def side_effect(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * 1024)
                return MagicMock(returncode=0, stderr=b"")

            with (
                patch("subprocess.run", side_effect=side_effect),
                patch(
                    "app.pipeline.reframe._build_video_filter",
                    wraps=_build_video_filter,
                ) as mock_bvf,
            ):
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=5.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                    color_hint="warm",
                )
                call_kwargs = mock_bvf.call_args.kwargs
                assert call_kwargs["color_hint"] == "warm"

    def test_color_hint_backward_compat_default_none(self):
        """reframe_and_export without color_hint defaults to 'none' (no color filter)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.mp4")

            def side_effect(cmd, **kwargs):
                with open(output, "wb") as f:
                    f.write(b"\x00" * 1024)
                return MagicMock(returncode=0, stderr=b"")

            with (
                patch("subprocess.run", side_effect=side_effect),
                patch(
                    "app.pipeline.reframe._build_video_filter",
                    wraps=_build_video_filter,
                ) as mock_bvf,
            ):
                reframe_and_export(
                    input_path="/fake/input.mp4",
                    start_s=0.0,
                    end_s=5.0,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=output,
                )
                call_kwargs = mock_bvf.call_args.kwargs
                assert call_kwargs["color_hint"] == "none"
