from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput


def _agent() -> ClipIntentPlannerAgent:
    return ClipIntentPlannerAgent(None)  # type: ignore[arg-type]


def _input() -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(
        creator_request=(
            "Label each sport, group the pub clips, put park clips first, and say "
            '"Post-match" on the pub chapter.'
        ),
        latest_user_message="",
    )


def test_parse_keeps_distinct_operations_for_one_attribute() -> None:
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "sport",
                    "op": "label",
                    "attribute": "each sport",
                    "source_quote": "Label each sport",
                },
                {
                    "intent_id": "pub-group",
                    "op": "group",
                    "attribute": "pub clips",
                    "source_quote": "group the pub clips",
                },
                {
                    "intent_id": "park-first",
                    "op": "order",
                    "attribute": "park clips",
                    "position": "first",
                    "source_quote": "put park clips first",
                },
                {
                    "intent_id": "pub-caption",
                    "op": "caption",
                    "attribute": "pub chapter",
                    "creator_text": "Post-match",
                    "source_quote": 'say "Post-match" on the pub chapter',
                },
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _input())
    assert [intent.op for intent in out.intents] == ["label", "group", "order", "caption"]
    assert out.intents[-1].creator_text == "Post-match"


@pytest.mark.parametrize(
    "raw",
    [
        {
            "intents": [
                {"intent_id": "x", "op": "label", "attribute": "sport", "source_quote": "made up"}
            ]
        },
        {
            "intents": [
                {
                    "intent_id": "x",
                    "op": "caption",
                    "attribute": "pub",
                    "creator_text": "invented words",
                    "source_quote": "group the pub clips",
                }
            ]
        },
    ],
)
def test_parse_rejects_unquoted_requirements_and_copy(raw: dict) -> None:
    with pytest.raises(SchemaError):
        _agent().parse(json.dumps(raw), _input())


def test_parse_rejects_partial_inventory_question() -> None:
    raw = {
        "intents": [
            {
                "intent_id": "sport",
                "op": "label",
                "attribute": "each sport",
                "source_quote": "Label each sport",
            }
        ],
        "question": "Which operations should I keep?",
    }
    with pytest.raises(SchemaError, match="partial"):
        _agent().parse(json.dumps(raw), _input())


def test_parse_rejects_duplicate_intent_ids_even_for_distinct_operations() -> None:
    raw = {
        "intents": [
            {
                "intent_id": "same-id",
                "op": "label",
                "attribute": "each sport",
                "source_quote": "Label each sport",
            },
            {
                "intent_id": "same-id",
                "op": "group",
                "attribute": "pub clips",
                "source_quote": "group the pub clips",
            },
        ],
        "question": None,
    }
    with pytest.raises(SchemaError, match="duplicate intent_id"):
        _agent().parse(json.dumps(raw), _input())


def test_pure_duration_request_has_no_intents() -> None:
    input = ClipIntentPlannerInput(creator_request="Make this a fast 20 second edit.")
    out = _agent().parse('{"intents": [], "question": null}', input)
    assert out.intents == []


def test_parse_keeps_same_attribute_distinct_across_clip_and_transcript_sources() -> None:
    request = "Label the score on clips and show the score I say."
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "visual-score",
                    "op": "label",
                    "attribute": "score",
                    "source_quote": "Label the score on clips",
                },
                {
                    "intent_id": "spoken-score",
                    "op": "label",
                    "attribute": "score",
                    "label_source": "transcript",
                    "transcript_kind": "score",
                    "source_quote": "show the score I say",
                },
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request))
    assert [intent.label_source for intent in out.intents] == ["clip", "transcript"]
    assert out.intents[1].transcript_kind == "score"
