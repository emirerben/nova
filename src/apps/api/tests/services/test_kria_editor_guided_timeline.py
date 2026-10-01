"""KRI-219 Lane B: the chat copilot's timeline ops on guided-native variants.

Every test asserts the LABEL window equals the WINDOW OF THE SEGMENT the label
follows, computed independently from the compiled ``timeline_slots``.
"""

from __future__ import annotations

import copy
import uuid

import pytest

from app.agents.edit_copilot import _parse_op, _ParseState
from app.services import kria_editor_ops as ops
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
    project_editor_draft,
)
from tests.services._guided_timeline_fixtures import (
    arm_guided,
    guided_bars,
    guided_job,
    guided_revision,
)


@pytest.fixture
def guided(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    job, variant = guided_job(revision, guided_bars(revision), job_id)
    arm_guided(monkeypatch, revision)
    return job, variant, revision


def _bar(compiled, media_id):
    rows = {row["id"]: row for row in compiled.payload.text_elements}
    return rows.get(f"clip-label-media-{media_id}")


def _segments(guided, compiled):
    """Independent recomputation of the persisted segments from the compiled slots."""
    from app.schemas.guided_edit_revision import normalize_guided_editor_revision
    from app.services.guided_timeline import build_guided_segments

    _job, _variant, revision = guided
    segments = build_guided_segments(
        revision,
        compiled.payload.timeline_slots,
        transitions_enabled=True,
        max_slots=120,
        max_total_s=120.0,
    )
    raw = {**revision, "revision_number": 2, "segments": segments, "state_hash": ""}
    return normalize_guided_editor_revision(raw)["segments"]


def _assert_labels_follow(guided, compiled, expected_media):
    segments = {}
    for segment in _segments(guided, compiled):
        segments.setdefault(segment["media_id"], []).append(segment)
    for media_id in expected_media:
        bar = _bar(compiled, media_id)
        assert bar is not None, media_id
        spans = segments[media_id]
        assert bar["start_s"] == pytest.approx(spans[0]["output_start_s"], abs=1e-3)
        assert bar["end_s"] == pytest.approx(spans[-1]["output_end_s"], abs=1e-3)


def test_flag_off_keeps_guided_timeline_families_withheld(monkeypatch, guided) -> None:
    from app.config import settings

    job, variant, _rev = guided
    on = build_editor_snapshot(job, variant)["allowed_op_families"]
    assert {"clip", "transition"} <= set(on)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", False, raising=False)
    off = build_editor_snapshot(job, variant)["allowed_op_families"]
    assert "clip" not in off and "transition" not in off
    assert "text" in off


def test_reorder_labels_follow_their_clip(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job, variant, [{"op": "reorder_clip", "from_index": 2, "to_index": 0}]
    )
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])
    assert (_bar(compiled, "m2")["start_s"], _bar(compiled, "m2")["end_s"]) == (0.0, 2.0)
    assert (_bar(compiled, "m0")["start_s"], _bar(compiled, "m0")["end_s"]) == (2.0, 4.0)
    assert (_bar(compiled, "m1")["start_s"], _bar(compiled, "m1")["end_s"]) == (4.0, 6.0)


def test_reorder_never_tombstones_and_always_ships_text(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job, variant, [{"op": "reorder_clip", "from_index": 3, "to_index": 1}]
    )
    # Invariant: a guided payload with timeline_slots always carries rebased text.
    assert compiled.payload.timeline_slots is not None
    assert compiled.payload.text_elements is not None
    ids = {row["id"] for row in compiled.payload.text_elements}
    assert {row["id"] for row in variant["text_elements"]} == ids


def test_trim_shifts_labels_and_stretches_title(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(job, variant, [{"op": "trim_output_start", "start_s": 1.0}])
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])
    assert (_bar(compiled, "m0")["start_s"], _bar(compiled, "m0")["end_s"]) == (0.0, 1.0)
    title = next(r for r in compiled.payload.text_elements if r["id"] == "guided-title")
    assert (title["start_s"], title["end_s"]) == (0.0, 7.0)


def test_captions_are_untouched(guided) -> None:
    job, variant, _rev = guided
    before = next(r for r in variant["text_elements"] if r["id"] == "caption-1")
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {"op": "reorder_clip", "from_index": 0, "to_index": 3},
            {"op": "trim_output_start", "start_s": 1.0},
        ],
    )
    after = next(r for r in compiled.payload.text_elements if r["id"] == "caption-1")
    assert after == before


