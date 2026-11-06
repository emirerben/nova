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


def test_parse_remints_duplicate_intent_ids_instead_of_dropping_an_operation() -> None:
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
    assert [intent.intent_id for intent in out.intents] == ["same-id", "same-id-2"]
    assert [intent.op for intent in out.intents] == ["label", "group"]
    assert out.salvage_question is None


# KRI-422: synthetic stand-in shaped like the failing production message (titles,
# six numbered shots, each with quoted word-for-word text). No real creator text.
KRI422_REQUEST = (
    'Opening title: "A morning in Lisbon with Ana" Closing title: "See you at the next race, '
    'Ana" Show this text on each shot, word for word: 1. The bakery photo of two friends with '
    'a cake: "Ana\'s gift to Sam: her first Portuguese cake" 2. The video of the girl skating: '
    '"The only girl in the race: Lia" 3. The video of the guy in the green T-shirt skating: '
    '"Sam from Ghana: bakes cakes, skates too" 4. The video of the guy in the red shirt '
    'talking to the camera: "First place comes down to two: Rui..." 5. The video of the guy '
    'with glasses in the grey T-shirt skating: "...and Ana" 6. The finish-line photo: '
    '"Rui 42.1, Ana 42.6: a narrow win for Rui" Don\'t add stock images, AI images or any '
    "other outside visuals. Make it 25 seconds."
)

# (model-minted id, attribute, exact on-screen copy, source quote) in the shape the
# real parser returned when it failed: ids built from the shot description, the
# longest past the 40-character bound.
KRI422_SHOTS = [
    (
        "caption_bakery_photo",
        "The bakery photo of two friends with a cake",
        "Ana's gift to Sam: her first Portuguese cake",
        "1. The bakery photo of two friends with a cake: \"Ana's gift to Sam: her first "
        'Portuguese cake"',
    ),
    (
        "caption_girl_skating_video",
        "The video of the girl skating",
        "The only girl in the race: Lia",
        '2. The video of the girl skating: "The only girl in the race: Lia"',
    ),
    (
        "caption_guy_green_tshirt_skating_video",
        "The video of the guy in the green T-shirt skating",
        "Sam from Ghana: bakes cakes, skates too",
        '3. The video of the guy in the green T-shirt skating: "Sam from Ghana: bakes cakes, '
        'skates too"',
    ),
    (
        "caption_guy_red_shirt_talking_video",
        "The video of the guy in the red shirt talking to the camera",
        "First place comes down to two: Rui...",
        '4. The video of the guy in the red shirt talking to the camera: "First place comes '
        'down to two: Rui..."',
    ),
    (
        "caption_guy_glasses_grey_tshirt_skating_video",
        "The video of the guy with glasses in the grey T-shirt skating",
        "...and Ana",
        '5. The video of the guy with glasses in the grey T-shirt skating: "...and Ana"',
    ),
    (
        "caption_finish_line_photo",
        "The finish-line photo",
        "Rui 42.1, Ana 42.6: a narrow win for Rui",
        '6. The finish-line photo: "Rui 42.1, Ana 42.6: a narrow win for Rui"',
    ),
]


def _kri422_raw(ids: list[str] | None = None) -> str:
    intents = [
        {
            "intent_id": intent_id,
            "op": "caption",
            "attribute": attribute,
            "label_source": "clip",
            "transcript_kind": None,
            "creator_text": copy,
            "caption_attribute": None,
            "position": None,
            "placeholder": False,
            "order_by": None,
            "source_quote": quote,
        }
        for (intent_id, attribute, copy, quote) in KRI422_SHOTS
    ]
    for intent, intent_id in zip(intents, ids or [], strict=False):
        intent["intent_id"] = intent_id
    return json.dumps({"intents": intents, "question": None})


def _kri422_input() -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(
        creator_request=KRI422_REQUEST, latest_user_message=KRI422_REQUEST, clip_facts=True
    )


def test_kri422_over_long_model_id_keeps_every_per_shot_caption() -> None:
    """The real failure: a 45-character model-minted id dropped a clear caption and
    Kria asked the creator to restate it. The id is a handle, not the instruction."""
    out = _agent().parse(_kri422_raw(), _kri422_input())
    assert out.salvage_question is None
    assert out.salvage_reasons == []
    assert [i.creator_text for i in out.intents] == [copy for _, _, copy, _ in KRI422_SHOTS]
    ids = [i.intent_id for i in out.intents]
    assert all(len(intent_id) <= 40 for intent_id in ids)
    assert len(set(ids)) == len(ids)
    # In-bound ids are untouched; the long one is shortened the same way every time.
    assert ids[:4] == [shot[0] for shot in KRI422_SHOTS[:4]]
    assert ids[4] != KRI422_SHOTS[4][0]
    assert ids[4] == _agent().parse(_kri422_raw(), _kri422_input()).intents[4].intent_id


def test_kri422_every_long_id_style_still_keeps_every_caption() -> None:
    """A wordier naming style puts several ids past the bound at once (prod lost 5 of 6)."""
    wordy = [f"caption_shot_{n}_{'x' * 40}" for n in range(1, 7)]
    out = _agent().parse(_kri422_raw(wordy), _kri422_input())
    assert out.salvage_question is None
    ids = [i.intent_id for i in out.intents]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert all(len(intent_id) <= 40 for intent_id in ids)


@pytest.mark.parametrize("missing", [None, "", "   ", 7])
def test_missing_or_non_string_intent_id_is_minted_not_dropped(missing: object) -> None:
    raw = json.loads(_kri422_raw())
    raw["intents"][1]["intent_id"] = missing
    out = _agent().parse(json.dumps(raw), _kri422_input())
    assert out.salvage_question is None
    assert out.intents[1].intent_id == "intent-2"
    assert len(out.intents) == 6


def test_salvage_reports_closed_vocabulary_reasons_never_copy() -> None:
    raw = json.loads(_kri422_raw())
    raw["intents"][2]["source_quote"] = "Sam bakes cakes"  # not the creator's words
    out = _agent().parse(json.dumps(raw), _kri422_input())
    assert len(out.intents) == 5
    assert out.salvage_reasons == ["source_quote_not_creator_text"]
    assert "Ghana" not in " ".join(out.salvage_reasons)
    assert out.salvage_question is not None


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


# ── La Mercè story edit (2026-10-04 prod): word-for-word chapter lines ───────
# Byte-identical to the creator's prod message (verified against the turn's
# request_digest). Chapter 5's line is 61 characters: the parser refused it with
# the 60-character cap meant for short phrases and asked the creator to restate
# their own words.

T04_REQUEST = (
    'Make it 20 seconds. Title: "This kid is climbing a tower made of people." Story edit in '
    "6 chapters at a quick pace with high energy, day shots first and night shots last. Use "
    "every uploaded file, in this chapter order, and show each chapter line on screen word "
    "for word. Chapter 1 · a helmeted child at the very top of a tall human tower · 3 seconds "
    "· Watch the very top. Chapter 2 · two full human towers in front of a stone building, "
    "crowd below · 4 seconds · It's a castell. The tallest ever built had 10 levels. Chapter "
    "3 · the packed base of shoulders, arms and hands, seen from above · 3 seconds · UNESCO "
    "heritage since 2010. Chapter 4 · two night shots of hooded devils and sparks over the "
    'crowd · 4 seconds · At night, "devils" run through the crowd with fireworks. Chapter 5 '
    "· fireworks in the night sky beside a lit tower · 4 seconds · It's La Mercè, "
    "Barcelona's biggest festival. Every September. Chapter 6 · white sparks shooting up, "
    "close · 2 seconds · Would you run through the fire?"
)
T04_LINES = [
    "Watch the very top.",
    "It's a castell. The tallest ever built had 10 levels.",
    "UNESCO heritage since 2010.",
    'At night, "devils" run through the crowd with fireworks.',
    "It's La Mercè, Barcelona's biggest festival. Every September.",
    "Would you run through the fire?",
]


def _t04_input() -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(creator_request=T04_REQUEST, latest_user_message=T04_REQUEST)


def _t04_caption(n: int) -> dict:
    line = T04_LINES[n - 1]
    return _games_intent(f"caption_chapter_{n}", "caption", f"Chapter {n}", line, creator_text=line)


def _t04_group(n: int) -> dict:
    return _games_intent(f"group_chapter_{n}", "group", f"Chapter {n}", f"Chapter {n}")


_T04_DAY_FIRST = _games_intent(
    "order_day", "order", "day shots", "day shots first", position="first"
)
_T04_NIGHT_LAST = _games_intent(
    "order_night", "order", "night shots", "night shots last", position="last"
)
_T04_INCLUDE = _games_intent(
    "include_all", "include", "every uploaded file", "Use every uploaded file"
)


