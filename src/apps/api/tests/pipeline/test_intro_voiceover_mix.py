"""Tests for intro_voiceover_mix.py — VO + ducked-music mixer for fixed-intro templates.

Strategy:
- Mock _probe_duration + subprocess.run to drive code paths deterministically.
- Inspect the assembled FFmpeg command + filter graph for correctness markers
  (acrossfade ramp matches music_fadeup_ms, atrim windows match intro_duration_s,
  loudnorm target matches output_lufs, etc.).
- Verify graceful degradation: VO probe fails → music-only path; music probe
  fails → VO-only path; both fail → input video copied.

This is a property test, NOT byte-identical render comparison. Mixing real
audio in CI is too slow and fragile across FFmpeg builds.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.intro_voiceover_mix import (
    _probe_duration,
    render_intro_voiceover_mix,
)


def _ok(stderr: bytes = b"") -> MagicMock:
    """A subprocess.run result with returncode=0."""
    r = MagicMock()
    r.returncode = 0
    r.stderr = stderr
    r.stdout = b""
    return r


def _fail(stderr: bytes = b"ffmpeg failed") -> MagicMock:
    r = MagicMock()
    r.returncode = 1
    r.stderr = stderr
    r.stdout = b""
    return r


# ── Happy path ───────────────────────────────────────────────────────────────


class TestRenderHappyPath:
    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_full_3stream_filter_graph_built(self, mock_probe, mock_run):
        # video=30, music=30, vo=10.5
        mock_probe.side_effect = lambda p: {
            "/v.mp4": 30.0, "/m.m4a": 30.0, "/vo.m4a": 10.5,
        }[p]
        mock_run.return_value = _ok()

        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
            music_duck_db=-12.0, music_fadeup_ms=200, output_lufs=-14.0,
        )

        # Inspect the ffmpeg call
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        # 3 inputs: video, music, voiceover
        assert cmd.count("-i") == 3
        # Filter graph contains the key parts
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "asplit=2" in fc
        assert "volume=-12.00dB" in fc
        assert "acrossfade=d=0.200" in fc
        assert "amix=inputs=2" in fc
        assert "loudnorm=I=-14.0" in fc
        assert "afade=t=out" in fc
        # Output stream mapping
        assert "[out_a]" in cmd
        # Audio re-encode (cannot copy because we mixed)
        assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "aac"
        # Video stream copy (no re-encode)
        assert cmd[cmd.index("-c:v") + 1] == "copy"


# ── Probe-failure fallbacks ──────────────────────────────────────────────────


class TestProbeFailures:
    @patch("app.pipeline.intro_voiceover_mix.shutil.copy2")
    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_video_probe_fail_copies_input(self, mock_probe, mock_run, mock_copy):
        mock_probe.side_effect = lambda p: 0.0 if p == "/v.mp4" else 30.0
        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
        )
        # FFmpeg never invoked; input video copied to output
        mock_run.assert_not_called()
        mock_copy.assert_called_once_with("/v.mp4", "/out.mp4")

    @patch("app.pipeline.intro_voiceover_mix.shutil.copy2")
    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_both_audio_probes_fail_copies_input(self, mock_probe, mock_run, mock_copy):
        # video ok, music+vo both fail
        mock_probe.side_effect = lambda p: 30.0 if p == "/v.mp4" else 0.0
        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
        )
        mock_run.assert_not_called()
        mock_copy.assert_called_once_with("/v.mp4", "/out.mp4")

    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_vo_probe_fail_falls_back_to_music_only(self, mock_probe, mock_run):
        # video=30, music=30, vo=0 (probe failed)
        mock_probe.side_effect = lambda p: {
            "/v.mp4": 30.0, "/m.m4a": 30.0, "/vo.m4a": 0.0,
        }[p]
        mock_run.return_value = _ok()

        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
        )

        cmd = mock_run.call_args[0][0]
        # Only 2 inputs: video + music (no VO)
        assert cmd.count("-i") == 2
        fc = cmd[cmd.index("-filter_complex") + 1]
        # Music-only graph: no asplit, no acrossfade, no amix
        assert "asplit" not in fc
        assert "amix" not in fc
        assert "loudnorm=I=-14.0" in fc
        assert "afade=t=out" in fc

    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_music_probe_fail_falls_back_to_vo_only(self, mock_probe, mock_run):
        # video=30, music=0 (failed), vo=10.5
        mock_probe.side_effect = lambda p: {
            "/v.mp4": 30.0, "/m.m4a": 0.0, "/vo.m4a": 10.5,
        }[p]
        mock_run.return_value = _ok()

        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
        )

        cmd = mock_run.call_args[0][0]
        # 2 inputs: video + voiceover (no music)
        assert cmd.count("-i") == 2
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "apad=pad_dur=30.000" in fc
        assert "asplit" not in fc  # no music to split
        assert "amix" not in fc


# ── FFmpeg failure → graceful copy ───────────────────────────────────────────


class TestFFmpegFailure:
    @patch("app.pipeline.intro_voiceover_mix.shutil.copy2")
    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_ffmpeg_nonzero_falls_back_to_copy(self, mock_probe, mock_run, mock_copy):
        mock_probe.side_effect = lambda p: {
            "/v.mp4": 30.0, "/m.m4a": 30.0, "/vo.m4a": 10.5,
        }[p]
        mock_run.return_value = _fail(b"some ffmpeg error")

        # Should NOT raise; should copy input video as fallback.
        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
        )
        mock_copy.assert_called_once_with("/v.mp4", "/out.mp4")


# ── Filter graph parameter coverage ──────────────────────────────────────────


class TestFilterGraphParams:
    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_custom_duck_db_and_fadeup_ms(self, mock_probe, mock_run):
        mock_probe.side_effect = lambda p: {
            "/v.mp4": 30.0, "/m.m4a": 30.0, "/vo.m4a": 10.5,
        }[p]
        mock_run.return_value = _ok()

        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
            music_duck_db=-9.0, music_fadeup_ms=350, output_lufs=-16.0,
        )
        fc = mock_run.call_args[0][0][
            mock_run.call_args[0][0].index("-filter_complex") + 1
        ]
        assert "volume=-9.00dB" in fc
        assert "acrossfade=d=0.350" in fc
        assert "loudnorm=I=-16.0" in fc

    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    @patch("app.pipeline.intro_voiceover_mix._probe_duration")
    def test_short_vo_truncated_by_apad_atrim(self, mock_probe, mock_run):
        # VO is 3.2s, intro is 10.5s — VO should be padded with silence to 30s.
        mock_probe.side_effect = lambda p: {
            "/v.mp4": 30.0, "/m.m4a": 30.0, "/vo.m4a": 3.2,
        }[p]
        mock_run.return_value = _ok()

        render_intro_voiceover_mix(
            video_path="/v.mp4", music_path="/m.m4a", voiceover_path="/vo.m4a",
            output_path="/out.mp4", intro_duration_s=10.5,
        )
        fc = mock_run.call_args[0][0][
            mock_run.call_args[0][0].index("-filter_complex") + 1
        ]
        # apad to full video duration (30s), then atrim guarantees no overshoot
        assert "apad=pad_dur=30.000" in fc
        assert "atrim=0:30.000" in fc


# ── _probe_duration sentinel ─────────────────────────────────────────────────


class TestProbeDuration:
    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    def test_probe_returns_float_on_success(self, mock_run):
        r = MagicMock()
        r.returncode = 0
        r.stdout = b"14.976893\n"
        mock_run.return_value = r
        assert _probe_duration("/x.mp4") == pytest.approx(14.976893)

    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    def test_probe_returns_zero_on_garbage(self, mock_run):
        r = MagicMock()
        r.returncode = 0
        r.stdout = b"not-a-number"
        mock_run.return_value = r
        assert _probe_duration("/x.mp4") == 0.0

    @patch("app.pipeline.intro_voiceover_mix.subprocess.run")
    def test_probe_returns_zero_on_empty(self, mock_run):
        r = MagicMock()
        r.returncode = 0
        r.stdout = b""
        mock_run.return_value = r
        assert _probe_duration("/x.mp4") == 0.0
