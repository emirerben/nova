"""Ambient-adaptive silence spans for speech cleanup (KRI-234).

A night-rain Talking take (job 62716037) had noise transients above
silencedetect's absolute -30 dBFS sample floor, so the detector reported ZERO
silences: pause tightening and the trailing-silence trim never ran, leaving
0.85 s of dead air between sentences and a 2.8 s silent tail. These tests pin
the noise-relative RMS spans that the speech-cleanup callers union in.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.pipeline import speech_cleanup_analysis
from app.pipeline.silence_cut import KEPT_GAP_S, build_cut_plan
from app.services import clip_speech
from app.services.clip_speech import (
    _ENERGY_EDGE_GUARD_S,
    _energy_spans,
    detect_silences,
    detect_silences_with_status,
)

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")

# Tone "speech" bursts over a white-noise bed whose peaks sit above -30 dBFS.
_SPEECH = ((0.0, 2.0), (3.5, 5.5), (6.0, 7.0))
_DURATION = 10.0


def _write_clip(path: Path, *, noise_amplitude: float) -> Path:
    gate = "+".join(f"between(t,{a},{b})" for a, b in _SPEECH)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=d={_DURATION}:c=white:a={noise_amplitude}:seed=7",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=220:d={_DURATION}",
            "-filter_complex",
            f"[1]volume=volume='2.0*if({gate},1,0)':eval=frame[s];[0][s]amix=inputs=2:normalize=0",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def rainy_clip(tmp_path: Path) -> Path:
    return _write_clip(tmp_path / "rainy.wav", noise_amplitude=0.03)


@pytest.fixture
def quiet_clip(tmp_path: Path) -> Path:
    return _write_clip(tmp_path / "quiet.wav", noise_amplitude=0.0005)


@requires_ffmpeg
def test_noise_peaks_hide_every_pause_from_plain_silencedetect(rainy_clip: Path):
    # The incident shape: the absolute floor sees no silence at all.
    assert detect_silences_with_status(str(rainy_clip), min_silence_s=0.1).spans == ()


@requires_ffmpeg
def test_ambient_spans_find_pauses_and_tail_with_edge_guard(rainy_clip: Path):
    result = detect_silences_with_status(str(rainy_clip), min_silence_s=0.1, ambient_adaptive=True)
    assert result.status == "ok"
    spans = result.spans
    # Interior pause 2.0-3.5 and the tail 7.0-10.0 are found.
    pause = next(s for s in spans if s[0] < 3.0 < s[1])
    tail = spans[-1]
    assert tail[1] == pytest.approx(_DURATION, abs=0.06)
    # Guarded: never closer than the guard (minus one window) to the sound.
    window = clip_speech._ENERGY_WINDOW_S
    assert pause[0] >= 2.0 + _ENERGY_EDGE_GUARD_S - window
    assert pause[1] <= 3.5 - _ENERGY_EDGE_GUARD_S + window
    assert tail[0] >= 7.0 + _ENERGY_EDGE_GUARD_S - window
    # No span intrudes into a speech burst.
    for start, end in spans:
        for a, b in _SPEECH:
            assert end <= a + 1e-6 or start >= b - 1e-6, (start, end)
    # The legacy list API returns the same merged spans.
    assert detect_silences(str(rainy_clip), min_silence_s=0.1, ambient_adaptive=True) == list(spans)


@requires_ffmpeg
def test_quiet_clip_keeps_plain_silencedetect_result(quiet_clip: Path):
    plain = detect_silences_with_status(str(quiet_clip), min_silence_s=0.1)
    adaptive = detect_silences_with_status(
        str(quiet_clip), min_silence_s=0.1, ambient_adaptive=True
    )
    assert plain.spans  # quiet footage is already handled by the absolute floor
    assert adaptive == plain


@requires_ffmpeg
def test_cut_plan_tightens_pause_and_tail_with_pre_and_post_roll(rainy_clip: Path):
    words = [
        {"text": "one", "start_s": 0.2, "end_s": 1.0},
        {"text": "two.", "start_s": 1.0, "end_s": 1.95},
        {"text": "three", "start_s": 3.55, "end_s": 4.5},
        {"text": "four.", "start_s": 4.5, "end_s": 5.45},
        {"text": "five.", "start_s": 6.05, "end_s": 6.95},
    ]
    before = build_cut_plan(
        words,
        list(detect_silences_with_status(str(rainy_clip), min_silence_s=0.1).spans),
        _DURATION,
        over_budget_policy="clamp",
    )
    assert before.removed == []  # the bug: nothing tightened, nothing trimmed

    spans = detect_silences_with_status(str(rainy_clip), min_silence_s=0.1, ambient_adaptive=True)
    plan = build_cut_plan(words, list(spans.spans), _DURATION, over_budget_policy="clamp")
    removed = [(r.start_s, r.end_s) for r in plan.removed]
    pause = next(r for r in removed if r[0] < 3.0 < r[1])
    # Pause tightened to the kept residual; both voices keep their roll.
    assert pause[0] - 2.0 >= KEPT_GAP_S / 2 - 1e-6
    assert 3.5 - pause[1] >= _ENERGY_EDGE_GUARD_S - clip_speech._ENERGY_WINDOW_S
    assert (3.5 - 2.0) - (pause[1] - pause[0]) <= 0.4 + 1e-6
    # Silent tail trimmed to well under half a second of trailing air... plus
    # the trail keep the plan has always left after the last word.
    assert removed[-1][1] == pytest.approx(_DURATION)
    assert removed[-1][0] - 7.0 <= 0.6
    # No removal ever starts before a word has ended or ends after one starts.
    for start, end in removed:
        for word in words:
            assert end <= word["start_s"] + 1e-6 or start >= word["end_s"] - 1e-6


def test_energy_spans_guard_interior_edges_only():
    windows = [(i * 0.05, -20.0 if 10 <= i < 20 else -40.0) for i in range(40)]
    spans = _energy_spans(windows, 2.0, threshold_db=-30.0, min_silence_s=0.1)
    assert spans == [
        (0.0, round(0.5 - _ENERGY_EDGE_GUARD_S, 3)),
        (round(1.0 + _ENERGY_EDGE_GUARD_S, 3), 2.0),
    ]


def test_energy_spans_drop_runs_shorter_than_guarded_minimum():
    windows = [(i * 0.05, -40.0 if 10 <= i < 14 else -20.0) for i in range(40)]
    assert _energy_spans(windows, 2.0, threshold_db=-30.0, min_silence_s=0.1) == []


def test_envelope_failure_keeps_silencedetect_spans():
    stderr = (
        "[silencedetect] silence_start: 2.0\n"
        "[silencedetect] silence_end: 3.0 | silence_duration: 1\n"
    )
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if any("silencedetect" in part for part in cmd):
            return SimpleNamespace(returncode=0, stderr=stderr.encode())
        return SimpleNamespace(returncode=1, stderr=b"boom")

    with (
        patch.object(
            clip_speech,
            "probe_video",
            return_value=SimpleNamespace(duration_s=10.0, has_audio=True),
        ),
        patch.object(clip_speech.subprocess, "run", side_effect=fake_run),
    ):
        result = detect_silences_with_status("clip.mp4", min_silence_s=0.1, ambient_adaptive=True)
    assert result.status == "ok"
    assert result.spans == ((2.0, 3.0),)
    assert len(calls) == 2


def test_plain_callers_never_run_the_envelope_pass():
    with (
        patch.object(
            clip_speech,
            "probe_video",
            return_value=SimpleNamespace(duration_s=10.0, has_audio=True),
        ),
        patch.object(
            clip_speech.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stderr=b""),
        ) as run,
    ):
        detect_silences_with_status("clip.mp4", min_silence_s=0.1)
        detect_silences("clip.mp4", min_silence_s=0.1)
        clip_speech.speech_coverage("clip.mp4")
    assert run.call_count == 3


def test_speech_cleanup_analysis_requests_ambient_spans():
    with patch(
        "app.services.clip_speech.detect_silences_with_status", return_value="sentinel"
    ) as detect:
        assert speech_cleanup_analysis._default_silence_detect("a.wav", min_silence_s=0.1) == (
            "sentinel"
        )
    detect.assert_called_once_with("a.wav", min_silence_s=0.1, ambient_adaptive=True)