def test_t04_chapter_line_over_60_chars_is_kept_verbatim() -> None:
    """The prod turn: six word-for-word chapter lines, one of them 61 characters."""
    assert len(T04_LINES[4]) == 61
    raw = json.dumps(
        {
            "intents": [*(_t04_caption(n) for n in range(1, 7)), _T04_DAY_FIRST, _T04_INCLUDE],
            "question": None,
        }
    )
    out = _agent().parse(raw, _t04_input())
    assert out.salvage_question is None
    assert out.salvage_reasons == []
    captions = [i.creator_text for i in out.intents if i.op == "caption"]
    assert captions == T04_LINES


def test_t04_full_inventory_with_a_group_per_chapter_fits_the_cap() -> None:
    """Replayed shape: group + caption per chapter, both orders, include, and the
    "in this chapter order" request -- 16 operations, none of them a question."""
    raw = json.dumps(
        {
            "intents": [
                *(intent for n in range(1, 7) for intent in (_t04_group(n), _t04_caption(n))),
                _T04_DAY_FIRST,
                _T04_NIGHT_LAST,
                _T04_INCLUDE,
                _games_intent(
                    "order_chapters",
                    "order",
                    "chapters",
                    "in this chapter order",
                    position="in this chapter order",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _t04_input())
    assert out.salvage_question is None
    assert len(out.intents) == 16
    chapter_order = next(i for i in out.intents if i.intent_id == "order_chapters")
    assert chapter_order.position is None
    assert [i.creator_text for i in out.intents if i.op == "caption"] == T04_LINES


def test_groups_duplicating_a_caption_collapse_before_the_cap_asks() -> None:
    """A caption already holds its chapter together (exactly like a group), so a
    same-named group is dropped before Kria asks the creator to restate anything."""
    from app.schemas.clip_intents import MAX_CLIP_INTENTS

    # A group + a caption per chapter is over the cap; the captions alone fit.
    chapters = MAX_CLIP_INTENTS // 2 + 1
    lines = [f"Line number {n} of the story" for n in range(1, chapters + 1)]
    request = " ".join(f'Chapter {n} · "{line}"' for n, line in enumerate(lines, 1))
    intents = []
    for n, line in enumerate(lines, 1):
        intents.append(_games_intent(f"g{n}", "group", f"Chapter {n}", f"Chapter {n}"))
        intents.append(_games_intent(f"c{n}", "caption", f"Chapter {n}", line, creator_text=line))
    out = _agent().parse(
        json.dumps({"intents": intents, "question": None}),
        ClipIntentPlannerInput(creator_request=request),
    )
    assert out.salvage_question is None
    assert [i.op for i in out.intents] == ["caption"] * chapters
    assert [i.creator_text for i in out.intents] == lines


def test_groups_are_not_collapsed_while_the_inventory_fits() -> None:
    raw = json.dumps({"intents": [_t04_group(1), _t04_caption(1)], "question": None})
    out = _agent().parse(raw, _t04_input())
    assert [i.op for i in out.intents] == ["group", "caption"]


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ("in this chapter order", None),
        ("sequential", None),
        ("in order", None),
        (["Chapter 1 · a helmeted child", "Chapter 2 · two full human towers"], None),
        ("at the start", "first"),
        ("first", "first"),
        ("at the very end", "last"),
    ],
)
def test_order_position_prose_is_normalised_not_dropped(position, expected) -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "order_x", "order", "chapters", "in this chapter order", position=position
                ),
                _t04_caption(1),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _t04_input())
    assert out.salvage_question is None
    assert out.intents[0].position == expected


def test_two_placements_packed_into_one_order_still_ask() -> None:
    """ "day first and night last" in ONE intent cannot be represented; guessing one
    side would silently drop the other, so the creator is still asked."""
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "order_shots",
                    "order",
                    "shots",
                    "day shots first and night shots last",
                    position="day shots first and night shots last",
                ),
                _t04_caption(1),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _t04_input())
    assert out.salvage_reasons == ["intent_invalid:position"]
    assert len(out.intents) == 1


