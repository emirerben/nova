"""KRI-282: a long, many-instruction creator prompt must degrade to a specific
question, never to the generic "couldn't safely verify" dead end.

The prompt below is a synthetic stand-in shaped like the failing production
request (50 clips, long creative brief, caption/order/group/label lines, curly
quotes, multi-line caption copy). No real creator text is committed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    ClipIntentSchemaError,
)
from app.schemas.clip_intents import MAX_CLIP_INTENTS
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from tests.agents.conftest import MockModelClient

LONG_PROMPT = (
    "Make a fast, energetic montage of my weekend trip with all of these clips. "
    "I want it to feel like a highlight reel that opens on the best moment and keeps "
    "building until the very last second.\n\n"
    "1. Put the sunrise clips first.\n"
    "2. Group the market clips together.\n"
    "3. Label each food item you can see.\n"
    "4. Say “Market morning” on the market chapter.\n"
    "5. Group the beach clips together.\n"
    "6. Say “Salt and sun\n   all day” on the beach chapter.\n"
    "7. Put the night clips last.\n"
    "8. Label the activity in each clip.\n"
    "9. Say “Home again” on the night chapter.\n"
)


def _clips(count: int = 50) -> list[IntentClip]:
    return [IntentClip(media_id=f"clip-{i}", kind="video", analysis=None) for i in range(count)]


def _intent(intent_id: str, op: str, attribute: str, quote: str, **extra) -> dict:
    base = {
        "intent_id": intent_id,
        "op": op,
        "attribute": attribute,
        "label_source": "clip",
        "transcript_kind": None,
        "creator_text": None,
        "caption_attribute": None,
        "position": None,
        "source_quote": quote,
    }
    base.update(extra)
    return base


def _nine_intents() -> list[dict]:
    # Straight quotes where the source has curly ones, and a collapsed newline in
    # the multi-line caption: exactly the typography drift a model produces.
    return [
        _intent("i1", "order", "sunrise clips", "Put the sunrise clips first", position="first"),
        _intent("i2", "group", "market clips", "Group the market clips together"),
        _intent("i3", "label", "food item", "Label each food item you can see"),
        _intent(
            "i4",
            "caption",
            "market chapter",
            'Say "Market morning" on the market chapter',
            creator_text="Market morning",
        ),
        _intent("i5", "group", "beach clips", "Group the beach clips together"),
        _intent(
            "i6",
            "caption",
            "beach chapter",
            'Say "Salt and sun all day" on the beach chapter',
            creator_text="Salt and sun all day",
        ),
        _intent("i7", "order", "night clips", "Put the night clips last", position="last"),
        _intent("i8", "label", "activity", "Label the activity in each clip"),
        _intent(
            "i9",
            "caption",
            "night chapter",
            'Say "Home again" on the night chapter',
            creator_text="Home again",
        ),
    ]


def _agent() -> ClipIntentPlannerAgent:
    return ClipIntentPlannerAgent(None)  # type: ignore[arg-type]


def _parse(intents: list[dict], request: str = LONG_PROMPT):
    return _agent().parse(
        json.dumps({"intents": intents, "question": None}),
        ClipIntentPlannerInput(creator_request=request),
    )


def test_curly_quotes_and_multiline_copy_are_source_backed() -> None:
    out = _parse(_nine_intents()[3:6])
    assert out.salvage_question is None
    assert [intent.creator_text for intent in out.intents if intent.op == "caption"] == [
        "Market morning",
        "Salt and sun all day",
    ]


def test_over_cap_keeps_first_valid_intents_and_asks_about_the_rest() -> None:
    out = _parse(_nine_intents())
    assert [intent.intent_id for intent in out.intents] == [f"i{n}" for n in range(1, 7)]
    assert len(out.intents) == MAX_CLIP_INTENTS
    assert out.salvage_question is not None
    assert "3 more" in out.salvage_question
    assert "Home again" not in out.salvage_question  # names instructions, never copies copy
    assert len(out.salvage_question) <= 400


def test_invalid_intents_are_dropped_and_named() -> None:
    intents = _nine_intents()[:3]
    intents.append(_intent("bad", "group", "rooftop clips", "Group the rooftop clips"))
    out = _parse(intents)
    assert [intent.intent_id for intent in out.intents] == ["i1", "i2", "i3"]
    assert out.salvage_question is not None
    assert "group: rooftop clips" in out.salvage_question
    assert "3 of your clip instructions" in out.salvage_question


def test_over_long_creator_text_drops_only_that_intent() -> None:
    long_copy = "x" * 80
    request = f'{LONG_PROMPT}\nSay "{long_copy}" on the finale chapter.'
    intents = [
        *_nine_intents()[:2],
        _intent(
            "long",
            "caption",
            "finale chapter",
            f'Say "{long_copy}" on the finale chapter',
            creator_text=long_copy,
        ),
    ]
    out = _parse(intents, request)
    assert [intent.intent_id for intent in out.intents] == ["i1", "i2"]
    assert "caption: finale chapter" in (out.salvage_question or "")


def test_benign_shape_drift_is_repaired_not_rejected() -> None:
    intents = [
        # position on a non-order op, caption_attribute on a non-caption op
        _intent("a", "group", "market clips", "Group the market clips together", position="first"),
        _intent(
            "b", "label", "food item", "Label each food item you can see", caption_attribute="x"
        ),
    ]
    out = _parse(intents)
    assert out.salvage_question is None
    assert all(i.position is None and i.caption_attribute is None for i in out.intents)


def test_all_rejected_raises_with_closed_vocabulary_class_and_dropped_previews() -> None:
    with pytest.raises(ClipIntentSchemaError) as info:
        _parse([_intent("bad", "group", "rooftop clips", "Group the rooftop clips")])
    assert info.value.error_class == "source_quote_not_creator_text"
    assert info.value.dropped == ["group: rooftop clips"]


def test_retry_clarification_names_the_actual_schema_error() -> None:
    agent = _agent()
    agent._last_schema_error = "intents[2]: creator_text is over 60 characters"
    assert "creator_text is over 60 characters" in agent.schema_clarification()
    agent._last_schema_error = ""
    assert "Fix this schema error" not in agent.schema_clarification()


def test_sensitive_run_records_error_class_but_never_content() -> None:
    agent = _agent()
    err = ClipIntentSchemaError("secret creator words", error_class="intent_invalid:creator_text")
    assert agent._safe_error(err) == "sensitive_agent_error:intent_invalid:creator_text"
    assert "secret" not in (agent._safe_error(err) or "")
    assert agent._safe_error(ValueError("secret")) == "sensitive_agent_error"


def _wire(monkeypatch, client: MockModelClient) -> AsyncMock:
    monkeypatch.setattr(service, "default_client", lambda: client)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return resolver


@pytest.mark.asyncio
async def test_fifty_clip_long_prompt_over_cap_asks_one_focused_question(monkeypatch) -> None:
    client = MockModelClient()
    client.queue("gemini-2.5-flash", {"intents": _nine_intents(), "question": None})
    resolver = _wire(monkeypatch, client)

    result = await service.plan_and_resolve_clip_intents(
        creator_request=LONG_PROMPT,
        latest_user_message=LONG_PROMPT,
        candidate_intents=None,
        clips=_clips(50),
        run_context=RunContext(),
    )

    assert len(client.invocations) == 1  # salvaged, no retry spent
    assert result.resolution.needs_creator
    question = result.resolution.question or ""
    assert "3 more" in question
    assert "restate which clips to use" not in question  # not the generic dead end
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_fifty_clip_long_prompt_within_cap_resolves_all(monkeypatch) -> None:
    client = MockModelClient()
    client.queue("gemini-2.5-flash", {"intents": _nine_intents()[3:6], "question": None})
    resolver = _wire(monkeypatch, client)

    result = await service.plan_and_resolve_clip_intents(
        creator_request=LONG_PROMPT,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(50),
        run_context=RunContext(),
    )

    assert not result.resolution.needs_creator
    assert len(result.requested_intents) == 3
    assert len(resolver.await_args.kwargs["clips"]) == 50


@pytest.mark.asyncio
async def test_terminal_failure_feeds_error_to_retry_names_dropped_and_logs_class_only(
    monkeypatch,
) -> None:
    bad = {"intents": [_intent("bad", "group", "rooftop clips", "Group the rooftop clips")]}
    client = MockModelClient()
    client.queue("gemini-2.5-flash", bad, bad)
    _wire(monkeypatch, client)

    with capture_logs() as logs:
        result = await service.plan_and_resolve_clip_intents(
            creator_request=LONG_PROMPT,
            latest_user_message=None,
            candidate_intents=None,
            clips=_clips(50),
            run_context=RunContext(),
        )

    assert len(client.invocations) == 2
    retry_prompt = client.invocations[1]["prompt"]
    assert "Fix this schema error" in retry_prompt
    assert "source_quote is not an exact contiguous substring" in retry_prompt
    question = result.resolution.question or ""
    assert "group: rooftop clips" in question
    event = next(e for e in logs if e["event"] == "clip_intent_planner.terminal_schema")
    assert event["error_class"] == "source_quote_not_creator_text"
    assert event["request_chars"] == len(LONG_PROMPT)
    assert "Market morning" not in json.dumps(event, default=str)
