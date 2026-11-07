"""Speech cleanup long pauses: the tight-pause cutter, limited to spans of 0.6 s or more, run by
default on a consented single-clip phone Talking render (``_phone_long_pause_cut``).

Failure modes written down before the code:

1. The long mode adds a cut tight mode would not (it may only REMOVE candidates), or cuts a
   natural pause shorter than ``MAX_PAUSE_S``.
2. Flag off still changes the plan or writes a receipt (must be the SAME plan object).
3. A creator's tight ask is routed to the long mode (or both run): tight keeps its path.
4. The long receipt lands under ``tight_pauses`` (brief checks / render promises would then
   think the creator asked for it) or does not land on the variant at all.
5. The extra cuts miss the public cleanup counts, or the editor's ``silence_cut.removed``.
6. The tight report shape changes (an extra ``mode`` key, a different ``limit_s``).
7. A cutter fault fails the render instead of keeping the consented plan.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.pipeline import silence_cut as sc
from app.pipeline import tight_pause_cut as tpc
from app.tasks import generative_build as gb
from tests.pipeline.test_tight_pause_cut import (
    CYCLE,
    build_take,
    consented,
    out_time,
)
from tests.tasks.test_phone_tight_pauses import (
    PLAIN_ASK,
    TIGHT_ASK,
    _cleanup_req,
    _snapshot,
)


@pytest.fixture
def take(tmp_path):
    path, words, dur, _ = build_take(tmp_path, (CYCLE * 4)[:23])
    # whisper-1 stamps are contiguous: each word runs on to the next word's start, so the
    # cleanup plan cannot see any pause (the bug this mode exists for).
    true_words = words
    words = [{**w, "end_s": nxt["start_s"]} for w, nxt in zip(words, words[1:], strict=False)] + [
        words[-1]
    ]
    plan = consented(path, words, dur)
    take = (path, words, dur, plan)
    return _Take(take, true_words)


class _Take(tuple):
    """(path, words, dur, plan) plus the true (unstretched) word times for gap checks."""

    def __new__(cls, parts, true_words):
        self = super().__new__(cls, parts)
        self.true_words = true_words
        return self


def test_min_pause_only_removes_candidates_and_leaves_short_pauses(take):
    path, words, dur, plan = take
    true_words = take.true_words
    tight, _ = tpc.apply_tight_pauses(plan, path, words, dur)
    long_, report = tpc.apply_tight_pauses(plan, path, words, dur, min_pause_s=sc.MAX_PAUSE_S)
    assert report.mode == "long" and report.limit_s == sc.MAX_PAUSE_S
    new_long = [r for r in long_.removed if r not in plan.removed]
    new_tight = [r for r in tight.removed if r not in plan.removed]
    assert new_long and len(new_long) <= len(new_tight)
    for r in new_long:  # inside something tight mode cut too
        assert any(t.start_s - 1e-6 <= r.start_s and r.end_s <= t.end_s + 1e-6 for t in new_tight)
    # Gaps shorter than 0.6 s (0.05..0.55) are exactly as long as before; longer ones are cut.
    for i, gap in enumerate([*(CYCLE * 4)[:23]]):
        a, b = true_words[i], true_words[i + 1]
        kept = out_time(long_, b["start_s"]) - out_time(long_, a["end_s"])
        before = out_time(plan, b["start_s"]) - out_time(plan, a["end_s"])
        if gap < sc.MAX_PAUSE_S - 0.01:
            assert kept == pytest.approx(before, abs=1e-6)
        else:
            assert kept < before


def test_tight_report_shape_is_unchanged(take):
    path, words, dur, plan = take
    _, report = tpc.apply_tight_pauses(plan, path, words, dur)
    variant = report.to_variant()
    assert "mode" not in variant and variant["limit_s"] == tpc.TIGHT_PAUSE_LIMIT_S


def _run(snapshot, take):
    path, words, dur, plan = take
    return gb._phone_long_pause_cut(snapshot, plan, words, clip_path=path, duration_s=dur)


def test_flag_off_keeps_the_same_plan_and_no_receipt(monkeypatch, take):
    monkeypatch.setattr(settings, "speech_cleanup_long_pauses_enabled", False)
    plan, receipt = _run(_snapshot(None), take)
    assert plan is take[3] and receipt is None


def test_a_tight_ask_is_left_to_the_tight_path(monkeypatch, take):
    monkeypatch.setattr(settings, "speech_cleanup_long_pauses_enabled", True)
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)
    plan, receipt = _run(_snapshot(TIGHT_ASK), take)
    assert plan is take[3] and receipt is None
    tight_plan, tight_receipt = gb._phone_tight_pause_cut(
        _snapshot(TIGHT_ASK), take[3], take[1], clip_path=take[0], duration_s=take[2]
    )
    assert tight_receipt is not None and "mode" not in tight_receipt
    assert tight_plan is not take[3]


def test_no_ask_runs_the_long_mode_and_a_fault_keeps_the_plan(monkeypatch, take):
    monkeypatch.setattr(settings, "speech_cleanup_long_pauses_enabled", True)
    plan, receipt = _run(_snapshot(PLAIN_ASK), take)
    assert receipt is not None and receipt["mode"] == "long" and receipt["limit_s"] == 0.6
    assert receipt["status"] == "applied" and plan is not take[3]

    def boom(*_a, **_k):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(tpc, "apply_tight_pauses", boom)
    kept, receipt = _run(_snapshot(None), take)
    assert kept is take[3] and receipt["status"] == "unavailable"


def test_worker_stores_long_receipt_separately_and_counts_the_cut(monkeypatch):
    from tests.tasks.test_kri965_editor_take_window import _run as run_worker

    monkeypatch.setattr(settings, "speech_cleanup_long_pauses_enabled", True)
    seen: dict = {}

    def apply(plan, _path, _words, _duration, *, min_pause_s=None):
        seen["min_pause_s"] = min_pause_s
        added = sc.Removal(5.0, 5.2, sc.REASON_SILENCE)
        keeps = [
            seg
            for lo, hi in plan.keep_segments
            for seg in ([(lo, 5.0), (5.2, hi)] if lo < 5.0 < 5.2 < hi else [(lo, hi)])
        ]
        removed = sorted([*plan.removed, added], key=lambda r: r.start_s)
        out = sc.CutPlan(
            keep_segments=keeps,
            removed=removed,
            time_saved_s=sum(r.end_s - r.start_s for r in removed),
        )
        report = tpc.TightPauseReport(
            "applied", 1, 0.2, 0.4, 1.6, 0, 0, 0, False, None, "long", min_pause_s
        )
        return out, report

    monkeypatch.setattr(tpc, "apply_tight_pauses", apply)
    _job, variant = run_worker(monkeypatch, [_cleanup_req(PLAIN_ASK)], cleanup="on", cut=False)

    assert seen["min_pause_s"] == sc.MAX_PAUSE_S
    assert variant["long_pauses"]["mode"] == "long"
    assert "tight_pauses" not in variant
    assert any(r["start_s"] == pytest.approx(5.0) for r in variant["silence_cut"]["removed"])
    assert variant["_speech_cleanup_outcome_context"]["output_removal_count"] == 3
