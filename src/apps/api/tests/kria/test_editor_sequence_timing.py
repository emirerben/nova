"""Typed timing contracts; language interpretation belongs to the existing model."""

from __future__ import annotations

import copy

import pytest

from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
from app.kria.editor_sequence_timing import (
    SequenceTimingIntent,
    sequence_timing_plan,
    sequence_timing_plan_from_operations,
    timing_only_operations,
)
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.services.test_duration_text_preservation import sequence


def parsed_operations(raw, snapshot):
    state = EditorOperationParseState(1.0)
    parsed = [parse_editor_operation(op, snapshot, state) for op in raw]
    assert all(op is not None for op in parsed)
    return parsed


@pytest.mark.parametrize("target", [None, 12, 20])
@pytest.mark.parametrize("gap_policy", ["scale", "close", "fixed"])
def test_typed_fill_is_one_atomic_existing_word_edit(monkeypatch, target, gap_policy):
    job, variant = sequence(monkeypatch, end_s=12.876)
    before = copy.deepcopy(variant["text_elements"])
    snapshot = build_editor_snapshot(job, variant)
    plan = sequence_timing_plan(SequenceTimingIntent("fill", target, gap_policy), snapshot)
    assert plan is not None and plan.operations
    assert len(plan.operations) == (1 if target is None else 2)
    assert plan.operations[-1]["op"] == "retime_text_sequence"
    operations = parsed_operations(plan.operations, snapshot)
    compiled = compile_editor_ops(job, variant, operations)
    rows = compiled.payload.text_elements
    assert [row["id"] for row in rows] == [row["id"] for row in before]
    assert [row["text"] for row in rows] == [row["text"] for row in before]
    assert rows[0]["start_s"] == 0
    assert rows[-1]["end_s"] == (target or 56)
    assert all(row["end_s"] - row["start_s"] >= 0.25 for row in rows)
    assert all(left["end_s"] <= right["start_s"] for left, right in zip(rows, rows[1:]))
    for old, new in zip(before, rows, strict=True):
        for key, value in old.items():
            if key not in {"start_s", "end_s", "source_params"}:
                assert new[key] == value
    derived = sequence_timing_plan_from_operations(operations, snapshot)
    assert derived is not None and derived.intent == plan.intent
    assert derived.target_ids == plan.target_ids
    assert timing_only_operations(operations)
    assert variant["text_elements"] == before


@pytest.mark.parametrize("target", [12.9, 20])
def test_typed_duration_preserves_current_absolute_word_timing(monkeypatch, target):
    job, variant = sequence(monkeypatch, end_s=12.876)
    before = copy.deepcopy(variant["text_elements"])
    snapshot = build_editor_snapshot(job, variant)
    plan = sequence_timing_plan(SequenceTimingIntent("preserve", target), snapshot)
    assert plan is not None and len(plan.operations) == 1
    assert plan.operations[0]["op"] == "set_total_duration"
    compiled = compile_editor_ops(job, variant, list(plan.operations))
    assert compiled.payload.text_elements == before
    derived = sequence_timing_plan_from_operations(list(plan.operations), snapshot)
    assert derived is not None and derived.intent.mode == "preserve"
    assert derived.intent.target_s == target


def test_typed_duration_refuses_to_drop_words_that_do_not_fit(monkeypatch):
    job, variant = sequence(monkeypatch, end_s=12.876)
    before = copy.deepcopy(variant)
    plan = sequence_timing_plan(
        SequenceTimingIntent("preserve", 12), build_editor_snapshot(job, variant)
    )
    assert plan is not None
    with pytest.raises(ValueError, match="12.876"):
        compile_editor_ops(job, variant, list(plan.operations))
    assert variant == before


@pytest.mark.parametrize("group_count", [0, 2])
def test_fill_requires_one_unambiguous_complete_group(monkeypatch, group_count):
    job, variant = sequence(monkeypatch)
    if group_count == 0:
        variant["text_elements"] = []
    else:
        extra = copy.deepcopy(variant["text_elements"][0])
        extra["id"] = "other::sequence-1"
        extra["source_params"]["sequence_source_id"] = "other"
        variant["text_elements"].append(extra)
    before = copy.deepcopy(variant)
    plan = sequence_timing_plan(SequenceTimingIntent("fill"), build_editor_snapshot(job, variant))
    assert plan is not None and not plan.operations and plan.reply
    assert variant == before


