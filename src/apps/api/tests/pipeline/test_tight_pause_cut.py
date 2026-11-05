"""Tight-pause cut: extra silence cuts on top of a consented speech-cleanup plan.

Failure modes written down BEFORE the code (repo testing policy):

1. A word is clipped: a new cut overlaps a word stamp, or leaves a word outside the keep
   segments, or sits closer than ``PAD_S`` to a word.
2. A consented removal is moved, shrunk or lost when new cuts are layered on.
3. Cuts are made outside detected silence (anything that is not silence is never removed).
4. An untranscribed voiced sound sitting in a "silent" gap is cut (voice guard bypassed).
5. A quiet unvoiced burst at a word's tail (a "t"/"s" release) is cut.
6. A one-word fragment between two cuts is left shorter than a jump-cut shot
   (``MIN_KEEP_SPEECH_SEGMENT_S``).
7. Silence detection or PCM decode fails and we cut anyway (blind cutting), or mutate the plan.
8. An already-tight take is rewritten (new plan object, churn) instead of left alone.
9. More cuts than ``MAX_REMOVALS`` (ffmpeg filter-graph length defense) are emitted.
10. Output is not deterministic between runs.
11. Kept pauses stay over the limit although cuts were possible (the creator's own check fails).
12. The new ``min_cut_s`` keyword changes the voice guard's default behaviour.
13. (v2) Word stamps stretched over a preceding pause hide that pause: the pause must still
    be cut, with the voiced part intact.
14. (v2) A soft word tail Kria's -30 dB spans call silence (about -38 dBFS) is clipped.
15. (v2) A noisy take (floor above -45 dBFS) gets cuts anyway.
16. (v2) A sliver next to a consented cut is left, so the pause stays over the limit.
17. (v2) The report measures pauses with Kria's own spans instead of the acceptance
    definition (highpass 90 Hz, -38 dB, 0.12 s).
18. (v3) Keep segments become phone-recipe clips (max 100): a capped plan plus one later
    cut must still compile; a consented plan already at the limit gets no new cuts.
19. (v3) A word's soft unvoiced ending INSIDE its own stamp is cut.
20. (v3) A successful empty pause measurement is treated as a failed one (and a failed one
    silently falls back to the -30 dB spans).
21. (review #2102) A failed pause measurement still cuts: the receipt then says "I didn't
    shorten the shorter pauses" about a video that was shortened. A failed measurement must
    leave the consented plan untouched (same object, no new cuts), in the I/O wrapper and in
    the pure function alike.
"""

from __future__ import annotations

import inspect
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.pipeline import silence_cut as sc
from app.pipeline import speech_voice_guard as svg
from app.pipeline import tight_pause_cut as tpc
from app.services import clip_speech

SR = 16_000
CYCLE = [0.05, 0.35, 0.45, 0.55, 0.9, 1.6]


def _word_tone(dur: float, f0: float = 140.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in (1, 2, 3, 4))
    sig = sig / np.max(np.abs(sig)) * amp
    fade = int(0.005 * SR)
    env = np.ones_like(sig)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    return sig * env


def _floor(n: int, rng: np.random.Generator, amp: float = 3e-4) -> np.ndarray:
    return rng.standard_normal(n) * amp  # about -70 dBFS


def build_take(
    tmp_path: Path,
    gaps: list[float],
    word_dur: float = 0.4,
    name: str = "take.wav",
    floor_amp: float = 3e-4,
) -> tuple[str, list[dict], float, np.ndarray]:
    rng = np.random.default_rng(7)
    words: list[dict] = []
    t = 0.5
    n_words = len(gaps) + 1
    pieces = [_floor(int(0.5 * SR), rng, floor_amp)]
    for i in range(n_words):
        dur = word_dur + (0.01 * (i % 3))
        words.append({"text": f"w{i}", "start_s": t, "end_s": t + dur})
        pieces.append(_word_tone(dur))
        t += dur
        if i < len(gaps):
            pieces.append(_floor(int(gaps[i] * SR), rng, floor_amp))
            t += gaps[i]
    pieces.append(_floor(int(0.8 * SR), rng, floor_amp))
    samples = np.concatenate(pieces).astype(np.float32)
    path = tmp_path / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path), words, len(samples) / SR, samples


def consented(path: str, words: list[dict], dur: float) -> sc.CutPlan:
    res = clip_speech.detect_silences_with_status(
        path, min_silence_s=tpc.SILENCE_MIN_S, ambient_adaptive=True
    )
    assert res.status == "ok"
    return sc.build_cut_plan(
        words, list(res.spans), dur, mixed_gap_enabled=True, over_budget_policy="clamp"
    )


