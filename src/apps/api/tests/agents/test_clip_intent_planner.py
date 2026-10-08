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
    lines = [f"Line number {n} of the story" for n in range(1, 10)]  # 18 intents > 16
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
    assert [i.op for i in out.intents] == ["caption"] * 9
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
    """Only caption copy gets the creator-caption bound; a label is a corner tag."""
    long_label = T04_LINES[4]
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

    assert MAX_CLIP_INTENTS == 16
    prompt = _agent().render_prompt(_t04_input())
    assert "fits in 16 operations" in prompt
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


def _cappadocia_input(request: str = _CAPPADOCIA_REQUEST) -> ClipIntentPlannerInput:
    return ClipIntentPlannerInput(
        creator_request=request, latest_user_message=request, clip_facts=True
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


def test_kri511_prod_shape_keeps_both_orders_and_drops_the_skip_silently() -> None:
    # The live capture that reproduced the prod reply (drop_classes op + order_by).
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
    out = _agent().parse(raw, _cappadocia_input())
    assert [(i.op, i.attribute, i.position, i.order_by) for i in out.intents] == [
        ("order", "balloons", None, None),
        ("order", "sunset valley", "last", None),
    ]
    assert out.salvage_question is None
    assert out.salvage_reasons == []
    assert out.silent_drops == {"exclusion_dropped": 1}


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
def test_kri511_real_include_asks_are_kept(request_text: str, attribute: str, quote: str) -> None:
    raw = json.dumps(
        {"intents": [_games_intent("keep", "include", attribute, quote)], "question": None}
    )
    out = _agent().parse(raw, ClipIntentPlannerInput(creator_request=request_text))
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
    prompt = " ".join(_agent().render_prompt(_cappadocia_input()).split())
    assert "No other op exists." in prompt
    assert "Never write it as `include`" in prompt
    assert "Skip the quad bike clip" in prompt
    assert "show the balloons while I talk about the balloons" in prompt
    assert "A list of names to spell is never a request to label clips" in prompt
    assert 'never the string "null"' in prompt