def test_label_copy_keeps_its_short_bound() -> None:
    """Only caption copy gets the creator-caption bound; a label is a corner tag.

    KRI-826: a creator label is bounded by the label bar (the fence answers "too long"),
    so the planner only refuses label copy past the stored bound: a whole sentence.
    """
    from app.schemas.clip_intents import CREATOR_LABEL_STORAGE_MAX

    long_label = f"{T04_LINES[4]} {T04_LINES[5]}"
    assert len(long_label) > CREATOR_LABEL_STORAGE_MAX
    raw = json.dumps(
        {
            "intents": [
                _games_intent("l1", "label", "festival", long_label, creator_text=long_label),
                _t04_caption(1),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _t04_input())
    assert out.salvage_reasons == ["creator_text_too_long"]


def test_caption_copy_over_the_creator_bound_is_still_rejected() -> None:
    from app.schemas.clip_intents import CREATOR_CAPTION_MAX_CHARS

    line = ("word " * 60).strip()
    assert len(line) > CREATOR_CAPTION_MAX_CHARS
    raw = json.dumps(
        {
            "intents": [
                _games_intent("c1", "caption", "Chapter 1", line, creator_text=line),
                _t04_caption(2),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=f"{line}. {T04_REQUEST}"))
    assert out.salvage_reasons == ["creator_text_too_long"]


def test_cap_fits_a_six_chapter_story_and_the_prompt_says_so() -> None:
    from app.schemas.clip_intents import MAX_CLIP_INTENTS

    assert MAX_CLIP_INTENTS == 40
    prompt = _agent().render_prompt(_t04_input())
    assert "fits in 40 operations" in prompt
    assert '`position` is exactly "first", "last", or null' in prompt


def test_quote_stitched_from_two_creator_spans_is_repaired_not_dropped() -> None:
    """Live replay (2/10 runs): Flash prefixed every chapter row with the request's
    "show each chapter line ... word for word." sentence. Both halves are the
    creator's exact words; only the join is not contiguous."""
    lead = "show each chapter line on screen word for word."
    intents = [
        _games_intent(
            f"caption_chapter_{n}",
            "caption",
            f"Chapter {n}",
            f"{lead} {row}",
            creator_text=T04_LINES[n - 1],
        )
        for n, row in enumerate(
            [
                "Chapter 1 · a helmeted child at the very top of a tall human tower · 3 seconds"
                " · Watch the very top.",
                "Chapter 2 · two full human towers in front of a stone building, crowd below"
                " · 4 seconds · It's a castell. The tallest ever built had 10 levels.",
                "Chapter 3 · the packed base of shoulders, arms and hands, seen from above"
                " · 3 seconds · UNESCO heritage since 2010.",
                "Chapter 4 · two night shots of hooded devils and sparks over the crowd"
                ' · 4 seconds · At night, "devils" run through the crowd with fireworks.',
                "Chapter 5 · fireworks in the night sky beside a lit tower · 4 seconds"
                " · It's La Mercè, Barcelona's biggest festival. Every September.",
                "Chapter 6 · white sparks shooting up, close · 2 seconds"
                " · Would you run through the fire?",
            ],
            1,
        )
    ]
    out = _agent().parse(json.dumps({"intents": intents, "question": None}), _t04_input())
    assert out.salvage_question is None
    assert [i.creator_text for i in out.intents] == T04_LINES
    assert out.intents[4].source_quote.startswith("Chapter 5 · fireworks")


def test_stitched_quote_with_an_invented_half_is_still_rejected() -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "c5",
                    "caption",
                    "Chapter 5",
                    "please print this line. It's La Mercè, Barcelona's biggest festival.",
                    creator_text="It's La Mercè, Barcelona's biggest festival.",
                ),
                _t04_caption(1),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _t04_input())
    assert out.salvage_reasons == ["source_quote_not_creator_text"]


# ── KRI-456: style asks and title lines are not caption intents ─────────────
# Prod thread 63fd08d6 (2026-10-06): "Big readable captions" and `Title: "..."`
# became caption intents; the first matched zero clips and stopped the turn with
# 'I couldn't find any clips for "captions"'.

_CACIO_REQUEST = (
    "Recipe video with my voiceover. Match every step I say to the clip that shows it: "
    "boiling the pasta, grating the pecorino, toasting the pepper, adding pasta water, "
    "tossing with the cheese, plating with pepper. Use the eating shot at the very end. "
    'Big readable captions. Title: "Cacio e pepe in 10 minutes".'
)


def _cacio_input() -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(creator_request=_CACIO_REQUEST, latest_user_message=None)


_CACIO_ORDER = _games_intent(
    "boil", "order", "boiling the pasta", "boiling the pasta", position=None
)


@pytest.mark.parametrize(
    "attribute",
    [
        "captions",
        "Big readable captions",
        "large captions",
        "the subtitles",
        "on-screen text",
        "burned-in captions",
        "altyazı",
        "altyazılar",
        "text",
    ],
)
def test_style_caption_is_dropped_silently(attribute: str) -> None:
    raw = json.dumps(
        {
            "intents": [
                _CACIO_ORDER,
                _games_intent(
                    "style",
                    "caption",
                    attribute,
                    "Big readable captions",
                    caption_attribute="what is said",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cacio_input())
    assert [i.intent_id for i in out.intents] == ["boil"]
    assert out.salvage_question is None
    assert out.salvage_reasons == []
    assert out.silent_drops == {"style_caption_dropped": 1}


def test_style_caption_with_a_garbled_quote_is_still_silent() -> None:
    # A quote that is not creator text would normally be a loud rejection.
    raw = json.dumps(
        {
            "intents": [
                _CACIO_ORDER,
                _games_intent(
                    "style",
                    "caption",
                    "captions",
                    "make the captions big",
                    caption_attribute="speech",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cacio_input())
    assert [i.intent_id for i in out.intents] == ["boil"]
    assert out.salvage_question is None


def test_style_caption_with_no_shape_is_silent_not_a_schema_error() -> None:
    # No creator_text and no caption_attribute fails the caption shape validator.
    raw = json.dumps(
        {
            "intents": [
                _CACIO_ORDER,
                _games_intent("style", "caption", "captions", "Big readable captions"),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cacio_input())
    assert [i.intent_id for i in out.intents] == ["boil"]
    assert out.silent_drops == {"style_caption_dropped": 1}


@pytest.mark.parametrize(
    ("attribute", "quote"),
    [
        ("title", 'Title: "Cacio e pepe in 10 minutes"'),
        ("the title", "Title:"),
        ("video title", "Big readable captions"),
        ("opening title", "Big readable captions"),
        ("intro title", "Big readable captions"),
        ("the video", "Big readable captions"),
        ("whole video", "Big readable captions"),
        ("the entire video", "Big readable captions"),
        # The attribute can be anything: the quote is the title line itself.
        ("the first clip", 'Title: "Cacio e pepe in 10 minutes"'),
        ("pasta clip", 'Title: "Cacio e pepe in 10 minutes"'),
    ],
)
def test_title_caption_is_dropped_silently(attribute: str, quote: str) -> None:
    raw = json.dumps(
        {
            "intents": [
                _CACIO_ORDER,
                _games_intent(
                    "title",
                    "caption",
                    attribute,
                    quote,
                    creator_text="Cacio e pepe in 10 minutes",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cacio_input())
    assert [i.intent_id for i in out.intents] == ["boil"]
    assert out.salvage_question is None
    assert out.silent_drops == {"title_caption_dropped": 1}


def test_turkish_title_prefix_is_a_title() -> None:
    request = 'Başlık: "Makarna tarifi". Adımları göster.'
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "t", "caption", "ilk klip", 'Başlık: "Makarna tarifi"', creator_text="Makarna"
                )
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request))
    assert out.intents == []
    assert out.silent_drops == {"title_caption_dropped": 1}


def test_live_shape_video_attribute_with_style_quote_is_a_style_drop() -> None:
    # Live Flash output on the old prompt (2026-10-06): attribute "video", the style
    # wording in caption_attribute, and the quote is just the style ask.
    raw = json.dumps(
        {
            "intents": [
                _CACIO_ORDER,
                _games_intent(
                    "caption_readable",
                    "caption",
                    "video",
                    "Big readable captions",
                    caption_attribute="Big readable captions",
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cacio_input())
    assert [i.intent_id for i in out.intents] == ["boil"]
    assert out.silent_drops == {"style_caption_dropped": 1}


def test_cacio_request_keeps_only_the_order_intents_and_reports_both_drops() -> None:
    steps = [
        "boiling the pasta",
        "grating the pecorino",
        "toasting the pepper",
        "adding pasta water",
        "tossing with the cheese",
        "plating with pepper",
    ]
    intents = [_games_intent(f"s{n}", "order", s, s) for n, s in enumerate(steps)]
    intents.append(
        _games_intent(
            "eat",
            "order",
            "the eating shot",
            "Use the eating shot at the very end",
            position="last",
        )
    )
    intents.append(
        _games_intent(
            "style", "caption", "captions", "Big readable captions", caption_attribute="speech"
        )
    )
    intents.append(
        _games_intent(
            "title",
            "caption",
            "first clip",
            'Title: "Cacio e pepe in 10 minutes"',
            creator_text="Cacio e pepe in 10 minutes",
        )
    )
    out = _agent().parse(json.dumps({"intents": intents, "question": None}), _cacio_input())
    assert len(out.intents) == 7
    assert out.intents[-1].position == "last"
    assert out.salvage_question is None
    assert out.silent_drops == {"style_caption_dropped": 1, "title_caption_dropped": 1}


def test_only_style_captions_returned_is_an_empty_inventory_not_an_error() -> None:
    request = "Big readable captions"
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "style", "caption", "captions", "Big readable captions", caption_attribute="x"
                )
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request))
    assert out.intents == []
    assert out.question is None
    assert out.salvage_question is None


@pytest.mark.parametrize(
    ("request_text", "attribute", "quote", "extra"),
    [
        (
            'Say "Kitchen notes" on clips showing ingredients.',
            "clips showing ingredients",
            'Say "Kitchen notes" on clips showing ingredients',
            {"creator_text": "Kitchen notes"},
        ),
        (
            'Say "Post-match" on the pub chapter.',
            "the pub chapter",
            'Say "Post-match" on the pub chapter',
            {"creator_text": "Post-match"},
        ),
        (
            "Chapter 5 · fireworks · It's La Mercè.",
            "Chapter 5",
            "Chapter 5 · fireworks · It's La Mercè.",
            {"creator_text": "It's La Mercè."},
        ),
        (
            "Caption the beach clips with what the weather was like.",
            "beach clips",
            "Caption the beach clips with what the weather was like",
            {"caption_attribute": "what the weather was like"},
        ),
        (
            "Add captions to the beach clips.",
            "captions for the beach clips",
            "Add captions to the beach clips",
            {"caption_attribute": "the beach"},
        ),
        (
            'Put the text "Hello" on screen.',
            "text",
            'Put the text "Hello" on screen',
            {"creator_text": "Hello"},
        ),
    ],
)
def test_real_chapter_captions_survive_the_style_and_title_guard(
    request_text: str, attribute: str, quote: str, extra: dict
) -> None:
    raw = json.dumps(
        {
            "intents": [_games_intent("c", "caption", attribute, quote, **extra)],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request_text))
    assert [i.op for i in out.intents] == ["caption"]
    assert out.silent_drops == {}


# KRI-511: prod thread 2ef61a47 (phone Narrated, stress-test kit N2). The planner had
# no way to say "leave this clip out": Flash invented `op: "exclude"` (a loud
# rejection, so the creator was asked to restate it) or wrote `include`, which
# FORCES the clip into the edit. "Show the balloons while I talk about the
# balloons" came back with `order_by: "voiceover_match"` and was rejected too.
_CAPPADOCIA_REQUEST = (
    "The voiceover is in Turkish, but I want the subtitles in English so my foreign "
    "followers understand. Spell the place names exactly: Göreme, Paşabağ, Avanos, "
    "Kızılçukur. Show the balloons while I talk about the balloons, and end on the "
    "sunset valley. Skip the quad bike clip."
)


def _cappadocia_input(
    request: str = _CAPPADOCIA_REQUEST, *, exclusions: bool = False
) -> ClipIntentPlannerInput:
    # `exclusions` = CLIP_INTENT_EXCLUDE_ENABLED (KRI-667). The KRI-511 cases below run
    # with it off: they pin the flag-off behaviour, which must stay exactly as it was.
    return ClipIntentPlannerInput(
        creator_request=request,
        latest_user_message=request,
        clip_facts=True,
        clip_exclusions=exclusions,
    )


_BALLOONS = _games_intent(
    "balloons",
    "order",
    "balloons",
    "Show the balloons while I talk about the balloons",
    position=None,
)
_SUNSET_LAST = _games_intent(
    "sunset", "order", "sunset valley", "end on the sunset valley", position="last"
)


@pytest.mark.parametrize("exclusions", [False, True])
def test_kri511_prod_shape_keeps_both_orders_and_the_skip_only_as_an_exclude(
    exclusions: bool,
) -> None:
    # The live capture that reproduced the prod reply (drop_classes op + order_by). Flag
    # off (KRI-511): the invented `exclude` is dropped silently. Flag on (KRI-667): it is
    # the real op, kept, and the creator is still never asked anything.
    raw = json.dumps(
        {
            "intents": [
                {**_BALLOONS, "order_by": "voiceover_match"},
                {**_SUNSET_LAST, "order_by": None},
                _games_intent(
                    "exclude_quad_bike", "exclude", "quad bike clip", "Skip the quad bike clip"
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cappadocia_input(exclusions=exclusions))
    assert [(i.op, i.attribute, i.position, i.order_by) for i in out.intents] == [
        ("order", "balloons", None, None),
        ("order", "sunset valley", "last", None),
        *([("exclude", "quad bike clip", None, None)] if exclusions else []),
    ]
    assert out.salvage_question is None
    assert out.salvage_reasons == []
    assert out.silent_drops == ({} if exclusions else {"exclusion_dropped": 1})


@pytest.mark.parametrize(
    ("request_text", "quote"),
    [
        (_CAPPADOCIA_REQUEST, "Skip the quad bike clip"),
        ("Make a montage and leave the quad bike clip out.", "leave the quad bike clip out"),
        ("Fun edit. Don't use the blurry ones.", "Don't use the blurry ones"),
        ("Fun edit, but without the drone shot.", "but without the drone shot"),
        ("Remove the selfie clip please.", "Remove the selfie clip"),
        ("Quad klibini kullanma.", "Quad klibini kullanma"),
    ],
)
def test_kri511_include_minted_from_a_skip_is_dropped_never_inverted(
    request_text: str, quote: str
) -> None:
    # Flag off: the include is dropped (it would force the clip IN). Flag on, see
    # test_kri667_include_minted_from_a_skip_is_repaired_into_an_exclude.
    raw = json.dumps(
        {
            "intents": [_games_intent("skip", "include", "quad bike clip", quote)],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cappadocia_input(request_text))
    assert out.intents == []
    assert out.salvage_question is None
    assert out.silent_drops == {"exclusion_dropped": 1}


@pytest.mark.parametrize("op", ["exclude", "Skip", "remove", "omit", "drop"])
def test_kri511_invented_exclusion_op_is_silent_even_with_a_garbled_quote(op: str) -> None:
    # Flag off. Flag on keeps a well-quoted one as `exclude` (see the KRI-667 cases).
    raw = json.dumps(
        {
            "intents": [
                _BALLOONS,
                _games_intent("x", op, "the quad bike", "skip quad bikes entirely"),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cappadocia_input())
    assert [i.intent_id for i in out.intents] == ["balloons"]
    assert out.salvage_question is None
    assert out.silent_drops == {"exclusion_dropped": 1}


@pytest.mark.parametrize(
    ("request_text", "attribute", "quote"),
    [
        ("Skip the laundry, but the gym has to be in.", "gym", "the gym has to be in"),
        ("Make sure you use the drone shot.", "drone shot", "Make sure you use the drone shot"),
        ("Don't forget the drone shot.", "drone shot", "Don't forget the drone shot"),
        (
            "Use the voice behind a montage of the remaining clips (excluding the talk to camera "
            "video).",
            "the remaining clips (excluding the talk to camera video)",
            "the remaining clips (excluding the talk to camera video)",
        ),
        ("Use every uploaded file.", "every uploaded file", "Use every uploaded file"),
    ],
)
@pytest.mark.parametrize("exclusions", [False, True])
def test_kri511_real_include_asks_are_kept(
    request_text: str, attribute: str, quote: str, exclusions: bool
) -> None:
    # KRI-667: with exclusions on, an ask to USE clips is never read as one to skip them,
    # even when it is worded around what it leaves out.
    raw = json.dumps(
        {"intents": [_games_intent("keep", "include", attribute, quote)], "question": None}
    )
    out = _agent().parse(
        raw, ClipIntentPlannerInput(creator_request=request_text, clip_exclusions=exclusions)
    )
    assert [i.op for i in out.intents] == ["include"]
    assert out.silent_drops == {}


@pytest.mark.parametrize(
    ("order_by", "position", "quote", "expected"),
    [
        ("voiceover_match", None, "Show the balloons while I talk about the balloons", None),
        ("voiceover", None, "Show the balloons while I talk about the balloons", None),
        ("null", None, "Show the balloons while I talk about the balloons", None),
        ("None", "last", "end on the sunset valley", None),
        # A valid value the quote never asked for orders EVERY clip by filming time.
        ("route", None, "Show the balloons while I talk about the balloons", None),
    ],
)
def test_kri511_invented_or_unasked_order_by_becomes_null(
    order_by: str, position: str | None, quote: str, expected: str | None
) -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent("o", "order", "balloons", quote, position=position, order_by=order_by)
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cappadocia_input())
    assert len(out.intents) == 1
    assert out.intents[0].order_by is expected
    assert out.intents[0].position == position
    assert out.salvage_question is None


@pytest.mark.parametrize(
    ("order_by", "expected"),
    [("filming_order", "capture_time"), ("chronological", "capture_time"), ("my_route", "route")],
)
def test_kri511_filming_order_synonym_maps_onto_the_enum(order_by: str, expected: str) -> None:
    request = "Put all clips in the order I filmed them."
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "o", "order", "all clips", "in the order I filmed them", order_by=order_by
                )
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request, clip_facts=True))
    assert [i.order_by for i in out.intents] == [expected]


def test_kri511_string_null_order_by_on_a_non_order_intent_is_repaired() -> None:
    # Live run: every intent carried `"order_by": "null"`, so all three were rejected.
    raw = json.dumps(
        {
            "intents": [
                {**_BALLOONS, "order_by": "null"},
                {**_SUNSET_LAST, "order_by": "null"},
                _games_intent(
                    "nl", "label", "place name", "Spell the place names exactly", order_by="null"
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cappadocia_input())
    assert [(i.attribute, i.order_by) for i in out.intents] == [
        ("balloons", None),
        ("sunset valley", None),
    ]
    assert out.salvage_question is None


@pytest.mark.parametrize(
    ("op", "attribute", "quote", "extra", "reason"),
    [
        ("include", "subtitles", "I want the subtitles in English", {}, "speech_caption_dropped"),
        (
            "include",
            "English subtitles",
            "I want the subtitles in English",
            {},
            "speech_caption_dropped",
        ),
        (
            "label",
            "subtitles_language",
            "I want the subtitles in English",
            {},
            "speech_caption_dropped",
        ),
        (
            "label",
            "place name",
            "Spell the place names exactly: Göreme, Paşabağ, Avanos, Kızılçukur",
            {},
            "spelling_dropped",
        ),
        (
            "include",
            "exact place name spelling: Göreme",
            "Spell the place names exactly: Göreme",
            {},
            "spelling_dropped",
        ),
        (
            "caption",
            "the place name Göreme in subtitles",
            "Spell the place names exactly: Göreme",
            {"caption_attribute": "Göreme"},
            "spelling_dropped",
        ),
        (
            "caption",
            "the balloon clips",
            "Spell the place names exactly: Göreme",
            {"creator_text": "Göreme"},
            "spelling_dropped",
        ),
    ],
)
def test_kri511_spoken_caption_instructions_are_not_clip_operations(
    op: str, attribute: str, quote: str, extra: dict, reason: str
) -> None:
    raw = json.dumps(
        {
            "intents": [_BALLOONS, _games_intent("s", op, attribute, quote, **extra)],
            "question": None,
        }
    )
    out = _agent().parse(raw, _cappadocia_input())
    assert [i.intent_id for i in out.intents] == ["balloons"]
    assert out.salvage_question is None
    assert out.silent_drops == {reason: 1}


@pytest.mark.parametrize(
    ("request_text", "attribute", "quote"),
    [
        (
            "Add subtitles and label the city in each clip.",
            "city in each clip",
            "label the city in each clip",
        ),
        (
            "Label each place: Alfama, LX Factory, Pink Street.",
            "place",
            "Label each place: Alfama, LX Factory, Pink Street",
        ),
    ],
)
def test_kri511_real_labels_beside_captions_are_kept(
    request_text: str, attribute: str, quote: str
) -> None:
    raw = json.dumps({"intents": [_games_intent("l", "label", attribute, quote)], "question": None})
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request_text))
    assert [i.op for i in out.intents] == ["label"]
    assert out.silent_drops == {}


def test_kri511_prompt_teaches_exclusions_narration_order_and_caption_text() -> None:
    # Flag off (KRI-667 off): the KRI-511 "no intent for an exclusion" prompt, unchanged.
    prompt = " ".join(_agent().render_prompt(_cappadocia_input()).split())
    assert "Never invent an op such as `exclude`" in prompt
    assert "- exclude:" not in prompt
    assert "caption|exclude" not in prompt
    assert "No other op exists." in prompt
    assert "Never write it as `include`" in prompt
    assert "Skip the quad bike clip" in prompt
    assert "show the balloons while I talk about the balloons" in prompt
    assert "A list of names to spell is never a request to label clips" in prompt
    assert 'never the string "null"' in prompt


# ── KRI-667: `exclude` is a real op (CLIP_INTENT_EXCLUDE_ENABLED) ───────────────────
# Ways this could go wrong, written before the code: an exclusion still lost or still
# turned into a question; an ask to USE clips ("the gym has to be in", "the remaining
# clips (excluding the talk video)") read as one to skip them; an include beside a
# skip ("Skip the laundry, but the gym has to be in") repaired into excluding the gym;
# a text/sound removal ("Remove the sport labels") turned into removing the sport
# clips; an unverified (garbled) exclusion kept, so footage is removed on a quote the
# creator never wrote; "skip the clips" (no particular clip) kept.


def _parse_exclusions(request: str, *intents: dict) -> object:
    raw = json.dumps({"intents": list(intents), "question": None}, ensure_ascii=False)
    return _agent().parse(raw, _cappadocia_input(request, exclusions=True))


@pytest.mark.parametrize(
    ("request_text", "attribute", "quote"),
    [
        (_CAPPADOCIA_REQUEST, "quad bike clip", "Skip the quad bike clip"),
        (
            "Make a montage and leave the quad bike clip out.",
            "quad bike clip",
            "leave the quad bike clip out",
        ),
        ("Fun edit. Don't use the blurry ones.", "blurry ones", "Don't use the blurry ones"),
        ("Fun edit, but without the drone shot.", "drone shot", "but without the drone shot"),
        ("Remove the selfie clip please.", "selfie clip", "Remove the selfie clip"),
        ("Quad klibini kullanma.", "quad klibi", "Quad klibini kullanma"),
        ("Çamaşır videosunu gösterme.", "çamaşır", "Çamaşır videosunu gösterme"),
    ],
)
def test_kri667_include_minted_from_a_skip_is_repaired_into_an_exclude(
    request_text: str, attribute: str, quote: str
) -> None:
    out = _parse_exclusions(request_text, _games_intent("skip", "include", attribute, quote))
    assert [(i.op, i.attribute) for i in out.intents] == [("exclude", attribute)]
    assert out.salvage_question is None
    assert out.silent_drops == {"exclusion_repaired": 1}


@pytest.mark.parametrize(
    ("request_text", "op", "attribute", "quote"),
    [
        # The gym is what the creator wants IN; only the laundry is skipped.
        (
            "Skip the laundry, but the gym has to be in.",
            "include",
            "gym",
            "Skip the laundry, but the gym has to be in",
        ),
        (
            "Skip the laundry, but the gym has to be in.",
            "exclude",
            "gym",
            "Skip the laundry, but the gym has to be in",
        ),
        (
            "Don't use the drone shot, use the beach ones.",
            "include",
            "beach ones",
            "Don't use the drone shot, use the beach ones",
        ),
        ("Skip the laundry and use the gym.", "include", "gym", "Skip the laundry and use the gym"),
        (
            "Spor salonu kalsın ama çamaşır klibini kullanma.",
            "include",
            "spor salonu",
            "Spor salonu kalsın ama çamaşır klibini kullanma",
        ),
        (
            "Spor salonu kalsın ama çamaşır klibini kullanma.",
            "exclude",
            "spor salonu",
            "Spor salonu kalsın ama çamaşır klibini kullanma",
        ),
        # The quote skips something else entirely.
        (
            "Fun edit. Don't use the blurry ones.",
            "include",
            "quad bike clip",
            "Don't use the blurry ones",
        ),
        # KRI-680: a comma after a finished verb still ends the clause the skip governs.
        (
            "Spor salonu kalsın, çamaşır klibini kullanma.",
            "include",
            "spor salonu",
            "Spor salonu kalsın, çamaşır klibini kullanma",
        ),
        (
            "Spor salonu kalsın, çamaşır klibini kullanma.",
            "exclude",
            "spor salonu",
            "Spor salonu kalsın, çamaşır klibini kullanma",
        ),
        (
            "Lunaparkla başla, karanlık klipleri kullanma.",
            "exclude",
            "lunapark",
            "Lunaparkla başla, karanlık klipleri kullanma",
        ),
        (
            "Lunapark klibini göster, karanlık klipleri kullanma.",
            "include",
            "lunapark klibi",
            "Lunapark klibini göster, karanlık klipleri kullanma",
        ),
    ],
)
def test_kri667_a_clip_beside_a_skip_is_never_excluded(
    request_text: str, op: str, attribute: str, quote: str
) -> None:
    out = _parse_exclusions(request_text, _games_intent("x", op, attribute, quote))
    assert out.intents == []
    assert out.salvage_question is None
    assert out.silent_drops == {"exclusion_dropped": 1}


# KRI-680 (Tigre night M1): "Çok karanlık, hiçbir şeyin görünmediği iki klibi kullanma"
# was dropped as `exclusion_dropped` on every turn: the comma cut "çok karanlık" off the
# clause the Turkish verb governs, so both near-black clips rendered. Turkish stacks a
# clip's descriptions before its noun with commas; only a finished verb ends the clause.
_M1_DARK = "Çok karanlık, hiçbir şeyin görünmediği iki klibi kullanma"


@pytest.mark.parametrize(
    ("op", "attribute"),
    [
        ("exclude", "çok karanlık klipler"),
        ("exclude", "karanlık klipler"),
        ("exclude", "çok karanlık, hiçbir şeyin görünmediği iki klip"),
        ("exclude", "hiçbir şeyin görünmediği klipler"),
        ("include", "çok karanlık klipler"),
        ("skip", "karanlık klipler"),
    ],
)
def test_kri680_dark_clips_described_before_a_comma_are_excluded(op: str, attribute: str) -> None:
    request = (
        "Gece 11'de tekneyle Tigre merkeze gittik. Başlık: Gece 23:00, Tigre. "
        f"{_M1_DARK}. Hızlı kesmeler, 25 saniye."
    )
    out = _parse_exclusions(request, _games_intent("dark", op, attribute, _M1_DARK))
    assert [(i.op, i.attribute) for i in out.intents] == [("exclude", attribute)]
    assert "exclusion_dropped" not in out.silent_drops


def test_kri667_skip_the_laundry_but_the_gym_has_to_be_in() -> None:
    request = "Skip the laundry, but the gym has to be in."
    out = _parse_exclusions(
        request,
        _games_intent("skip", "exclude", "laundry", "Skip the laundry"),
        _games_intent("gym", "include", "gym", "the gym has to be in"),
    )
    assert [(i.op, i.attribute) for i in out.intents] == [
        ("exclude", "laundry"),
        ("include", "gym"),
    ]
    assert out.silent_drops == {}


@pytest.mark.parametrize(
    "op", ["exclude", "Skip", "remove", "omit", "drop", "skip_clip", "exclude-videos"]
)
def test_kri667_exclusion_op_and_its_synonyms_are_the_exclude_op(op: str) -> None:
    out = _parse_exclusions(
        _CAPPADOCIA_REQUEST,
        _BALLOONS,
        _games_intent("x", op, "the quad bike", "Skip the quad bike clip"),
    )
    assert [(i.op, i.attribute) for i in out.intents] == [
        ("order", "balloons"),
        ("exclude", "the quad bike"),
    ]
    assert out.salvage_question is None


def test_kri667_an_unverified_exclusion_is_dropped_silently_never_kept_or_asked() -> None:
    # "skip quad bikes entirely" is not the creator's text: removing footage on it would
    # act on words nobody wrote, and asking would make the creator restate a skip.
    out = _parse_exclusions(
        _CAPPADOCIA_REQUEST,
        _BALLOONS,
        _games_intent("x", "exclude", "the quad bike", "skip quad bikes entirely"),
    )
    assert [i.intent_id for i in out.intents] == ["balloons"]
    assert out.salvage_question is None
    assert out.silent_drops == {"exclusion_dropped": 1}


def test_kri667_exclude_shape_is_repaired_never_printed_or_placed() -> None:
    out = _parse_exclusions(
        "Skip the quad bike clip.",
        _games_intent(
            "x",
            "exclude",
            "quad bike clip",
            "Skip the quad bike clip",
            creator_text="quad bike",
            caption_attribute="quad",
            position="last",
            order_by="null",
        ),
    )
    [intent] = out.intents
    assert (intent.op, intent.creator_text, intent.caption_attribute, intent.position) == (
        "exclude",
        None,
        None,
        None,
    )
    assert intent.order_by is None


@pytest.mark.parametrize(
    ("request_text", "op", "attribute", "quote", "reason"),
    [
        (
            "Remove the sport labels.",
            "exclude",
            "sport labels",
            "Remove the sport labels",
            "non_clip_exclusion_dropped",
        ),
        (
            "Remove the sport labels.",
            "remove",
            "sport",
            "Remove the sport labels",
            "non_clip_exclusion_dropped",
        ),
        (
            "Remove the sport labels.",
            "include",
            "sport",
            "Remove the sport labels",
            "non_clip_exclusion_dropped",
        ),
        (
            "Skip the title card.",
            "exclude",
            "title card",
            "Skip the title card",
            "non_clip_exclusion_dropped",
        ),
        (
            "No music on the beach clips.",
            "exclude",
            "beach clips",
            "No music on the beach clips",
            "non_clip_exclusion_dropped",
        ),
        (
            "Etiketleri kaldır, müziği çıkar.",
            "exclude",
            "müzik",
            "müziği çıkar",
            "non_clip_exclusion_dropped",
        ),
        (
            "I don't want subtitles.",
            "exclude",
            "subtitles",
            "I don't want subtitles",
            "non_clip_exclusion_dropped",
        ),
        # Names no particular clip: removing "the clips" would remove everything.
        ("Skip the clips.", "exclude", "clips", "Skip the clips", "exclusion_dropped"),
    ],
)
def test_kri667_a_text_or_sound_removal_is_never_a_clip_exclusion(
    request_text: str, op: str, attribute: str, quote: str, reason: str
) -> None:
    out = _parse_exclusions(request_text, _games_intent("x", op, attribute, quote))
    assert out.intents == []
    assert out.salvage_question is None
    assert out.silent_drops == {reason: 1}


def test_kri667_an_exclude_in_words_the_parser_cannot_read_is_the_models_statement() -> None:
    # Any language: "I'd rather not have the laundry in it" has no exclusion verb the
    # fallback reads, so the model's explicit `exclude` stands (resolution is fenced).
    request = "I'd rather not have the laundry in it."
    out = _parse_exclusions(
        request, _games_intent("x", "exclude", "laundry", "I'd rather not have the laundry in it")
    )
    assert [(i.op, i.attribute) for i in out.intents] == [("exclude", "laundry")]


def test_kri667_flag_off_never_keeps_an_exclude() -> None:
    raw = json.dumps(
        {
            "intents": [_games_intent("x", "exclude", "laundry", "Skip the laundry")],
            "question": None,
        }
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request="Skip the laundry."))
    assert out.intents == []
    assert out.silent_drops == {"exclusion_dropped": 1}


def test_kri667_prompt_teaches_exclude_only_with_the_flag() -> None:
    off = _without_hide_part(_agent().render_prompt(_cappadocia_input()))
    on = _without_hide_part(_agent().render_prompt(_cappadocia_input(exclusions=True)))
    flat = " ".join(on.split())
    assert "- exclude:" in on
    assert '"op":"label|group|order|include|caption|exclude"' in on
    assert "Never invent an op such as `exclude`" not in on
    assert '"Skip the laundry, but the gym has to be in" is one `exclude`' in flat
    assert '"remove the sport labels"' in flat
    assert "Never ask about an exclusion" in flat
    # Everything outside the three slots is shared, byte for byte.
    assert off.split("No other op exists.")[0] == on.split("- exclude:")[0]
    # (KRI-993: the `exclusive` JSON field rides the same switch.)
    assert off.split("Showing a clip when")[1] == (
        on.split("Showing a clip when")[1]
        .replace("caption|exclude", "caption")
        .replace(',"exclusive":false', "")
        .replace(',"requirement_id":"r1|null"', "")
        .replace(',"by_length":"too_short|null","under_s":null', "")
    )
    # KRI-994: the requirement link is taught only with exclusions on.
    assert '"requirement_id":"r1|null"' in on and "requirement_id" not in off
    # KRI-1031: so is the length mark ("too short to use").
    assert '"by_length":"too_short|null"' in on and "by_length" not in off
    assert "clip_exclusions" not in _cappadocia_input().model_dump()


def test_kri667_exclude_candidates_never_reach_the_flag_off_prompt() -> None:
    from app.schemas.clip_intents import ClipIntent

    candidates = [
        ClipIntent(intent_id="g", op="group", attribute="pub clips"),
        ClipIntent(intent_id="x", op="exclude", attribute="laundry"),
    ]
    off = _agent().render_prompt(
        ClipIntentPlannerInput(creator_request="Skip the laundry.", candidate_intents=candidates)
    )
    on = _agent().render_prompt(
        ClipIntentPlannerInput(
            creator_request="Skip the laundry.", candidate_intents=candidates, clip_exclusions=True
        )
    )
    assert '"op": "exclude"' not in off and '"op": "group"' in off
    assert '"op": "exclude"' in on


def test_kri667_turkish_question_names_an_exclude_it_could_not_fit() -> None:
    from app.schemas.clip_intents import MAX_CLIP_INTENTS

    request = "Klipleri grupla. Quad klibini kullanma."
    intents = [
        _games_intent(f"g{i}", "group", f"grup {i}", "Klipleri grupla")
        for i in range(MAX_CLIP_INTENTS)
    ] + [_games_intent("x", "exclude", "quad klibi", "Quad klibini kullanma")]
    raw = json.dumps({"intents": intents, "question": None}, ensure_ascii=False)
    out = _agent().parse(
        raw,
        ClipIntentPlannerInput(creator_request=request, reply_language="tr", clip_exclusions=True),
    )
    assert out.salvage_question is not None
    assert "hariç tutma: quad klibi" in out.salvage_question


# ── KRI-531: model slips on a Turkish chapter request are repaired, never asked ──
# Prod thread 9beeeec6 (stress kit M3, 2026-10-08): "I understood 3 of your clip
# instructions. I couldn't safely verify "caption: Üniversite"; "caption: Öğle
# arası"; "caption: Spor" (and 5 more)". Every intent below is a live Gemini 2.5
# Flash capture from replaying that exact message (50 runs); the creator had
# nothing to restate.
_BERLIN_REQUEST = (
    "Berlin'de öğrenci olarak bir günüm. Videoları çektiğim saat sırasına göre diz, "
    "sabahtan geceye. Bölüm başlıkları koy: Sabah, Üniversite, Öğle arası, Spor, Akşam. "
    "Aynı videodan iki tane varsa birini kullan. En sonda Elif'in kameraya söylediği "
    "cümleyi kendi sesiyle kullan. Videonun tamamı dikey olsun. 25 saniye olsun."
)
_BERLIN_CHAPTERS = ["Sabah", "Üniversite", "Öğle arası", "Spor", "Akşam"]
_BERLIN_ELIF_QUOTE = "En sonda Elif'in kameraya söylediği cümleyi kendi sesiyle kullan."


def _berlin_input() -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(
        creator_request=_BERLIN_REQUEST,
        latest_user_message=_BERLIN_REQUEST,
        clip_facts=True,
        reply_language="tr",
    )


def _berlin_caption(title: str, quote: str) -> dict:
    return _games_intent(
        f"caption_{len(title)}_{title[:3]}",
        "caption",
        f"{title} chapter",
        quote,
        creator_text=title,
    )


def _berlin_ellipsis_captions(joiner: str = " ... ") -> list[dict]:
    lead = "Bölüm başlıkları koy:"
    return [_berlin_caption("Sabah", f"{lead} Sabah")] + [
        _berlin_caption(title, f"{lead}{joiner}{title}") for title in _BERLIN_CHAPTERS[1:]
    ]


def test_kri531_prod_shape_keeps_every_chapter_and_asks_nothing() -> None:
    # raw capture: the prod drop classes (op + source quote) plus a null-attribute
    # filming order and an `audio` op carrying transcript fields, all in one run.
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "order_capture_time_morning_night",
                    "order",
                    None,  # type: ignore[arg-type]
                    "Videoları çektiğim saat sırasına göre diz, sabahtan geceye.",
                    order_by="capture_time",
                ),
                *_berlin_ellipsis_captions(),
                _games_intent(
                    "audio_elif_final_narration",
                    "audio",
                    "Elif'in kameraya söylediği cümle",
                    _BERLIN_ELIF_QUOTE,
                    label_source="transcript",
                    transcript_kind="participant",
                    position="last",
                ),
                _games_intent(
                    "set_vertical",
                    "set_style",
                    "Videonun tamamı",
                    "Videonun tamamı dikey olsun.",
                    creator_text="dikey",
                ),
                _games_intent(
                    "timing_duration_25s", "timing", "Videonun tamamı", "25 saniye olsun."
                ),
                _games_intent(
                    "select_unique_clips",
                    "select_unique",
                    "Aynı videodan iki tane varsa",
                    "Aynı videodan iki tane varsa birini kullan.",
                ),
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_question is None
    assert out.salvage_reasons == []
    assert [i.creator_text for i in out.intents if i.op == "caption"] == _BERLIN_CHAPTERS
    order, *_, last = out.intents
    assert (order.op, order.order_by, order.attribute) == (
        "order",
        "capture_time",
        "Videoları çektiğim saat sırasına göre diz, sabahtan geceye.",
    )
    assert (last.op, last.position, last.label_source, last.transcript_kind) == (
        "order",
        "last",
        "clip",
        None,
    )
    assert out.silent_drops == {"non_clip_op_dropped": 3}


@pytest.mark.parametrize("joiner", [" ... ", " ..., ", " …, ", "... ", " [...] "])
def test_kri531_ellipsis_quote_keeps_the_creators_exact_words(joiner: str) -> None:
    raw = json.dumps(
        {"intents": _berlin_ellipsis_captions(joiner), "question": None}, ensure_ascii=False
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_question is None
    assert [i.creator_text for i in out.intents] == _BERLIN_CHAPTERS
    assert [i.source_quote for i in out.intents][1:] == _BERLIN_CHAPTERS[1:]


def test_kri531_ellipsis_quote_with_invented_words_is_still_rejected() -> None:
    raw = json.dumps(
        {
            "intents": [
                _berlin_caption("Sabah", "Bölüm başlıkları koy: Sabah"),
                _berlin_caption("Gece", "Bölüm başlıkları koy: ... Gece"),
                _games_intent(
                    "c_uni",
                    "caption",
                    "Üniversite chapter",
                    "Bölüm başlıkları ekle: ... Üniversite",
                    creator_text="Üniversite",
                ),
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert [i.creator_text for i in out.intents] == ["Sabah"]
    assert out.salvage_reasons == ["source_quote_not_creator_text"]


def test_kri531_transcript_fields_on_an_order_are_cleared_not_rejected() -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "order_last_elif",
                    "order",
                    "Elif'in kameraya söylediği cümle",
                    _BERLIN_ELIF_QUOTE,
                    label_source="transcript",
                    transcript_kind="participant",
                    position="last",
                )
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_question is None
    assert [(i.op, i.position, i.label_source, i.transcript_kind) for i in out.intents] == [
        ("order", "last", "clip", None)
    ]


def test_kri531_transcript_label_still_needs_its_kind() -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "label_score", "label", "score", "Label the score", label_source="transcript"
                )
            ],
            "question": None,
        }
    )
    with pytest.raises(SchemaError) as err:
        _agent().parse(raw, ClipIntentPlannerInput(creator_request="Label the score."))
    assert err.value.error_class == "intent_invalid:model"


@pytest.mark.parametrize(
    ("op", "position", "expected"),
    [
        ("audio", "last", ("order", "last")),
        ("voice", "at the very end", ("order", "last")),
        ("use_sound", "end", ("order", "last")),
        ("audio", None, None),
        # The quote names no opening placement: a stray model position moves nothing.
        ("audio", "first", None),
    ],
)
def test_kri531_audio_op_with_a_placement_is_an_order(
    op: str, position: str | None, expected: tuple[str, str] | None
) -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "elif",
                    op,
                    "Elif'in kameraya söylediği cümle",
                    _BERLIN_ELIF_QUOTE,
                    position=position,
                )
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_question is None
    assert [(i.op, i.position) for i in out.intents] == ([expected] if expected else [])
    assert out.silent_drops == ({} if expected else {"non_clip_op_dropped": 1})


def test_kri531_audio_op_with_two_placements_still_asks() -> None:
    raw = json.dumps(
        {
            "intents": [
                _berlin_caption("Sabah", "Bölüm başlıkları koy: Sabah"),
                _games_intent(
                    "elif",
                    "keep_audio",
                    "Elif'in kameraya söylediği cümle",
                    _BERLIN_ELIF_QUOTE,
                    position="both first and last",
                ),
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_reasons == ["intent_invalid:position"]


@pytest.mark.parametrize(
    "op",
    ["timing", "set_timing", "duration", "style", "set_style", "format", "select", "select_unique"],
)
def test_kri531_invented_non_clip_ops_are_dropped_silently(op: str) -> None:
    raw = json.dumps(
        {
            "intents": [
                _berlin_caption("Sabah", "Bölüm başlıkları koy: Sabah"),
                _games_intent("x", op, "Videonun tamamı", "25 saniye olsun."),
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert [i.creator_text for i in out.intents] == ["Sabah"]
    assert out.salvage_question is None
    assert out.silent_drops == {"non_clip_op_dropped": 1}


@pytest.mark.parametrize("op", ["highlight", "chapter", "order_by_time"])
def test_kri531_unknown_ops_that_may_be_clip_operations_are_named_not_restated(op: str) -> None:
    # Leverkusen M2: asking to restate an op the schema doesn't have looped (no wording adds
    # one). The draft names it instead (tests/services/test_unexpressible_clip_ops.py).
    raw = json.dumps(
        {
            "intents": [
                _berlin_caption("Sabah", "Bölüm başlıkları koy: Sabah"),
                _games_intent("x", op, "Spor", "Bölüm başlıkları koy: Sabah, Üniversite"),
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_question is None
    assert [i.creator_text for i in out.intents] == ["Sabah"]
    assert out.unexpressed == ["Bölüm başlıkları koy: Sabah, Üniversite"]
    assert out.silent_drops == {"unexpressible_op_dropped": 1}


def test_kri531_null_attribute_only_repaired_on_a_filming_order() -> None:
    quote = "Videoları çektiğim saat sırasına göre diz, sabahtan geceye."
    raw = json.dumps(
        {
            "intents": [
                _games_intent("o", "order", None, quote, order_by="capture_time"),  # type: ignore[arg-type]
                _games_intent("i", "include", None, quote),  # type: ignore[arg-type]
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert [(i.op, i.attribute) for i in out.intents] == [("order", quote)]
    assert out.salvage_reasons == ["intent_invalid:attribute"]


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ("last", ("order", "last")),
        ("sonda", ("order", "last")),
        # No opening placement in the quote: keep the include, drop the stray position.
        ("first", ("include", None)),
        (None, ("include", None)),
    ],
)
def test_kri531_include_placed_last_is_an_order(
    position: str | None, expected: tuple[str, str | None]
) -> None:
    # Live capture (7/40 fresh runs): the ending came back as an include, whose
    # position is cleared, so the edit no longer ended on Elif's clip.
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "include_elif",
                    "include",
                    "Elif'in kameraya söylediği cümle",
                    _BERLIN_ELIF_QUOTE,
                    position=position,
                )
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _berlin_input())
    assert out.salvage_question is None
    assert [(i.op, i.position) for i in out.intents] == [expected]


@pytest.mark.parametrize("exclusions", [False, True])
def test_kri531_include_with_a_placement_word_but_an_exclusion_is_never_an_order(
    exclusions: bool,
) -> None:
    # Flag off: dropped, as before. Flag on (KRI-667): the exclude it asked for, with the
    # stray placement cleared -- never an `order` that would END the edit on those clips.
    request = "Fun edit. Don't use the blurry ones at the end."
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "x",
                    "include",
                    "blurry ones",
                    "Don't use the blurry ones at the end",
                    position="last",
                )
            ],
            "question": None,
        }
    )
    out = _agent().parse(
        raw, ClipIntentPlannerInput(creator_request=request, clip_exclusions=exclusions)
    )
    if exclusions:
        assert [(i.op, i.attribute, i.position) for i in out.intents] == [
            ("exclude", "blurry ones", None)
        ]
        assert out.silent_drops == {"exclusion_repaired": 1}
    else:
        assert out.intents == []
        assert out.silent_drops == {"exclusion_dropped": 1}


# ---------------------------------------------------------------- KRI-677
# Tigre night M1: the brief rendered its r2 order fact with `json.dumps`, so the
# model saw `\"Hadi gidelim\" dediğimiz iskele klibi` and copied that into
# source_quote. Nothing in the creator's own words carries a backslash, so both
# quoted order asks failed provenance on every turn after the first (the picker
# answer became the latest message) and the creator was told to rewrite them.
_KRI677_REQUEST = (
    "Gece 11'de tekneyle Tigre merkeze arkadaşımız Atakan'ı almaya gittik. "
    'Başlık: "Gece 23:00, Tigre". "Hadi gidelim" dediğimiz iskele klibiyle başla; '
    '"tam 20 dakika sürdü" dediğim klip en sonda olsun ve o cümle duyulsun. '
    "Hızlı kesmeler, 25 saniye."
)
_KRI677_FIRST_QUOTE = '"Hadi gidelim" dediğimiz iskele klibiyle başla'
_KRI677_LAST_QUOTE = '"tam 20 dakika sürdü" dediğim klip en sonda olsun'


def _kri677_turn_two_input() -> ClipIntentPlannerInput:
    # Turn two as `plan_and_resolve_clip_intents` sees it: every creator message joined
    # (`_full_creator_request`), the picker answer as the latest message, and the live
    # brief rendered by the real renderer as the non-authoritative recall aid.
    from app.kria.brief import BriefRequirement, CreativeBrief, render_brief_request
    from app.services.song_order import CLIP_ANSWER_MESSAGE

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description="Özel klip sırası",
                facts={
                    "first_clip": '"Hadi gidelim" dediğimiz iskele klibi',
                    "key": "narrative",
                    "last_clip": '"tam 20 dakika sürdü" dediğim klip',
                },
            )
        ],
    )
    return ClipIntentPlannerInput(
        creator_request=f"{_KRI677_REQUEST}\n{CLIP_ANSWER_MESSAGE}",
        latest_user_message=CLIP_ANSWER_MESSAGE,
        generated_brief=render_brief_request(brief),
        clip_facts=True,
    )


@pytest.mark.parametrize("escaped", [False, True], ids=["creator_words", "json_escaped_brief_copy"])
def test_kri677_quoted_spoken_line_asks_survive_the_picker_turn(escaped: bool) -> None:
    def quote(text: str) -> str:
        # The pre-fix brief line, copied verbatim by the model: `\"` around the spoken line.
        return text.replace('"', '\\"') if escaped else text

    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "first",
                    "order",
                    "iskele klibi",
                    quote(_KRI677_FIRST_QUOTE),
                    position="first",
                    order_by=None,
                ),
                _games_intent(
                    "last",
                    "order",
                    "tam 20 dakika sürdü dediğim klip",
                    quote(_KRI677_LAST_QUOTE),
                    position="last",
                    order_by=None,
                ),
            ],
            "question": None,
        }
    )
    out = _agent().parse(raw, _kri677_turn_two_input())
    assert [(i.op, i.position, i.source_quote) for i in out.intents] == [
        ("order", "first", _KRI677_FIRST_QUOTE),
        ("order", "last", _KRI677_LAST_QUOTE),
    ]
    assert out.salvage_reasons == []
    assert out.salvage_question is None


def test_kri677_rendered_brief_never_shows_escaped_quotes_to_the_model() -> None:
    # The planner prompt forbids copying brief wording, but a `\"` the model has only
    # ever seen in the brief is exactly what it copies. The brief must read as typed.
    rendered = _kri677_turn_two_input().generated_brief or ""
    assert '"Hadi gidelim" dediğimiz iskele klibi' in rendered
    assert "\\" not in rendered


# ── KRI-552: cleanup asks read as `caption:` intents, and labels cut mid-word ──
# Prod thread ddc6e71b (stress kit N3, 2026-10-08, seq 19): "I understood 2 of your clip
# instructions. I couldn't safely verify "caption: long pauses"; "caption: the part where I
# restart the kilometer t". Please restate just those so I can add them." The drop class on the
# turn was `intent_invalid:model`: a `caption` with neither copy nor a topic to print.
_MARATHON_REQUEST = (
    "My first marathon story with my voiceover. Cut my long pauses and the part where I restart "
    'the kilometer thirty sentence. When I say "the medal", show my medal photo. When I say '
    '"four hours and twelve minutes", show the watch photo. Keep the crowd noise quiet under my '
    "voice. Clean captions."
)


def _marathon_input() -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(
        creator_request=_MARATHON_REQUEST, latest_user_message=_MARATHON_REQUEST
    )


def _marathon_misfiled_cleanup_raw() -> str:
    return json.dumps(
        {
            "intents": [
                _games_intent(
                    "show_medal",
                    "include",
                    "my medal photo",
                    'When I say "the medal", show my medal photo.',
                ),
                _games_intent(
                    "show_watch",
                    "include",
                    "the watch photo",
                    'When I say "four hours and twelve minutes", show the watch photo.',
                ),
                _games_intent("cut_pauses", "caption", "long pauses", "Cut my long pauses"),
                _games_intent(
                    "cut_restart",
                    "caption",
                    "the part where I restart the kilometer thirty sentence",
                    "the part where I restart the kilometer thirty sentence",
                ),
            ],
            "question": None,
        }
    )


def test_kri552_the_prod_shape_still_asks_but_never_cuts_a_label_mid_word() -> None:
    # What a model that still misfiles the cleanup asks gets back: the question names each
    # instruction in whole words, never "...restart the kilometer t".
    out = _agent().parse(_marathon_misfiled_cleanup_raw(), _marathon_input())

    assert [i.intent_id for i in out.intents] == ["show_medal", "show_watch"]
    assert out.salvage_reasons == ["intent_invalid:model"]
    assert out.salvage_question is not None
    assert '"caption: long pauses"' in out.salvage_question
    assert '"caption: the part where I restart the kilometer…"' in out.salvage_question
    assert 'kilometer t"' not in out.salvage_question
    assert out.salvage_question.endswith("Please restate just those so I can add them.")


def test_kri552_a_turkish_question_names_the_labels_in_whole_words_too() -> None:
    raw = json.dumps(
        {
            "intents": [
                _games_intent(
                    "cut_restart",
                    "caption",
                    "kilometre otuz cümlesini baştan söylediğim yeri",
                    "kilometre otuz cümlesini baştan söylediğim yeri",
                )
            ],
            "question": None,
        },
        ensure_ascii=False,
    )
    request = "Uzun duraklamaları kes. kilometre otuz cümlesini baştan söylediğim yeri çıkar."
    # one valid intent keeps the question a partial-salvage one
    raw_dict = json.loads(raw)
    raw_dict["intents"].insert(
        0, _games_intent("order_a", "include", "uzun duraklamalar", "Uzun duraklamaları kes.")
    )
    out = _agent().parse(
        json.dumps(raw_dict, ensure_ascii=False),
        ClipIntentPlannerInput(creator_request=request, reply_language="tr"),
    )

    assert out.salvage_question is not None
    assert "yazı: kilometre otuz cümlesini baştan…" in out.salvage_question
    assert out.salvage_question.endswith("Sadece bunları yeniden yazar mısın, ekleyeyim.")


def test_kri552_the_prompt_sends_speech_cleanup_to_the_step_that_owns_it() -> None:
    # The model, not a regex, tells a cleanup ask from a caption (KRI-667); the prompt says so
    # in both languages, with the exact prod request as the example.
    prompt = " ".join(_agent().render_prompt(_marathon_input()).split())

    assert "is speech cleanup, which another step handles" in prompt
    assert "never a caption, label, group, order, or include" in prompt
    assert "uzun duraklamaları kes" in prompt
    assert "Cut my long pauses and the part where I restart the kilometer thirty sentence" in prompt


# sha256 of the kill-switch-OFF rendering of `_input()` on origin/main c4f0aa826 (before
# KRI-993), rendered from that commit's template with the same variables.
_OFF_PROMPT_SHA256 = "e0f6d521fbf27a58b2e429655f3f6a19d49cbf5b72881e4e5d06648b2cee39f5"


# 29 Ekim (prod thread 64bb9853) added two paragraphs to every rendering, kill switch or not:
# a random / any order and words shown a piece at a time are no clip operation. Without them
# the kill-switch-OFF prompt is still byte for byte the pre-KRI-993 one.
_29EKIM_START = "\n\nA request for NO particular sequence is not a clip operation"
_29EKIM_END = "even when they quote the words: return no\nintent for them.\n"
# Leverkusen M2 (2026-10-30) added the `hide_part` op to every rendering, kill switch or not
# (a privacy ask is never an invented op): its bullet and its three JSON fields.
_HIDE_PART_START = "\n- hide_part: a part at the START or the END of ONE clip"
_HIDE_PART_END = "`caption_attribute` and `position` null."
_HIDE_PART_JSON = (
    ("|hide_part", ""),
    (',"part_edge":"start|end|null","part_s":null,"part_attribute":null', ""),
)


def _without_hide_part(prompt: str) -> str:
    start = prompt.index(_HIDE_PART_START)
    end = prompt.index(_HIDE_PART_END, start) + len(_HIDE_PART_END)
    prompt = prompt[:start] + prompt[end:]
    for added, before in _HIDE_PART_JSON:
        prompt = prompt.replace(added, before)
    return prompt


def _with_cap_16(prompt: str) -> str:
    """KRI-1122 raised the operation cap the prompt states; the baseline said 16."""
    from app.schemas.clip_intents import MAX_CLIP_INTENTS

    for phrase in ("fits in {} operations", "If more than {} distinct operations"):
        prompt = prompt.replace(phrase.format(MAX_CLIP_INTENTS), phrase.format(16))
    return prompt


def test_kri993_kill_switch_off_prompt_is_byte_identical_to_before() -> None:
    import hashlib

    off = _with_cap_16(_without_hide_part(_agent().render_prompt(_input())))
    start = off.index(_29EKIM_START)
    end = off.index(_29EKIM_END, start) + len(_29EKIM_END)
    before_29ekim = off[:start] + "\n" + off[end:]
    assert hashlib.sha256(before_29ekim.encode()).hexdigest() == _OFF_PROMPT_SHA256


def test_kri993_on_prompt_keeps_a_blank_line_around_the_scope_paragraph() -> None:
    on = _agent().render_prompt(ClipIntentPlannerInput(creator_request="x", clip_exclusions=True))
    assert "\n\nAn edit limited to a subject" in on
    assert "`exclusive` is false on every other intent.\n\nShowing a clip when" in on