def out_time(plan: sc.CutPlan, t: float) -> float:
    return t - sum(max(0.0, min(t, r.end_s) - r.start_s) for r in plan.removed)


def word_gaps_out(plan: sc.CutPlan, words: list[dict]) -> list[float]:
    return [
        out_time(plan, b["start_s"]) - out_time(plan, a["end_s"])
        for a, b in zip(words, words[1:], strict=False)
    ]


def _new_removals(before: sc.CutPlan, after: sc.CutPlan) -> list[sc.Removal]:
    return [r for r in after.removed if r not in before.removed]


@pytest.fixture
def cycle_take(tmp_path):
    gaps = (CYCLE * 4)[:23]
    path, words, dur, samples = build_take(tmp_path, gaps)
    return path, words, dur, samples, consented(path, words, dur)


def test_cycle_gaps_all_tight_and_report_clean(cycle_take):
    path, words, dur, _, plan = cycle_take
    out, report = tpc.apply_tight_pauses(plan, path, words, dur)
    assert report.status == "applied"
    assert report.added_cuts > 0
    assert max(word_gaps_out(out, words)) <= 0.355
    assert report.pauses_over_limit == 0
    assert report.longest_pause_s is not None and report.longest_pause_s <= 0.355
    assert report.longest_pause_before_s is not None and report.longest_pause_before_s > 0.35
    v = report.to_variant()
    assert v["version"] == 1 and v["status"] == "applied" and v["limit_s"] == 0.35


def test_words_stay_whole_and_cuts_stay_in_silence_with_pad(cycle_take):
    path, words, dur, _, plan = cycle_take
    out, _ = tpc.apply_tight_pauses(plan, path, words, dur)
    spans = clip_speech.detect_silences_with_status(
        path, min_silence_s=tpc.SILENCE_MIN_S, ambient_adaptive=True
    ).spans
    for r in _new_removals(plan, out):
        assert r.reason == sc.REASON_SILENCE
        assert any(lo - 1e-6 <= r.start_s and r.end_s <= hi + 1e-6 for lo, hi in spans)
        for w in words:
            assert (
                r.end_s <= w["start_s"] - sc.PAD_S + 1e-6
                or r.start_s >= w["end_s"] + sc.PAD_S - 1e-6
            )
    for w in words:
        assert any(
            lo <= w["start_s"] + 1e-6 and w["end_s"] <= hi + 1e-6 for lo, hi in out.keep_segments
        )


def test_consented_removals_all_still_present(cycle_take):
    path, words, dur, _, plan = cycle_take
    out, _ = tpc.apply_tight_pauses(plan, path, words, dur)
    for r in plan.removed:
        assert r in out.removed
    assert out.time_saved_s == pytest.approx(sum(r.end_s - r.start_s for r in out.removed))
    assert [r.start_s for r in out.removed] == sorted(r.start_s for r in out.removed)


def test_untranscribed_voiced_sound_in_gap_is_not_cut(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.0, 1.0, 1.0, 1.0, 1.0])
    gap_lo, gap_hi = words[1]["end_s"], words[2]["start_s"]
    mid = int((gap_lo + 0.4) * SR)
    samples = samples.copy()
    tone = _word_tone(0.3, f0=120.0, amp=0.25)
    samples[mid : mid + len(tone)] += tone
    silences = [(words[i]["end_s"], words[i + 1]["start_s"]) for i in range(len(words) - 1)]
    plan = sc.CutPlan(keep_segments=[(0.0, dur)], removed=[], time_saved_s=0.0)
    out, report = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    voiced = (mid / SR, mid / SR + 0.3)
    for r in _new_removals(plan, out):
        assert r.end_s <= voiced[0] + 1e-6 or r.start_s >= voiced[1] - 1e-6
    # the quiet check already protects the tone, so the guard has nothing left to judge
    assert gap_lo < voiced[0] and voiced[1] < gap_hi


