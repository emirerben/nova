"""Qendresa 13: the phone Talking render's tight-pause glue (`_phone_tight_pause_cut`).

Failure modes written down before the code:

1. Flag off (or no binding, or an unreadable one, or a cleanup ask that is not "tight") still
   changes the plan or writes a receipt: the render must be byte-identical (same plan OBJECT).
2. Flag on + a tight ask adds no cuts (the glue never calls the cutter, or drops its plan).
3. The cutter raising fails the render, or returns the half-built plan, instead of keeping the
   consented plan and reporting `unavailable` (receipts would then claim a tight cut).
4. The receipt is not stored on the variant when the flag is on, or leaks onto the variant when
   the flag is off.
5. The tight cuts do not reach the cleanup bookkeeping (the public "removed N / M ms" receipt
   still counts only the consented cuts) or the editor's `silence_cut.removed` record (the
   editor would replay the whole take under captions timed for the cut).
6. A consented removal is lost or moved by the layering.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline import silence_cut as sc
from app.pipeline import tight_pause_cut as tpc
from app.services import clip_speech
from app.tasks import generative_build as gb
from tests.pipeline.test_tight_pause_cut import CYCLE, build_take

TIGHT_ASK = "Cut out all my pauses so it's tight"
PLAIN_ASK = "cut the long pauses"
KEEP_ASK = "keep my pauses"


def _snapshot(text: str | None) -> dict:
    if text is None:
        return {}
    req = BriefRequirement(
        id="r1", kind="audio", scope="global", description=text, facts={"speech_cleanup": True}
    )
    brief = CreativeBrief(version=1, requirements=[req])
    binding = BriefBinding.create(uuid.uuid4(), brief)
    return {"creator_brief_binding": binding.model_dump(mode="json")}


@pytest.fixture
def take(tmp_path: Path):
    gaps = (CYCLE * 4)[:23]
    path, words, dur, _ = build_take(tmp_path, gaps)
    res = clip_speech.detect_silences_with_status(
        path, min_silence_s=tpc.SILENCE_MIN_S, ambient_adaptive=True
    )
    plan = sc.build_cut_plan(
        words, list(res.spans), dur, mixed_gap_enabled=True, over_budget_policy="clamp"
    )
    return path, words, dur, plan


def _run(snapshot, take):
    path, words, dur, plan = take
    return gb._phone_tight_pause_cut(snapshot, plan, words, clip_path=path, duration_s=dur)


@pytest.mark.parametrize("text", [None, PLAIN_ASK, KEEP_ASK])
def test_no_tight_ask_keeps_the_same_plan_and_no_receipt(monkeypatch, take, text):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)
    plan, receipt = _run(_snapshot(text), take)
    assert plan is take[3]
    assert receipt is None


def test_flag_off_keeps_the_same_plan_and_no_receipt(monkeypatch, take):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False)
    plan, receipt = _run(_snapshot(TIGHT_ASK), take)
    assert plan is take[3]
    assert receipt is None


def test_unreadable_binding_keeps_the_plan(monkeypatch, take):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)
    plan, receipt = _run({"creator_brief_binding": {"garbage": 1}}, take)
    assert plan is take[3]
    assert receipt is None


def test_tight_ask_adds_cuts_and_keeps_every_consented_removal(monkeypatch, take):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)
    consented = take[3]
    plan, receipt = _run(_snapshot(TIGHT_ASK), take)
    assert receipt is not None and receipt["status"] == "applied"
    assert receipt["added_cuts"] > 0
    assert len(plan.removed) > len(consented.removed)
    for removal in consented.removed:
        assert removal in plan.removed
    assert plan.time_saved_s > consented.time_saved_s
    # The editor rebuilds the take from this record: the tight cuts are in it.
    summary = gb._phone_cut_summary(plan, probe_s=take[2], original_s=take[2])
    assert len(summary["removed"]) == len(plan.removed)


def test_a_failing_cutter_keeps_the_consented_plan_and_reports_unavailable(monkeypatch, take):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)

    def boom(*_a, **_k):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(tpc, "apply_tight_pauses", boom)
    plan, receipt = _run(_snapshot(TIGHT_ASK), take)
    assert plan is take[3]
    assert receipt is not None
    assert receipt["status"] == "unavailable" and receipt["reason"] == "failed"
    assert receipt["added_cuts"] == 0
    assert receipt["pauses_over_limit"] is None


# -- the full phone Talking worker (`_run_phone_subtitled_job`) ----------------------------------


def _cleanup_req(text: str) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="audio", scope="global", description=text, facts={"speech_cleanup": True}
    )


def _stub_cutter(monkeypatch) -> None:
    """A cutter that adds one 0.2 s silence cut at 5.0 s (the full-job fixture's audio is not
    a real file; the cutter itself is proven in tests/pipeline/test_tight_pause_cut.py)."""

    def apply(plan, _path, _words, _duration):
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
            status="applied",
            added_cuts=1,
            added_removed_s=0.2,
            longest_pause_s=0.3,
            longest_pause_before_s=0.9,
            pauses_over_limit=0,
            guard_dropped=0,
            guard_trimmed=0,
            capped=False,
            reason=None,
        )
        return out, report

    monkeypatch.setattr(tpc, "apply_tight_pauses", apply)


def test_worker_with_flag_on_stores_the_receipt_and_the_cut_reaches_editor_and_receipt(
    monkeypatch,
):
    from tests.tasks.test_kri965_editor_take_window import (
        _assert_editor_plays_the_phone_render,
    )
    from tests.tasks.test_kri965_editor_take_window import (
        _run as run_worker,
    )

    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)
    _stub_cutter(monkeypatch)
    job, variant = run_worker(monkeypatch, [_cleanup_req(TIGHT_ASK)], cleanup="on", cut=False)

    assert variant["tight_pauses"]["status"] == "applied"
    assert variant["tight_pauses"]["added_cuts"] == 1
    # The editor rebuilds the take from silence_cut.removed: the tight cut is in it, and the
    # editor plays exactly the spans the device recipe plays.
    assert any(
        r["start_s"] == pytest.approx(5.0) and r["end_s"] == pytest.approx(5.2)
        for r in variant["silence_cut"]["removed"]
    )
    spans = _assert_editor_plays_the_phone_render(job, variant)
    assert all(not (lo < 5.1 < hi) for lo, hi in spans)
    # The public receipt counts what was cut (consented 2 + tight 1).
    assert variant["silence_cut_outcome"] == "applied"
    context = variant["_speech_cleanup_outcome_context"]
    assert context["output_removal_count"] == 3
    assert context["output_removed_ms"] == round(variant["silence_cut"]["time_saved_s"] * 1000)


def test_worker_with_flag_off_writes_no_receipt_and_never_calls_the_cutter(monkeypatch):
    from tests.tasks.test_kri965_editor_take_window import _run as run_worker

    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False)

    def explode(*_a, **_k):
        raise AssertionError("the cutter must not run with the flag off")

    monkeypatch.setattr(tpc, "apply_tight_pauses", explode)
    _job, variant = run_worker(monkeypatch, [_cleanup_req(TIGHT_ASK)], cleanup="on", cut=False)

    assert "tight_pauses" not in variant
    assert variant["_speech_cleanup_outcome_context"]["output_removal_count"] == 2
