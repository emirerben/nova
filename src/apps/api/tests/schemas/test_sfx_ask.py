"""KRI-1561: the general sound-effect ask on CreativeStrategy.

Same contract as reaction beats (tests/schemas/test_reaction_beats.py): the field stays OUT of
every derived JSON schema (the Kria `apply_strategy` tool schema stays byte-identical), OUT of a
default dump, and round-trips when set directly. The Main Creator wire schema adds it back
(`MODEL_HIDDEN`), and the creator prompt teaches it only where the manifest advertises it.
"""

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.sfx_intent import SfxAskIntent
from app.kria.registry import ApplyStrategyArguments


def test_default_dump_omits_sfx_ask():
    strategy = CreativeStrategy()
    assert "sfx_ask" not in strategy.model_dump(mode="json")
    assert "sfx_ask" not in strategy.model_dump(mode="json", exclude_none=True)


def test_sfx_ask_round_trips_and_strips_its_text():
    strategy = CreativeStrategy(sfx_ask={"request_text": "  add effects  ", "max_placements": 3})
    dumped = strategy.model_dump(mode="json", exclude_none=True)
    assert dumped["sfx_ask"] == {"request_text": "add effects", "max_placements": 3}
    assert CreativeStrategy.model_validate(dumped).sfx_ask == SfxAskIntent(
        request_text="add effects", max_placements=3
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"request_text": ""},
        {"request_text": "   "},
        {"request_text": "x", "max_placements": 0},
        {"request_text": "x", "max_placements": 7},
        {"request_text": "x", "unknown": 1},
        {"request_text": "x" * 401},
    ],
)
def test_sfx_ask_rejects_a_blank_or_unbounded_ask(bad):
    with pytest.raises(ValidationError):
        SfxAskIntent.model_validate(bad)


def test_the_derived_schemas_never_mention_sfx_ask():
    assert "sfx_ask" not in CreativeStrategy.model_json_schema()["properties"]
    schema_text = str(ApplyStrategyArguments.model_json_schema())
    assert "sfx_ask" not in schema_text and "SfxAskIntent" not in schema_text


def test_the_main_creator_wire_schema_lets_the_model_author_it():
    from app.agents._creator_wire import MODEL_HIDDEN, main_creator_schema

    assert "sfx_ask" in MODEL_HIDDEN
    wire_text = str(main_creator_schema())
    assert "sfx_ask" in wire_text and "request_text" in wire_text