def test_quiet_unvoiced_burst_after_word_end_survives(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.0, 1.0, 1.0, 1.0])
    rng = np.random.default_rng(3)
    samples = samples.copy()
    s0 = int(words[1]["end_s"] * SR)
    burst = rng.standard_normal(int(0.08 * SR)) * 0.0178  # about -35 dBFS
    samples[s0 : s0 + len(burst)] += burst.astype(np.float32)
    silences = [(words[i]["end_s"], words[i + 1]["start_s"]) for i in range(len(words) - 1)]
    plan = sc.CutPlan(keep_segments=[(0.0, dur)], removed=[], time_saved_s=0.0)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    lo, hi = words[1]["end_s"], words[1]["end_s"] + 0.08
    assert _new_removals(plan, out)
    for r in out.removed:
        assert r.end_s <= lo + 1e-6 or r.start_s >= hi - 1e-6


def test_single_short_word_between_two_pauses_keeps_a_real_fragment(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.5, 0.5, 0.5, 0.5], word_dur=0.25)
    silences = [(words[i]["end_s"], words[i + 1]["start_s"]) for i in range(len(words) - 1)]
    plan = sc.CutPlan(keep_segments=[(0.0, dur)], removed=[], time_saved_s=0.0)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    new = _new_removals(plan, out)
    for lo, hi in out.keep_segments:
        has_word = any(lo - 1e-6 <= w["start_s"] and w["end_s"] <= hi + 1e-6 for w in words)
        touches_new = any(abs(r.end_s - lo) < 1e-6 or abs(r.start_s - hi) < 1e-6 for r in new)
        if has_word and touches_new:
            assert hi - lo >= sc.MIN_KEEP_SPEECH_SEGMENT_S - 1e-6


def test_silence_detect_failure_leaves_plan_unchanged(cycle_take):
    path, words, dur, _, plan = cycle_take
    bad = lambda *a, **k: SimpleNamespace(spans=(), status="ffmpeg_failed")  # noqa: E731
    out, report = tpc.apply_tight_pauses(plan, path, words, dur, silence_detect=bad)
    assert out is plan
    assert report.status == "unavailable" and report.reason == "silence_detect_failed"
    assert report.to_variant()["reason"] == "silence_detect_failed"


def test_pcm_decode_failure_leaves_plan_unchanged(cycle_take):
    path, words, dur, _, plan = cycle_take

    def boom(_p):
        raise OSError("no ffmpeg")

    out, report = tpc.apply_tight_pauses(plan, path, words, dur, pcm_loader=boom)
    assert out is plan
    assert report.status == "unavailable" and report.reason == "audio_unavailable"

    out2, report2 = tpc.apply_tight_pauses(
        plan, path, words, dur, pcm_loader=lambda _p: np.zeros(10, dtype=np.float32)
    )
    assert out2 is plan and report2.reason == "audio_unavailable"


def test_already_tight_take_returns_same_plan(tmp_path):
    path, words, dur, _ = build_take(tmp_path, [0.1, 0.15, 0.2, 0.12] * 6)
    plan = consented(path, words, dur)
    out, report = tpc.apply_tight_pauses(plan, path, words, dur)
    assert out is plan
    assert report.status == "no_change" and report.added_cuts == 0
    assert report.pauses_over_limit == 0


def test_cap_keeps_total_removals_within_max(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.5] * 125)
    silences = [(words[i]["end_s"], words[i + 1]["start_s"]) for i in range(len(words) - 1)]
    plan = sc.CutPlan(keep_segments=[(0.0, dur)], removed=[], time_saved_s=0.0)
    out, report = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert len(out.removed) <= sc.MAX_REMOVALS
    assert len(out.keep_segments) <= tpc.TIGHT_MAX_KEEP_SEGMENTS
    assert report.capped is True


def test_deterministic(cycle_take):
    path, words, dur, _, plan = cycle_take
    a, ra = tpc.apply_tight_pauses(plan, path, words, dur)
    b, rb = tpc.apply_tight_pauses(plan, path, words, dur)
    assert a.removed == b.removed and a.keep_segments == b.keep_segments and ra == rb


def test_kept_pause_stats_merges_pause_split_by_a_cut():
    words = [
        {"text": "a", "start_s": 0.0, "end_s": 0.4},
        {"text": "b", "start_s": 2.0, "end_s": 2.4},
    ]
    sil = [(0.4, 2.0)]
    plan = sc.CutPlan(keep_segments=[(0.0, 2.4)], removed=[], time_saved_s=0.0)
    assert tpc.kept_pause_stats(plan, words, sil, 2.4) == (pytest.approx(1.6), 1)
    cut = replace(
        plan,
        removed=[sc.Removal(0.8, 1.6, sc.REASON_SILENCE)],
        keep_segments=[(0.0, 0.8), (1.6, 2.4)],
    )
    longest, over = tpc.kept_pause_stats(cut, words, sil, 2.4)
    assert longest == pytest.approx(0.8) and over == 1


