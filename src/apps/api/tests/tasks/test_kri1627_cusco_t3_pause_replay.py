"""KRI-1627, Cusco DJI T3 "Sticker 1" (prod job a4f7b702, 2026-11-16): replayed.

Clean up speech kept a 1.65 s mid-sentence pause between "Ant Dağları'nın" and "bayırlarında"
(source 42.00-43.65 s). whisper-1 stretched the tail of the word before it AND the head of the
word after it into the silence (both stamps meet at 42.76 s), so rule 3 never saw a gap, and
KRI-1512's head trim refuses that shape ("both edges in one pause"). It is still kept on
`mixed-gap-v11-stretched-words`.

Real: the cleanup analysis (`analyze_speech_cleanup`: the late-word move, the stretched-head and
stretched-pair trims, the cut planner, the KRI-1017 voice guard). Fixture
(`tests/fixtures/kri1627_cusco_t3/pause.json`): the words the job stored, the kit clip's silence
spans and voiced runs (the take's PCM is re-synthesized from them), and an independent
voice-band read of the clip. No model call, no media. Failure modes:
docs/reviews/kri-1627-stretched-pairs-failure-modes.md.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_voice_guard import quiet_floor_db, voiced_runs
from app.services.clip_speech import SilenceDetectionResult

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri1627_cusco_t3" / "pause.json"
    ).read_text(encoding="utf-8")
)
DURATION_S = float(FIXTURE["duration_s"])
WORDS = FIXTURE["words"]
SILENCES = [tuple(span) for span in FIXTURE["silence_spans"]]
VOICED = [tuple(run) for run in FIXTURE["voiced_runs"]]
PROD_REMOVED = [tuple(span) for span in FIXTURE["prod"]["removed"]]
GAPS = FIXTURE["speech_truth"]["voice_band_gaps"]
PAUSE = (42.00, 43.65)  # the voice-band gap the review found, 1.65 s
SR = 16_000
_HOP_S = 0.01  # speech_voice_guard.HOP_S
STICKER_WORDS = ("yılanla", "puma.", "kondor,")


def _pcm(voiced=None) -> np.ndarray:
    """The take re-synthesized: room noise at the clip's floor, a voiced tone in each voiced run
    at the clip's speech level (as `test_kri1502_cusco_t2_replay._pcm`)."""
    rng = np.random.default_rng(1627)
    n = int(DURATION_S * SR)
    floor_rms = 10 ** (FIXTURE["floor_db"] / 20)
    speech_rms = 10 ** (FIXTURE["speech_db"] / 20)
    out = rng.normal(0.0, floor_rms, n)
    t = np.arange(n) / SR
    tone = sum(np.sin(2 * np.pi * 140.0 * k * t) / k for k in (1, 2, 3, 4))
    tone *= speech_rms / np.sqrt(np.mean(tone**2))
    for lo, hi in VOICED if voiced is None else voiced:
        a, b = int((lo + _HOP_S) * SR), int((hi - _HOP_S) * SR)
        out[a:b] = tone[a:b] + rng.normal(0.0, floor_rms, b - a)
    return out.astype(np.float32)


def _analysis(
    monkeypatch, *, pairs: bool, stretched: bool = True, late: bool = True, voiced=None
) -> SpeechCleanupAnalysisResult:
    monkeypatch.setattr(settings, "speech_cleanup_late_words_enabled", late)
    monkeypatch.setattr(settings, "speech_cleanup_stretched_words_enabled", stretched)
    monkeypatch.setattr(settings, "speech_cleanup_stretched_pairs_enabled", pairs)
    samples = _pcm(voiced)
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="d" * 64,
            local_media_path="preflight.wav",
            duration_s=DURATION_S,
            source_window_start_s=0.0,
            source_window_end_s=DURATION_S,
            mixed_gap_mode="apply",
            over_budget_policy="clamp",
            max_removal_frac_required=1.0,
            filler_check=True,
        ),
        transcribe_fn=lambda *_a, **_k: {
            "language": "tr",
            "words": copy.deepcopy(WORDS),
            "low_confidence": False,
        },
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=SILENCES, status="ok"),
        pcm_loader=lambda _path: samples,
    )


def _removed(result: SpeechCleanupAnalysisResult) -> list[tuple[float, float]]:
    return [(round(r.start_s, 3), round(r.end_s, 3)) for r in result.cut_plan.removed]


def _kept_s(lo: float, hi: float, removed) -> float:
    return hi - lo - sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in removed)


def _word(result: SpeechCleanupAnalysisResult, text: str) -> tuple[float, float]:
    [word] = [w for w in result.timed_words if w.text == text]
    return word.start_s, word.end_s


