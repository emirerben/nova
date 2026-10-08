from __future__ import annotations

import json

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.services.test_kria_editor_ops import _job, _variant


def _parse(snapshot: dict, operation: dict):
    raw = json.dumps({"intent": "edit", "ops": [operation], "confidence": 1.0, "reply": "Done."})
    return EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance="speed up clip 2", variant_snapshot=snapshot)
    )


def test_snapshot_projects_field_capability_and_rejects_legacy_speed(monkeypatch):
    variant = _variant()
    job = _job(variant)
    job.id = "eval"
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"text_elements": True, "timeline": True, "clips": {"playback_rate": False}},
    )
    snapshot = build_editor_snapshot(job, variant)
    assert snapshot["timeline_patch_capabilities"]["playback_rate"] is False
    parsed = _parse(
        snapshot,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"playback_rate": 2}},
    )
    assert parsed.ops == []
    assert parsed.outcome in {"unsupported", "rejected"}


def test_capability_map_does_not_hide_other_supported_fields(monkeypatch):
    variant = _variant()
    job = _job(variant)
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"text_elements": True, "timeline": True, "clips": {"playback_rate": False}},
    )
    snapshot = build_editor_snapshot(job, variant)
    parsed = _parse(
        snapshot,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"duration_s": 1}},
    )
    assert parsed.ops and parsed.ops[0]["patch"]["duration_s"] == 1
    compiled = compile_editor_ops(job, variant, parsed.ops)
    assert compiled.payload.timeline_slots[0].duration_s == 1


def test_legacy_snapshot_without_map_keeps_parser_compatibility(monkeypatch):
    variant = _variant()
    job = _job(variant)
    job.id = "eval"
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"text_elements": True, "timeline": True, "clips": {}},
    )
    snapshot = build_editor_snapshot(job, variant)
    snapshot.pop("timeline_patch_capabilities", None)
    parsed = _parse(
        snapshot,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"playback_rate": 2}},
    )
    assert parsed.ops