def test_fill_uses_ordinal_word_order_not_snapshot_array_order(monkeypatch):
    job, variant = sequence(monkeypatch)
    ids = [row["id"] for row in variant["text_elements"]]
    variant["text_elements"].reverse()
    plan = sequence_timing_plan(
        SequenceTimingIntent("fill", 20), build_editor_snapshot(job, variant)
    )
    assert plan is not None and plan.target_ids == tuple(ids)
    result = compile_editor_ops(job, variant, list(plan.operations))
    by_id = {row["id"]: row for row in result.payload.text_elements}
    assert by_id[ids[0]]["start_s"] == 0
    assert by_id[ids[-1]]["end_s"] == 20
    assert all(by_id[left]["end_s"] <= by_id[right]["start_s"] for left, right in zip(ids, ids[1:]))


def test_group_fit_preserves_an_unmentioned_independent_layer(monkeypatch):
    job, variant = sequence(monkeypatch)
    layer = {
        "id": "independent-brand",
        "text": "Brand",
        "start_s": 2,
        "end_s": 5,
        "position": "custom",
        "x_frac": 0.15,
        "y_frac": 0.2,
    }
    variant["text_elements"].append(copy.deepcopy(layer))
    plan = sequence_timing_plan(
        SequenceTimingIntent("fill", 20), build_editor_snapshot(job, variant)
    )
    assert plan is not None and plan.operations
    result = compile_editor_ops(job, variant, list(plan.operations))
    assert next(row for row in result.payload.text_elements if row["id"] == layer["id"]) == layer


def test_partial_group_from_model_is_refused_without_dropping_other_words(monkeypatch):
    job, variant = sequence(monkeypatch)
    before = copy.deepcopy(variant)
    snapshot = build_editor_snapshot(job, variant)
    operations = parsed_operations(
        [
            {
                "op": "retime_text_sequence",
                "selector": {"bar_indexes": [0, 1]},
                "start_s": 0,
                "end_s": 20,
                "fit_to_video": True,
            }
        ],
        snapshot,
    )
    with pytest.raises(ValueError, match="complete"):
        compile_editor_ops(job, variant, operations)
    assert variant == before


def test_compound_style_request_retains_its_style_operation(monkeypatch):
    job, variant = sequence(monkeypatch)
    snapshot = build_editor_snapshot(job, variant)
    plan = sequence_timing_plan(SequenceTimingIntent("fill", 20), snapshot)
    operations = parsed_operations(
        [
            *plan.operations,
            {"op": "patch_text", "selector": {"bar_indexes": [0]}, "patch": {"size_scale": 1.2}},
        ],
        snapshot,
    )
    assert not timing_only_operations(operations)
    derived = sequence_timing_plan_from_operations(operations, snapshot)
    assert derived is not None and derived.intent.mode == "fill"
    compiled = compile_editor_ops(job, variant, operations)
    assert compiled.payload.text_elements[0]["size_px"] == pytest.approx(76 * 1.2)
    assert compiled.payload.text_elements[-1]["end_s"] == 20
    assert len(compiled.payload.text_elements) == 27


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize(
    "operation",
    [
        {"op": "set_total_duration", "target_s": 20},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {"duration_s": 1}},
    ],
)
def test_duration_parser_retains_strict_preservation_boolean(monkeypatch, value, operation):
    job, variant = sequence(monkeypatch)
    parsed = parse_editor_operation(
        {**operation, "preserve_text": value},
        build_editor_snapshot(job, variant),
        EditorOperationParseState(1.0),
    )
    assert parsed is not None
    if value:
        assert parsed["preserve_text"] is True


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
@pytest.mark.parametrize(
    "operation",
    [
        {"op": "set_total_duration", "target_s": 20},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {"duration_s": 1}},
    ],
)
def test_duration_parser_rejects_nonboolean_preservation(monkeypatch, value, operation):
    job, variant = sequence(monkeypatch)
    assert (
        parse_editor_operation(
            {**operation, "preserve_text": value},
            build_editor_snapshot(job, variant),
            EditorOperationParseState(1.0),
        )
        is None
    )


@pytest.mark.parametrize(
    "operations,expected",
    [
        ([], False),
        ([{"op": "set_total_duration", "target_s": 20}], True),
        ([{"op": "patch_slots", "patch": {"duration_s": 2}}], True),
        ([{"op": "patch_slots", "patch": {}}], False),
        ([{"op": "patch_slots", "patch": {"duration_s": 2, "look_preset": "none"}}], False),
        ([{"op": "set_total_duration", "target_s": 20}, {"op": "patch_text"}], False),
    ],
)
def test_timing_identity_guard_excludes_compound_operations(operations, expected):
    assert timing_only_operations(operations) is expected