def test_guard_default_min_cut_unchanged(tmp_path):
    sig = inspect.signature(svg.guard_audible_speech)
    assert sig.parameters["min_cut_s"].default == svg.MIN_CUT_S
    assert sig.parameters["min_cut_s"].kind is inspect.Parameter.KEYWORD_ONLY
    rng = np.random.default_rng(1)
    samples = np.concatenate([_floor(SR, rng), _word_tone(0.3), _floor(int(0.9 * SR), rng)]).astype(
        np.float32
    )
    cut = sc.Removal(0.0, 2.2, sc.REASON_SILENCE)
    plan = sc.CutPlan(keep_segments=[], removed=[cut], time_saved_s=2.2)
    a = svg.guard_audible_speech(plan, samples)
    b = svg.guard_audible_speech(plan, samples, min_cut_s=svg.MIN_CUT_S)
    assert a[0].removed == b[0].removed and a[1] == b[1]
    c = svg.guard_audible_speech(plan, samples, min_cut_s=5.0)
    assert c[0].removed == []


def _gap_silences(words):
    return [(words[i]["end_s"], words[i + 1]["start_s"]) for i in range(len(words) - 1)]


def _whole_plan(dur):
    return sc.CutPlan(keep_segments=[(0.0, dur)], removed=[], time_saved_s=0.0)


def test_v2_stretched_stamp_pause_is_cut_and_voice_intact(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.3, 1.2, 0.3, 0.3])
    silences = _gap_silences(words)
    true_start = words[2]["start_s"]
    words[2] = {**words[2], "start_s": true_start - 0.8}
    plan = _whole_plan(dur)
    out, report = tpc.tighten_pauses(
        plan, words, silences, dur, samples=samples, measure_silences=silences
    )
    new = _new_removals(plan, out)
    assert new and report.status == "applied"
    for r in new:
        assert r.end_s <= true_start - sc.PAD_S + 1e-6 or r.start_s >= words[2]["end_s"]
    assert report.longest_pause_s <= 0.355 and report.pauses_over_limit == 0


def test_v2_soft_tail_and_pad_after_it_are_kept(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.0, 1.0, 1.0])
    rng = np.random.default_rng(5)
    samples = samples.copy()
    s0 = int(words[1]["end_s"] * SR)
    tail = rng.standard_normal(int(0.15 * SR)) * 0.0126  # about -38 dBFS
    samples[s0 : s0 + len(tail)] += tail.astype(np.float32)
    silences = _gap_silences(words)
    plan = _whole_plan(dur)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    lo, hi = words[1]["end_s"], words[1]["end_s"] + 0.15 + sc.PAD_S
    assert _new_removals(plan, out)
    for r in out.removed:
        assert r.end_s <= lo + 1e-6 or r.start_s >= hi - 0.01


def test_v2_breath_at_floor_plus_12_db_does_not_block_the_cut(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.6, 0.3, 0.3])
    rng = np.random.default_rng(6)
    samples = samples.copy()
    mid = int((words[0]["end_s"] + 0.7) * SR)
    breath = rng.standard_normal(int(0.2 * SR)) * 3e-4 * 10 ** (12 / 20)
    samples[mid : mid + len(breath)] += breath.astype(np.float32)
    silences = _gap_silences(words)
    out, report = tpc.tighten_pauses(
        _whole_plan(dur), words, silences, dur, samples=samples, measure_silences=silences
    )
    assert report.status == "applied"
    assert report.longest_pause_s <= 0.355


def test_v2_sliver_next_to_consented_cut_is_extended(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.3, 1.2, 0.3, 0.3, 0.3])
    silences = _gap_silences(words)
    r0 = words[1]["end_s"] + sc.PAD_S
    r1 = words[2]["start_s"] - 0.2
    consented_cut = sc.Removal(r0, r1, sc.REASON_SILENCE)
    plan = sc.CutPlan(
        keep_segments=[(0.0, r0), (r1, dur)], removed=[consented_cut], time_saved_s=r1 - r0
    )
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    sliver = [r for r in _new_removals(plan, out) if abs(r.start_s - r1) < 1e-6]
    assert sliver and sliver[0].end_s - sliver[0].start_s == pytest.approx(0.08, abs=0.015)


