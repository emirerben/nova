"""Provider inputs describe executable editor operations without claiming a schema."""

import json

import pytest

from app.agents.edit_copilot import format_editor_snapshot
from app.agents.editor_ops_v2.capabilities import (
    build_editor_capability_manifest,
    serialize_editor_capabilities,
)


def test_provider_catalog_is_present_and_reports_version_refusal():
    snapshot = {"editor_ops_version": 2, "allowed_op_families": ["text", "timeline"]}
    manifest = build_editor_capability_manifest(snapshot)
    assert manifest["max_ops"] == 16
    assert {row["name"] for row in manifest["operations"]} == {
        "set_total_duration",
        "retime_text_sequence",
        "patch_text",
        "reorder_clips_by",
        "set_texts_timing",
        "replace_text_sequence",
    }
    assert all(row["availability"] for row in manifest["operations"])
    assert serialize_editor_capabilities(snapshot) in format_editor_snapshot(snapshot)
    assert json.loads(serialize_editor_capabilities(snapshot)) == manifest
    legacy = build_editor_capability_manifest({"allowed_op_families": ["text", "timeline"]})
    assert all(not row["availability"] for row in legacy["operations"])


@pytest.mark.parametrize("target", [12, 20])
def test_composed_timing_spatial_style_and_shuffle_use_real_parser_compiler(monkeypatch, target):
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
    from tests.services.test_duration_text_preservation import sequence

    job, variant = sequence(monkeypatch)
    snapshot = build_editor_snapshot(job, variant)
    ids = [row["id"] for row in variant["text_elements"]]
    before = [(row["id"], row["text"]) for row in variant["text_elements"]]
    raw = [
        {"op": "set_total_duration", "target_s": target},
        {
            "op": "retime_text_sequence",
            "selector": {"ids": ids},
            "start_s": 0,
            "end_s": target,
            "fit_to_video": True,
            "gap_policy": "scale",
        },
        {
            "op": "patch_text",
            "selector": {"ids": ids},
            "patch": {"x_frac": 0.3, "y_frac": 0.35, "alignment": "left", "color": "#FF0000"},
        },
        {"op": "reorder_clips_by", "criterion": "random"},
    ]

    def parse():
        state = EditorOperationParseState(confidence=0.99)
        result = [parse_editor_operation(op, snapshot, state) for op in raw]
        assert all(result), state
        return result

    parsed = parse()
    assert parsed == parse()
    compiled = compile_editor_ops(job, variant, parsed)
    bars = compiled.payload.text_elements
    assert [(row["id"], row["text"]) for row in bars] == before
    assert bars[0]["start_s"] == 0
    assert bars[-1]["end_s"] == pytest.approx(target)
    assert all(row["x_frac"] == 0.3 and row["y_frac"] == 0.35 for row in bars)
    assert all(row["alignment"] == "left" and row["color"] == "#FF0000" for row in bars)
    slots = compiled.payload.timeline_slots
    assert sum(row.duration_s for row in slots) == pytest.approx(target)
    assert len({row.slot_id for row in slots}) == len(slots) == 28


def test_ten_clip_shuffle_is_a_retry_stable_complete_permutation(monkeypatch):
    import uuid

    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.config import settings
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
    from tests.services._guided_timeline_fixtures import arm_guided, guided_job, guided_revision

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id, count=10, duration_s=2)
    job, variant = guided_job(revision, [], job_id)
    arm_guided(monkeypatch, revision)
    snapshot = build_editor_snapshot(job, variant)
    results = []
    for _ in range(2):
        op = parse_editor_operation(
            {"op": "reorder_clips_by", "criterion": "random"},
            snapshot,
            EditorOperationParseState(confidence=0.99),
        )
        assert op is not None
        results.append(compile_editor_ops(job, variant, [op]).payload.timeline_slots)
    assert results[0] == results[1]
    assert {row.slot_id for row in results[0]} == {f"s{i + 1}" for i in range(10)}
    assert len(results[0]) == 10


@pytest.mark.parametrize(
    "raw",
    [
        {"op": "execute_code", "code": "arbitrary"},
        {"op": "set_total_duration", "target_s": 1000000},
        {"op": "reorder_clips_by", "criterion": "custom", "order": [0, 0]},
        {"op": "patch_text", "selector": {"ids": ["missing"]}, "patch": {"color": "#FF0000"}},
    ],
)
def test_discovery_does_not_bypass_parser_refusal(monkeypatch, raw):
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.services.kria_editor_ops import build_editor_snapshot
    from tests.services.test_duration_text_preservation import sequence

    job, variant = sequence(monkeypatch)
    snapshot = build_editor_snapshot(job, variant)
    assert parse_editor_operation(raw, snapshot, EditorOperationParseState(confidence=0.99)) is None


