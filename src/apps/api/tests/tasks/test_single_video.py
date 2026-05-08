"""Integration tests for the single_video template path.

Tests the helper functions in template_orchestrate.py that compose the new
template: ASS subtitle generation, body window selection, and silent concat.

These hit real ffmpeg/ffprobe via the helpers; tests need ffmpeg on PATH
(installed in the dev container and in CI). Tests covering the full
orchestrator entry (Celery task, GCS download, copy gen) are out of scope
here — see eval/ for end-to-end smoke runs.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.tasks.template_orchestrate import (
    _HARD_MIN_BODY_S,
    _audio_energy_curve,
    _build_fixed_intro_ass,
    _concat_silent,
    _render_intro_with_captions,
    _seconds_to_ass_time,
    _select_body_window,
    _select_body_window_peak_anchored,
)

# Skip these tests if ffmpeg is missing (e.g. minimal CI without media tools).
HAVE_FFMPEG = subprocess.run(
    ["which", "ffmpeg"], capture_output=True, check=False
).returncode == 0
pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")


# ── _seconds_to_ass_time ─────────────────────────────────────────────────────


class TestSecondsToAssTime:
    def test_zero(self):
        assert _seconds_to_ass_time(0.0) == "0:00:00.00"

    def test_sub_minute(self):
        assert _seconds_to_ass_time(7.34) == "0:00:07.34"

    def test_minute_boundary(self):
        assert _seconds_to_ass_time(60.0) == "0:01:00.00"

    def test_hour_boundary(self):
        assert _seconds_to_ass_time(3661.5) == "1:01:01.50"

    def test_negative_clamped(self):
        assert _seconds_to_ass_time(-1.0) == "0:00:00.00"


# ── _build_fixed_intro_ass ───────────────────────────────────────────────────


class TestBuildFixedIntroAss:
    def test_writes_one_dialogue_per_caption(self, tmp_path):
        captions = [
            {"text": "Do you smoke?", "start_s": 0.0, "end_s": 1.4},
            {"text": "No",            "start_s": 1.5, "end_s": 2.7},
        ]
        out = tmp_path / "captions.ass"
        _build_fixed_intro_ass(captions, {}, str(out))
        content = out.read_text(encoding="utf-8")

        # Header invariants
        assert "[Script Info]" in content
        assert "PlayResX: 1080" in content
        assert "PlayResY: 1920" in content
        # Two Dialogue events
        assert content.count("Dialogue: ") == 2
        assert "Do you smoke?" in content
        assert "No\n" in content or "No," in content  # trailing newline or escape

    def test_font_family_translation_dmsans(self, tmp_path):
        captions = [{"text": "Hi", "start_s": 0.0, "end_s": 1.0}]
        out = tmp_path / "captions.ass"
        _build_fixed_intro_ass(
            captions, {"font_family": "DMSans-Bold"}, str(out)
        )
        # The seed-script value DMSans-Bold maps to ASS-registered "DM Sans"
        assert "DM Sans" in out.read_text()

    def test_unknown_font_passes_through(self, tmp_path):
        captions = [{"text": "Hi", "start_s": 0.0, "end_s": 1.0}]
        out = tmp_path / "captions.ass"
        _build_fixed_intro_ass(
            captions, {"font_family": "SomeFancyFont"}, str(out)
        )
        assert "SomeFancyFont" in out.read_text()

    def test_color_hex_converted_to_bgr(self, tmp_path):
        captions = [{"text": "Hi", "start_s": 0.0, "end_s": 1.0}]
        out = tmp_path / "captions.ass"
        # Red (#FF0000) → BGR 0000FF
        _build_fixed_intro_ass(
            captions, {"color": "#FF0000"}, str(out)
        )
        assert "&H000000FF" in out.read_text()

    def test_caption_text_with_apostrophe_preserved(self, tmp_path):
        # "I don't have" must render as-is; ASS handles apostrophes in text.
        captions = [{"text": "I don't have", "start_s": 0.0, "end_s": 1.0}]
        out = tmp_path / "captions.ass"
        _build_fixed_intro_ass(captions, {}, str(out))
        assert "I don't have" in out.read_text()


# ── _render_intro_with_captions ──────────────────────────────────────────────


class TestRenderIntroWithCaptions:
    def test_renders_30fps_silent_mp4_with_correct_duration(self, tmp_path):
        captions = [
            {"text": "Do you smoke?", "start_s": 0.0, "end_s": 1.4},
            {"text": "No",            "start_s": 1.5, "end_s": 2.5},
        ]
        out = tmp_path / "intro.mp4"
        _render_intro_with_captions(
            captions=captions,
            style={"font_family": "DMSans-Bold", "font_size": 64, "color": "#FFFFFF"},
            intro_duration_s=3.0,
            background_color="#000000",
            out_path=str(out),
            tmpdir=str(tmp_path),
        )
        assert out.exists()
        # ffprobe: duration ~3s, fps=30, no audio, size 1080x1920
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate", "-show_entries",
             "format=duration", "-of", "default=nw=1", str(out)],
            capture_output=True, check=False,
        )
        text = probe.stdout.decode()
        assert "width=1080" in text
        assert "height=1920" in text
        assert "r_frame_rate=30/1" in text
        # No audio stream
        assert "codec_type=audio" not in text
        # Duration close to 3.0 (allow ±100ms for keyframe alignment)
        for line in text.splitlines():
            if line.startswith("duration="):
                dur = float(line.split("=", 1)[1])
                assert abs(dur - 3.0) < 0.15
                break
        else:
            pytest.fail(f"no duration line in probe: {text}")


# ── _concat_silent ───────────────────────────────────────────────────────────


def _make_silent_segment(out_path: str, duration_s: float, color: str = "blue") -> None:
    """Helper: render a silent test mp4 with the same spec as intro/body."""
    subprocess.run(
        ["ffmpeg", "-f", "lavfi",
         "-i", f"color=c={color}:s=1080x1920:d={duration_s}:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
         "-an", "-y", out_path],
        capture_output=True, check=True,
    )


class TestConcatSilent:
    def test_concat_two_segments(self, tmp_path):
        a = tmp_path / "a.mp4"
        b = tmp_path / "b.mp4"
        out = tmp_path / "joined.mp4"
        _make_silent_segment(str(a), 1.5, color="red")
        _make_silent_segment(str(b), 2.0, color="green")
        _concat_silent([str(a), str(b)], str(out), str(tmp_path))
        assert out.exists()

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(out)],
            capture_output=True, check=False,
        )
        dur = float(probe.stdout.decode().strip())
        # Allow ±100ms for keyframe alignment.
        assert abs(dur - 3.5) < 0.2


# ── _select_body_window ──────────────────────────────────────────────────────
# Tests assume `_audio_energy_curve` returns [] (no audio peak) unless the
# test explicitly patches it — keeping the existing tests valid as
# "midpoint-fallback" cases. Audio-peak behavior gets its own block below.


class TestSelectBodyWindow:
    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    @patch("app.pipeline.scene_detect.detect_scenes")
    @patch("app.pipeline.probe.probe_video")
    def test_no_scene_cuts_uses_natural_midpoint(self, mock_probe, mock_scenes, _energy):
        mock_probe.return_value = MagicMock(duration_s=60.0)
        mock_scenes.return_value = []
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        # natural_start = (60 - 19.5) / 2 = 20.25
        assert start == pytest.approx(20.25)
        assert end == pytest.approx(20.25 + 19.5)
        # End MUST be exactly start + target — total budget guaranteed
        assert (end - start) == pytest.approx(19.5)

    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    @patch("app.pipeline.scene_detect.detect_scenes")
    @patch("app.pipeline.probe.probe_video")
    def test_snap_to_nearest_scene_within_tolerance(self, mock_probe, mock_scenes, _energy):
        from app.pipeline.scene_detect import SceneCut
        mock_probe.return_value = MagicMock(duration_s=60.0)
        # natural midpoint is 20.25; cuts at 19.0 (Δ1.25), 22.0 (Δ1.75), 30.0 (Δ9.75)
        mock_scenes.return_value = [
            SceneCut(timestamp_s=19.0, score=0),
            SceneCut(timestamp_s=22.0, score=1),
            SceneCut(timestamp_s=30.0, score=2),
        ]
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        # 19.0 is within tolerance (Δ1.25 ≤ 1.5); 22.0 is not (Δ1.75 > 1.5)
        assert start == 19.0
        # end ALWAYS = start + target — invariant under refactor
        assert end == pytest.approx(19.0 + 19.5)

    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    @patch("app.pipeline.scene_detect.detect_scenes")
    @patch("app.pipeline.probe.probe_video")
    def test_no_eligible_cuts_keeps_base_pick(self, mock_probe, mock_scenes, _energy):
        from app.pipeline.scene_detect import SceneCut
        mock_probe.return_value = MagicMock(duration_s=60.0)
        # No cuts within ±1.5 of natural=20.25 → keep base_start (no forced snap).
        # New behavior: don't drag the window to a far cut just because it exists.
        mock_scenes.return_value = [
            SceneCut(timestamp_s=10.0, score=0),
            SceneCut(timestamp_s=25.0, score=1),
        ]
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        assert start == pytest.approx(20.25)
        assert end == pytest.approx(20.25 + 19.5)

    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    @patch("app.pipeline.scene_detect.detect_scenes")
    @patch("app.pipeline.probe.probe_video")
    def test_snap_disabled_returns_natural_midpoint(self, mock_probe, mock_scenes, _energy):
        mock_probe.return_value = MagicMock(duration_s=60.0)
        # detect_scenes should not be called when snap is off
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=False,
        )
        mock_scenes.assert_not_called()
        assert start == pytest.approx(20.25)
        assert end == pytest.approx(20.25 + 19.5)

    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    @patch("app.pipeline.scene_detect.detect_scenes")
    @patch("app.pipeline.probe.probe_video")
    def test_short_video_raises(self, mock_probe, mock_scenes, _energy):
        mock_probe.return_value = MagicMock(duration_s=10.0)
        with pytest.raises(ValueError, match="too short"):
            _select_body_window(
                "/u.mp4",
                target_duration_s=19.5,
                tolerance_s=1.5,
                snap_start_to_scene=True,
            )

    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    @patch("app.pipeline.scene_detect.detect_scenes")
    @patch("app.pipeline.probe.probe_video")
    def test_cut_too_close_to_end_excluded(self, mock_probe, mock_scenes, _energy):
        from app.pipeline.scene_detect import SceneCut
        mock_probe.return_value = MagicMock(duration_s=21.0)
        # max_start = 21 - 19.5 = 1.5; cut at 5.0 would overflow
        mock_scenes.return_value = [
            SceneCut(timestamp_s=0.5, score=0),
            SceneCut(timestamp_s=5.0, score=1),  # ineligible: > max_start
        ]
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        # natural_start = (21-19.5)/2 = 0.75; only 0.5 is eligible (within Δ0.25)
        assert start == 0.5
        assert end == pytest.approx(0.5 + 19.5)
        # Total still bounded


# ── Audio-peak path (the actual fix for the Cavani penalty bug) ──────────────


class TestAudioEnergyPeakSelection:
    """Audio-peak detection beats midpoint when the action is off-center."""

    @patch("app.pipeline.scene_detect.detect_scenes", return_value=[])
    @patch("app.pipeline.probe.probe_video")
    @patch("app.tasks.template_orchestrate._audio_energy_curve")
    def test_picks_loudest_window_not_midpoint(
        self, mock_energy, mock_probe, _scenes
    ):
        """User clip 50s; loudest 19.5s window starts at t=25 — not midpoint=15.25."""
        mock_probe.return_value = MagicMock(duration_s=50.0)
        # 50 buckets of 1s. Buckets 25..44 (the celebration) are loud.
        energy = [0.05] * 25 + [0.5] * 20 + [0.05] * 5
        mock_energy.return_value = energy
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        # window_buckets = round(19.5) = 20; sliding sum picks index 25
        assert start == pytest.approx(25.0)
        assert end == pytest.approx(25.0 + 19.5)
        assert (end - start) == pytest.approx(19.5)

    @patch("app.pipeline.scene_detect.detect_scenes", return_value=[])
    @patch("app.pipeline.probe.probe_video")
    @patch("app.tasks.template_orchestrate._audio_energy_curve", return_value=[])
    def test_no_audio_falls_back_to_midpoint(self, _energy, mock_probe, _scenes):
        mock_probe.return_value = MagicMock(duration_s=60.0)
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        assert start == pytest.approx(20.25)  # natural midpoint
        assert end == pytest.approx(20.25 + 19.5)

    @patch("app.pipeline.scene_detect.detect_scenes", return_value=[])
    @patch("app.pipeline.probe.probe_video")
    @patch("app.tasks.template_orchestrate._audio_energy_curve")
    def test_audio_peak_clamped_to_max_start(self, mock_energy, mock_probe, _scenes):
        """Peak window can't slide past (user_dur - target_duration_s)."""
        mock_probe.return_value = MagicMock(duration_s=25.0)
        # Loudest single bucket at end, but window can't start past max_start=5.5
        energy = [0.05] * 24 + [10.0]
        mock_energy.return_value = energy
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        # max_start = 25 - 19.5 = 5.5; peak idx 5 (last valid sliding-window index)
        # is the only window containing the loud bucket → start=5
        assert 0.0 <= start <= 5.5
        assert end == pytest.approx(start + 19.5)

    @patch("app.pipeline.scene_detect.detect_scenes", return_value=[])
    @patch("app.pipeline.probe.probe_video")
    @patch("app.tasks.template_orchestrate._audio_energy_curve")
    def test_audio_peak_overrides_midpoint_in_quiet_video_too(
        self, mock_energy, mock_probe, _scenes
    ):
        """Even with all-quiet audio, the algorithm still picks SOME peak —
        slight noise variance produces a deterministic best window."""
        mock_probe.return_value = MagicMock(duration_s=30.0)
        # All buckets nearly equal; first bucket marginally louder
        energy = [0.11] + [0.10] * 29
        mock_energy.return_value = energy
        start, end = _select_body_window(
            "/u.mp4",
            target_duration_s=19.5,
            tolerance_s=1.5,
            snap_start_to_scene=True,
        )
        # First window includes the marginal-louder bucket
        assert start == pytest.approx(0.0)
        assert end == pytest.approx(19.5)


