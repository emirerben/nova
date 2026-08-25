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


def test_fast_montage_uses_cut_sources_for_mixed_media_variety() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    agent_input = _input(3)
    agent_input.direction = "fast_montage"
    agent_input.pace = "fast"
    agent_input.target_duration_s = 3
    for media in agent_input.media:
        if media.kind == "video":
            media.duration_s = 2.0
    payload = {
        "title": "A quick Corfu cut",
        "duration_s": 3,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": "cut-1",
                "media_id": "media-0",
                "source_start_s": 0.0,
                "source_end_s": 0.8,
                "output_duration_s": 0.8,
                "role": "hook",
            },
            {
                "cut_id": "cut-2",
                "media_id": "media-1",
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_duration_s": 1.0,
                "role": "build",
            },
            {
                "cut_id": "cut-3",
                "media_id": "media-2",
                "source_start_s": 0.0,
                "source_end_s": 1.2,
                "output_duration_s": 1.2,
                "role": "payoff",
            },
        ],
    }

    output = agent.parse(json.dumps(payload), agent_input)

    assert output.story_beats == []
    assert [cut.media_id for cut in output.fast_cuts or []] == [
        "media-0",
        "media-1",
        "media-2",
    ]


def _fractional_fast_payload(*, declared_duration_s: float = 14.2) -> dict:
    cuts = []
    for index in range(14):
        duration_s = 1.2 if index == 13 else 1.0
        cuts.append(
            {
                "cut_id": f"cut-{index + 1}",
                "media_id": f"media-{index % 3}",
                "source_start_s": float(index),
                "source_end_s": round(index + duration_s, 3),
                "output_duration_s": duration_s,
                "role": "hook" if index == 0 else "payoff" if index == 13 else "build",
                "beat_align": True,
            }
        )
    return {
        "title": "A quick Corfu cut",
        "duration_s": declared_duration_s,
        "story_beats": [],
        "fast_cuts": cuts,
    }


def _fractional_fast_input(*, target_duration_s: int = 14) -> EditProposalAgentInput:
    agent_input = _input(3)
    agent_input.direction = "fast_montage"
    agent_input.pace = "fast"
    agent_input.target_duration_s = target_duration_s
    for media in agent_input.media:
        if media.kind == "video":
            media.duration_s = 30.0
    return agent_input


def test_fast_montage_reconciles_fractional_provider_duration_to_server_target() -> None:
    output = EditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(_fractional_fast_payload()),
        _fractional_fast_input(),
    )

    cuts = output.fast_cuts or []
    assert output.duration_s == 14
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(14)
    assert [cut.output_duration_s for cut in cuts[:-1]] == [1.0] * 13
    assert cuts[-1].output_duration_s == 1.0
    assert cuts[-1].source_end_s == 14.0
    assert cuts[-1].beat_align is False
    assert all(cut.beat_align for cut in cuts[:-1])


