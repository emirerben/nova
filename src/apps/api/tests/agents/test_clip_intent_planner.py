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


def test_parse_reads_null_label_source_as_clip() -> None:
    # Live Flash output on 2026-10-01 (golden/mixed): a null label_source failed
    # the whole inventory, so the creator got a clarifying question instead.
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "sport",
                    "op": "label",
                    "attribute": "each sport",
                    "label_source": None,
                    "transcript_kind": None,
                    "source_quote": "Label each sport",
                },
                {
                    "intent_id": "pub-caption",
                    "op": "caption",
                    "attribute": "pub chapter",
                    "label_source": None,
                    "creator_text": "Post-match",
                    "source_quote": 'say "Post-match" on the pub chapter',
                },
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _input())
    assert [intent.label_source for intent in out.intents] == ["clip", "clip"]
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


def test_parse_rejects_source_quote_found_only_in_generated_brief() -> None:
    request = "Come up with creative ideas for these London clips."
    generated = "- [order/global] chronological order from sunset walk to night cycle"
    raw = {
        "intents": [
            {
                "intent_id": "polluted-order",
                "op": "order",
                "attribute": "sunset walk to night cycle",
                "order_by": "capture_time",
                "source_quote": "chronological order from sunset walk to night cycle",
            }
        ],
        "question": None,
    }

    with pytest.raises(SchemaError, match="source_quote is not an exact contiguous substring"):
        _agent().parse(
            json.dumps(raw),
            ClipIntentPlannerInput(
                creator_request=request,
                latest_user_message=request,
                generated_brief=generated,
                clip_facts=True,
            ),
        )


def test_parse_rejects_incomplete_caption_shape() -> None:
    caption = {
        "intent_id": "missing-topic",
        "op": "caption",
        "attribute": "pub chapter",
        "source_quote": "caption the pub chapter",
    }
    raw = {"intents": [caption], "question": None}

    with pytest.raises(SchemaError, match="caption_attribute") as info:
        _agent().parse(json.dumps(raw), _input())
    assert info.value.error_class.startswith("intent_invalid")


def test_parse_repairs_conflicting_caption_copy_in_favour_of_exact_words() -> None:
    caption = {
        "intent_id": "conflicting-copy",
        "op": "caption",
        "attribute": "pub chapter",
        "creator_text": "Post-match",
        "caption_attribute": "weather",
        "source_quote": 'say "Post-match" on the pub chapter',
    }
    out = _agent().parse(json.dumps({"intents": [caption], "question": None}), _input())
    assert out.salvage_question is None
    assert out.intents[0].creator_text == "Post-match"
    assert out.intents[0].caption_attribute is None


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
    out = _agent().parse(json.dumps(raw), _input())
    assert [intent.intent_id for intent in out.intents] == ["same-id"]
    assert out.salvage_question is not None
    assert "pub clips" in out.salvage_question


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


def test_clip_facts_controls_order_by_prompt_contract() -> None:
    request = "Put all clips in the order I filmed them."

    enabled = _agent().render_prompt(
        ClipIntentPlannerInput(creator_request=request, clip_facts=True)
    )
    disabled = _agent().render_prompt(ClipIntentPlannerInput(creator_request=request))

    assert '"order_by":"capture_time|route|null"' in enabled
    assert "You MUST include that `order_by` value" in enabled
    assert '"order_by":"capture_time|route|null"' not in disabled
    assert "You MUST include that `order_by` value" not in disabled


@pytest.mark.parametrize(
    ("order_by", "creator_request", "source_quote"),
    [
        (
            "capture_time",
            "Put all clips in the order I filmed them.",
            "in the order I filmed them",
        ),
        ("route", "Arrange all clips in the order of my route.", "in the order of my route"),
    ],
)
def test_parse_accepts_fact_backed_order_by(
    order_by: str,
    creator_request: str,
    source_quote: str,
) -> None:
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "all-clips-order",
                    "op": "order",
                    "attribute": source_quote,
                    "order_by": order_by,
                    "source_quote": source_quote,
                }
            ],
            "question": None,
        }
    )

    out = _agent().parse(
        raw,
        ClipIntentPlannerInput(creator_request=creator_request, clip_facts=True),
    )

    assert len(out.intents) == 1
    assert out.intents[0].order_by == order_by
    assert out.intents[0].position is None