# ── _select_body_window_peak_anchored ────────────────────────────────────────


class TestSelectBodyWindowPeakAnchored:
    """Locks in the short-clip robustness: clips shorter than `min_body_s`
    use the full available duration instead of raising. Only sub-floor
    (effectively unreadable) clips fail.
    """

    @patch(
        "app.tasks.template_orchestrate._audio_energy_curve",
        return_value=[0.1, 0.2, 0.3, 0.4],
    )
    @patch("app.pipeline.probe.probe_video")
    def test_short_clip_uses_full_duration(self, mock_probe, _energy):
        # 5s clip, template wants 8s min — should NOT raise.
        mock_probe.return_value = MagicMock(duration_s=5.0)
        start, end = _select_body_window_peak_anchored(
            "/u.mp4",
            pre_roll_s=4.0,
            max_body_s=14.0,
            min_body_s=8.0,
        )
        assert start == pytest.approx(0.0)
        assert end == pytest.approx(5.0)

    @patch(
        "app.tasks.template_orchestrate._audio_energy_curve",
        return_value=[],
    )
    @patch("app.pipeline.probe.probe_video")
    def test_unusable_clip_raises(self, mock_probe, _energy):
        # Below the hard floor — corrupt / near-empty file.
        mock_probe.return_value = MagicMock(duration_s=_HARD_MIN_BODY_S / 2)
        with pytest.raises(ValueError, match="unusable"):
            _select_body_window_peak_anchored(
                "/u.mp4",
                pre_roll_s=4.0,
                max_body_s=14.0,
                min_body_s=8.0,
            )

    @patch(
        "app.tasks.template_orchestrate._audio_energy_curve",
        return_value=[0.1] * 5 + [0.9] + [0.1] * 14,  # peak at t=5
    )
    @patch("app.pipeline.probe.probe_video")
    def test_normal_clip_uses_peak_anchored_window(self, mock_probe, _energy):
        # 20s clip, peak at t=5, pre_roll 4 → start=1, end=15.
        mock_probe.return_value = MagicMock(duration_s=20.0)
        start, end = _select_body_window_peak_anchored(
            "/u.mp4",
            pre_roll_s=4.0,
            max_body_s=14.0,
            min_body_s=8.0,
        )
        assert start == pytest.approx(1.0)
        assert end == pytest.approx(15.0)