def test_fast_montage_splits_and_interleaves_recoverable_overlong_windows() -> None:
    media = [
        EditProposalMedia(
            media_id=f"media-{index}",
            lane="clip",
            kind="video",
            duration_s=10,
        )
        for index in range(5)
    ]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=14,
        media=media,
    )
    raw_cuts = [
        {
            "cut_id": f"cut-{index + 1}",
            "media_id": f"media-{index % 5}",
            "source_start_s": float(index % 2) * 2,
            "source_end_s": float(index % 2) * 2 + 1.4,
            "output_duration_s": 1.4,
            "role": "hook" if index == 0 else "payoff" if index == 9 else "build",
            "beat_align": True,
        }
        for index in range(10)
    ]

    output = EditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "title": "Fast Corfu",
                "duration_s": 14,
                "story_beats": [],
                "fast_cuts": raw_cuts,
            }
        ),
        agent_input,
    )

    cuts = output.fast_cuts or []
    assert len(cuts) == 20
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(14)
    assert all(0.4 <= cut.output_duration_s <= 1.2 for cut in cuts)
    assert all(left.media_id != right.media_id for left, right in zip(cuts, cuts[1:]))
    assert cuts[0].role == "hook"
    assert cuts[-1].role == "payoff"
    assert all(cut.role == "build" for cut in cuts[1:-1])
    assert all(cut.beat_align is False for cut in cuts)
    for raw_cut in raw_cuts:
        parts = sorted(
            (cut for cut in cuts if cut.cut_id.startswith(f"{raw_cut['cut_id']}-part-")),
            key=lambda cut: cut.source_start_s,
        )
        assert [(cut.source_start_s, cut.source_end_s) for cut in parts] == [
            (raw_cut["source_start_s"], raw_cut["source_start_s"] + 0.7),
            (raw_cut["source_start_s"] + 0.7, raw_cut["source_end_s"]),
        ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda cut: cut.update(source_end_s=cut["source_end_s"] + 0.2),
            "must match its source window",
        ),
        (lambda cut: cut.update(media_id="unknown"), "unknown media"),
        (lambda cut: cut.update(source_start_s=29.0, source_end_s=30.4), "exceeds video"),
        (lambda cut: cut.update(transition="dissolve"), "Input should be 'none'"),
        (
            lambda cut: cut.update(output_duration_s=0.3, source_end_s=0.3),
            "greater than or equal to 0.4",
        ),
    ],
)
def test_fast_montage_split_repair_rejects_material_cut_violations(
    mutation,
    message: str,  # noqa: ANN001
) -> None:
    payload = _fractional_fast_payload(declared_duration_s=14)
    payload["fast_cuts"] = payload["fast_cuts"][:10]
    for cut in payload["fast_cuts"]:
        cut["source_end_s"] = cut["source_start_s"] + 1.4
        cut["output_duration_s"] = 1.4
    mutation(payload["fast_cuts"][0])

    with pytest.raises(SchemaError, match=message):
        EditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(payload),
            _fractional_fast_input(),
        )


def test_fast_montage_rejects_expansion_beyond_cut_limit() -> None:
    media = [
        EditProposalMedia(
            media_id=f"media-{index}",
            lane="clip",
            kind="video",
            duration_s=60,
        )
        for index in range(2)
    ]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=60,
        media=media,
    )
    cuts = [
        {
            "cut_id": f"cut-{index + 1}",
            "media_id": f"media-{index % 2}",
            "source_start_s": index,
            "source_end_s": index + 1.21,
            "output_duration_s": 1.21,
            "role": "hook" if index == 0 else "payoff" if index == 49 else "build",
        }
        for index in range(50)
    ]

    with pytest.raises(SchemaError, match="expansion exceeds 80"):
        EditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "title": "Fast cut",
                    "duration_s": 60.5,
                    "story_beats": [],
                    "fast_cuts": cuts,
                }
            ),
            agent_input,
        )


def test_fast_montage_prompt_states_absolute_timing_contract() -> None:
    prompt = EditProposalAgent(None).render_prompt(_fractional_fast_input())  # type: ignore[arg-type]

    assert "ABSOLUTE LIMIT" in prompt
    assert "NEVER exceed 1.2 seconds" in prompt
    assert "14s montage needs at least 12 cuts" in prompt
    assert "For this 14s target, emit at least 12 cuts" in prompt


def test_fast_montage_uses_valid_cut_total_over_provider_declared_arithmetic() -> None:
    payload = _fractional_fast_payload()
    payload["fast_cuts"][-1]["output_duration_s"] = 1.0
    payload["fast_cuts"][-1]["source_end_s"] = 14.0

    output = EditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(payload),
        _fractional_fast_input(),
    )

    cuts = output.fast_cuts or []
    assert output.duration_s == 14
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(14)
    assert cuts[-1].output_duration_s == 1.0
    assert cuts[-1].source_end_s == 14.0
    assert cuts[-1].beat_align is True


def test_fast_montage_rejects_material_duration_drift() -> None:
    with pytest.raises(SchemaError, match="too far from the server target"):
        EditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(_fractional_fast_payload(declared_duration_s=20)),
            _fractional_fast_input(),
        )


