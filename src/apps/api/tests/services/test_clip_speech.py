"""Unit tests for speech_coverage + detect_silences (Lane C spine detection
signal; silence ranges for the silence-cut pipeline, plans/010)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import clip_speech
from app.services.clip_speech import (
    _merge_intervals,
    _silence_intervals,
    _silent_seconds,
    detect_silences,
    speech_coverage,
)


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


# ── _silence_intervals / _merge_intervals (pure parser) ─────────────────────


def test_silence_intervals_pairs_in_emission_order_unmerged():
    stderr = (
        "[silencedetect] silence_start: 7.0\n"
        "[silencedetect] silence_end: 9.0 | silence_duration: 2.0\n"
        "[silencedetect] silence_start: 2.0\n"
        "[silencedetect] silence_end: 4.0 | silence_duration: 2.0\n"
    )
    assert _silence_intervals(stderr, duration=10.0) == [(7.0, 9.0), (2.0, 4.0)]


def test_merge_intervals_sorts_and_coalesces_overlaps():
    assert _merge_intervals([(7.0, 9.0), (2.0, 4.0), (3.5, 5.0)]) == [(2.0, 5.0), (7.0, 9.0)]


def test_merge_intervals_keeps_disjoint_ranges():
    assert _merge_intervals([(1.0, 2.0), (5.0, 6.0)]) == [(1.0, 2.0), (5.0, 6.0)]


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
    # Pre-parameterization filter arg, byte-identical (int-style -30, no .0).
    assert "silencedetect=noise=-30dB:d=0.3" in captured["cmd"]


# ── IRON-RULE regression pin (plans/010 T2) ─────────────────────────────────
# speech_coverage must return the exact pre-refactor value for the same
# synthetic silencedetect stderr. Expected values computed with the
# pre-detect_silences implementation; do NOT update them to match code changes.


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        # No silence markers at all (pure ffmpeg progress noise) → 1.0.
        ("frame=  240 fps= 48 q=-0.0 size=N/A time=00:00:10.00\n", 1.0),
        # Several silences: [1,2] + [5.5,8] = 3.5s silent of 10s → 0.65.
        (
            "[silencedetect @ 0x1] silence_start: 1.0\n"
            "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n"
            "[silencedetect @ 0x1] silence_start: 5.5\n"
            "[silencedetect @ 0x1] silence_end: 8.0 | silence_duration: 2.5\n",
            0.65,
        ),
        # Unclosed trailing silence_start closes at clip end:
        # [1,2] + [8.5,10] = 2.5s silent → 0.75.
        (
            "[silencedetect @ 0x1] silence_start: 1.0\n"
            "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n"
            "[silencedetect @ 0x1] silence_start: 8.5\n",
            0.75,
        ),
        # Garbage lines interleaved: only real markers count → [3,5] → 0.8.
        (
            "Stream mapping:\n"
            "  Stream #0:1 -> #0:0 (aac (native) -> pcm_s16le (native))\n"
            "silence_start: not-a-number\n"
            "[silencedetect @ 0x1] silence_start: 3.0\n"
            "frame=  120 fps= 60 dup=0 drop=0\n"
            "[silencedetect @ 0x1] silence_end: 5.0 | silence_duration: 2.0\n",
            0.8,
        ),
    ],
)
def test_iron_rule_speech_coverage_pinned_values(stderr: str, expected: float):
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result(stderr)),
    ):
        assert speech_coverage("x.mp4") == pytest.approx(expected)


# ── detect_silences ──────────────────────────────────────────────────────────


def test_detect_silences_returns_sorted_pairs():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 2.0\n"
        "[silencedetect @ 0x1] silence_end: 4.0 | silence_duration: 2.0\n"
        "[silencedetect @ 0x1] silence_start: 7.0\n"
        "[silencedetect @ 0x1] silence_end: 9.0 | silence_duration: 2.0\n"
    )
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result(stderr)),
    ):
        assert detect_silences("x.mp4") == [(2.0, 4.0), (7.0, 9.0)]


def test_detect_silences_merges_overlapping_and_sorts():
    # Malformed pairing (missing mid-stream end) yields overlapping raw
    # intervals: starts [2,3] / ends [4,5] → (2,4)+(3,5) → merged (2,5).
    stderr = (
        "[silencedetect @ 0x1] silence_start: 2.0\n"
        "[silencedetect @ 0x1] silence_end: 4.0 | silence_duration: 2.0\n"
        "[silencedetect @ 0x1] silence_start: 3.0\n"
        "[silencedetect @ 0x1] silence_end: 5.0 | silence_duration: 2.0\n"
    )
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result(stderr)),
    ):
        assert detect_silences("x.mp4") == [(2.0, 5.0)]


def test_detect_silences_unclosed_trailing_closes_at_clip_end():
    stderr = "[silencedetect @ 0x1] silence_start: 8.0\n"
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe(duration_s=10.0)),
        patch.object(clip_speech.subprocess, "run", return_value=_ffmpeg_result(stderr)),
    ):
        assert detect_silences("x.mp4") == [(8.0, 10.0)]


def test_detect_silences_empty_on_probe_failure():
    with patch.object(clip_speech, "probe_video", side_effect=RuntimeError("boom")):
        assert detect_silences("x.mp4") == []


def test_detect_silences_empty_on_ffmpeg_exception():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", side_effect=OSError("no ffmpeg")),
    ):
        assert detect_silences("x.mp4") == []


def test_detect_silences_empty_on_ffmpeg_timeout():
    timeout = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", side_effect=timeout),
    ):
        assert detect_silences("x.mp4") == []


def test_detect_silences_empty_on_ffmpeg_nonzero_exit():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(
            clip_speech.subprocess, "run", return_value=_ffmpeg_result("err", returncode=1)
        ),
    ):
        assert detect_silences("x.mp4") == []


def test_detect_silences_no_audio_short_circuits_without_ffmpeg():
    with (
        patch.object(clip_speech, "probe_video", return_value=_probe(has_audio=False)),
        patch.object(clip_speech.subprocess, "run") as run,
    ):
        assert detect_silences("x.mp4") == []
        run.assert_not_called()


def _captured_detect_silences_cmd(**kwargs) -> tuple[list, dict]:
    """Run detect_silences with mocked subprocess; return (cmd, run kwargs)."""
    captured: dict = {}

    def _capture_run(cmd, **run_kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = run_kwargs
        return _ffmpeg_result("")

    with (
        patch.object(clip_speech, "probe_video", return_value=_probe()),
        patch.object(clip_speech.subprocess, "run", _capture_run),
    ):
        detect_silences("x.mp4", **kwargs)
    return captured["cmd"], captured["kwargs"]


def test_detect_silences_command_keeps_audio_only_flags_and_timeout():
    """Same -vn/-sn/-dn pin as test_ffmpeg_command_decodes_audio_only: the
    parameterized path must never regress to decoding video (60s-timeout
    incident in the module header)."""
    cmd, kwargs = _captured_detect_silences_cmd()
    for flag in ("-vn", "-sn", "-dn"):
        assert flag in cmd
    assert cmd.index("-vn") > cmd.index("-i")
    assert kwargs["timeout"] == 60


def test_detect_silences_default_filter_matches_speech_coverage():
    # Float defaults must format int-style: noise=-30dB (not -30.0dB), d=0.3.
    cmd, _ = _captured_detect_silences_cmd()
    assert "silencedetect=noise=-30dB:d=0.3" in cmd


def test_detect_silences_honors_custom_min_silence():
    cmd, _ = _captured_detect_silences_cmd(min_silence_s=0.1)
    assert "silencedetect=noise=-30dB:d=0.1" in cmd


def test_detect_silences_honors_custom_noise_floor():
    cmd, _ = _captured_detect_silences_cmd(noise_db=-35.5, min_silence_s=0.1)
    assert "silencedetect=noise=-35.5dB:d=0.1" in cmd
