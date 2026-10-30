"""Duration requests must not rewrite an independent authored text clock."""

from __future__ import annotations

import copy
import uuid

import pytest

from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from tests.services._guided_timeline_fixtures import arm_guided, guided_job, guided_revision


def sequence(monkeypatch, *, end_s=10.8):
    from app.config import settings

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    job_id = str(uuid.uuid4())
    rows = [
        {
            "id": f"opening::sequence-{i + 1}",
            "text": f"word{i + 1}",
            "start_s": round(i * end_s / 27, 6),
            "end_s": round((i + 1) * end_s / 27, 6),
            "role": "generative_intro",
            "position": "middle",
            "font_family": "Playfair Display",
            "size_px": 76 if i != 2 else 111,
            "max_width_frac": 0.68,
            "source_params": {"sequence_source_id": "opening"},
        }
        for i in range(27)
    ]
    revision = guided_revision(job_id, count=28, duration_s=2, text_elements=rows)
    job, variant = guided_job(revision, rows, job_id)
    arm_guided(monkeypatch, revision)
    return job, variant


@pytest.mark.parametrize("target", [20, 12])
def test_duration_only_keeps_every_word_property(monkeypatch, target):
    job, variant = sequence(monkeypatch)
    before = copy.deepcopy(variant["text_elements"])
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_total_duration", "target_s": target, "strategy": "proportional"}]
    )
    assert compiled.payload.text_elements == before
    assert variant["text_elements"] == before
    assert sum(s.duration_s for s in compiled.payload.timeline_slots) == pytest.approx(
        target, abs=0.000001
    )


def test_nonfitting_sequence_asks_before_mutation(monkeypatch):
    job, variant = sequence(monkeypatch, end_s=12.876)
    before = copy.deepcopy(variant)
    with pytest.raises(ValueError, match="12.876"):
        compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 12}])
    assert variant == before


def test_revert_then_second_duration_uses_restored_text(monkeypatch):
    job, variant = sequence(monkeypatch)
    before = copy.deepcopy(variant["text_elements"])
    first = compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 20}])
    staged = project_editor_draft(
        variant, first.payload.model_dump(mode="json", exclude_none=True), job
    )
    assert staged["text_elements"] == before
    restored = copy.deepcopy(variant)
    second = compile_editor_ops(job, restored, [{"op": "set_total_duration", "target_s": 12}])
    assert second.payload.text_elements == before


def test_duration_preserves_intentional_independent_layers(monkeypatch):
    job, variant = sequence(monkeypatch)
    rows = [
        dict(variant["text_elements"][0], id="layer-a", text="Heading", start_s=0, end_s=5),
        dict(variant["text_elements"][1], id="layer-b", text="Subheading", start_s=0, end_s=5),
    ]
    for row in rows:
        row.pop("source_params")
    variant["text_elements"] = rows
    compiled = compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 20}])
    assert compiled.payload.text_elements == rows


def test_explicit_retime_remains_available(monkeypatch):
    job, variant = sequence(monkeypatch)
    before = copy.deepcopy(variant["text_elements"])
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "set_texts_timing",
                "selector": {"group": "all"},
                "target_ids": [r["id"] for r in before],
                "expected_count": 27,
                "shift_s": 1,
            }
        ],
    )
    assert [r["start_s"] for r in compiled.payload.text_elements] == [
        round(r["start_s"] + 1, 3) for r in before
    ]


def test_explicit_preservation_ignores_incidental_model_text_operations(monkeypatch):
    job, variant = sequence(monkeypatch)
    before = copy.deepcopy(variant["text_elements"])
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "remove_texts",
                "selector": {"group": "all"},
                "target_ids": [r["id"] for r in before],
                "expected_count": 27,
            },
            {"op": "add_text", "text": "Unrequested replacement", "start_s": 0, "end_s": 1},
            {"op": "set_total_duration", "target_s": 20},
        ],
        preserve_text=True,
    )
    assert compiled.payload.text_elements == before
    assert not compiled.text_diff


