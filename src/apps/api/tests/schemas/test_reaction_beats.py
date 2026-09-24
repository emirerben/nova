"""KRI-178: reaction-beat/closing-media contract and CreativeStrategy wiring.

Mirrors the KRI-127 clip_intents precedent (tests/schemas/test_label_intent_
migration.py's schema-shape assertions): reaction_beats/closing_media must
stay OUT of CreativeStrategy's JSON schema (the Kria apply_strategy tool
schema is built from it) and out of a default strategy's dump, while still
round-tripping cleanly when a caller sets them directly.
"""

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import MAX_REACTION_BEATS, ClosingMedia, ReactionBeat
from app.kria.registry import ApplyStrategyArguments


def test_reaction_beat_round_trips():
    beat = ReactionBeat(
        beat_id="greenwood",
        trigger="Mason Greenwood",
        visual_id="media-1",
        visual_role="photo",
        sound="buzzer",
        hold_s=2.0,
    )
    dumped = beat.model_dump(mode="json")
    assert ReactionBeat.model_validate(dumped) == beat


def test_closing_media_round_trips():
    closing = ClosingMedia(visual_id="salah", badge_visual_id="goat-badge", from_trigger="Salah")
    dumped = closing.model_dump(mode="json")
    assert ClosingMedia.model_validate(dumped) == closing


def test_reaction_beat_requires_visual_id_or_sound():
    with pytest.raises(ValidationError, match="visual_id or sound"):
        ReactionBeat(beat_id="no-op", trigger="hello")
    # Either one alone is sufficient.
    ReactionBeat(beat_id="visual-only", trigger="hello", visual_id="media-1")
    ReactionBeat(beat_id="sound-only", trigger="hello", sound="ding")


def test_reaction_beat_id_must_be_slug_like():
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="not a slug!", trigger="hello", sound="ding")
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="", trigger="hello", sound="ding")
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="x" * 41, trigger="hello", sound="ding")


def test_reaction_beat_text_fields_are_stripped_and_non_empty():
    beat = ReactionBeat(beat_id="b1", trigger="  Mason   Greenwood  ", sound="  buzzer  ")
    assert beat.trigger == "Mason Greenwood"
    assert beat.sound == "buzzer"
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="b2", trigger="   ", sound="buzzer")


def test_reaction_beat_hold_s_is_bounded():
    ReactionBeat(beat_id="b1", trigger="hi", sound="ding", hold_s=0.5)
    ReactionBeat(beat_id="b2", trigger="hi", sound="ding", hold_s=8.0)
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="b3", trigger="hi", sound="ding", hold_s=0.1)
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="b4", trigger="hi", sound="ding", hold_s=9.0)


def test_reaction_beat_and_closing_media_forbid_extra_fields():
    with pytest.raises(ValidationError):
        ReactionBeat(beat_id="b1", trigger="hi", sound="ding", unexpected="nope")
    with pytest.raises(ValidationError):
        ClosingMedia(visual_id="salah", unexpected="nope")


def test_reaction_beats_are_frozen():
    beat = ReactionBeat(beat_id="b1", trigger="hi", sound="ding")
    with pytest.raises(ValidationError):
        beat.trigger = "bye"


def test_creative_strategy_default_dump_omits_reaction_fields():
    strategy = CreativeStrategy()
    dumped = strategy.model_dump(mode="json")
    assert "reaction_beats" not in dumped
    assert "closing_media" not in dumped
    dumped_exclude_none = strategy.model_dump(mode="json", exclude_none=True)
    assert "reaction_beats" not in dumped_exclude_none
    assert "closing_media" not in dumped_exclude_none


def test_creative_strategy_round_trips_reaction_beats_and_closing_media():
    strategy = CreativeStrategy(
        reaction_beats=[
            {"beat_id": "greenwood", "trigger": "Mason Greenwood", "visual_id": "m1"},
            {
                "beat_id": "greenwood-no",
                "trigger": "no",
                "after": "Mason Greenwood",
                "visual_id": "reject-x",
                "sound": "buzzer",
            },
        ],
        closing_media={
            "visual_id": "salah",
            "badge_visual_id": "goat-badge",
            "from_trigger": "Salah",
        },
    )
    dumped = strategy.model_dump(mode="json", exclude_none=True)
    assert len(dumped["reaction_beats"]) == 2
    assert dumped["closing_media"]["visual_id"] == "salah"
    assert CreativeStrategy.model_validate(dumped) == strategy


def test_creative_strategy_bounds_reaction_beats_count():
    beats = [
        {"beat_id": f"b{i}", "trigger": f"trigger {i}", "sound": "ding"}
        for i in range(MAX_REACTION_BEATS + 1)
    ]
    with pytest.raises(ValidationError):
        CreativeStrategy(reaction_beats=beats)


def test_creative_strategy_json_schema_never_mentions_reaction_fields():
    schema = CreativeStrategy.model_json_schema()
    assert "reaction_beats" not in schema["properties"]
    assert "closing_media" not in schema["properties"]
    # SkipJsonSchema must also keep any nested $defs out of the schema.
    defs = schema.get("$defs", {})
    assert "ReactionBeat" not in defs
    assert "ClosingMedia" not in defs


def test_apply_strategy_arguments_schema_never_mentions_reaction_fields():
    """The Kria `apply_strategy` tool builds its argument schema from
    CreativeStrategy -- this must stay byte-identical whether or not
    reaction_beats/closing_media exist on the model (KRI-178)."""
    schema = ApplyStrategyArguments.model_json_schema()
    schema_text = str(schema)
    assert "reaction_beats" not in schema_text
    assert "closing_media" not in schema_text
    assert "ReactionBeat" not in schema_text
    assert "ClosingMedia" not in schema_text