def test_composition_catalog_does_not_increase_server_operation_budget(monkeypatch):
    from unittest.mock import MagicMock

    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.services.kria_editor_ops import build_editor_snapshot
    from tests.services.test_duration_text_preservation import sequence

    job, variant = sequence(monkeypatch)
    snapshot = build_editor_snapshot(job, variant)
    result = EditCopilotAgent(MagicMock()).parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 0.99,
                "reply": "Proposed changes",
                "ops": [{"op": "set_total_duration", "target_s": 20}] * 17,
            }
        ),
        EditCopilotInput(utterance="Make the video 20 seconds", variant_snapshot=snapshot),
    )
    assert len(result.ops) <= 16


def test_actual_model_prompt_contains_the_common_serialized_catalog():
    from unittest.mock import MagicMock

    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput

    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["text", "timeline"],
        "max_duration_s": 240,
        "text_bars": [{"id": "opening::sequence-1", "text": "Keep this exact word"}],
    }
    prompt = EditCopilotAgent(MagicMock()).render_prompt(
        EditCopilotInput(utterance="Move the exact wording left", variant_snapshot=snapshot)
    )
    assert serialize_editor_capabilities(snapshot) in prompt
    assert "Move the exact wording left" in prompt
    assert "opening::sequence-1" in prompt


@pytest.mark.parametrize("enabled", [False, True])
def test_sequence_mode_weight_discovery_matches_actual_parser_flag(monkeypatch, enabled):
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.config import settings

    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", enabled)
    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["text"],
        "text_bars": [{"id": "opening", "text": "Keep every word", "start_s": 0, "end_s": 12}],
    }
    descriptor = next(
        row
        for row in build_editor_capability_manifest(snapshot)["operations"]
        if row["name"] == "replace_text_sequence"
    )
    assert descriptor["constraints"]["mode_weight_honored"] is enabled
    parsed = parse_editor_operation(
        {
            "op": "replace_text_sequence",
            "selector": {"ids": ["opening"]},
            "segments": ["Keep", "every", "word"],
            "mode": "cumulative",
            "weight": "length",
        },
        snapshot,
        EditorOperationParseState(confidence=0.99),
    )
    if enabled:
        assert parsed is not None
        assert parsed["mode"] == "cumulative" and parsed["weight"] == "length"
    else:
        assert parsed is None
        assert descriptor["constraints"]["effective_mode"] == "replace"
        assert descriptor["constraints"]["effective_weight"] == "equal"


def test_explicit_preserve_refuses_to_split_or_retime_the_original_opening(monkeypatch):
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
    from tests.services.test_duration_text_preservation import sequence

    job, variant = sequence(monkeypatch)
    opening = dict(
        variant["text_elements"][0], id="opening", text="Keep every exact word", end_s=10
    )
    variant["text_elements"] = [opening]
    snapshot = build_editor_snapshot(job, variant)
    raw = [
        {"op": "set_total_duration", "target_s": 20, "preserve_text": True},
        {"op": "set_texts_timing", "selector": {"ids": ["opening"]}, "start_s": 0, "end_s": 20},
        {
            "op": "replace_text_sequence",
            "selector": {"ids": ["opening"]},
            "segments": ["Keep", "every", "exact", "word"],
        },
    ]
    parsed = [
        parse_editor_operation(op, snapshot, EditorOperationParseState(confidence=0.99))
        for op in raw
    ]
    assert all(parsed)
    compiled = compile_editor_ops(job, variant, parsed)
    assert compiled.payload.text_elements == [opening]
    refused = parse_editor_operation(
        {
            "op": "replace_text_sequence",
            "selector": {"ids": ["opening"]},
            "segments": ["Replacement", "wording"],
        },
        snapshot,
        EditorOperationParseState(confidence=0.99),
    )
    assert refused is None


@pytest.mark.parametrize(
    "extra",
    [{"mode": "cumulative"}, {"weight": "length"}, {"mode": "unknown"}, {"weight": "unknown"}],
)
def test_disabled_sequence_options_refuse_instead_of_silently_using_defaults(monkeypatch, extra):
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.config import settings

    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", False)
    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["text"],
        "text_bars": [{"id": "opening", "text": "Keep every word", "start_s": 0, "end_s": 12}],
    }
    state = EditorOperationParseState(confidence=0.99)
    assert (
        parse_editor_operation(
            {
                "op": "replace_text_sequence",
                "selector": {"ids": ["opening"]},
                "segments": ["Keep", "every", "word"],
                **extra,
            },
            snapshot,
            state,
        )
        is None
    )
    assert any(r["reason"] == "capability_unavailable" for r in state.rejection_reasons)