def test_split_label_spans_both_children(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job, variant, [{"op": "split_clip", "slot_index": 1, "at_s": 0.8}]
    )
    slots = compiled.payload.timeline_slots
    assert len(slots) == 5
    assert slots[2].parent_segment_id == "s2"
    assert slots[2].slot_id  # deterministic id so the label can follow the child
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])
    assert (_bar(compiled, "m1")["start_s"], _bar(compiled, "m1")["end_s"]) == (2.0, 4.0)
    assert _bar(compiled, "m1")["segment_id"] == "s2"


def test_remove_drops_the_label_and_says_so(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(job, variant, [{"op": "remove_clip", "slot_index": 1}])
    assert _bar(compiled, "m1") is None
    assert "Removed label for clip 2" in compiled.changes
    _assert_labels_follow(guided, compiled, ["m0", "m2", "m3"])
    assert (_bar(compiled, "m2")["start_s"], _bar(compiled, "m2")["end_s"]) == (2.0, 4.0)
    title = next(r for r in compiled.payload.text_elements if r["id"] == "guided-title")
    assert title["end_s"] == 6.0


def test_retime_shortens_the_clip_and_its_label(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "patch_slots",
                "selector": {"slot_indexes": [0]},
                "patch": {"playback_rate": 2},
            }
        ],
    )
    assert compiled.payload.timeline_slots[0].playback_rate == 2
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])
    assert (_bar(compiled, "m0")["start_s"], _bar(compiled, "m0")["end_s"]) == (0.0, 1.0)
    assert (_bar(compiled, "m1")["start_s"], _bar(compiled, "m1")["end_s"]) == (1.0, 3.0)


def test_transition_overlap_moves_every_label_window(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "patch_slots",
                "selector": {"all": True},
                "patch": {"transition_after": "crossfade", "transition_duration_s": 0.3},
            }
        ],
    )
    slots = compiled.payload.timeline_slots
    assert [s.transition_after for s in slots] == ["crossfade"] * 3 + ["cut"]
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])
    assert _bar(compiled, "m1")["start_s"] == pytest.approx(1.7)
    title = next(r for r in compiled.payload.text_elements if r["id"] == "guided-title")
    assert title["end_s"] == pytest.approx(7.1)


def test_set_total_duration_proportional_and_labels(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_total_duration", "target_s": 6, "strategy": "proportional"}]
    )
    assert [s.duration_s for s in compiled.payload.timeline_slots] == [1.5] * 4
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])
    assert (_bar(compiled, "m3")["start_s"], _bar(compiled, "m3")["end_s"]) == (4.5, 6.0)


def test_set_total_duration_accounts_for_transition_overlap(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "patch_slots",
                "selector": {"all": True},
                "patch": {"transition_after": "crossfade", "transition_duration_s": 0.3},
            },
            {"op": "set_total_duration", "target_s": 10, "strategy": "uniform"},
        ],
    )
    segments = _segments(guided, compiled)
    assert max(s["output_end_s"] for s in segments) == pytest.approx(10.0, abs=0.05)
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2", "m3"])


def test_set_total_duration_trim_tail_drops_tail_labels(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_total_duration", "target_s": 5, "strategy": "trim_tail"}]
    )
    segments = _segments(guided, compiled)
    assert max(s["output_end_s"] for s in segments) == pytest.approx(5.0, abs=0.05)
    assert _bar(compiled, "m3") is None
    assert "Removed label for clip 4" in compiled.changes
    _assert_labels_follow(guided, compiled, ["m0", "m1", "m2"])


def test_set_total_duration_unreachable_reports_longest(guided) -> None:
    job, variant, _rev = guided
    with pytest.raises(KriaEditorOpError, match=r"Longest possible is 40\.0 s"):
        compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 45}])


def test_patch_slots_caps_duration_at_the_footage(guided) -> None:
    job, variant, _rev = guided
    compiled = compile_editor_ops(
        job,
        variant,
        [{"op": "patch_slots", "selector": {"all": True}, "patch": {"duration_s": 30}}],
    )
    assert [s.duration_s for s in compiled.payload.timeline_slots] == [10.0] * 4