def test_explicit_sequence_interval_is_not_promoted_to_whole_video(monkeypatch):
    job, variant = sequence(monkeypatch)
    snapshot = build_editor_snapshot(job, variant)
    operations = parsed_operations(
        [
            {
                "op": "retime_text_sequence",
                "selector": {"group": "title"},
                "start_s": 3,
                "end_s": 15,
                "fit_to_video": False,
            }
        ],
        snapshot,
    )
    assert sequence_timing_plan_from_operations(operations, snapshot).span_s == (3, 15)
    compiled = compile_editor_ops(job, variant, operations)
    assert compiled.payload.text_elements[0]["start_s"] == 3
    assert compiled.payload.text_elements[-1]["end_s"] == 15


@pytest.mark.parametrize("brief_preserve", [False, True])
def test_preservation_precedes_conflicting_whole_sequence_operation(monkeypatch, brief_preserve):
    job, variant = sequence(monkeypatch, end_s=12.876)
    rows = copy.deepcopy(variant["text_elements"])
    snapshot = build_editor_snapshot(job, variant)
    plan = sequence_timing_plan(SequenceTimingIntent("fill", 20), snapshot)
    operations = copy.deepcopy(list(plan.operations))
    if not brief_preserve:
        operations[0]["preserve_text"] = True
    actual = sequence_timing_plan_from_operations(
        operations, snapshot, preserve_text=brief_preserve
    )
    assert actual.intent.mode == "preserve"
    compiled = compile_editor_ops(job, variant, operations, preserve_text=brief_preserve)
    assert compiled.payload.text_elements == rows


def test_explicit_sequence_selection_reconciles_among_multiple_groups(monkeypatch):
    job, variant = sequence(monkeypatch)
    original = copy.deepcopy(variant["text_elements"])
    other = copy.deepcopy(original[0])
    other["id"] = "other::sequence-1"
    other["source_params"]["sequence_source_id"] = "other"
    variant["text_elements"].append(other)
    snapshot = build_editor_snapshot(job, variant)
    operations = parsed_operations(
        [
            {
                "op": "retime_text_sequence",
                "selector": {"ids": [r["id"] for r in original]},
                "start_s": 0,
                "end_s": 56,
                "fit_to_video": True,
            }
        ],
        snapshot,
    )
    plan = sequence_timing_plan_from_operations(operations, snapshot)
    assert plan is not None and plan.source_id == "opening"
    assert set(plan.target_ids) == {r["id"] for r in original}
    compiled = compile_editor_ops(job, variant, operations)
    assert next(r for r in compiled.payload.text_elements if r["id"] == other["id"]) == other


def test_bounded_retime_replaces_whole_video_policy_with_actual_span(monkeypatch):
    from app.kria.brief import BriefRequirement, CreativeBrief, apply_updates
    from app.kria.brief_checks import PlanFacts, check_requirement
    from app.kria.editor_sequence_timing import merge_sequence_brief_updates

    job, variant = sequence(monkeypatch, end_s=12.876)
    original = copy.deepcopy(variant["text_elements"])
    snapshot = build_editor_snapshot(job, variant)
    operations = parsed_operations(
        [
            {"op": "set_total_duration", "target_s": 20},
            {
                "op": "retime_text_sequence",
                "selector": {"group": "title"},
                "start_s": 0,
                "end_s": 12,
                "fit_to_video": False,
            },
        ],
        snapshot,
    )
    plan = sequence_timing_plan_from_operations(operations, snapshot)
    assert plan.span_s == (0, 12)
    prior = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Fill the video",
                facts={
                    "sequence_timing": {
                        "mode": "fill",
                        "source_id": "opening",
                        "target_ids": [row["id"] for row in original],
                    }
                },
            )
        ],
    )
    compiled = compile_editor_ops(job, variant, operations)
    final = apply_updates(
        prior,
        merge_sequence_brief_updates(
            prior, [], plan, snapshot, "Retime text to 12 seconds in the 20 second video"
        ),
        source_turn_id="turn",
    )
    policy = next(req for req in final.live() if req.facts.get("sequence_timing"))
    assert policy.id == "r1"
    assert policy.facts["sequence_timing"]["mode"] == "range"

    def facts(rows):
        return PlanFacts(
            editor=True,
            duration_s=20,
            text_spans=tuple(
                (row["id"], row.get("role", ""), row["start_s"], row["end_s"]) for row in rows
            ),
        )

    assert check_requirement(policy, facts(compiled.payload.text_elements)).status == "met"
    assert check_requirement(policy, facts(original)).status == "partial"
