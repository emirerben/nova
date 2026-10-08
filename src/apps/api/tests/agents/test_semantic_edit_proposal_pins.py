"""KRI-526: the cloud semantic agent must know about the creator's pinned corner text.

Failure modes (written before the code): the agent input never carried the pins, so a quoted
pin in the request ("put 'Part 1' top left") was also treated as an allowed beat caption and
could be placed as a thought / text binding, burning the same words twice; and the prompt gave
the model no reason to avoid it. A pin-free input must stay unchanged.
"""

from __future__ import annotations

import pytest

from app.agents.semantic_edit_proposal import SemanticEditProposalAgent, _creator_captions
from app.schemas.edit_proposal import PinnedText
from tests.agents.test_semantic_edit_proposal import _input, _raw

PIN = PinnedText(text="Part 1", corner="top_left")
REQUEST = 'Put "Part 1" in the top left corner the whole video.'


def _parse(raw: str, **updates: object):  # noqa: ANN202
    return SemanticEditProposalAgent(None).parse(raw, _input(**updates))  # type: ignore[arg-type]


def test_a_quoted_pin_is_not_an_allowed_beat_caption() -> None:
    assert _creator_captions(_input(creator_request=REQUEST)) != {}  # the old behaviour
    assert _creator_captions(_input(creator_request=REQUEST, pinned_texts=[PIN])) == {}


def test_a_pin_repeated_as_a_thought_is_blanked() -> None:
    raw = _raw(
        chapters=[
            {
                "chapter_id": "one",
                "topic": "Park",
                "thought": "Part 1",
                "role": "hook",
                "weight": 1,
                "layout": "fullscreen",
                "sources": [{"media_id": "m001", "candidate_index": 0, "weight": 1}],
            },
            {
                "chapter_id": "two",
                "topic": "Pub",
                "thought": "",
                "role": "payoff",
                "weight": 1,
                "layout": "fullscreen",
                "sources": [{"media_id": "m002", "candidate_index": 0, "weight": 1}],
            },
        ]
    )
    without = _parse(raw, creator_request=REQUEST)
    assert without.chapters[0].thought == "Part 1"  # the leak this ticket closes

    plan = _parse(raw, creator_request=REQUEST, pinned_texts=[PIN])
    assert [chapter.thought for chapter in plan.chapters] == ["", ""]
    assert "blanked_pinned_text_thought:0" in plan.repairs


@pytest.mark.parametrize("direction", ["fast_montage", "guided_story"])
def test_a_pin_repeated_as_a_text_binding_is_dropped(direction: str) -> None:
    raw = _raw(text_bindings=[{"text": "Part 1", "chapter_ids": ["one"]}])
    plan = _parse(raw, direction=direction, creator_request=REQUEST, pinned_texts=[PIN])
    assert plan.text_bindings == []


def test_a_pin_free_prompt_is_unchanged_and_a_pinned_one_names_the_text() -> None:
    agent = SemanticEditProposalAgent(None)
    plain = agent.render_prompt(_input())  # type: ignore[arg-type]
    pinned = agent.render_prompt(_input(pinned_texts=[PIN]))  # type: ignore[arg-type]
    assert "PINNED CORNER TEXT" not in plain
    assert "PINNED CORNER TEXT" in pinned and '["Part 1"]' in pinned
    assert "pinned_texts" not in _input().model_dump(mode="json")
