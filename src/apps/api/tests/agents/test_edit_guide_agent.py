import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.edit_guide import (
    EditGuideAgent,
    EditGuideBeatInput,
    EditGuideInput,
)
from app.schemas.edit_proposal import EditConversationTurn, ProposalBrief


def _brief_payload() -> dict:
    return {
        "direction": "guided_story",
        "goal": "Show what stood out about the town and food",
        "pace": "balanced",
        "duration_s": 24,
    }


def test_briefing_forces_ready_after_three_creator_answers() -> None:
    agent_input = EditGuideInput(
        phase="briefing",
        turns=[
            EditConversationTurn(role="user", content="A travel diary"),
            EditConversationTurn(role="agent", content="What should stand out?"),
            EditConversationTurn(role="user", content="Food and architecture"),
            EditConversationTurn(role="agent", content="How should it feel?"),
            EditConversationTurn(role="user", content="Reflective and warm"),
        ],
    )
    output = EditGuideAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "reply": "I have enough to build the story.",
                "suggestions": [],
                "brief": _brief_payload(),
                "ready_to_plan": False,
                "revision": None,
            }
        ),
        agent_input,
    )
    assert output.ready_to_plan is True


def test_ready_brief_strips_generic_answer_suggestions() -> None:
    output = EditGuideAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "reply": "I’ll build a reflective 30-second travel diary.",
                "suggestions": ["Sounds great", "Let's do it"],
                "brief": {**_brief_payload(), "pace": "relaxed", "duration_s": 30},
                "ready_to_plan": True,
                "revision": None,
            }
        ),
        EditGuideInput(phase="briefing"),
    )

    assert output.ready_to_plan is True
    assert output.suggestions == []


def test_question_keeps_concrete_answer_suggestions() -> None:
    output = EditGuideAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "reply": "Which part should lead the story?",
                "suggestions": ["Food", "Architecture", "The coast"],
                "brief": _brief_payload(),
                "ready_to_plan": False,
                "revision": None,
            }
        ),
        EditGuideInput(phase="briefing"),
    )
    assert output.suggestions == ["Food", "Architecture", "The coast"]


def test_malformed_model_json_is_rejected() -> None:
    with pytest.raises(SchemaError, match="invalid output"):
        EditGuideAgent(None).parse("not-json", EditGuideInput(phase="briefing"))  # type: ignore[arg-type]


def test_whitespace_only_model_reply_is_rejected() -> None:
    with pytest.raises(SchemaError, match="reply cannot be blank"):
        EditGuideAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "reply": "   \n\t",
                    "suggestions": [],
                    "brief": _brief_payload(),
                    "ready_to_plan": False,
                    "revision": None,
                }
            ),
            EditGuideInput(phase="briefing"),
        )


def test_agent_retry_budget_stays_below_web_proxy_timeout() -> None:
    spec = EditGuideAgent.spec
    worst_case_s = spec.max_attempts * spec.timeout_s + sum(spec.backoff_s)
    assert worst_case_s < 60


def test_review_clarification_normalizes_brief_without_revision() -> None:
    agent_input = EditGuideInput(
        phase="review",
        brief=ProposalBrief(goal="Keep the coast"),
        title="Corfu",
        beats=[EditGuideBeatInput(beat_id="coast", topic="Coast", duration_s=4, media_count=1)],
    )
    output = EditGuideAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "reply": "Should food come first?",
                "suggestions": ["Yes", "No"],
                "brief": {**_brief_payload(), "goal": "A different goal"},
                "ready_to_plan": True,
                "revision": None,
            }
        ),
        agent_input,
    )
    assert output.brief == agent_input.brief


def test_briefing_cannot_smuggle_a_draft_revision() -> None:
    with pytest.raises(SchemaError, match="briefing response cannot revise"):
        EditGuideAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "reply": "Done.",
                    "suggestions": [],
                    "brief": _brief_payload(),
                    "ready_to_plan": True,
                    "revision": {
                        **_brief_payload(),
                        "title": "Corfu",
                        "story_beats": [
                            {
                                "beat_id": "food",
                                "topic": "Food",
                                "thought": "Market colors fill the frame.",
                                "layout": "fullscreen",
                                "duration_s": 4,
                            }
                        ],
                    },
                }
            ),
            EditGuideInput(phase="briefing"),
        )


