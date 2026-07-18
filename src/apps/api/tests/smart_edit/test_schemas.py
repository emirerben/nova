from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.smart_edit.schemas import (
    SMART_EDIT_SCHEMA_VERSION,
    SemanticEventIntent,
    SmartEditEvent,
    SmartEditPlanDocument,
    SmartWord,
    build_event_id,
)


def _event_id() -> str:
    return build_event_id(
        preset_version="cigdem-v1",
        role="list_item",
        start_word_id="w000001",
        end_word_id="w000002",
        collision_ordinal=0,
    )


def test_smart_word_keeps_raw_and_display_text_on_one_timeline() -> None:
    word = SmartWord(
        word_id="w000001",
        spoken_text="markalar",
        display_text="markalar",
        normalized_text="markalar",
        start_ms=100,
        end_ms=420,
        timing_quality="aligned",
        display_alignment=["w000001"],
        language="tr",
    )
    assert word.end_ms == 420


def test_smart_word_rejects_invalid_timing_and_alignment_ids() -> None:
    with pytest.raises(ValidationError):
        SmartWord(
            word_id="w1",
            spoken_text="bir",
            display_text="1",
            normalized_text="bir",
            start_ms=400,
            end_ms=300,
            timing_quality="aligned",
            display_alignment=["bad"],
        )


def test_planner_intent_rejects_exact_timing_and_paths() -> None:
    with pytest.raises(ValidationError):
        SemanticEventIntent.model_validate(
            {
                "role": "hook",
                "start_word_id": "w000001",
                "end_word_id": "w000002",
                "anchor_word_id": "w000001",
                "confidence_tier": "high",
                "lane_tokens": ["hook_pop"],
                "asset_intent_tags": ["brand_example"],
                "rationale": "opening promise",
                "start_ms": 123,
                "gcs_path": "smart-assets/private.mp4",
            }
        )


def test_event_rejects_duplicate_lane_kinds_and_raw_asset_paths() -> None:
    payload = {
        "event_id": _event_id(),
        "role": "list_item",
        "start_word_id": "w000001",
        "end_word_id": "w000002",
        "anchor": {"word_id": "w000001", "offset_ms": 0},
        "active_start_ms": 100,
        "active_end_ms": 700,
        "confidence_tier": "high",
        "enabled": True,
        "lanes": [
            {
                "kind": "visual",
                "asset_id": "smart-assets/private.png",
                "zone": "top_right",
                "entrance_token": "pop",
            }
        ],
    }
    with pytest.raises(ValidationError):
        SmartEditEvent.model_validate(payload)

    payload["lanes"] = [
        {
            "kind": "caption_emphasis",
            "token": "lime_keyword",
            "baseline_caption_word_ids": ["w000001"],
        },
        {
            "kind": "caption_emphasis",
            "token": "bold_keyword",
            "baseline_caption_word_ids": ["w000002"],
        },
    ]
    with pytest.raises(ValidationError, match="at most one lane"):
        SmartEditEvent.model_validate(payload)


def test_baseline_captions_are_separate_from_optional_disabled_events() -> None:
    event = SmartEditEvent.model_validate(
        {
            "event_id": _event_id(),
            "role": "list_item",
            "start_word_id": "w000001",
            "end_word_id": "w000002",
            "anchor": {"word_id": "w000001", "offset_ms": 0},
            "active_start_ms": 100,
            "active_end_ms": 700,
            "confidence_tier": "high",
            "enabled": False,
            "lanes": [],
        }
    )
    plan = SmartEditPlanDocument(
        schema_version=SMART_EDIT_SCHEMA_VERSION,
        baseline_captions=[
            {
                "cue_id": "cue-1",
                "word_ids": ["w000001", "w000002"],
                "display_text": "Birinci konu",
            }
        ],
        events=[event],
    )
    assert plan.baseline_captions[0].display_text == "Birinci konu"
    assert plan.events[0].enabled is False


def test_plan_document_rejects_dangling_word_and_event_references() -> None:
    event = {
        "event_id": _event_id(),
        "role": "list_item",
        "start_word_id": "w000001",
        "end_word_id": "w000002",
        "anchor": {"word_id": "w000001", "offset_ms": 0},
        "active_start_ms": 100,
        "active_end_ms": 700,
        "confidence_tier": "high",
        "lanes": [
            {
                "kind": "sfx",
                "role_tokens": ["chapter_number_pop"],
                "sync_to_event_id": "0" * 24,
                "offset_ms": 0,
                "gain_token": "foreground_soft",
            }
        ],
    }
    with pytest.raises(ValidationError, match="unknown Smart event"):
        SmartEditPlanDocument.model_validate(
            {
                "baseline_captions": [
                    {
                        "cue_id": "cue-1",
                        "word_ids": ["w000001", "w000002"],
                        "display_text": "Birinci konu",
                    }
                ],
                "events": [event],
            }
        )

    event["lanes"] = []
    event["end_word_id"] = "w000003"
    with pytest.raises(ValidationError, match="outside baseline captions"):
        SmartEditPlanDocument.model_validate(
            {
                "baseline_captions": [
                    {
                        "cue_id": "cue-1",
                        "word_ids": ["w000001", "w000002"],
                        "display_text": "Birinci konu",
                    }
                ],
                "events": [event],
            }
        )


def test_events_require_ordered_spans_and_their_own_sfx_anchor() -> None:
    event_id = _event_id()
    base = {
        "event_id": event_id,
        "role": "list_item",
        "start_word_id": "w000002",
        "end_word_id": "w000001",
        "anchor": {"word_id": "w000001", "offset_ms": 0},
        "active_start_ms": 100,
        "active_end_ms": 700,
        "confidence_tier": "high",
        "lanes": [],
    }
    with pytest.raises(ValidationError, match="start <= anchor <= end"):
        SmartEditEvent.model_validate(base)

    base.update(
        {
            "start_word_id": "w000001",
            "end_word_id": "w000002",
            "anchor": {"word_id": "w000003", "offset_ms": 0},
        }
    )
    with pytest.raises(ValidationError, match="start <= anchor <= end"):
        SmartEditEvent.model_validate(base)

    other_event_id = build_event_id(
        preset_version="cigdem-v1",
        role="example",
        start_word_id="w000001",
        end_word_id="w000002",
        collision_ordinal=0,
    )
    base.update(
        {
            "anchor": {"word_id": "w000001", "offset_ms": 0},
            "lanes": [
                {
                    "kind": "sfx",
                    "role_tokens": ["chapter_number_pop"],
                    "sync_to_event_id": other_event_id,
                    "offset_ms": 0,
                    "gain_token": "foreground_soft",
                }
            ],
        }
    )
    with pytest.raises(ValidationError, match="containing Smart event"):
        SmartEditPlanDocument.model_validate(
            {
                "baseline_captions": [
                    {
                        "cue_id": "cue-1",
                        "word_ids": ["w000001", "w000002"],
                        "display_text": "Birinci konu",
                    }
                ],
                "events": [
                    base,
                    {
                        **base,
                        "event_id": other_event_id,
                        "role": "example",
                        "lanes": [],
                    },
                ],
            }
        )


def test_event_id_is_deterministic_and_collision_ordinal_changes_it() -> None:
    first = _event_id()
    assert first == _event_id()
    second = build_event_id(
        preset_version="cigdem-v1",
        role="list_item",
        start_word_id="w000001",
        end_word_id="w000002",
        collision_ordinal=1,
    )
    assert first != second
    assert len(first) == 24