def test_parse_drops_order_by_when_clip_facts_are_disabled() -> None:
    request = "Put all clips in the order I filmed them."
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "all-clips-order",
                    "op": "order",
                    "attribute": "in the order I filmed them",
                    "order_by": "capture_time",
                    "source_quote": "in the order I filmed them",
                }
            ],
            "question": None,
        }
    )

    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request))

    assert out.intents == []


def test_parse_rejects_order_by_combined_with_position() -> None:
    request = "Put all clips in the order I filmed them and put the park clips first."
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "invalid-order",
                    "op": "order",
                    "attribute": "park clips",
                    "position": "first",
                    "order_by": "capture_time",
                    "source_quote": "put the park clips first",
                }
            ],
            "question": None,
        }
    )

    with pytest.raises(SchemaError, match="order_by requires op=order and no position"):
        _agent().parse(raw, ClipIntentPlannerInput(creator_request=request, clip_facts=True))


_GAMES_DAY_REQUEST = (
    "We ran a backyard games day: kickball, then tug of war, then relay races, then pizza. "
    "Make a chronological, very fast paced video. Group content by game and add the game name "
    "to the bottom left. Also include a text for the pizza and the warmup as well. For "
    "individual shots of people, add a text placeholder so I can replace with their real names"
)


def _games_intent(intent_id: str, op: str, attribute: str, quote: str, **extra: object) -> dict:
    return {
        "intent_id": intent_id,
        "op": op,
        "attribute": attribute,
        "source_quote": quote,
        **extra,
    }


def test_real_prompt_shape_fits_cap_and_keeps_every_instruction() -> None:
    """KRI-282: a natural 7-operation montage request must not overflow or fail."""
    from app.schemas.clip_intents import MAX_CLIP_INTENTS

    assert MAX_CLIP_INTENTS >= 8
    group_quote = "Group content by game"
    chapter_quote = "include a text for the pizza and the warmup"
    raw = json.dumps(
        {
            "intents": [
                _games_intent("g1", "group", "kickball clips", group_quote),
                _games_intent("g2", "group", "tug of war clips", group_quote),
                _games_intent("g3", "group", "relay race clips", group_quote),
                _games_intent("l1", "label", "game", "add the game name to the bottom left"),
                _games_intent(
                    "c1", "caption", "the pizza", chapter_quote, caption_attribute="pizza"
                ),
                _games_intent(
                    "c2", "caption", "the warmup", chapter_quote, caption_attribute="warmup"
                ),
                _games_intent(
                    "l2",
                    "label",
                    "person's name",
                    "add a text placeholder so I can replace with their real names",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST))
    assert len(out.intents) == 7
    assert out.salvage_question is None


_PLACEHOLDER_QUOTE = "add a text placeholder so I can replace with their real names"


def _placeholder_raw(**extra: object) -> str:
    return json.dumps(
        {
            "intents": [
                _games_intent(
                    "p1",
                    "caption",
                    "individual shots of people",
                    _PLACEHOLDER_QUOTE,
                    **extra,
                )
            ],
            "question": None,
        }
    )


def test_invented_placeholder_token_is_repaired_not_dropped() -> None:
    """KRI-282: a model-invented creator_text token must never be accepted as creator copy,
    but the placeholder request itself is supported -- repaired, not a salvage question."""
    out = _agent().parse(
        _placeholder_raw(creator_text="NAME_PLACEHOLDER"),
        ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST),
    )
    assert out.salvage_question is None
    assert out.question is None
    (intent,) = out.intents
    assert (intent.op, intent.placeholder) == ("label", True)
    assert intent.creator_text is None
    assert intent.attribute == "individual shots of people"


@pytest.mark.parametrize(
    "extra",
    [
        {"placeholder": True},
        {"placeholder": True, "creator_text": "[Name]", "caption_attribute": "names"},
        {"placeholder": None},
    ],
)
def test_placeholder_request_in_every_model_shape_becomes_a_placeholder_label(extra) -> None:
    out = _agent().parse(
        _placeholder_raw(**extra), ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST)
    )
    assert [(i.op, i.placeholder, i.creator_text) for i in out.intents] == [("label", True, None)]
    assert out.salvage_question is None


