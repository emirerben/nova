"""Tight pauses on a gated take: Qendresa Q2 run 2 replay ("Cut all my pauses", job 1d512b9c).

Prod left 3 pauses over 0.35 s (longest 0.55 s). The take has digital silence between lines
(a gate / TTS joins), so the 5th-percentile quiet floor is -120 dBFS and was clamped to -70; the
"sound" bars (+15 dB, +10 dB in a stamp) then sat at -55 / -60 dBFS, and faint unvoiced breaths
and clicks (-46..-57 dBFS, which the acceptance tool hears as pause) protected the pause around
them with ``PAD_S`` on both sides. Three pauses (source 15.89 s, 26.58 s, 29.48 s) kept them.

Failure modes written down BEFORE the fix:

1. A pause stays over 0.35 s although only faint, unvoiced, word-detached sound sits in it
   (the Q2 run 2 bug): the creator's own check fails.
2. A word, or a word's audible tail, is cut: a new cut overlaps sound the acceptance tool calls
   speech (>= -38 dBFS), or a word the consented plan kept is no longer kept.
3. A voiced sound (an untranscribed soft word) in a pause is cut.
4. A soft word tail that runs straight on from the word (no gate dip) is cut because it is
   below -38 dBFS on a gated take. (Sound under room tone + 15 dB is not "sound" on any take,
   gated or not; the gated take now measures that bar from its room tone, not from -70.)
5. A word's release or soft ending AFTER its loud part is cut, or loses its ``PAD_S``, on a
   gated take because a gate dip separates it from the vowel: inside the stamp, or outside
   it (the Q2 take's "generic." /k/ release at 21.00-21.05 s sits after the stamp's end).
6. An ungated take's floor moves, or faint sound just before a word on an ungated take (no
   gate dip: a soft onset) loses its protection. (Faint unvoiced sound more than 0.2 s from
   any word sound is pause on every take now, left to the voice guard.)
7. The receipt says every pause is tight while the rendered audio still has a long one
   (measure the joined keep segments with the acceptance definition, not the receipt).
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import audio_pcm
from app.pipeline import silence_cut as sc
from app.pipeline import speech_voice_guard as svg
from app.pipeline import tight_pause_cut as tpc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SR = svg.SAMPLE_RATE
SPEECH_DBFS = -38.0  # the acceptance tool's silence bar (score.py: silencedetect n=-38dB)


def _write_wav(path: Path, samples: np.ndarray) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path)


@pytest.fixture(scope="module")
def q2_run2(tmp_path_factory):
    meta = json.loads((FIXTURES / "kri1288_q2_run2_tight_pauses.json").read_text())
    out = tmp_path_factory.mktemp("q2run2") / "take.mp4"
    # The phone path reads the uploaded mp4; mux the bit-exact AAC stream under a tiny video.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:r=1"]
        + ["-i", str(FIXTURES / "kri1288_q2_run2_take.m4a"), "-map", "0:v", "-map", "1:a"]
        + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", "-shortest", str(out)],
        check=True,
        timeout=60,
    )
    dur = float(meta["duration_s"])
    removed = [sc.Removal(r["start_s"], r["end_s"], r["reason"]) for r in meta["consented"]]
    plan = sc.CutPlan(
        keep_segments=tpc._complement(removed, dur),
        removed=removed,
        time_saved_s=sum(r.end_s - r.start_s for r in removed),
    )
    new_plan, report = tpc.apply_tight_pauses(plan, str(out), meta["words"], dur)
    return str(out), meta, plan, new_plan, report


def _levels(samples: np.ndarray, lo_s: float, hi_s: float) -> np.ndarray:
    win, hop = int(SR * 0.02), int(SR * 0.01)
    x = samples[int(lo_s * SR) : int(hi_s * SR)].astype(np.float64)
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    starts = np.arange(0, len(x) - win + 1, hop)
    return np.array([10 * np.log10(np.mean(x[i : i + win] ** 2) + 1e-12) for i in starts])


def _joined_keep_audio(media: str, keep: list[tuple[float, float]], out: Path) -> str:
    chains = [
        f"[0:a]atrim=start={lo:.4f}:end={hi:.4f},asetpts=PTS-STARTPTS[a{i}]"
        for i, (lo, hi) in enumerate(keep)
    ]
    concat = "".join(f"[a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=0:a=1[o]"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", media, "-filter_complex"]
        + [";".join([*chains, concat]), "-map", "[o]", "-c:a", "pcm_s16le", str(out)],
        check=True,
        timeout=60,
    )
    return str(out)


def test_q2_run2_receipt_has_no_pause_over_the_limit(q2_run2):
    _, _, _, _, report = q2_run2
    assert report.status == "applied"
    # prod: 7 cuts, longest 0.55 s, 3 over 0.35 s
    assert report.pauses_over_limit == 0, report.to_variant()
    assert report.longest_pause_s <= tpc.TIGHT_PAUSE_LIMIT_S + 0.005
    assert report.guard_dropped == 0


def test_q2_run2_joined_audio_has_no_pause_over_the_limit(q2_run2, tmp_path):
    media, meta, _, new_plan, _ = q2_run2
    joined = _joined_keep_audio(media, new_plan.keep_segments, tmp_path / "out.wav")
    spans = tpc.measure_silence_spans(joined)
    assert spans is not None

    def out_t(t: float) -> float:
        return t - sum(max(0.0, min(t, r.end_s) - r.start_s) for r in new_plan.removed)

    kept = [
        w
        for w in meta["words"]
        if tpc._word_kept((w["start_s"], w["end_s"]), new_plan.keep_segments)
    ]
    first, last = out_t(kept[0]["start_s"]), out_t(kept[-1]["end_s"])
    # score.py: silences that start inside the talk (first + 0.2 .. last - 0.2)
    inner = [hi - lo for lo, hi in spans if first + 0.2 < lo < last - 0.2]
    assert inner and max(inner) <= tpc.TIGHT_PAUSE_LIMIT_S + 0.005, sorted(inner)[-4:]


def test_q2_run2_no_word_or_word_sound_is_cut(q2_run2):
    media, meta, plan, new_plan, _ = q2_run2
    samples = audio_pcm.decode_pcm_f32(media)
    new = [r for r in new_plan.removed if r not in plan.removed]
    assert new
    for r in new:
        # nothing the acceptance tool hears as sound, nor anything within 0.08 s of it
        assert _levels(samples, r.start_s - 0.08, r.end_s + 0.08).max() < SPEECH_DBFS, r
        assert not svg.voiced_runs(
            samples, r.start_s, r.end_s, floor_db=tpc.TIGHT_MIN_FLOOR_DBFS
        ), r
    # A word's tail / release (sound over room tone + 15 dB within 0.2 s after the word's
    # loud part, gate dip or not) keeps PAD_S: "generic." ends 20.94 s, /k/ release 21.00-21.05 s.
    win, hop = int(SR * 0.02), int(SR * 0.01)
    starts = np.arange(0, len(samples) - win + 1, hop)
    csum = np.concatenate([[0.0], np.cumsum(samples.astype(np.float64) ** 2)])
    level = 10 * np.log10((csum[starts + win] - csum[starts]) / win + 1e-12)
    t = starts / SR
    floor = max(tpc._room_floor_db(samples, SR), tpc.TIGHT_MIN_FLOOR_DBFS)
    loud_t = t[level >= SPEECH_DBFS]
    tails = [
        (ti, ti + 0.02)
        for ti, lv in zip(t, level, strict=True)
        if floor + 15 < lv < SPEECH_DBFS and len(loud_t[(loud_t < ti) & (loud_t >= ti - 0.2)])
    ]
    assert any(20.99 <= a <= 21.04 for a, _ in tails)  # the /k/ release is among them
    for r in new:
        for a, b in tails:
            assert r.end_s <= a - sc.PAD_S + 0.01 or r.start_s >= b + sc.PAD_S - 0.01, (r, a)
    for w in meta["words"]:
        span = (w["start_s"], w["end_s"])
        if tpc._word_kept(span, plan.keep_segments):
            assert tpc._word_kept(span, new_plan.keep_segments), w
    # every consented removal is still there, unchanged
    assert all(r in new_plan.removed for r in plan.removed)


def _gated_take(tmp_path: Path, extra) -> tuple[str, list[dict], float, np.ndarray]:
    """w0 | 1.0 s pause | w1 | 1.0 s pause | w2, room tone -63 dBFS, gate-closed (exact 0)
    stretches inside the pauses like the Q2 take, ``extra(samples, words)`` adds sounds."""

    rng = np.random.default_rng(41)
    tone = lambda d: np.sin(2 * np.pi * 150 * np.arange(int(d * SR)) / SR) * 0.3  # noqa: E731
    room = lambda d: rng.standard_normal(int(d * SR)) * 10 ** (-63 / 20)  # noqa: E731
    parts, words, t = [room(0.5)], [], 0.5
    for i in range(3):
        words.append({"text": f"w{i}", "start_s": t, "end_s": t + 0.5})
        parts.append(tone(0.5))
        t += 0.5
        if i < 2:
            parts.append(np.concatenate([room(0.1), np.zeros(int(0.8 * SR)), room(0.1)]))
            t += 1.0
    parts.append(room(0.5))
    samples = np.concatenate(parts).astype(np.float32)
    samples = extra(samples, words)
    return _write_wav(tmp_path / "g.wav", samples), words, len(samples) / SR, samples


def _noise_at(samples: np.ndarray, lo_s: float, hi_s: float, dbfs: float, seed: int):
    rng = np.random.default_rng(seed)
    out = samples.copy()
    a, b = int(lo_s * SR), int(hi_s * SR)
    out[a:b] = (rng.standard_normal(b - a) * 10 ** (dbfs / 20)).astype(np.float32)
    return out


def _run(words, dur, samples):
    silences = [(words[i]["end_s"], words[i + 1]["start_s"]) for i in range(len(words) - 1)]
    plan = sc.CutPlan(keep_segments=[(0.0, dur)], removed=[], time_saved_s=0.0)
    out, report = tpc.tighten_pauses(
        plan, words, silences, dur, samples=samples, measure_silences=silences
    )
    return plan, out, report


def test_gated_take_detached_breath_in_a_pause_is_cut(tmp_path):
    # faint unvoiced breath in the middle of the gate-closed stretch, -48 dBFS
    def extra(s, words):
        mid = words[0]["end_s"] + 0.45
        return _noise_at(s, mid, mid + 0.12, -48.0, 5)

    _, words, dur, samples = _gated_take(tmp_path, extra)
    plan, out, report = _run(words, dur, samples)
    lo = words[0]["end_s"] + 0.45
    assert any(r.start_s <= lo and r.end_s >= lo + 0.12 for r in out.removed), out.removed


def test_gated_take_soft_tail_running_on_from_the_word_is_kept(tmp_path):
    # -45 dBFS tail straight after w0 (no dip): sound by the +15 dB rule, under the -38 dBFS
    # acceptance bar, and part of the word: never cut
    def extra(s, words):
        return _noise_at(s, words[0]["end_s"], words[0]["end_s"] + 0.15, -45.0, 6)

    _, words, dur, samples = _gated_take(tmp_path, extra)
    plan, out, _ = _run(words, dur, samples)
    keep_to = words[0]["end_s"] + 0.15 + sc.PAD_S - 0.01
    assert out.removed
    for r in out.removed:
        assert r.end_s <= words[0]["end_s"] or r.start_s >= keep_to, r


def test_gated_take_soft_sound_in_the_stamp_after_its_core_is_kept(tmp_path):
    # a release after a gate dip, inside w0's own stamp: a word's tail, never cut
    def extra(s, words):
        lo = words[0]["end_s"] + 0.12
        return _noise_at(s, lo, lo + 0.04, -50.0, 7)

    _, words, dur, samples = _gated_take(tmp_path, extra)
    release = (words[0]["end_s"] + 0.12, words[0]["end_s"] + 0.16)
    words[0] = {**words[0], "end_s": release[1]}
    plan, out, _ = _run(words, dur, samples)
    for r in out.removed:
        assert r.end_s <= release[0] or r.start_s >= release[1], r


def test_gated_take_release_after_a_dip_outside_the_stamp_keeps_its_pad(tmp_path):
    # w0's stamp ends at the vowel; a -45 dBFS release follows a 60 ms gate dip
    def extra(s, words):
        lo = words[0]["end_s"] + 0.06
        out = s.copy()
        out[int(words[0]["end_s"] * SR) : int(lo * SR)] = 0.0
        return _noise_at(out, lo, lo + 0.05, -45.0, 8)

    _, words, dur, samples = _gated_take(tmp_path, extra)
    release_end = words[0]["end_s"] + 0.11
    plan, out, _ = _run(words, dur, samples)
    assert out.removed
    for r in out.removed:
        assert r.end_s <= words[0]["end_s"] or r.start_s >= release_end + sc.PAD_S - 0.01, r


def test_gated_take_voiced_faint_sound_in_a_pause_is_not_cut(tmp_path):
    def extra(s, words):
        lo = words[0]["end_s"] + 0.4
        a = int(lo * SR)
        t = np.arange(int(0.2 * SR)) / SR
        out = s.copy()
        out[a : a + len(t)] = (np.sin(2 * np.pi * 130 * t) * 10 ** (-45 / 20)).astype(np.float32)
        return out

    _, words, dur, samples = _gated_take(tmp_path, extra)
    plan, out, _ = _run(words, dur, samples)
    lo = words[0]["end_s"] + 0.4
    for r in out.removed:
        assert r.end_s <= lo + 1e-6 or r.start_s >= lo + 0.2 - 1e-6, r


def test_ungated_take_faint_onset_just_before_a_word_is_kept(tmp_path):
    # room tone all through (no gate), a -45 dBFS soft onset 0.1 s before w1's vowel
    def extra(s, words):
        rng = np.random.default_rng(9)
        out = s.copy()
        gap = out == 0.0
        out[gap] = (rng.standard_normal(int(gap.sum())) * 10 ** (-63 / 20)).astype(np.float32)
        lo = words[1]["start_s"] - 0.1
        return _noise_at(out, lo, lo + 0.06, -45.0, 10)

    _, words, dur, samples = _gated_take(tmp_path, extra)
    onset = (words[1]["start_s"] - 0.1, words[1]["start_s"] - 0.04)
    plan, out, _ = _run(words, dur, samples)
    assert out.removed
    for r in out.removed:
        assert r.end_s <= onset[0] - sc.PAD_S + 0.01 or r.start_s >= words[1]["end_s"], r


def test_ungated_take_floor_is_unchanged():
    rng = np.random.default_rng(3)
    samples = np.concatenate(
        [rng.standard_normal(SR) * 3e-4, np.sin(np.arange(SR) * 0.06) * 0.3]
    ).astype(np.float32)
    assert tpc._room_floor_db(samples, SR) == svg.quiet_floor_db(samples, SR)
