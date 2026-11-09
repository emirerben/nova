"""Çiğdem S2 "ss2b" end to end: "okay, we're recording" opening a take is cut by speech cleanup.

A synthetic take shaped like the incident (slate 0.0-1.04 s, a pause, then the talk) is written to
a real WAV and pushed through the REAL stages: ffmpeg silencedetect, the real cut planner (V1
baseline and V2 mixed-gap candidate), the real PCM decode and voice guard. Only speech-to-text is
replaced, by the words the incident's transcript stamped.

Ways this could mislead (docs/reviews/cigdem-s2-failure-modes.md A):

- the slate is cut anyway as a pause (negative control: flag off keeps every slate word);
- the voice guard drops the slate cut because it is voiced (the cut must survive the guard);
- the cut eats the first real word (the kept audio must start at or before "Kervan").
"""

from __future__ import annotations

import shutil
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SR = 16_000
DURATION_S = 6.0
WORDS = (
    ("Okay,", 0.00, 0.30),
    ("we're", 0.36, 0.60),
    ("recording.", 0.60, 1.04),
    ("Kervan", 1.54, 1.90),
    ("celebrated", 1.95, 2.45),
    ("its", 2.50, 2.62),
    ("tenth", 2.66, 2.98),
    ("year", 3.02, 3.30),
    ("with", 3.34, 3.50),
    ("a", 3.54, 3.60),
    ("sunrise", 3.64, 4.10),
    ("show.", 4.14, 4.60),
)
KERVAN_START = 1.54


def _voice(n: int, level_db: float = -20.0) -> np.ndarray:
    t = np.arange(n) / SR
    f = 118.0 * (1.0 + 0.03 * np.sin(2 * np.pi * 3.0 * t))
    phase = 2 * np.pi * np.cumsum(f) / SR
    signal = sum(np.sin(k * phase) / k for k in range(1, 13))
    signal = signal * np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.03)
    signal = signal / np.sqrt(np.mean(signal**2))
    return signal * 10 ** (level_db / 20)


@pytest.fixture(scope="module")
def take(tmp_path_factory) -> Path:
    rng = np.random.default_rng(1108)
    x = rng.standard_normal(int(DURATION_S * SR)) * 10 ** (-55 / 20)
    for _text, start, end in WORDS:
        i, j = int(start * SR), int(end * SR)
        x[i:j] += _voice(j - i)
    path = tmp_path_factory.mktemp("ss2b-slate") / "take.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _analyze(take: Path, mode: str):
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="e2e-ss2b-slate",
            local_media_path=str(take),
            duration_s=DURATION_S,
            mixed_gap_mode=mode,
        ),
        transcribe_fn=lambda *_a, **_k: types.SimpleNamespace(
            words=[
                types.SimpleNamespace(text=text, start_s=start, end_s=end, confidence=0.99)
                for text, start, end in WORDS
            ],
            language="en",
            low_confidence=False,
        ),
    )


def _kept_at(result, at_s: float) -> bool:
    return not any(r.start_s <= at_s < r.end_s for r in result.cut_plan.removed)


@pytest.mark.parametrize("mode", ["off", "apply"])
def test_slate_is_cut_and_the_talk_starts_on_its_first_word(take, monkeypatch, mode):
    monkeypatch.setattr(settings, "speech_cleanup_slate_cut_enabled", True)
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", True)
    result = _analyze(take, mode)
    for _text, start, end in WORDS[:3]:
        assert not _kept_at(result, (start + end) / 2), result.cut_plan.removed
    for _text, start, end in WORDS[3:]:
        assert _kept_at(result, (start + end) / 2), result.cut_plan.removed
    assert _kept_at(result, KERVAN_START + 0.01)


@pytest.mark.parametrize("mode", ["off", "apply"])
def test_flag_off_keeps_the_slate(take, monkeypatch, mode):
    # Negative control: the same take without the detector keeps every slate word.
    monkeypatch.setattr(settings, "speech_cleanup_slate_cut_enabled", False)
    result = _analyze(take, mode)
    for _text, start, end in WORDS[:3]:
        assert _kept_at(result, (start + end) / 2), result.cut_plan.removed


@pytest.mark.parametrize("mode", ["off", "apply"])
@pytest.mark.parametrize("flag", ["retake_cut_enabled", "speech_cleanup_retakes_enabled"])
def test_slate_waits_for_consented_retakes(take, monkeypatch, mode, flag):
    # Review of #2230: the slate is cut as a retake, so it stays behind the same consent fence
    # as every other retake (both retake switches on); with either off it is kept.
    monkeypatch.setattr(settings, "speech_cleanup_slate_cut_enabled", True)
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", True)
    monkeypatch.setattr(settings, flag, False)
    result = _analyze(take, mode)
    for _text, start, end in WORDS[:3]:
        assert _kept_at(result, (start + end) / 2), result.cut_plan.removed
