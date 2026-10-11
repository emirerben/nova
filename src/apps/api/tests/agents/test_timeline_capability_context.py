from __future__ import annotations

import json

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)
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


# --- Crop through patch_slots (KRI-44) ------------------------------------------------

_CROP = {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}


def _crop_caps(monkeypatch, *, editable: bool) -> None:
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {
            "text_elements": True,
            "timeline": True,
            "clips": {"source_crop": {"editable": editable, "reason": None}},
        },
    )


def _crop_op(slot_index: int) -> dict:
    return {
        "op": "patch_slots",
        "selector": {"slot_indexes": [slot_index]},
        "patch": {"source_crop": _CROP},
    }


def test_crop_applies_to_a_photo_slot_not_only_a_video(monkeypatch):
    variant = _variant()
    job = _job(variant)
    _crop_caps(monkeypatch, editable=True)
    snapshot = build_editor_snapshot(job, variant)
    assert snapshot["timeline_patch_capabilities"]["source_crop"] is True

    parsed = _parse(snapshot, _crop_op(1))  # slot-2 is users/u/b.jpg
    compiled = compile_editor_ops(job, variant, parsed.ops)

    assert compiled.payload.timeline_slots[1].source_crop == _CROP
    assert compiled.payload.timeline_slots[0].source_crop is None


def test_crop_works_on_a_phone_rendered_edit_once_the_capability_is_open(monkeypatch):
    variant = {**_variant(), "render_destination": "device"}
    job = _job(variant)
    _crop_caps(monkeypatch, editable=True)
    snapshot = build_editor_snapshot(job, variant)
    assert snapshot["timeline_patch_capabilities"]["source_crop"] is True
    assert compile_editor_ops(job, variant, _parse(snapshot, _crop_op(0)).ops).payload


def test_crop_is_refused_when_the_edit_does_not_offer_it(monkeypatch):
    variant = _variant()
    job = _job(variant)
    _crop_caps(monkeypatch, editable=False)
    snapshot = build_editor_snapshot(job, variant)
    assert snapshot["timeline_patch_capabilities"]["source_crop"] is False
    assert _parse(snapshot, _crop_op(0)).ops == []


def test_a_photo_shown_as_a_card_cannot_be_cropped(monkeypatch):
    variant = _variant()
    variant["ai_timeline"]["slots"][1]["layout"] = "supporting_card"
    job = _job(variant)
    _crop_caps(monkeypatch, editable=True)
    parsed = _parse(build_editor_snapshot(job, variant), _crop_op(1))
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(job, variant, parsed.ops)