def test_placeholder_flag_without_a_placeholder_request_still_needs_source_quote() -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "p1", "label", "people", "this quote is not in the request", placeholder=True
                )
            ],
            "question": None,
        }
    )
    with pytest.raises(SchemaError):
        _agent().parse(raw, ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST))


def test_unflagged_label_is_not_a_placeholder() -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent("l1", "label", "game", "add the game name to the bottom left")
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST))
    assert out.intents[0].placeholder is False
    assert "placeholder" not in out.intents[0].model_dump(mode="json")


def test_group_with_garbled_quote_is_repaired_from_the_creators_own_sentence() -> None:
    """Named values in prose ARE the groups; a garbled quote must not make the creator restate."""
    request = "We played football, then dodgeball, then beach volleyball. Group content by sport."
    raw = json.dumps(
        {
            "intents": [
                _games_intent("g1", "group", "dodgeball", "Group the dodgeball footage together")
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request))
    assert out.salvage_question is None
    assert (
        out.intents[0].source_quote == "We played football, then dodgeball, then beach volleyball."
    )


def test_group_with_unsupported_garbled_quote_is_still_dropped() -> None:
    raw = json.dumps(
        {
            "intents": [_games_intent("g1", "group", "sailing", "Group the sailing footage")],
            "question": None,
        }
    )
    with pytest.raises(SchemaError):
        _agent().parse(raw, ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST))


def test_real_prompt_shape_with_placeholder_is_eight_intents_and_no_question() -> None:
    """Redacted shape of the real ~47-clip Olympics prompt: 8 intents, 0 questions."""
    request = (
        "We hosted a field day with two teams: football, then dodgeball, then beach volleyball, "
        "then the pub. Create a chronological, very fast paced video. Group content by sport "
        "and add the sports name to the bottom left. Also include a text for the pub and the "
        "pregame as well. For individual shots of people, add a text placeholder so I can "
        "replace with their real names"
    )
    group_quote = "Group content by sport"
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "o1",
                    "order",
                    "all clips",
                    "Create a chronological, very fast paced video",
                    order_by="capture_time",
                ),
                _games_intent("g1", "group", "football clips", group_quote),
                _games_intent("g2", "group", "dodgeball clips", group_quote),
                _games_intent("g3", "group", "beach volleyball clips", group_quote),
                _games_intent("l1", "label", "sport", "add the sports name to the bottom left"),
                _games_intent(
                    "c1",
                    "caption",
                    "pub clips",
                    "include a text for the pub and the pregame",
                    caption_attribute="pub",
                ),
                _games_intent(
                    "c2",
                    "caption",
                    "pregame clips",
                    "include a text for the pub and the pregame",
                    caption_attribute="pregame",
                ),
                _games_intent(
                    "l2",
                    "caption",
                    "individual shots of people",
                    "For individual shots of people, add a text placeholder so I can replace with "
                    "their real names",
                    creator_text="NAME_PLACEHOLDER",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request, clip_facts=True))
    assert len(out.intents) == 8
    assert out.question is None
    assert out.salvage_question is None
    placeholders = [i for i in out.intents if i.placeholder]
    assert [(i.op, i.attribute) for i in placeholders] == [("label", "individual shots of people")]


def test_prompt_teaches_compact_inventory_and_named_values() -> None:
    prompt = _agent().render_prompt(ClipIntentPlannerInput(creator_request=_GAMES_DAY_REQUEST))
    assert "is ONE\n  `label` intent" in prompt
    assert "ask them to name the sports again" in prompt
    assert '"placeholder": true' in prompt
    assert "never ask the creator to restate it" in prompt