def retime(rows, end_s):
    return {
        "op": "retime_text_sequence",
        "selector": {"group": "all"},
        "target_ids": [r["id"] for r in rows],
        "expected_count": len(rows),
        "start_s": 0,
        "end_s": end_s,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_one_operation_retimes_every_word_without_replacement(monkeypatch, reverse):
    job, variant = sequence(monkeypatch, end_s=12.876)
    before = copy.deepcopy(variant["text_elements"])
    operations = [{"op": "set_total_duration", "target_s": 12}, retime(before, 12)]
    if reverse:
        operations.reverse()
    result = compile_editor_ops(job, variant, operations)
    after = result.payload.text_elements
    assert [r["id"] for r in after] == [r["id"] for r in before]
    for old, new in zip(before, after, strict=True):
        for key, value in (old.get("source_params") or {}).items():
            assert (new.get("source_params") or {})[key] == value
        assert {k: v for k, v in old.items() if k not in {"start_s", "end_s", "source_params"}} == {
            k: v for k, v in new.items() if k not in {"start_s", "end_s", "source_params"}
        }
    assert after[0]["start_s"] == 0
    assert after[-1]["end_s"] == 12
    assert all(a["end_s"] == b["start_s"] for a, b in zip(after, after[1:]))
    staged = project_editor_draft(
        variant, result.payload.model_dump(mode="json", exclude_none=True), job
    )
    repeated = compile_editor_ops(job, staged, [retime(before, 12)])
    assert repeated.payload.text_elements == after


def test_retime_rejects_unreadable_window_and_keeps_input(monkeypatch):
    job, variant = sequence(monkeypatch)
    before = copy.deepcopy(variant)
    with pytest.raises(ValueError, match="6.75"):
        compile_editor_ops(job, variant, [retime(variant["text_elements"], 6)])
    assert variant == before


def test_sequence_cannot_be_stacked_by_bulk_absolute_timing(monkeypatch):
    job, variant = sequence(monkeypatch)
    op = retime(variant["text_elements"], 4)
    op["op"] = "set_texts_timing"
    with pytest.raises(ValueError, match="overlap"):
        compile_editor_ops(job, variant, [op])


def test_sequence_cannot_extend_past_final_duration(monkeypatch):
    job, variant = sequence(monkeypatch)
    with pytest.raises(ValueError, match="fit"):
        compile_editor_ops(job, variant, [retime(variant["text_elements"], 60)])


def test_sequence_retime_uses_original_identity_order_not_corrupted_times(monkeypatch):
    job, variant = sequence(monkeypatch)
    variant["text_elements"].reverse()
    for row in variant["text_elements"]:
        row["start_s"], row["end_s"] = 0, 0.5
    result = compile_editor_ops(job, variant, [retime(variant["text_elements"], 12)])
    rows = {row["id"]: row for row in result.payload.text_elements}
    assert rows["opening::sequence-1"]["start_s"] == 0
    assert rows["opening::sequence-27"]["end_s"] == 12
    assert rows["opening::sequence-9"]["end_s"] == rows["opening::sequence-10"]["start_s"]


def test_unknown_sequence_order_is_not_guessed(monkeypatch):
    job, variant = sequence(monkeypatch)
    variant["text_elements"][0]["id"] = "unknown-word-id"
    with pytest.raises(ValueError, match="word order"):
        compile_editor_ops(job, variant, [retime(variant["text_elements"], 12)])


def test_sequence_retime_is_rejected_by_an_older_handler_table(monkeypatch):
    from app.services import kria_editor_ops as compiler

    job, variant = sequence(monkeypatch)
    compiler._load_lane_handlers()
    monkeypatch.setattr(compiler, "_load_lane_handlers", lambda: None)
    monkeypatch.delitem(compiler._OP_HANDLERS, "retime_text_sequence")
    before = copy.deepcopy(variant)
    with pytest.raises(ValueError, match="not portable"):
        compile_editor_ops(job, variant, [retime(variant["text_elements"], 12)])
    assert variant == before


def test_existing_sequence_overlap_allows_unchanged_duration_edit(monkeypatch):
    job, variant = sequence(monkeypatch)
    rows = variant["text_elements"]
    rows[1]["start_s"] = rows[0]["start_s"]
    rows[1]["end_s"] = rows[0]["end_s"]
    before = copy.deepcopy(rows)
    compiled = compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 20}])
    assert compiled.payload.text_elements == before


def test_sequence_collision_cannot_worsen_an_existing_overlap():
    from app.services.kria_editor_ops_text import validate_sequence_collisions

    before = [
        {"id": "s::sequence-1", "sequence_source_id": "s", "start_s": 0, "end_s": 1},
        {"id": "s::sequence-2", "sequence_source_id": "s", "start_s": 0.5, "end_s": 2},
    ]
    after = copy.deepcopy(before)
    after[1]["start_s"] = 0
    with pytest.raises(ValueError, match="overlap"):
        validate_sequence_collisions(after, before)