def test_v2_noisy_take_gets_no_cuts(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.0, 1.0, 1.0], floor_amp=0.0178)
    silences = _gap_silences(words)
    plan = _whole_plan(dur)
    out, report = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert out is plan
    assert report.status == "no_change" and report.reason == "noisy_take"
    assert report.longest_pause_s is None and report.pauses_over_limit is None


def test_v2_measure_uses_acceptance_definition(tmp_path):
    rng = np.random.default_rng(9)
    parts = [_floor(SR, rng), _word_tone(0.4), rng.standard_normal(SR) * 0.0015]  # about -56 dBFS
    parts += [_word_tone(0.4), rng.standard_normal(SR) * 0.02, _word_tone(0.4), _floor(SR, rng)]
    samples = np.concatenate(parts).astype(
        np.float32
    )  # second gap is -34 dBFS, which ffmpeg does not call silent at -38 dB
    path = tmp_path / "m.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    spans = tpc.measure_silence_spans(str(path))
    assert spans is not None
    assert any(lo < 1.5 and hi > 2.3 for lo, hi in spans)  # quiet breath gap is a pause
    assert not any(lo < 3.0 and hi > 3.6 for lo, hi in spans)  # the -34 dBFS gap is not


def test_v2_word_free_short_shot_between_two_cuts_is_given_back(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.3, 2.0, 0.3, 0.3])
    samples = samples.copy()
    mid = int((words[1]["end_s"] + 0.9) * SR)
    blip = _word_tone(0.2, f0=120.0, amp=0.25)
    samples[mid : mid + len(blip)] += blip
    silences = _gap_silences(words)
    plan = _whole_plan(dur)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    new = _new_removals(plan, out)
    assert new
    for lo, hi in out.keep_segments:
        touches = any(abs(r.end_s - lo) < 1e-6 or abs(r.start_s - hi) < 1e-6 for r in new)
        if touches:
            assert hi - lo >= sc.MIN_KEEP_SPEECH_SEGMENT_S - 1e-6


def test_v3_capped_plan_still_compiles_with_a_later_cut(tmp_path):
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services.phone_sources import PhoneSourceBinding

    gaps = ([0.45, 0.5, 0.4, 0.9, 0.45, 0.55] * 30)[:170]
    path, words, dur, _ = build_take(tmp_path, gaps)
    plan = consented(path, words, dur)
    out, report = tpc.apply_tight_pauses(plan, path, words, dur)
    assert len(out.keep_segments) <= tpc.TIGHT_MAX_KEEP_SEGMENTS
    longest = max(out.keep_segments, key=lambda s: s[1] - s[0])
    extra = sc.Removal(longest[0] + 1.0, longest[0] + 1.3, "opening_cut")
    removed = sorted([*out.removed, extra], key=lambda r: r.start_s)
    later = replace(out, removed=removed, keep_segments=tpc._complement(removed, dur))
    binding = PhoneSourceBinding(
        media_id="clip-0",
        proxy_path="user/analysis-proxy-clip-0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=dur,
            width=1080,
            height=1920,
            orientation_degrees=0,
            has_audio=True,
        ),
    )
    recipe = compile_phone_subtitled_plan((binding,), caption_cues=[], cut_plan=later)
    video = next(t for t in recipe.tracks if t.kind == "video")
    assert len(video.clips) == len(later.keep_segments) <= 100


def test_v3_consented_plan_at_segment_limit_gets_no_new_cuts(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.5] * 125)
    silences = _gap_silences(words)
    removed = [sc.Removal(a + sc.PAD_S, b - sc.PAD_S, sc.REASON_SILENCE) for a, b in silences[:90]]
    plan = sc.CutPlan(
        keep_segments=tpc._complement(removed, dur),
        removed=removed,
        time_saved_s=sum(r.end_s - r.start_s for r in removed),
    )
    assert len(plan.keep_segments) >= tpc.TIGHT_MAX_KEEP_SEGMENTS
    out, report = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert out is plan and report.capped is True


def test_v3_soft_unvoiced_ending_inside_the_stamp_is_kept(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.0, 1.0, 1.0])
    rng = np.random.default_rng(11)
    samples = samples.copy()
    true_end = words[1]["end_s"]
    s0 = int(true_end * SR)
    tail = rng.standard_normal(int(0.3 * SR)) * 3e-4 * 10 ** (12 / 20)
    samples[s0 : s0 + len(tail)] += tail.astype(np.float32)
    silences = _gap_silences(words)  # Kria calls the soft tail silence
    words[1] = {**words[1], "end_s": true_end + 0.3}  # the stamp covers the tail
    plan = _whole_plan(dur)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert _new_removals(plan, out)
    for r in out.removed:
        assert r.end_s <= true_end + 1e-6 or r.start_s >= true_end + 0.3 + sc.PAD_S - 0.01


