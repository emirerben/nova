import pytest

from app.agents._runtime import SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.narration_annotations import (
    NarrationAnnotationAgent,
    NarrationAnnotationInput,
)


def _input() -> NarrationAnnotationInput:
    return NarrationAnnotationInput(
        creator_request="Highlight the score and the topic.",
        words=[
            {"word_id": "w000000", "text": "score", "start_s": 0.0, "end_s": 0.2},
            {"word_id": "w000001", "text": "1-0", "start_s": 0.2, "end_s": 0.4},
        ],
        media_analysis=[{"asset_id": "asset-0", "subject_focus": "single_subject"}],
        timeline=[
            {
                "timeline_id": "shot-0",
                "asset_id": "asset-0",
                "start_s": 0.0,
                "end_s": 1.0,
            }
        ],
        requirements={"score_labels": True, "context_labels": ["topic"]},
    )


def test_parse_rejects_unknown_source_ids_visibly() -> None:
    with pytest.raises(SchemaError, match="unknown start_word_id"):
        NarrationAnnotationAgent(None).parse(
            '{"annotations":['
            '{"kind":"score","start_word_id":"w000001","end_word_id":"w000001"},'
            '{"kind":"score","start_word_id":"w999999"},'
            '{"kind":"topic","start_word_id":"w000000","timeline_id":"unknown"}'
            "]}",
            _input(),
        )


def test_parse_rejects_unknown_fields_and_invalid_confidence() -> None:
    agent = NarrationAnnotationAgent(None)
    with pytest.raises(SchemaError, match="unknown field"):
        agent.parse(
            '{"annotations":[{"kind":"score","start_word_id":"w000001","unexpected":"drop-me"}]}',
            _input(),
        )
    with pytest.raises(SchemaError, match="schema validation"):
        agent.parse(
            '{"annotations":[{"kind":"score","start_word_id":"w000001","confidence":"certain"}]}',
            _input(),
        )


def test_creator_request_uses_shared_limit() -> None:
    accepted = NarrationAnnotationInput.model_validate(
        {**_input().model_dump(), "creator_request": "x" * CREATOR_REQUEST_MAX_CHARS}
    )
    assert accepted.creator_request
    with pytest.raises(ValueError):
        NarrationAnnotationInput.model_validate(
            {**_input().model_dump(), "creator_request": "x" * (CREATOR_REQUEST_MAX_CHARS + 1)}
        )


def test_prompt_contains_exact_grounding_contract() -> None:
    prompt = NarrationAnnotationAgent(None).render_prompt(_input())

    assert "EXACT TIMED WORDS" in prompt
    assert "incidental" in prompt
    assert "free-form description" in prompt
    assert "generic" in prompt
    assert "topic kind" in prompt


def test_prompt_version_is_pinned() -> None:
    assert NarrationAnnotationAgent.spec.prompt_version == "2026-09-07.2"
