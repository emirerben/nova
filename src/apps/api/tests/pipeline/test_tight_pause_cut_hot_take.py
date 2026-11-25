"""KRI-1524: tight pauses on a take recorded hot (an action camera), against its own floor.

Cusco DJI T1 (job d988bb99): two ordinary outdoor takes, room floor -40 dBFS, speech 28.7 /
30.6 dB above it. The absolute gate (floor above -45 dBFS = noisy) skipped both, and the
absolute pause measure (silencedetect -38 dB) could not see a single pause in them, so seven
0.5-1.2 s gaps stayed after "every pause is cut down to about 0.3 s".

Failure modes written down BEFORE the code (repo testing policy):

1. A genuinely noisy take (speech under ``TIGHT_MIN_SPEECH_OVER_FLOOR_DB`` above the floor) is
   classed hot and tightened: soft speech hidden in the noise gets cut.
2. A quiet take (floor at or below ``TIGHT_MAX_FLOOR_DBFS``) changes in any way with the switch
   on: plan, report or measure.
3. Switch off changes anything: the hot take must get today's ``noisy_take`` report and the
   SAME plan object.
4. The relative measure splits one pause on room-tone flicker, so the report under-counts the
   longest pause and says "tight" while a gap stays.
5. The relative measure calls a soft word silence (over-reports pauses).
6. PCM decode fails in the join-measure path: it must fall back to today's measure, not raise.
7. A take too short for one frame, or all gate (digital silence), crashes the verdict.
8. A cut on the hot take clips a word (the protections are floor-relative but were never
   exercised above -45 dBFS).
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import silence_cut as sc
from app.pipeline import tight_pause_cut as tpc
from tests.pipeline.test_tight_pause_cut import SR, _word_tone, consented

HOT_FLOOR_AMP = 0.01  # about -40 dBFS: the Cusco DJI room floor
HOT_WORD_AMP = 0.8  # speech about 31 dB above that floor
NOISY_FLOOR_AMP = 0.0178  # about -35 dBFS
NOISY_WORD_AMP = 0.3  # speech about 18 dB above it: a restaurant / crowd take
GAPS = [0.9, 0.6, 1.2, 0.5, 0.8]


def _write(path: Path, samples: np.ndarray) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path)


def _take(
    tmp_path: Path,
    gaps: list[float],
    *,
    floor_amp: float,
    word_amp: float,
    name: str = "take.wav",
    gap_inserts: dict[int, np.ndarray] | None = None,
) -> tuple[str, list[dict], float, np.ndarray]:
    """0.5 s lead, 0.4 s words separated by ``gaps``, 0.8 s tail, Gaussian room tone under all of
    it. ``gap_inserts[i]`` is added at the middle of gap ``i``."""
    rng = np.random.default_rng(11)
    words: list[dict] = []
    t = 0.5
    pieces = [np.zeros(int(0.5 * SR))]
    for i in range(len(gaps) + 1):
        words.append({"text": f"w{i}", "start_s": t, "end_s": t + 0.4})
        pieces.append(_word_tone(0.4, amp=word_amp))
        t += 0.4
        if i < len(gaps):
            gap = np.zeros(int(gaps[i] * SR))
            insert = (gap_inserts or {}).get(i)
            if insert is not None:
                mid = (len(gap) - len(insert)) // 2
                gap[mid : mid + len(insert)] += insert
            pieces.append(gap)
            t += gaps[i]
    pieces.append(np.zeros(int(0.8 * SR)))
    clean = np.concatenate(pieces)
    samples = (clean + rng.standard_normal(len(clean)) * floor_amp).astype(np.float32)
    return _write(tmp_path / name, samples), words, len(samples) / SR, samples


@pytest.fixture
def hot(tmp_path):
    path, words, dur, samples = _take(
        tmp_path, GAPS, floor_amp=HOT_FLOOR_AMP, word_amp=HOT_WORD_AMP, name="hot.wav"
    )
    return path, words, dur, samples, consented(path, words, dur)


@pytest.fixture
def noisy(tmp_path):
    path, words, dur, samples = _take(
        tmp_path, GAPS, floor_amp=NOISY_FLOOR_AMP, word_amp=NOISY_WORD_AMP, name="noisy.wav"
    )
    return path, words, dur, samples, consented(path, words, dur)


def _out_gaps(plan: sc.CutPlan, words: list[dict]) -> list[float]:
    def out_t(t: float) -> float:
        return t - sum(max(0.0, min(t, r.end_s) - r.start_s) for r in plan.removed)

    return [out_t(b["start_s"]) - out_t(a["end_s"]) for a, b in zip(words, words[1:], strict=False)]


# --- the gate ---------------------------------------------------------------------------


def test_take_noise_verdicts(hot, noisy, tmp_path):
    _, _, _, hot_samples, _ = hot
    _, _, _, noisy_samples, _ = noisy
    _, _, _, quiet_samples = _take(tmp_path, GAPS, floor_amp=3e-4, word_amp=0.3, name="q.wav")
    assert tpc._room_floor_db(hot_samples, SR) > tpc.TIGHT_MAX_FLOOR_DBFS
    assert tpc.take_noise(hot_samples, SR, relative=True) == "hot"
    assert tpc.take_noise(hot_samples, SR, relative=False) == "noisy"  # today's gate
    assert tpc.take_noise(noisy_samples, SR, relative=True) == "noisy"  # FM 1
    assert tpc.take_noise(quiet_samples, SR, relative=True) == "quiet"
    assert tpc.take_noise(quiet_samples, SR, relative=False) == "quiet"


def test_take_noise_survives_a_short_or_all_gate_take():  # FM 7
    assert tpc.take_noise(np.zeros(10, dtype=np.float32), SR, relative=True) == "quiet"
    assert tpc.take_noise(np.zeros(SR, dtype=np.float32), SR, relative=True) == "quiet"


# --- the cut ----------------------------------------------------------------------------


def test_hot_take_is_cut_tight_and_measured(hot):
    path, words, dur, _, plan = hot
    out, report = tpc.apply_tight_pauses(plan, path, words, dur, relative_noise=True)

    assert report.status == "applied" and report.reason is None
    assert report.added_cuts > 0
    # The measure sees the take's pauses: never the "longest pause 0.0" of a deaf measure.
    assert report.longest_pause_before_s is not None and report.longest_pause_before_s > 0.35
    assert report.pauses_over_limit == 0
    assert report.longest_pause_s is not None and report.longest_pause_s <= 0.355
    assert max(_out_gaps(out, words)) <= 0.355
    for r in plan.removed:
        assert r in out.removed


def test_hot_take_cuts_never_touch_a_word(hot):  # FM 8
    path, words, dur, _, plan = hot
    out, _ = tpc.apply_tight_pauses(plan, path, words, dur, relative_noise=True)
    new = [r for r in out.removed if r not in plan.removed]
    assert new
    for r in new:
        for w in words:
            assert (
                r.end_s <= w["start_s"] - sc.PAD_S + 1e-6
                or r.start_s >= w["end_s"] + sc.PAD_S - 1e-6
            ), (r, w)
    for w in words:
        assert any(
            lo <= w["start_s"] + 1e-6 and w["end_s"] <= hi + 1e-6 for lo, hi in out.keep_segments
        )


def test_hot_take_switch_off_is_todays_noisy_report(hot):  # FM 3
    path, words, dur, _, plan = hot
    out, report = tpc.apply_tight_pauses(plan, path, words, dur)
    assert out is plan
    assert (
        report.to_variant()
        == tpc.TightPauseReport(
            "no_change", 0, 0.0, None, None, None, 0, 0, False, "noisy_take"
        ).to_variant()
    )


def test_noisy_take_stays_untouched_with_the_switch_on(noisy):  # FM 1
    path, words, dur, _, plan = noisy
    out, report = tpc.apply_tight_pauses(plan, path, words, dur, relative_noise=True)
    assert out is plan
    assert report.status == "no_change" and report.reason == "noisy_take"
    _, off = tpc.apply_tight_pauses(plan, path, words, dur)
    assert report.to_variant() == off.to_variant()


def test_long_mode_follows_the_same_gate(hot, noisy):
    path, words, dur, _, plan = hot
    _, report = tpc.apply_tight_pauses(
        plan, path, words, dur, min_pause_s=sc.MAX_PAUSE_S, relative_noise=True
    )
    assert report.reason != "noisy_take" and report.longest_pause_before_s is not None
    path, words, dur, _, plan = noisy
    _, report = tpc.apply_tight_pauses(
        plan, path, words, dur, min_pause_s=sc.MAX_PAUSE_S, relative_noise=True
    )
    assert report.reason == "noisy_take"


def test_quiet_take_is_byte_identical_with_the_switch_on(tmp_path):  # FM 2
    path, words, dur, _ = _take(tmp_path, GAPS, floor_amp=3e-4, word_amp=0.3)
    plan = consented(path, words, dur)
    on_plan, on = tpc.apply_tight_pauses(plan, path, words, dur, relative_noise=True)
    off_plan, off = tpc.apply_tight_pauses(plan, path, words, dur)
    assert on_plan == off_plan
    assert on.to_variant() == off.to_variant()
    assert tpc.measure_take_silences(path, duration_s=dur, relative_noise=True) == (
        tpc.measure_silence_spans(path, duration_s=dur)
    )


# --- the measure ------------------------------------------------------------------------


def test_relative_measure_keeps_a_flickering_pause_whole(tmp_path):  # FM 4
    rng = np.random.default_rng(5)
    flicker = np.zeros(int(0.5 * SR))
    for k in range(4):  # 20 ms bursts 6 dB over the room tone, every 0.12 s
        s = int((0.05 + 0.12 * k) * SR)
        flicker[s : s + int(0.02 * SR)] = (
            rng.standard_normal(int(0.02 * SR)) * HOT_FLOOR_AMP * np.sqrt(3.0)
        )  # with the room tone under it: twice its amplitude
    path, words, dur, samples = _take(
        tmp_path,
        [1.2, 0.6],
        floor_amp=HOT_FLOOR_AMP,
        word_amp=HOT_WORD_AMP,
        gap_inserts={0: flicker},
    )
    spans = tpc.relative_silence_spans(samples, SR, dur)
    gap = (words[0]["end_s"], words[1]["start_s"])
    inside = [s for s in spans if s[0] < gap[1] and s[1] > gap[0]]
    assert len(inside) == 1, spans
    assert inside[0][1] - inside[0][0] >= 1.1


def test_relative_measure_does_not_call_a_soft_word_silence(tmp_path):  # FM 5
    soft = _word_tone(0.25, f0=180.0, amp=HOT_FLOOR_AMP * 10 ** (16 / 20) * 1.6)
    path, words, dur, samples = _take(
        tmp_path,
        [1.4, 0.6],
        floor_amp=HOT_FLOOR_AMP,
        word_amp=HOT_WORD_AMP,
        gap_inserts={0: soft},
    )
    spans = tpc.relative_silence_spans(samples, SR, dur)
    mid = (words[0]["end_s"] + words[1]["start_s"]) / 2
    soft_span = (mid - 0.125, mid + 0.125)
    for lo, hi in spans:
        assert hi <= soft_span[0] + 0.02 or lo >= soft_span[1] - 0.02, (lo, hi, soft_span)


def test_measure_take_silences_switch_off_is_todays_measure(hot):
    path, _, dur, _, _ = hot
    assert tpc.measure_take_silences(path, duration_s=dur) == tpc.measure_silence_spans(
        path, duration_s=dur
    )


def test_measure_take_silences_falls_back_when_decode_fails(hot, monkeypatch):  # FM 6
    path, _, dur, _, _ = hot

    def boom(*_a, **_k):
        raise OSError("decode failed")

    monkeypatch.setattr(tpc.audio_pcm, "decode_pcm_f32", boom)
    assert tpc.measure_take_silences(path, duration_s=dur, relative_noise=True) == (
        tpc.measure_silence_spans(path, duration_s=dur)
    )


def test_measure_take_silences_sees_a_hot_takes_pauses(hot):
    path, words, dur, _, _ = hot
    assert tpc.measure_silence_spans(path, duration_s=dur) == []  # the incident: deaf measure
    spans = tpc.measure_take_silences(path, duration_s=dur, relative_noise=True)
    for a, b in zip(words, words[1:], strict=False):
        gap = b["start_s"] - a["end_s"]
        covered = sum(max(0.0, min(hi, b["start_s"]) - max(lo, a["end_s"])) for lo, hi in spans)
        assert covered >= gap - 0.06, (a, b, spans)