def test_duration_edit_keeps_explicit_clip_pin_binding(monkeypatch):
    job, variant = sequence(monkeypatch)
    variant["text_elements"] = [
        {
            "id": "guided-pinned-0",
            "text": "Part 1",
            "start_s": 2.0,
            "end_s": 4.0,
            "segment_id": "s2",
            "position": "middle",
        }
    ]
    compiled = compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 20}])
    slots = compiled.payload.timeline_slots
    pin = compiled.payload.text_elements[0]
    assert pin["start_s"] == pytest.approx(slots[0].duration_s, abs=0.000001)
    assert pin["end_s"] == pytest.approx(slots[0].duration_s + slots[1].duration_s, abs=0.000001)


def test_source_window_clock_does_not_round_past_actual_source(monkeypatch):
    from types import SimpleNamespace

    from app.agents.editor_ops_v2.timeline import _write_durations

    duration = 13 / 30
    state = SimpleNamespace(
        slots=[
            {
                "slot_id": f"s{i}",
                "clip_index": i,
                "media_kind": "video",
                "in_s": 0,
                "duration_s": duration,
                "source_duration_s": duration,
                "source_end_s": duration,
            }
            for i in range(3)
        ],
        changed=set(),
    )
    _write_durations(state, [0, 1, 2], [duration] * 3)
    assert all(row["source_end_s"] <= duration + 0.000001 for row in state.slots)
    assert sum(row["duration_s"] for row in state.slots) == pytest.approx(1.3, abs=0.000002)


def test_source_capped_duration_edit_projects_without_invalid_source_window(monkeypatch):
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id, count=3, duration_s=13 / 30, source_duration_s=13 / 30)
    for segment in revision["segments"]:
        segment["preserve_source_window"] = True
    job, variant = guided_job(revision, [], job_id)
    arm_guided(monkeypatch, revision)
    compiled = compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 1.3}])
    projected = project_editor_draft(
        variant, compiled.payload.model_dump(mode="json", exclude_none=True), job
    )
    assert len(projected["guided_draft_slots"]) == 3
    assert all(row["source_end_s"] <= 13 / 30 + 0.000001 for row in projected["guided_draft_slots"])
    assert sum(row["duration_s"] for row in projected["guided_draft_slots"]) == pytest.approx(
        1.3, abs=0.000002
    )


def test_unreadable_sequence_returns_a_typed_minimum_instead_of_a_retry_loop(monkeypatch):
    from app.services.kria_editor_ops import TextTimingConflict

    job, variant = sequence(monkeypatch)
    with pytest.raises(TextTimingConflict) as failure:
        compile_editor_ops(job, variant, [retime(variant["text_elements"], 5)])
    assert failure.value.constraint == "readability"
    assert failure.value.required_duration_s == pytest.approx(6.75)
    assert len(failure.value.conflicting_text_ids) == 27
    from app.tasks.kria_runtime import _text_timing_conflict_plan

    question = _text_timing_conflict_plan(failure.value)
    assert question.turn_value == "question"
    assert "6.8" in question.response
    assert "longer video" in question.response
    assert "shorten the text" in question.response
    assert not question.intents
    assert not question.reply_options


def test_unreachable_numeric_length_does_not_silently_fill_a_shorter_video(monkeypatch):
    from app.config import settings
    from app.services.kria_editor_ops import TextTimingConflict

    job_id = str(uuid.uuid4())
    rows = [
        {
            "id": f"opening::sequence-{i + 1}",
            "text": word,
            "start_s": i * 0.5,
            "end_s": (i + 1) * 0.5,
            "source_params": {"sequence_source_id": "opening"},
        }
        for i, word in enumerate(("one", "two"))
    ]
    revision = guided_revision(
        job_id, count=1, duration_s=1, source_duration_s=1.02, text_elements=rows
    )
    job, variant = guided_job(revision, rows, job_id)
    arm_guided(monkeypatch, revision)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    op = {**retime(rows, 1.04), "fit_to_video": True}
    before = copy.deepcopy(variant)
    with pytest.raises(TextTimingConflict) as failure:
        compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 1.04}, op])
    assert failure.value.constraint == "duration"
    assert failure.value.required_duration_s == pytest.approx(1)
    assert variant == before