def test_review_revision_must_preserve_every_beat_id_once() -> None:
    agent_input = EditGuideInput(
        phase="review",
        title="Corfu",
        beats=[
            EditGuideBeatInput(
                beat_id="food",
                topic="Food",
                duration_s=4,
                media_count=2,
            ),
            EditGuideBeatInput(
                beat_id="town",
                topic="Town",
                duration_s=4,
                media_count=2,
            ),
        ],
    )
    with pytest.raises(SchemaError, match="preserve every existing story beat"):
        EditGuideAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "reply": "I moved food first.",
                    "suggestions": [],
                    "brief": _brief_payload(),
                    "ready_to_plan": True,
                    "revision": {
                        **_brief_payload(),
                        "title": "Corfu",
                        "story_beats": [
                            {
                                "beat_id": "food",
                                "topic": "Food",
                                "thought": "Market colors fill the frame.",
                                "layout": "fullscreen",
                                "duration_s": 4,
                            }
                        ],
                    },
                }
            ),
            agent_input,
        )


def test_review_prompt_uses_short_refs_and_maps_them_back_to_server_ids() -> None:
    opaque_lisbon_id = "7dd5e0f6-4a16-4ef5-9584-c81b811a819f"
    opaque_istanbul_id = "e984706a-e702-4f54-b6e8-fce4ad11e50e"
    agent_input = EditGuideInput(
        phase="review",
        title="Summer 26",
        beats=[
            EditGuideBeatInput(
                beat_id=opaque_lisbon_id,
                topic="Architecture",
                duration_s=2,
                media_count=1,
                media_refs=["media_1"],
            ),
            EditGuideBeatInput(
                beat_id=opaque_istanbul_id,
                topic="Cityscape",
                duration_s=2,
                media_count=1,
                media_refs=["media_2"],
            ),
        ],
    )

    prompt = EditGuideAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert opaque_lisbon_id not in prompt
    assert opaque_istanbul_id not in prompt
    assert '"beat_id": "beat_1"' in prompt
    assert '"media_refs": ["media_2"]' in prompt

    output = EditGuideAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "reply": "I put Lisbon before Istanbul and shortened the labels.",
                "suggestions": [],
                "brief": {**_brief_payload(), "duration_s": 10},
                "ready_to_plan": True,
                "revision": {
                    **_brief_payload(),
                    "duration_s": 10,
                    "title": "Summer 26",
                    "story_beats": [
                        {
                            "beat_id": "beat_1",
                            "topic": "Lisbon",
                            "thought": "Lisbon",
                            "layout": "fullscreen",
                            "duration_s": 2,
                        },
                        {
                            "beat_id": "beat_2",
                            "topic": "Istanbul",
                            "thought": "Istanbul",
                            "layout": "fullscreen",
                            "duration_s": 2,
                        },
                    ],
                },
            }
        ),
        agent_input,
    )

    assert output.revision is not None
    assert [beat.beat_id for beat in output.revision.story_beats] == [
        opaque_lisbon_id,
        opaque_istanbul_id,
    ]


def test_review_revision_rejects_invented_personal_experience() -> None:
    agent_input = EditGuideInput(
        phase="review",
        title="Corfu",
        beats=[
            EditGuideBeatInput(
                beat_id="food",
                topic="Food",
                duration_s=4,
                media_count=2,
            )
        ],
    )
    with pytest.raises(SchemaError, match="unsupported personal experience"):
        EditGuideAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "reply": "I made the food chapter more personal.",
                    "suggestions": [],
                    "brief": _brief_payload(),
                    "ready_to_plan": True,
                    "revision": {
                        **_brief_payload(),
                        "title": "Corfu",
                        "story_beats": [
                            {
                                "beat_id": "food",
                                "topic": "Food",
                                "thought": "I loved how delicious every meal tasted.",
                                "layout": "fullscreen",
                                "duration_s": 4,
                            }
                        ],
                    },
                }
            ),
            agent_input,
        )


def test_review_revision_allows_creator_authored_personal_experience() -> None:
    agent_input = EditGuideInput(
        phase="review",
        title="Corfu",
        beats=[
            EditGuideBeatInput(
                beat_id="food",
                topic="Food",
                thought="I loved how lively the market felt.",
                thought_source="user",
                duration_s=4,
                media_count=2,
            )
        ],
    )
    output = EditGuideAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "reply": "I kept your wording and moved food first.",
                "suggestions": [],
                "brief": _brief_payload(),
                "ready_to_plan": True,
                "revision": {
                    **_brief_payload(),
                    "title": "Corfu",
                    "story_beats": [
                        {
                            "beat_id": "food",
                            "topic": "Food",
                            "thought": "I loved how lively the market felt.",
                            "layout": "fullscreen",
                            "duration_s": 4,
                        }
                    ],
                },
            }
        ),
        agent_input,
    )
    assert output.revision is not None
    assert output.revision.story_beats[0].thought == "I loved how lively the market felt."