# ------------------------------------------------------------------ [o] the oracle


def test_o_the_pause_is_silent_and_both_stamps_reach_into_it():
    """The independent voice-band read has a 1.65 s non-speech gap at the pause; whisper-1's
    stamps for the word before and the word after both reach into it and meet there."""
    assert [g for g in GAPS if g[0] == PAUSE[0]] == [[42.0, 43.65, 1.65]]
    by_text = {w["text"]: w for w in WORDS}
    tail, head = by_text["Dağları'nın"], by_text["bayırlarında..."]
    assert tail["start_s"] < PAUSE[0] < tail["end_s"] == head["start_s"] < PAUSE[1] < head["end_s"]
    # No voiced run in the pause beyond the 30 ms edges the floor clearance allows.
    inside = sum(max(0.0, min(43.617, b) - max(42.028, a)) for a, b in VOICED)
    assert inside < 0.05


def test_o_the_resynthesized_take_is_voiced_where_the_clip_is():
    samples = _pcm()
    heard = voiced_runs(samples, 0.0, DURATION_S, floor_db=quiet_floor_db(samples))

    def total(runs, lo, hi):
        return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in runs)

    for lo, hi in ((41.0, 42.1), PAUSE, (43.5, 44.6)):
        assert total(heard, lo, hi) == pytest.approx(total(VOICED, lo, hi), abs=0.03)
    assert total(heard, 0.0, DURATION_S) == pytest.approx(total(VOICED, 0.0, DURATION_S), rel=0.03)


# ------------------------------------------------------------------ the controls


def test_control_both_word_fixes_off_reproduce_the_ten_cuts_prod_made(monkeypatch):
    result = _analysis(monkeypatch, pairs=False, stretched=False, late=False)
    assert _removed(result) == PROD_REMOVED
    assert _kept_s(*PAUSE, PROD_REMOVED) == pytest.approx(1.65)


def test_control_switch_off_keeps_the_pause(monkeypatch):
    """KRI-1512 alone (today's main): the pair is refused and the pause stays whole."""
    removed = _removed(_analysis(monkeypatch, pairs=False))
    assert _kept_s(*PAUSE, removed) == pytest.approx(1.65)


def test_control_voice_in_the_pause_trims_nothing(monkeypatch):
    voiced = [*VOICED, (42.70, 42.95)]
    off = _removed(_analysis(monkeypatch, pairs=False, voiced=voiced))
    on = _analysis(monkeypatch, pairs=True, voiced=voiced)
    assert _removed(on) == off
    assert _word(on, "Dağları'nın") == (41.46, 42.76)


def test_control_no_voice_before_the_pause_trims_nothing(monkeypatch):
    voiced = [run for run in VOICED if run != (41.453, 42.031)]
    on = _analysis(monkeypatch, pairs=True, voiced=voiced)
    assert _word(on, "Dağları'nın") == (41.46, 42.76)
    assert _word(on, "bayırlarında...") == (42.76, 44.5)


# ------------------------------------------------------------------ the fix


def test_both_stamps_go_back_to_the_silence_edges(monkeypatch):
    result = _analysis(monkeypatch, pairs=True)
    assert _word(result, "Dağları'nın") == (41.46, 42.028)
    assert _word(result, "bayırlarında...") == (43.617, 44.5)


def test_the_pause_is_cut_and_every_other_cut_stays(monkeypatch):
    off = _removed(_analysis(monkeypatch, pairs=False))
    on = _removed(_analysis(monkeypatch, pairs=True))
    new = [span for span in on if span not in off]
    assert [span for span in off if span not in on] == []
    [(lo, hi)] = new
    assert PAUSE[0] < lo < hi < PAUSE[1]
    # Rule 3 keeps KEPT_GAP_S around the cut: about a quarter second of the pause stays.
    assert _kept_s(*PAUSE, on) == pytest.approx(0.31, abs=0.05)


def test_no_cut_touches_a_voiced_run(monkeypatch):
    removed = _removed(_analysis(monkeypatch, pairs=True))
    for lo, hi in removed:
        assert sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in VOICED) < 0.02, (lo, hi)


def test_the_sticker_words_keep_their_times(monkeypatch):
    off = _analysis(monkeypatch, pairs=False)
    on = _analysis(monkeypatch, pairs=True)
    for text in STICKER_WORDS:
        assert _word(on, text) == _word(off, text)
    changed = [
        w.text
        for w, before in zip(on.timed_words, off.timed_words, strict=True)
        if (w.start_s, w.end_s) != (before.start_s, before.end_s)
    ]
    assert changed == ["Dağları'nın", "bayırlarında..."]