def test_fast_montage_rejects_unreconcilable_duration_drift() -> None:
    media = [
        EditProposalMedia(
            media_id=f"media-{index}",
            lane="clip",
            kind="video",
            duration_s=1.16,
        )
        for index in range(10)
    ]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=12,
        media=media,
    )
    cuts = [
        {
            "cut_id": f"cut-{index + 1}",
            "media_id": f"media-{index}",
            "source_start_s": 0,
            "source_end_s": 1.16,
            "output_duration_s": 1.16,
            "role": "hook" if index == 0 else "payoff" if index == 9 else "build",
        }
        for index in range(10)
    ]

    with pytest.raises(SchemaError, match="cannot fit the server target"):
        EditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "title": "A quick cut",
                    # The declaration matches the server target, but the ten
                    # source-pinned cuts total only 11.6s and cannot extend.
                    # Removing the declaration-vs-cuts check must not make this
                    # unsafe schedule acceptable.
                    "duration_s": 12,
                    "story_beats": [],
                    "fast_cuts": cuts,
                }
            ),
            agent_input,
        )


def test_fast_montage_duration_repair_never_reuses_source_footage() -> None:
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=4,
        media=[
            EditProposalMedia(media_id="a", lane="clip", kind="video", duration_s=1.6),
            EditProposalMedia(media_id="b", lane="clip", kind="video", duration_s=0.8),
            EditProposalMedia(media_id="c", lane="clip", kind="video", duration_s=0.8),
        ],
    )
    cuts = [
        ("cut-a-1", "a", 0.0, 0.8),
        ("cut-c", "c", 0.0, 0.8),
        ("cut-b", "b", 0.0, 0.8),
        ("cut-a-2", "a", 0.8, 1.6),
    ]
    payload = {
        "title": "No repeated source footage",
        "duration_s": 4,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": cut_id,
                "media_id": media_id,
                "source_start_s": source_start_s,
                "source_end_s": source_end_s,
                "output_duration_s": source_end_s - source_start_s,
                "role": "hook" if index == 0 else "payoff" if index == 3 else "build",
            }
            for index, (cut_id, media_id, source_start_s, source_end_s) in enumerate(cuts)
        ],
    }

    with pytest.raises(SchemaError, match="cannot fit the server target"):
        EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]


def test_fast_montage_rejects_existing_overlapping_source_footage() -> None:
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=4,
        media=[
            EditProposalMedia(media_id="a", lane="clip", kind="video", duration_s=2.0),
            EditProposalMedia(media_id="b", lane="clip", kind="video", duration_s=1.0),
            EditProposalMedia(media_id="c", lane="clip", kind="video", duration_s=1.0),
        ],
    )
    payload = {
        "title": "No overlapping source footage",
        "duration_s": 4,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": "cut-a-1",
                "media_id": "a",
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_duration_s": 1.0,
                "role": "hook",
            },
            {
                "cut_id": "cut-b",
                "media_id": "b",
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_duration_s": 1.0,
                "role": "build",
            },
            {
                "cut_id": "cut-c",
                "media_id": "c",
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_duration_s": 1.0,
                "role": "build",
            },
            {
                "cut_id": "cut-a-2",
                "media_id": "a",
                "source_start_s": 0.8,
                "source_end_s": 1.8,
                "output_duration_s": 1.0,
                "role": "payoff",
            },
        ],
    }

    with pytest.raises(SchemaError, match="reuses overlapping source footage"):
        EditProposalAgent(None).parse(json.dumps(payload), agent_input)  # type: ignore[arg-type]


def test_guided_story_fractional_duration_remains_invalid() -> None:
    payload = json.loads(_raw(["media-0", "media-1", "media-2"]))
    payload["duration_s"] = 24.2

    with pytest.raises(SchemaError, match="valid integer"):
        EditProposalAgent(None).parse(json.dumps(payload), _input(3))  # type: ignore[arg-type]


def test_accepts_one_intentionally_unused_source_from_six() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    selected = [f"media-{index}" for index in range(5)]
    agent_input = _input(6)
    agent_input.goal = "Make a 10-second travel reel and leave one weaker clip unused."
    agent_input.target_duration_s = 15
    payload = json.loads(_raw(selected))
    payload["duration_s"] = 15
    for beat in payload["story_beats"]:
        beat["duration_s"] = 5

    output = agent.parse(json.dumps(payload), agent_input)

    assert {media_id for beat in output.story_beats for media_id in beat.media_ids} == set(selected)


def test_rejects_two_unused_sources_from_six() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]

    with pytest.raises(SchemaError, match="need at least 5"):
        agent.parse(_raw([f"media-{index}" for index in range(4)]), _input(6))


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
