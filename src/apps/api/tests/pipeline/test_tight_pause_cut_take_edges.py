"""KRI-720 G1: the tight pass also trims the silence where a take meets its neighbours.

Failure modes written down BEFORE the code (docs/reviews/kri-720-multi-take-cleanup.md T2/T3/T4):

1. The join trim cuts a word's soft ending (the "t"/"s" release at the end of the last word) or its
   quiet onset: the edge cuts must pass the same protections as every other tight cut.
2. The join keeps more than the tight pass's per-side pad, so two takes meeting still leave a
   pause on the 0.3 s limit (0.15 + 0.15 in the old trim).
3. The edge flags leak: without them the plan is byte-identical to before (one take, flag off).
4. A noisy take is cut at its edges anyway (soft speech hides in the noise).
5. A head-only / tail-only request trims the other edge too (the video's own first take must keep
   its opening, its last take its ending).
6. The clip budget is ignored: every kept segment is a clip on the one main track.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import silence_cut as sc
from app.pipeline import tight_pause_cut as tpc
from tests.pipeline.test_tight_pause_cut import (
    CYCLE,
    SR,
    _floor,
    _word_tone,
    build_take,
    consented,
)


def _take(tmp_path: Path, parts: list[tuple], name: str = "edge.wav", floor_amp: float = 3e-4):
    """parts: ("floor", s) | ("word", s) | ("soft", s, amp). Returns path, words, dur."""
    rng = np.random.default_rng(11)
    pieces: list[np.ndarray] = []
    words: list[dict] = []
    t = 0.0
    for part in parts:
        kind, dur = part[0], part[1]
        if kind == "word":
            words.append({"text": f"w{len(words)}", "start_s": t, "end_s": t + dur})
            pieces.append(_word_tone(dur))
        elif kind == "soft":
            pieces.append(rng.standard_normal(int(dur * SR)) * part[2])
        else:
            pieces.append(_floor(int(dur * SR), rng, floor_amp))
        t += dur
    samples = np.concatenate(pieces).astype(np.float32)
    path = tmp_path / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path), words, len(samples) / SR


EDGE_PARTS = [("floor", 1.2), ("word", 0.4), ("floor", 0.45), ("word", 0.4), ("floor", 1.4)]


def _run(tmp_path, parts=None, **kwargs):
    path, words, dur = _take(tmp_path, parts or EDGE_PARTS)
    plan = consented(path, words, dur)
    out, report = tpc.apply_tight_pauses(plan, path, words, dur, **kwargs)
    return plan, out, report, words, dur


def test_a_join_edge_keeps_only_the_pad_beside_the_word(tmp_path):
    _plan, out, report, words, dur = _run(tmp_path, trim_head=True, trim_tail=True)
    assert report.status == "applied"
    lead = words[0]["start_s"] - out.keep_segments[0][0]
    tail = out.keep_segments[-1][1] - words[-1]["end_s"]
    assert lead == pytest.approx(sc.PAD_S, abs=0.03)
    assert tail == pytest.approx(sc.PAD_S, abs=0.03)
    # Both sides of a join together stay under the 0.3 s the format allows.
    assert lead + tail <= 0.3
    # Every word is still fully inside the kept audio.
    for w in words:
        assert any(a <= w["start_s"] and w["end_s"] <= b for a, b in out.keep_segments)


def test_a_soft_word_ending_at_the_join_survives(tmp_path):
    parts = [
        ("floor", 1.2),
        ("word", 0.4),
        ("floor", 0.45),
        ("word", 0.4),
        ("soft", 0.15, 0.004),  # a quiet release, about -48 dBFS, 20 dB over the room
        ("floor", 1.4),
    ]
    _plan, out, report, words, _dur = _run(tmp_path, parts, trim_head=True, trim_tail=True)
    assert report.status == "applied"
    release_end = words[-1]["end_s"] + 0.15
    assert out.keep_segments[-1][1] >= release_end - 0.005


def test_edge_flags_off_is_byte_identical(tmp_path):
    plan, out, _report, _words, _dur = _run(tmp_path)
    path, words, dur = _take(tmp_path, EDGE_PARTS, "again.wav")
    again, _ = tpc.apply_tight_pauses(
        consented(path, words, dur), path, words, dur, trim_head=False, trim_tail=False
    )
    assert again.removed == out.removed and again.keep_segments == out.keep_segments
    # The video's own opening and ending are not a join: nothing before the first word goes.
    assert out.keep_segments[0][0] == plan.keep_segments[0][0]
    assert out.keep_segments[-1][1] == plan.keep_segments[-1][1]


def test_head_only_leaves_the_tail_alone(tmp_path):
    plan, out, _report, words, _dur = _run(tmp_path, trim_head=True)
    assert words[0]["start_s"] - out.keep_segments[0][0] == pytest.approx(sc.PAD_S, abs=0.03)
    assert out.keep_segments[-1][1] == plan.keep_segments[-1][1]


def test_tail_only_leaves_the_head_alone(tmp_path):
    plan, out, _report, words, _dur = _run(tmp_path, trim_tail=True)
    assert out.keep_segments[0][0] == plan.keep_segments[0][0]
    assert out.keep_segments[-1][1] - words[-1]["end_s"] == pytest.approx(sc.PAD_S, abs=0.03)


def test_a_noisy_take_is_not_cut_at_its_edges(tmp_path):
    path, words, dur = _take(tmp_path, EDGE_PARTS, "noisy.wav", floor_amp=0.03)
    plan = consented(path, words, dur)
    out, report = tpc.apply_tight_pauses(plan, path, words, dur, trim_head=True, trim_tail=True)
    assert report.reason == "noisy_take"
    assert out is plan


def test_the_clip_budget_caps_the_kept_segments(tmp_path):
    gaps = (CYCLE * 4)[:23]
    path, words, dur, _ = build_take(tmp_path, gaps)
    plan = consented(path, words, dur)
    free, free_report = tpc.apply_tight_pauses(plan, path, words, dur)
    room = len(plan.keep_segments) + 2
    capped, report = tpc.apply_tight_pauses(plan, path, words, dur, max_keep_segments=room)
    assert len(free.keep_segments) > room and not free_report.capped
    assert len(capped.keep_segments) <= room and report.capped and report.added_cuts > 0
    # A budget below what the consented plan already uses adds nothing.
    none, report0 = tpc.apply_tight_pauses(plan, path, words, dur, max_keep_segments=0)
    assert none.removed == plan.removed and report0.added_cuts == 0
