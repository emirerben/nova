"""Unit tests for speech_coverage (Lane C spine detection signal)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services import clip_speech
from app.services.clip_speech import _silent_seconds, speech_coverage


def _probe(duration_s: float = 10.0, has_audio: bool = True):
    return SimpleNamespace(duration_s=duration_s, has_audio=has_audio)


def _ffmpeg_result(stderr: str, returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stderr=stderr.encode())


# ── _silent_seconds (pure parser) ──────────────────────────────────────────


def test_silent_seconds_paired_intervals():
    stderr = (
        "[silencedetect] silence_start: 2.0\n"
        "[silencedetect] silence_end: 4.0 | silence_duration: 2.0\n"
        "[silencedetect] silence_start: 7.0\n"
        "[silencedetect] silence_end: 9.0 | silence_duration: 2.0\n"
    )
    assert _silent_seconds(stderr, duration=10.0) == 4.0


def test_silent_seconds_unclosed_trailing_clamps_to_duration():
    # File ends mid-silence: a final silence_start with no matching silence_end.
    stderr = "[silencedetect] silence_start: 8.0\n"
    assert _silent_seconds(stderr, duration=10.0) == 2.0


def test_silent_seconds_negative_start_floored():
    stderr = (
        "[silencedetect] silence_start: -0.5\n"
        "[silencedetect] silence_end: 1.0 | silence_duration: 1.5\n"
    )
    assert _silent_seconds(stderr, duration=10.0) == 1.0


# ── speech_coverage ─────────────────────────────────────────────────────────


def test_all_speech_no_silence_markers_is_full_coverage():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result("")),
    ):
        assert speech_coverage("x.mp4") == 1.0


def test_fully_silent_clip_is_zero_coverage():
    stderr = "silence_start: 0.0\nsilence_end: 10.0 | silence_duration: 10.0\n"
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result(stderr)),
    ):
        assert speech_coverage("x.mp4") == 0.0


def test_partial_silence_is_partial_coverage():
    # 4s silent of 10s → 0.6 coverage.
    stderr = "silence_start: 0.0\nsilence_end: 4.0 | silence_duration: 4.0\n"
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result(stderr)),
    ):
        assert abs(speech_coverage("x.mp4") - 0.6) < 1e-6


def test_no_audio_stream_is_zero_without_ffmpeg():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe(has_audio=False)),
        patch.object(clip_speech.subprocess, "run") as run,
    ):
        assert speech_coverage("x.mp4") == 0.0
        run.assert_not_called()  # short-circuits before spending an ffmpeg pass


def test_probe_failure_is_zero():
    with patch.object(clip_speech, "probe_video", side_effect=RuntimeError("boom")):
        assert speech_coverage("x.mp4") == 0.0


def test_zero_duration_is_zero():
    with patch.object(clip_speech, "probe_video", return_value=_probe(duration_s=0.0)):
        assert speech_coverage("x.mp4") == 0.0


def test_ffmpeg_nonzero_exit_is_zero():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(
            clip_speech.subprocess, "run", return_value=_ffmpeg_result("err", returncode=1)
        ),
    ):
        assert speech_coverage("x.mp4") == 0.0


def test_ffmpeg_exception_is_zero():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", side_effect=OSError("no ffmpeg")),
    ):
        assert speech_coverage("x.mp4") == 0.0


def test_ffmpeg_command_decodes_audio_only():
    """-vn/-sn/-dn must stay in the silencedetect command: decoding the video
    stream just to measure audio silence is 10-50x slower and can blow the 60s
    probe timeout on long phone clips — which scores 0.0 and misroutes a
    genuinely narrated clip set to montage (self-narration resolution)."""
    captured: dict = {}

    def _capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ffmpeg_result("")

    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", _capture_run),
    ):
        speech_coverage("x.mp4")

    for flag in ("-vn", "-sn", "-dn"):
        assert flag in captured["cmd"]
    # Audio-strip flags must come before the output spec, after the input.
    assert captured["cmd"].index("-vn") > captured["cmd"].index("-i")
