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
        """colorspace is first (SDR no-op), then setpts before scale/crop."""
        filters = _build_video_filter("16:9", None, speed_factor=2.0)
        assert filters[0] == "colorspace=all=bt709"
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
        """has_grid=False (default) adds no grid filters."""
        filters = _build_video_filter("9:16", None)
        joined = ",".join(filters)
        assert "iw/3" not in joined  # no inner grid line at 1/3
        assert "2*iw/3" not in joined  # no inner grid line at 2/3

    def test_grid_overlay_adds_inner_drawbox_lines(self):
        """has_grid=True adds 4 drawbox calls (2V + 2H) for the inner rule-of-thirds lines.

        Inner-only by design: drawgrid would also draw the outer frame at x=0/iw
        and y=0/ih which doesn't belong to the rule-of-thirds grid.
        """
        filters = _build_video_filter("9:16", None, has_grid=True)
        joined = ",".join(filters)
        # 2 vertical inner lines at iw/3 and 2*iw/3, 2 horizontal at ih/3 and 2*ih/3.
        assert "x=iw/3-" in joined
        assert "x=2*iw/3-" in joined
        assert "y=ih/3-" in joined
        assert "y=2*ih/3-" in joined
        # Default white color @ 0.6 opacity, thickness 3, fill stroke.
        assert "color=0xFFFFFF@0.60" in joined
        assert "w=3:h=ih" in joined  # vertical stroke uses thickness as width
        assert "h=3" in joined  # horizontal stroke uses thickness as height

    def test_grid_overlay_custom_color_opacity_thickness(self):
        """Grid respects custom color/opacity/thickness across all 4 inner lines."""
        filters = _build_video_filter(
            "9:16", None,
            has_grid=True,
            grid_color="#E63946",
            grid_opacity=0.8,
            grid_thickness=5,
        )
        joined = ",".join(filters)
        assert "color=0xE63946@0.80" in joined
        assert "w=5:h=ih" in joined  # vertical stroke width = thickness
        # 4 grid drawbox calls (excluding any highlight overlays which we didn't request).
        grid_boxes = [f for f in filters if "drawbox" in f and "0xE63946@0.80" in f]
        assert len(grid_boxes) == 4

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

    def test_grid_highlight_adds_two_drawbox_for_v_and_h_lines(self):
        """grid_highlight_intersection adds ONE vertical + ONE horizontal stroke
        on top of the 4 base grid drawboxes."""
        filters = _build_video_filter(
            "9:16", None,
            has_grid=True,
            grid_highlight_intersection="bottom-right",
            grid_highlight_color="#D9435A",
            grid_highlight_windows=[(0.0, 0.4), (1.0, 1.3)],
        )
        drawboxes = [f for f in filters if f.startswith("drawbox=")]
        # 4 base grid drawboxes (white) + 2 highlight drawboxes (red).
        assert len(drawboxes) == 6
        highlights = [f for f in drawboxes if "0xD9435A" in f]
        assert len(highlights) == 2
        # bottom-right -> vertical at 2*iw/3, horizontal at 2*ih/3
        v_line = next(f for f in highlights if "h=ih" in f)
        h_line = next(f for f in highlights if "w=iw" in f)
        assert "x=2*iw/3" in v_line
        assert "y=2*ih/3" in h_line
        # Both highlights carry the windowed enable expression.
        for f in highlights:
            assert "enable='between(t,0.000,0.400)+between(t,1.000,1.300)'" in f

    def test_grid_highlight_skipped_when_intersection_missing(self):
        """has_grid alone (no intersection) draws only the 4 white base grid lines."""
        filters = _build_video_filter(
            "9:16", None,
            has_grid=True,
            grid_highlight_intersection=None,
            grid_highlight_windows=[(0.0, 0.4)],
        )
        drawboxes = [f for f in filters if f.startswith("drawbox=")]
        # All 4 base grid drawboxes present, none with the highlight color.
        assert len(drawboxes) == 4
        assert not any("0xE63946" in f or "0xD9435A" in f for f in drawboxes)

    def test_grid_highlight_sustained_when_windows_none(self):
        """intersection without windows → highlight on for the whole slot (no enable clause)."""
        filters = _build_video_filter(
            "9:16", None,
            has_grid=True,
            grid_highlight_intersection="top-left",
            grid_highlight_windows=None,
        )
        drawboxes = [f for f in filters if f.startswith("drawbox=")]
        # 4 base + 2 highlight (no windows).
        assert len(drawboxes) == 6
        highlights = [f for f in drawboxes if "0xE63946" in f]
        assert len(highlights) == 2
        for f in highlights:
            assert "enable=" not in f, "sustained highlight must skip the enable clause"

    def test_grid_highlight_top_left_picks_left_v_and_top_h(self):
        """top-left intersection -> left-vertical (iw/3) + top-horizontal (ih/3)."""
        filters = _build_video_filter(
            "9:16", None,
            has_grid=True,
            grid_highlight_intersection="top-left",
            grid_highlight_windows=[(0.0, 0.4)],
        )
        highlights = [
            f for f in filters
            if f.startswith("drawbox=") and "0xE63946" in f
        ]
        assert len(highlights) == 2
        v_line = next(f for f in highlights if "h=ih" in f)
        h_line = next(f for f in highlights if "w=iw" in f)
        assert "x=iw/3" in v_line and "2*iw/3" not in v_line
        assert "y=ih/3" in h_line and "2*ih/3" not in h_line

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
            # The 4 base grid drawboxes carry the default white color; the
            # last one must still come before the ass subtitle filter so
            # captions render on top of the grid.
            grid_idx = max(
                i for i, f in enumerate(filters)
                if f.startswith("drawbox=") and "0xFFFFFF" in f
            )
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

    def test_fast_preset_includes_scenecut(self):
        """Final-output encodes (preset=fast) include scenecut=40:keyint=90."""
        args = _encoding_args("/tmp/out.mp4", preset="fast")
        assert "-x264-params" in args
        x264_idx = args.index("-x264-params")
        assert "scenecut=40" in args[x264_idx + 1]
        assert "keyint=90" in args[x264_idx + 1]

    def test_ultrafast_enforces_closed_gop_no_bframes(self):
        """Intermediate encodes (preset=ultrafast) must explicitly disable
        B-frames and open-GOP so the downstream stream-copy concat doesn't
        produce 1-frame alpha-blend artifacts at slot boundaries (PR #87
        introduced -c copy concat; open-GOP B-frames at clip start cause
        the decoder to interpolate across boundaries → visible crossfade
        at every hard cut, perceived as 'lag/glitch at scene start')."""
        args = _encoding_args("/tmp/out.mp4", preset="ultrafast")
        assert "-bf" in args, "ultrafast must explicitly disable B-frames"
        assert args[args.index("-bf") + 1] == "0"
        assert "-x264-params" in args, "ultrafast must set x264-params for closed-GOP"
        params = args[args.index("-x264-params") + 1]
        assert "open_gop=0" in params, f"expected open_gop=0 in {params!r}"
        assert "bframes=0" in params, f"expected bframes=0 in {params!r}"
        assert "scenecut=0" in params, f"ultrafast keeps scenecut=0: {params!r}"


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


