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


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["story_beats"][0].update(thought=""), "thoughts cannot be empty"),
        (lambda payload: payload.update(duration_s=35), "creator's target"),
        (
            lambda payload: [beat.update(duration_s=1) for beat in payload["story_beats"]],
            "beat durations do not fit",
        ),
    ],
)
def test_rejects_render_critical_story_contradictions(mutate, message: str) -> None:  # noqa: ANN001
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw(["media-0", "media-1", "media-2"]))
    mutate(payload)

    with pytest.raises(SchemaError, match=message):
        agent.parse(json.dumps(payload), _input(3))