def test_two_turn_draft_stays_guided_and_labels_keep_following(guided) -> None:
    job, variant, _rev = guided
    first = compile_editor_ops(
        job, variant, [{"op": "reorder_clip", "from_index": 2, "to_index": 0}]
    )
    payload = first.payload.model_dump(mode="json", exclude_none=True)
    projected = project_editor_draft(variant, payload, job)

    assert "user_timeline" not in projected
    assert projected["guided_draft_slots"]
    assert ops._is_guided_native(job, projected)
    assert "clip" in build_editor_snapshot(job, projected)["allowed_op_families"]
    order = [row["media_id"] for row in ops._variant_slots(projected, job)]
    assert order == ["m2", "m0", "m1", "m3"]

    second = compile_editor_ops(job, projected, [{"op": "remove_clip", "slot_index": 1}])
    ids = [s.slot_id for s in second.payload.timeline_slots if not s.removed]
    assert ids == ["s3", "s2", "s4"]
    assert _bar(second, "m0") is None
    assert (_bar(second, "m2")["start_s"], _bar(second, "m2")["end_s"]) == (0.0, 2.0)
    assert (_bar(second, "m1")["start_s"], _bar(second, "m1")["end_s"]) == (2.0, 4.0)
    assert (_bar(second, "m3")["start_s"], _bar(second, "m3")["end_s"]) == (4.0, 6.0)
    assert "Removed label for clip 2" in second.changes


def test_two_turn_split_child_keeps_label_span(guided) -> None:
    job, variant, _rev = guided
    first = compile_editor_ops(job, variant, [{"op": "split_clip", "slot_index": 0, "at_s": 0.8}])
    projected = project_editor_draft(
        variant, first.payload.model_dump(mode="json", exclude_none=True), job
    )
    second = compile_editor_ops(
        job, projected, [{"op": "reorder_clip", "from_index": 1, "to_index": 0}]
    )
    # The right child moved first; the m0 label spans the adjacent children only
    # when adjacent, else the first child: here they are adjacent (0, 1 swapped).
    bar = _bar(second, "m0")
    assert bar is not None and bar["end_s"] - bar["start_s"] == pytest.approx(2.0)


def test_visual_media_stays_withheld_on_guided(guided) -> None:
    job, variant, _rev = guided
    assert "visual_media" not in build_editor_snapshot(job, variant)["allowed_op_families"]


def _v2_snapshot(job, variant):
    return build_editor_snapshot(job, variant)


def test_parser_accepts_and_rejects_bulk_shapes(guided) -> None:
    job, variant, _rev = guided
    snapshot = _v2_snapshot(job, variant)
    good = {
        "op": "patch_slots",
        "selector": {"all": True},
        "patch": {"transition_after": "flash", "transition_duration_s": 0.2},
    }
    assert _parse_op(good, snapshot, _ParseState(0.9))["patch"]["transition_after"] == "flash"
    for bad in (
        {
            "op": "patch_slots",
            "selector": {"all": True, "slot_indexes": [0]},
            "patch": {"duration_s": 2},
        },
        {"op": "patch_slots", "selector": {"slot_indexes": [9]}, "patch": {"duration_s": 2}},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {"look_preset": "sepia"}},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {"playback_rate": 9}},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {"transition_duration_s": 0.5}},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {}},
        {"op": "patch_slots", "selector": {"clip_ids": ["nope"]}, "patch": {"duration_s": 2}},
        {"op": "set_total_duration", "target_s": 0.2},
        {"op": "set_total_duration", "target_s": 10, "strategy": "wobble"},
    ):
        assert _parse_op(bad, snapshot, _ParseState(0.9)) is None, bad
    plain = {k: v for k, v in snapshot.items() if k != "editor_ops_version"}
    assert _parse_op(good, plain, _ParseState(0.9)) is None


def test_device_variant_refuses_looks_and_speed(guided) -> None:
    job, variant, _rev = guided
    device = {**copy.deepcopy(variant), "render_destination": "device"}
    for patch in ({"look_preset": "golden_hour"}, {"playback_rate": 2}):
        with pytest.raises(KriaEditorOpError, match="not available"):
            compile_editor_ops(
                job,
                device,
                [{"op": "patch_slots", "selector": {"all": True}, "patch": patch}],
            )


def test_last_clip_transition_is_ignored(guided) -> None:
    job, variant, _rev = guided
    with pytest.raises(KriaEditorOpError, match="no transition after"):
        compile_editor_ops(
            job,
            variant,
            [
                {
                    "op": "patch_slots",
                    "selector": {"slot_indexes": [3]},
                    "patch": {"transition_after": "crossfade"},
                }
            ],
        )


def test_too_long_timeline_is_a_clean_op_error(guided) -> None:
    job, variant, _rev = guided
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(
            job,
            variant,
            [
                {
                    "op": "patch_slots",
                    "selector": {"all": True},
                    "patch": {"in_s": 9.9, "duration_s": 5},
                }
            ],
        )