# ──────────────────────────────────────────────────────────────────────────
# Slot assembly: _join_or_concat split-render
# ──────────────────────────────────────────────────────────────────────────
#
# Regression context: the 17-slot Dimples Passport recipe (15 hard-cuts +
# 1 dissolve) used to truncate to ~3-4s in production because chaining many
# xfade=fade:duration=0.001 filters causes FFmpeg to drop frames. Reference
# good output: ~/Downloads/brazil.mp4 — 21.4s, 642 frames @ 30fps.
#
# These tests run real FFmpeg (no mocks) because the truncation bug only
# reproduces with actual frame counting through the filter chain.

import subprocess as _subprocess  # noqa: E402

from app.tasks.template_orchestrate import _join_or_concat, _probe_duration  # noqa: E402, PLC0415


def _make_test_clip(tmp_path, idx: int, dur_s: float = 1.2) -> str:
    """Generate a 1080x1920 30fps test clip of `dur_s` seconds.

    Uses lavfi testsrc so each clip has unique visible content (frame counter
    + tile pattern), making it easy to eyeball the output if a test ever
    needs manual debugging.
    """
    out = str(tmp_path / f"clip_{idx:02d}.mp4")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc=duration={dur_s}:size=1080x1920:rate=30",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        out,
    ]
    result = _subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"failed to make test clip {idx}: {result.stderr.decode()[:200]}"
        )
    return out


