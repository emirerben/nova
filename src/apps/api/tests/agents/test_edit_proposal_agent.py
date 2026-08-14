import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
)


def _input(count: int = 7) -> EditProposalAgentInput:
    return EditProposalAgentInput(
        idea="Corfu trip",
        direction="guided_story",
        goal="Show food, town, and coast",
        pace="balanced",
        target_duration_s=24,
        media=[
            EditProposalMedia(
                media_id=f"media-{index}",
                lane="asset" if index else "clip",
                kind="image" if index % 2 else "video",
                subject=f"subject {index}",
            )
            for index in range(count)
        ],
    )


def _raw(media_ids: list[str]) -> str:
    return json.dumps(
        {
            "title": "What I noticed in Corfu",
            "duration_s": 24,
            "story_beats": [
                {
                    "topic": f"Corfu {index + 1}",
                    "thought": "A visible detail worth revisiting.",
                    "media_ids": media_ids[index::3] or media_ids[:1],
                    "layout": "fullscreen",
                    "duration_s": 7,
                }
                for index in range(3)
            ],
        }
    )


def test_requires_seven_distinct_sources_when_available() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaError, match="need at least 7"):
        agent.parse(_raw(["media-0"]), _input())


def test_accepts_every_source_for_a_small_upload() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    output = agent.parse(_raw(["media-0", "media-1", "media-2"]), _input(3))
    assert output.title == "What I noticed in Corfu"


def test_rejects_repeated_chapter_topics() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw([f"media-{index}" for index in range(7)]))
    for beat in payload["story_beats"]:
        beat["topic"] = "Architecture"

    with pytest.raises(SchemaError, match="at least 3 distinct topics"):
        agent.parse(json.dumps(payload), _input())


def test_rejects_more_than_five_chapters() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw([f"media-{index}" for index in range(7)]))
    payload["story_beats"] = [
        {
            "topic": f"Chapter {index}",
            "thought": "A visible detail connects this part of the story.",
            "media_ids": [f"media-{index}", f"media-{(index + 1) % 7}"],
            "layout": "fullscreen",
            "duration_s": 4,
        }
        for index in range(6)
    ]

    with pytest.raises(SchemaError, match="at most 5 items"):
        agent.parse(json.dumps(payload), _input())


def test_rejects_unsupported_personal_draft_without_creator_context() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw([f"media-{index}" for index in range(7)]))
    payload["story_beats"][0]["thought"] = "Enjoying a delicious meal by the water."

    with pytest.raises(SchemaError, match="unsupported personal experience"):
        agent.parse(json.dumps(payload), _input())


def test_rejects_context_free_action_lead() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw([f"media-{index}" for index in range(7)]))
    payload["story_beats"][0]["thought"] = "Exploring the narrow streets at sunset."

    with pytest.raises(SchemaError, match="unsupported personal experience"):
        agent.parse(json.dumps(payload), _input())


def test_neutralizes_context_free_sensory_modifier() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw([f"media-{index}" for index in range(7)]))
    payload["story_beats"][0]["thought"] = "A refreshing ice cream sits beside a tasty pastry."

    output = agent.parse(json.dumps(payload), _input())

    assert output.story_beats[0].thought == "An ice cream sits beside a pastry."


def test_creator_context_can_authorize_a_personal_draft() -> None:
    agent_input = _input()
    for media in agent_input.media:
        media.user_context = "I loved this meal by the water."
    payload = json.loads(_raw([f"media-{index}" for index in range(7)]))
    payload["story_beats"][0]["thought"] = "I loved this meal by the water."

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]

    assert output.story_beats[0].thought == "I loved this meal by the water."


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["story_beats"][0].update(thought=""), "thoughts cannot be empty"),
        (lambda payload: payload.update(duration_s=35), "creator's target"),
        (
            lambda payload: [beat.update(duration_s=1) for beat in payload["story_beats"]],
            "beat durations do not fit",
        ),
        (
            lambda payload: payload["story_beats"][0].update(
                thought="one two three four five six seven eight nine ten eleven twelve thirteen "
                "fourteen fifteen sixteen seventeen eighteen nineteen"
            ),
            "exceeds 18 words",
        ),
    ],
)
def test_rejects_render_critical_story_contradictions(mutate, message: str) -> None:  # noqa: ANN001
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw(["media-0", "media-1", "media-2"]))
    mutate(payload)

    with pytest.raises(SchemaError, match=message):
        agent.parse(json.dumps(payload), _input(3))
