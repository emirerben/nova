"""realign_labels: the server snaps clip labels onto their clip's CURRENT window.

Incident shape (thread e798bda2): the creator extended a clip on the phone, so
every label bar sat ~0.53 s early against its linked slot window.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.edit_copilot import _parse_op, _ParseState
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)
from tests.services._guided_timeline_fixtures import (
    arm_guided,
    guided_bars,
    guided_job,
    guided_revision,
)

SHIFT = 0.533


@pytest.fixture
def guided(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    job, variant = guided_job(revision, guided_bars(revision), job_id)
    arm_guided(monkeypatch, revision)
    return job, variant, revision


def _row(variant, bar_id):
    return next(r for r in variant["text_elements"] if r["id"] == bar_id)


def _skew(variant, media_ids, by=SHIFT):
    for media_id in media_ids:
        row = _row(variant, f"clip-label-media-{media_id}")
        row["start_s"] = round(max(0.0, row["start_s"] - by), 3)
        row["end_s"] = round(row["end_s"] - by, 3)


def _compiled_rows(compiled):
    return {r["id"]: r for r in compiled.payload.text_elements}


def _parse(variant, job, op=None, utterance="texts aren't aligned"):
    snapshot = build_editor_snapshot(job, variant)
    state = _ParseState(0.9)
    state.utterance = utterance
    parsed = _parse_op(op or {"op": "realign_labels"}, snapshot, state)
    return parsed, state


def test_misaligned_labels_snap_to_their_windows(guided) -> None:
    job, variant, revision = guided
    _skew(variant, ["m0", "m1", "m2", "m3"])
    parsed, _state = _parse(variant, job)
    assert parsed["op"] == "realign_labels"
    assert parsed["expected_count"] == 4
    compiled = compile_editor_ops(job, variant, [parsed])
    rows = _compiled_rows(compiled)
    for segment in revision["segments"]:
        row = rows[f"clip-label-media-{segment['media_id']}"]
        assert row["start_s"] == pytest.approx(segment["output_start_s"], abs=1e-3)
        assert row["end_s"] == pytest.approx(segment["output_end_s"], abs=1e-3)
    assert "Realign 4 labels" in compiled.changes[0]


def test_already_aligned_is_an_honest_noop(guided) -> None:
    job, variant, _rev = guided
    parsed, state = _parse(variant, job)
    assert parsed is None
    assert "already line up" in (state.selector_clarification or "")
    assert not state.rejection_reasons


def test_compile_noop_raises_plainly(guided) -> None:
    job, variant, _rev = guided
    with pytest.raises(KriaEditorOpError, match="already line up"):
        compile_editor_ops(job, variant, [{"op": "realign_labels"}])


def test_style_position_and_text_are_untouched(guided) -> None:
    job, variant, _rev = guided
    row = _row(variant, "clip-label-media-m1")
    row.update({"x_frac": 0.2, "y_frac": 0.4, "color": "#FF0000", "text": "Mine"})
    _skew(variant, ["m1"])
    parsed, _ = _parse(variant, job)
    out = _compiled_rows(compile_editor_ops(job, variant, [parsed]))["clip-label-media-m1"]
    assert (out["x_frac"], out["y_frac"], out["color"], out["text"]) == (
        0.2,
        0.4,
        "#FF0000",
        "Mine",
    )
    assert out["start_s"] == pytest.approx(2.0, abs=1e-3)


def test_selector_limits_to_named_labels(guided) -> None:
    job, variant, _rev = guided
    _skew(variant, ["m1", "m2"])
    parsed, _ = _parse(variant, job, {"op": "realign_labels", "selector": {"clip_ids": ["m1"]}})
    assert parsed["target_ids"] == ["clip-label-media-m1"]
    rows = _compiled_rows(compile_editor_ops(job, variant, [parsed]))
    assert rows["clip-label-media-m1"]["start_s"] == pytest.approx(2.0, abs=1e-3)
    assert rows["clip-label-media-m2"]["start_s"] == pytest.approx(4.0 - SHIFT, abs=1e-3)


def test_captions_titles_and_free_text_never_touched(guided) -> None:
    job, variant, _rev = guided
    _skew(variant, ["m0", "m1"])
    before = {r["id"]: (r["start_s"], r["end_s"]) for r in variant["text_elements"]}
    parsed, _ = _parse(variant, job)
    rows = _compiled_rows(compile_editor_ops(job, variant, [parsed]))
    for keep in ("guided-title", "caption-1"):
        assert (rows[keep]["start_s"], rows[keep]["end_s"]) == before[keep]


def test_within_tolerance_is_left_alone(guided) -> None:
    job, variant, _rev = guided
    _skew(variant, ["m1"], by=0.04)
    parsed, state = _parse(variant, job)
    assert parsed is None and state.selector_clarification


def test_unknown_selector_and_no_labels(guided) -> None:
    job, variant, _rev = guided
    parsed, _ = _parse(variant, job, {"op": "realign_labels", "selector": {"bogus": 1}})
    assert parsed is None
    parsed, state = _parse(variant, job, {"op": "realign_labels", "selector": {"group": "title"}})
    assert parsed is None and "no clip labels" in state.selector_clarification


def test_op_unavailable_without_v2_snapshot(guided) -> None:
    job, variant, _rev = guided
    _skew(variant, ["m1"])
    snapshot = build_editor_snapshot(job, variant)
    snapshot.pop("editor_ops_version")
    assert _parse_op({"op": "realign_labels"}, snapshot, _ParseState(0.9)) is None


def test_same_bundle_timeline_op_leaves_realign_to_the_rebase(guided) -> None:
    job, variant, _rev = guided
    _skew(variant, ["m1"])
    parsed, _ = _parse(variant, job)
    compiled = compile_editor_ops(
        job,
        variant,
        [{"op": "trim_clip_start", "slot_index": 0, "start_s": 0.5}, parsed],
    )
    rows = _compiled_rows(compiled)
    assert rows["clip-label-media-m1"]["end_s"] > rows["clip-label-media-m1"]["start_s"]


def test_label_each_clip_alignment_ask_gets_plain_reply(guided) -> None:
    from app.agents.edit_copilot import _LABEL_ALIGNMENT_NOT_WORDING

    job, variant, _rev = guided
    snapshot = build_editor_snapshot(job, variant)
    snapshot["label_facts"] = True
    for row in snapshot["text_bars"]:
        if row.get("clip_id"):
            row["edited"] = True
    for slot in snapshot["slots"]:
        slot["facts"] = [{"kind": "place", "value": "Lisbon"}]
    state = _ParseState(0.9)
    state.utterance = "The texts aren't aligned with their respective videos"
    op = _parse_op({"op": "label_each_clip", "source": "facts"}, snapshot, state)
    assert op is None
    assert state.rejection_reasons[0]["detail"] == _LABEL_ALIGNMENT_NOT_WORDING
    # A genuine label ask keeps a plain-language (not internal) note.
    state = _ParseState(0.9)
    state.utterance = "label each clip with the place"
    assert _parse_op({"op": "label_each_clip", "source": "facts"}, snapshot, state) is None
    assert state.rejection_reasons[0]["detail"] == (
        "The labels you edited by hand were kept, and no other clip needs a label."
    )