@pytest.mark.skipif(
    _subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False).returncode != 0,
    reason="ffmpeg not installed",
)
class TestJoinOrConcatSplitRender:
    """The Dimples Passport bug: 17 slots + 15 hard-cut + 1 dissolve must
    NOT truncate to ~3s. The fix groups hard-cut runs through concat demuxer
    and only invokes xfade for real visual transitions."""

    def test_dimples_shape_does_not_truncate(self, tmp_path):
        """REGRESSION: 17 slots, 4 hard-cut + 1 crossfade + 11 hard-cut.
        Output must be within 1s of sum(durations) - crossfade overlap.
        Pre-fix this returned ~3-4s; brazil.mp4 reference is 21.4s.
        """
        slot_dur = 1.2
        n_slots = 17
        paths = [_make_test_clip(tmp_path, i, dur_s=slot_dur) for i in range(n_slots)]
        durations = [slot_dur] * n_slots
        # Dimples slot 6 has dissolve → between paths[4] and paths[5].
        transitions = ["none"] * 4 + ["crossfade"] + ["none"] * 11
        assert len(transitions) == n_slots - 1

        out = str(tmp_path / "joined.mp4")
        _join_or_concat(paths, transitions, durations, out, str(tmp_path))

        actual = _probe_duration(out)
        # Crossfade overlaps ~0.3s (clamped to 30% of shorter neighbour).
        expected = (slot_dur * n_slots) - 0.3
        assert abs(actual - expected) < 1.0, (
            f"output {actual:.2f}s diverged from expected ~{expected:.2f}s "
            f"— xfade truncation bug returned (compare brazil.mp4 21.4s)"
        )

    def test_all_hard_cuts_uses_concat_only(self, tmp_path):
        """All 'none' transitions → single concat group, no xfade call.
        17-slot all-hard-cut shape (which previously also went through the
        buggy xfade path when has_interstitials=False)."""
        slot_dur = 0.8
        n_slots = 17
        paths = [_make_test_clip(tmp_path, i, dur_s=slot_dur) for i in range(n_slots)]
        durations = [slot_dur] * n_slots
        transitions = ["none"] * (n_slots - 1)

        out = str(tmp_path / "joined.mp4")
        _join_or_concat(paths, transitions, durations, out, str(tmp_path))

        actual = _probe_duration(out)
        expected = slot_dur * n_slots  # no xfade overlap
        assert abs(actual - expected) < 0.5, (
            f"all-hard-cut output {actual:.2f}s != expected {expected:.2f}s"
        )

    def test_all_visual_transitions_still_works(self, tmp_path):
        """Pure xfade chain with real durations — no regression for templates
        whose every boundary is visual."""
        slot_dur = 2.0
        paths = [_make_test_clip(tmp_path, i, dur_s=slot_dur) for i in range(3)]
        durations = [slot_dur] * 3
        transitions = ["crossfade", "crossfade"]

        out = str(tmp_path / "joined.mp4")
        _join_or_concat(paths, transitions, durations, out, str(tmp_path))

        actual = _probe_duration(out)
        expected = (slot_dur * 3) - 0.6  # 2 × 0.3s overlap
        assert abs(actual - expected) < 0.5

    def test_single_slot_passthrough(self, tmp_path):
        """Single slot → copy through, no concat or xfade."""
        slot_dur = 1.5
        paths = [_make_test_clip(tmp_path, 0, dur_s=slot_dur)]
        out = str(tmp_path / "joined.mp4")
        _join_or_concat(paths, [], [slot_dur], out, str(tmp_path))

        actual = _probe_duration(out)
        assert abs(actual - slot_dur) < 0.2

    def test_mixed_groups_split_correctly(self, tmp_path):
        """Pattern: hard,hard,VISUAL,hard,hard → 2 concat groups + 1 xfade.
        Each group has 3 slots; xfade joins the groups."""
        slot_dur = 1.0
        paths = [_make_test_clip(tmp_path, i, dur_s=slot_dur) for i in range(6)]
        durations = [slot_dur] * 6
        transitions = ["none", "none", "wipe_left", "none", "none"]

        out = str(tmp_path / "joined.mp4")
        _join_or_concat(paths, transitions, durations, out, str(tmp_path))

        actual = _probe_duration(out)
        # Two groups: 3s + 3s, joined with wipe_left (0.3s overlap)
        expected = 6.0 - 0.3
        assert abs(actual - expected) < 0.5


class TestJoinWithTransitionsRejectsNone:
    """transitions.py contract: caller must group out 'none' before calling."""

    def test_raises_on_none_transition(self, tmp_path):
        from app.pipeline.transitions import join_with_transitions  # noqa: PLC0415

        with pytest.raises(ValueError, match="does not handle 'none'"):
            join_with_transitions(
                ["a.mp4", "b.mp4", "c.mp4"],
                ["crossfade", "none"],  # second is invalid
                [1.0, 1.0, 1.0],
                str(tmp_path / "out.mp4"),
            )

    def test_accepts_only_visual_transitions(self, tmp_path):
        """Sanity: schema validation passes when all transitions are visual.
        FFmpeg invocation isn't tested here — just the validation gate."""
        from app.pipeline.transitions import join_with_transitions  # noqa: PLC0415

        # File-not-found error is fine; we only care that ValueError is NOT
        # raised by the 'none' guard.
        with pytest.raises(Exception) as excinfo:
            join_with_transitions(
                ["/nonexistent_a.mp4", "/nonexistent_b.mp4"],
                ["crossfade"],
                [1.0, 1.0],
                str(tmp_path / "out.mp4"),
            )
        assert "does not handle 'none'" not in str(excinfo.value)