def test_v3_empty_measurement_is_a_measurement_and_failed_one_is_unmeasured(
    cycle_take, monkeypatch
):
    path, words, dur, _, plan = cycle_take
    monkeypatch.setattr(tpc, "measure_silence_spans", lambda *a, **k: [])
    out, report = tpc.apply_tight_pauses(plan, path, words, dur)
    assert report.status == "applied"
    assert report.longest_pause_s == 0.0 and report.pauses_over_limit == 0
    assert _new_removals(plan, out)
    monkeypatch.setattr(tpc, "measure_silence_spans", lambda *a, **k: None)
    out2, report2 = tpc.apply_tight_pauses(plan, path, words, dur)
    # Review #2102: no measurement, no tight cuts (the receipt says nothing was shortened).
    assert out2 is plan
    assert report2.status == "unavailable" and report2.reason == "unmeasured"
    assert report2.added_cuts == 0 and report2.added_removed_s == 0.0
    assert report2.longest_pause_s is None and report2.longest_pause_before_s is None
    assert report2.pauses_over_limit is None


def test_review_2102_pure_cutter_without_measurement_adds_no_cuts(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [1.0, 1.0, 1.0])
    silences = _gap_silences(words)
    plan = _whole_plan(dur)
    measured, _ = tpc.tighten_pauses(
        plan, words, silences, dur, samples=samples, measure_silences=silences
    )
    assert _new_removals(plan, measured)  # the same take is cut when it can be measured
    out, report = tpc.tighten_pauses(plan, words, silences, dur, samples=samples, measurable=False)
    assert out is plan
    assert (report.status, report.reason, report.added_cuts) == ("unavailable", "unmeasured", 0)


def _noise(samples, lo_s, hi_s, rel_db, seed):
    rng = np.random.default_rng(seed)
    out = samples.copy()
    a, b = int(lo_s * SR), int(hi_s * SR)
    out[a:b] += (rng.standard_normal(b - a) * 3e-4 * 10 ** (rel_db / 20)).astype(np.float32)
    return out


def test_v4_stretched_stamp_far_from_core_is_cut_despite_soft_noise(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.3, 1.5, 0.3, 0.3])
    silences = _gap_silences(words)
    ts = words[2]["start_s"]
    words[2] = {**words[2], "start_s": ts - 0.8}
    samples = _noise(samples, ts - 0.8, ts - 0.4, 12, 21)
    plan = _whole_plan(dur)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert any(r.start_s <= ts - 0.7 and r.end_s >= ts - 0.45 for r in _new_removals(plan, out))


def test_v4_soft_onset_just_before_the_core_is_kept(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.3, 1.5, 0.3, 0.3])
    silences = _gap_silences(words)
    ts = words[2]["start_s"]
    words[2] = {**words[2], "start_s": ts - 0.3}
    samples = _noise(samples, ts - 0.15, ts, 12, 22)
    plan = _whole_plan(dur)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert _new_removals(plan, out)
    for r in out.removed:
        assert r.end_s <= ts - 0.15 - sc.PAD_S + 0.01 or r.start_s >= ts - 1e-6


def test_v5_room_tone_flicker_in_onset_zone_does_not_block_the_cut(tmp_path):
    path, words, dur, samples = build_take(tmp_path, [0.3, 1.5, 0.3, 0.3])
    silences = _gap_silences(words)
    ts = words[2]["start_s"]
    words[2] = {**words[2], "start_s": ts - 0.3}
    rng = np.random.default_rng(31)
    samples = samples.copy()
    frame = int(0.02 * SR)
    for i in range(int(0.3 / 0.02)):
        db = rng.choice([6.0, 7.0, 8.0])
        a = int((ts - 0.3) * SR) + i * frame
        samples[a : a + frame] += (rng.standard_normal(frame) * 3e-4 * 10 ** (db / 20)).astype(
            np.float32
        )
    plan = _whole_plan(dur)
    out, _ = tpc.tighten_pauses(plan, words, silences, dur, samples=samples)
    assert any(r.start_s <= ts - 0.2 and r.end_s >= ts - 0.13 for r in _new_removals(plan, out))