# ── _audio_energy_curve direct probe ─────────────────────────────────────────


class TestAudioEnergyCurve:
    @patch("app.tasks.template_orchestrate.subprocess.run")
    def test_parses_rms_lines(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        # ffmpeg writes astats output to stderr.
        mock_result.stderr = (
            b"some ignorable line\n"
            b"[ametadata @ 0x1] lavfi.astats.Overall.RMS_level=-12.50\n"
            b"[ametadata @ 0x1] lavfi.astats.Overall.RMS_level=-20.00\n"
            b"[ametadata @ 0x1] lavfi.astats.Overall.RMS_level=-inf\n"
        )
        mock_run.return_value = mock_result
        curve = _audio_energy_curve("/x.mp4", bucket_s=1.0)
        # Three samples; -inf is clamped to -90dB → tiny linear value
        assert len(curve) == 3
        # -12.5dB → ~0.237 linear amplitude
        assert curve[0] == pytest.approx(10 ** (-12.5 / 20), rel=1e-3)
        # -inf-clamped sample is essentially silence
        assert curve[2] < 1e-4

    @patch("app.tasks.template_orchestrate.subprocess.run")
    def test_returns_empty_on_ffmpeg_failure(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"some error"
        mock_run.return_value = mock_result
        assert _audio_energy_curve("/x.mp4", bucket_s=1.0) == []

    @patch("app.tasks.template_orchestrate.subprocess.run")
    def test_returns_empty_when_no_audio(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b"Stream #0: Video only - no audio metadata\n"
        mock_run.return_value = mock_result
        assert _audio_energy_curve("/x.mp4", bucket_s=1.0) == []
