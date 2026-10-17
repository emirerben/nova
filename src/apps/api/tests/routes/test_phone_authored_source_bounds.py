"""KRI-736: Save never 422s ``TIMELINE_OUT_OF_BOUNDS`` for a window within 50 ms of the source end.

Prod 2026-10-17 15:21Z (job 5a8f5990, client protocol 11): the authored resolver refused any
window that ended more than 1 ms past the catalog source, while the phone's own window math
(``KriaSourceWindowGeometry.boundsTolerance``) and the guided-v2 server rule both allow 50 ms.
The resolver now shortens such a slot to the source end; anything further out is still refused.

Drives the real ``prepare_authored_editor_commit`` path on a phone guided job
(sources are 10 s, each kept clip starts at 1.0 s).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import generative_jobs as gj
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.editor_empty_drafts import stage_initial_authored_baseline
from tests.routes.test_phone_guided_clip_delete_order_contract import _order_required_job

VARIANT = "guided_story"
SOURCE_S = 10.0


def _commit(monkeypatch, *, in_s=1.0, duration_s=None, overshoot=0.0, rate=None):
    """Delete m1 and leave the first kept clip with a window ending `overshoot` past the source."""
    job, variant, revision = _order_required_job(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    sources = revision["sources"]
    target = next(r for r in rows if sources[r["clip_index"]]["media_id"] == "m1")
    slots = []
    for row in rows:
        edit = {
            "slot_id": row["slot_id"],
            "clip_index": row["clip_index"],
            "in_s": row["in_s"],
            "duration_s": row["duration_s"],
            "transition_after": "cut",
        }
        if not slots:
            span = SOURCE_S + overshoot - in_s
            edit.update(in_s=in_s, duration_s=duration_s or span / (rate or 1.0))
            if rate is not None:
                edit["playback_rate"] = rate
        slots.append(gj.TimelineSlotEdit(**edit))
    baseline = stage_initial_authored_baseline(job, VARIANT, rows)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        deletions=[{"kind": "clip", "id": target["slot_id"]}],
        timeline_slots=slots,
    )
    body = gj.normalize_editor_deletion_request(baseline, VARIANT, body)
    gj.require_guided_story_editor_commit(baseline, VARIANT, body)
    prepare_authored_editor_commit(baseline, VARIANT, body, validation_arguments={})
    saved = baseline.assembly_plan["variants"][0]["user_timeline"]["slots"]
    return next(s for s in saved if not s.get("removed")), slots[0]


@pytest.mark.parametrize("overshoot", [0.0003, 0.02, 0.05])
def test_a_window_within_50ms_of_the_source_end_is_shortened_not_refused(monkeypatch, overshoot):
    saved, posted = _commit(monkeypatch, overshoot=overshoot)

    if overshoot > 0.001:
        assert saved["in_s"] + saved["duration_s"] <= SOURCE_S
        assert saved["duration_s"] == pytest.approx(SOURCE_S - 1.0, abs=1e-5)
        assert saved["duration_s"] < posted.duration_s
    else:  # frame-rounding overshoot is persisted exactly as before
        assert saved["duration_s"] == posted.duration_s


def test_the_shortened_duration_respects_the_playback_rate(monkeypatch):
    # Speed is not enabled on the phone pilot, so this one drives the resolver itself.
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, _ = _order_required_job(monkeypatch)
    row = gj.editor_deletion_timeline(job, variant)[0]
    slot = gj.TimelineSlotEdit(
        slot_id=row["slot_id"],
        clip_index=row["clip_index"],
        in_s=1.0,
        duration_s=(SOURCE_S + 0.04 - 1.0) / 2.0,
        playback_rate=2.0,
        transition_after="cut",
    )
    [saved] = resolve_authored_phone_slots(job, variant, [slot])

    assert saved["in_s"] + saved["duration_s"] * 2.0 <= SOURCE_S + 1e-9
    assert saved["duration_s"] == pytest.approx((SOURCE_S - 1.0) / 2.0, abs=1e-5)


def test_a_window_more_than_50ms_past_the_source_is_still_refused_with_diagnostics(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _commit(monkeypatch, overshoot=0.06)

    detail = exc.value.detail
    assert exc.value.status_code == 422
    assert detail["code"] == "TIMELINE_OUT_OF_BOUNDS"
    assert detail["clip_index"] == 0
    assert detail["in_s"] == 1.0
    assert detail["source_duration_s"] == SOURCE_S
    assert detail["overshoot_s"] == pytest.approx(0.06, abs=1e-3)
    assert detail["rate"] == 1.0
    assert "slot_id" in detail and "slot_order" in detail


def test_an_in_point_past_the_source_is_still_refused(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _commit(monkeypatch, in_s=SOURCE_S + 0.01, duration_s=0.3)

    assert exc.value.detail["code"] == "TIMELINE_OUT_OF_BOUNDS"


def test_a_window_that_would_shrink_below_the_minimum_is_still_refused(monkeypatch):
    # 40 ms of footage left: the shortened slot would be under the 0.1 s minimum.
    with pytest.raises(HTTPException) as exc:
        _commit(monkeypatch, in_s=SOURCE_S - 0.04, duration_s=0.5)

    assert exc.value.detail["code"] == "TIMELINE_OUT_OF_BOUNDS"
