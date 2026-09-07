"""Small replay/live quality gate for creator-memory extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.agents.creator_memory_extractor import (
    CreatorMemoryExtractorAgent,
    CreatorMemoryExtractorInput,
)


@dataclass(frozen=True)
class ExtractorCase:
    name: str
    input: dict
    expected_operation: str
    replay: dict


CASES = (
    ExtractorCase(
        name="durable_font",
        input={
            "source_message": "From now on, always use Playfair Display font.",
            "candidate_hint": "explicit",
            "current_memory": [],
        },
        expected_operation="activate_explicit",
        replay={
            "operation": "activate_explicit",
            "instruction": "Always use Playfair Display font",
            "category": "video_style",
            "enforcement": "constraint",
            "normalized_key": "font_family",
            "structured_value": {"font_family": "Playfair Display"},
            "target_item_id": None,
            "confidence": 0.99,
            "reason_code": "explicit_durable",
        },
    ),
    ExtractorCase(
        name="soft_tone",
        input={
            "source_message": "Videolarımda genellikle sakin ve sıcak bir tonu tercih ederim.",
            "candidate_hint": "soft",
            "current_memory": [],
        },
        expected_operation="suggest",
        replay={
            "operation": "suggest",
            "instruction": "Keep the tone calm and warm",
            "category": "stories_pacing",
            "enforcement": "advisory",
            "normalized_key": "tone",
            "structured_value": {"tone": "calm and warm"},
            "target_item_id": None,
            "confidence": 0.85,
            "reason_code": "soft_preference",
        },
    ),
    ExtractorCase(
        name="locked_conflict",
        input={
            "source_message": "From now on use Inter instead.",
            "candidate_hint": "explicit",
            "current_memory": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "normalized_key": "font_family",
                    "instruction": "Always use Playfair Display font",
                    "enforcement": "constraint",
                    "state": "active",
                    "user_locked": True,
                }
            ],
        },
        expected_operation="suggest",
        replay={
            "operation": "suggest",
            "instruction": "Use Inter font instead",
            "category": "video_style",
            "enforcement": "advisory",
            "normalized_key": "font_family",
            "structured_value": {"font_family": "Inter"},
            "target_item_id": None,
            "confidence": 0.9,
            "reason_code": "contradiction",
        },
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_creator_memory_extractor_eval(
    case: ExtractorCase,
    eval_mode: str,
    live_model_client,
) -> None:
    agent = CreatorMemoryExtractorAgent(live_model_client)
    input_value = CreatorMemoryExtractorInput.model_validate(case.input)
    output = (
        agent.run(input_value)
        if eval_mode == "live"
        else agent.parse(json.dumps(case.replay), input_value)
    )

    assert output.operation == case.expected_operation
    if output.operation in {"activate_explicit", "supersede"}:
        assert output.enforcement in {"constraint", "default"}
    if output.operation == "suggest":
        assert output.enforcement == "advisory"
