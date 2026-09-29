"""KRI-219 adversarial-review regressions (timeline rebase, legacy media kinds)."""

from __future__ import annotations

import types
import uuid

import pytest

from app.agents.editor_ops_v2 import register_all_handlers
from app.agents.editor_ops_v2 import timeline as tl
from app.services import kria_editor_ops as ops
from app.services import kria_editor_timeline as ket
from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
from tests.services._guided_timeline_fixtures import (
    arm_guided,
    guided_bars,
    guided_job,
    guided_revision,
)
from tests.services.test_kria_editor_ops import _job, _variant

register_all_handlers()


def _guided(monkeypatch, mutate=None):
    from app.config import settings

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    bars = guided_bars(revision)
    if mutate:
        bars = mutate(bars)
    job, variant = guided_job(revision, bars, job_id)
    arm_guided(monkeypatch, revision)
    return job, variant


def _free_bar(start, end, bar_id="free-1"):
    return {
        "id": bar_id,
        "text": "Free text",
        "start_s": start,
        "end_s": end,
        "role": "generative_intro",
        "position": "middle",
        "font_family": "DM Sans",
        "size_px": 60,
        "color": "#FFFFFF",
        "effect": "static",
        "alignment": "center",
    }


def _text(compiled, bar_id):
    return next((r for r in compiled.payload.text_elements if r["id"] == bar_id), None)


def test_free_bar_spanning_two_clips_survives_reorder(monkeypatch) -> None:
    # Free bar over clips m1+m2 (2.2-5.8); m2 moves ahead of m1 -> projected end
    # lands before the projected start. It must survive, not be dropped.
    job, variant = _guided(monkeypatch, lambda bars: [*bars, _free_bar(2.2, 5.8)])
    compiled = compile_editor_ops(
        job, variant, [{"op": "reorder_clip", "from_index": 2, "to_index": 0}]
    )
    bar = _text(compiled, "free-1")
    assert bar is not None, compiled.changes
    assert bar["end_s"] - bar["start_s"] >= 0.2
    assert not any(c.startswith("Removed text") for c in compiled.changes)


def test_free_bar_survives_split_child_reorder(monkeypatch) -> None:
    job, variant = _guided(monkeypatch, lambda bars: [*bars, _free_bar(1.0, 3.0)])
    first = compile_editor_ops(job, variant, [{"op": "split_clip", "slot_index": 0, "at_s": 1.2}])
    projected = ops.project_editor_draft(
        variant, first.payload.model_dump(mode="json", exclude_none=True), job
    )
    second = compile_editor_ops(
        job, projected, [{"op": "reorder_clip", "from_index": 2, "to_index": 0}]
    )
    assert _text(second, "free-1") is not None


def test_title_one_clip_long_stays_at_the_start_on_reorder(monkeypatch) -> None:
    def mutate(bars):
        for bar in bars:
            if bar["id"] == "guided-title":
                bar["end_s"] = 2.0
                bar["segment_id"] = "s1"
        return bars

    job, variant = _guided(monkeypatch, mutate)
    compiled = compile_editor_ops(
        job, variant, [{"op": "reorder_clip", "from_index": 0, "to_index": 3}]
    )
    title = _text(compiled, "guided-title")
    assert (title["start_s"], title["end_s"]) == (0.0, 2.0)


def test_round_keeps_frame_grid_precision() -> None:
    assert ket._round(1.0333333333) == 1.033333


def test_duration_only_transition_patch_on_cut_is_not_applied(monkeypatch) -> None:
    job, variant = _guided(monkeypatch)
    with pytest.raises(KriaEditorOpError, match="hard cut"):
        compile_editor_ops(
            job,
            variant,
            [
                {
                    "op": "patch_slots",
                    "selector": {"slot_indexes": [0]},
                    "patch": {"transition_duration_s": 0.5},
                }
            ],
        )


def test_legacy_slots_get_media_kind_from_clip_paths() -> None:
    variant = _variant()  # slot-1 = a.mp4, slot-2 = b.jpg (no media_kind on rows)
    job = _job(variant)
    rows = ops._variant_slots(variant, job)
    assert [row["media_kind"] for row in rows] == ["video", "image"]
    state = types.SimpleNamespace(slots=rows)
    assert tl._select(state, {"media_kind": "image"}) == [1]
    assert tl._select(state, {"media_kind": "video"}) == [0]
    assert tl._is_image(rows[1]) and not tl._is_image(rows[0])
