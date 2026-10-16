"""KRI-696: a tapped answer for the creator's own caption copy settles on the next turn.

Prod thread FEFCCF5C (Tigre night N2): rule 3's text ``3 · 20 minutes of darkness`` was
asked in the clip picker, the creator tapped Clip 5, and the next turn asked the same
question again. The picker settle path grounded ``creator_text`` with the short-phrase
caption bounds instead of the creator-copy bounds the resolver uses, so any creator text
with ``·``, a curly ``’`` (iPhone Smart Punctuation), ``#``, ``%``, ``/`` or ``→`` never
settled and the picker looped.

Ways this can fail, each pinned below:

1. The tapped answer is refused for punctuation the creator typed -> asked again.
2. The settled caption is rewritten (curly apostrophe straightened, ``·`` dropped).
3. The fix loosens the fence: creator copy the request no longer contains still settles.
4. The answer settles on turn 2 but the planner hands the intent to the resolver anyway.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.agents.clip_request_resolver import ClipRequestResolverAgent, ClipRequestResolverOutput
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import fold_clip_selections

_CREATOR_TEXTS = [
    "20 minutes of darkness",  # control: settled before the fix too
    "3 · 20 minutes of darkness",
    "you’re all amigos",
    "Day 1 / Lisbon",
    "#1 Gaudí",
    "100% worth it",
    "Leave before 11 →",
]


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=f"m{i}",
            kind="video",
            analysis={"subject": "night boat on the river", "description": "dark water"},
            gcs_path=f"users/u/clips/m{i}.mp4",
        )
        for i in range(1, 7)
    ]


def _caption(text: str) -> PlannedClipIntent:
    return PlannedClipIntent(
        intent_id="caption_rule3_text",
        op="caption",
        attribute="rule 3",
        creator_text=text,
        source_quote=text,
    )


def _request(text: str) -> str:
    return f'For rule 3 put the text "{text}" on the right clip.'


def _wire(monkeypatch, intents: list[PlannedClipIntent]) -> MagicMock:
    """Planner returns ``intents``; the resolver agent finds no clips on its own."""
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    planner = MagicMock()
    planner.run.return_value = ClipIntentPlannerOutput(intents=intents)
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=planner))
    resolver_agent = MagicMock(return_value=ClipRequestResolverOutput())
    monkeypatch.setattr(
        ClipRequestResolverAgent,
        "run",
        lambda self, input, *, ctx=None: resolver_agent(input),
    )
    return resolver_agent


async def _turn(request: str, selections=None):
    return await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )


async def _ask_then_tap(monkeypatch, text: str, *, turn2_request: str | None = None):
    request = _request(text)
    _wire(monkeypatch, [_caption(text)])
    first = await _turn(request)
    question = first.resolution.clip_question
    assert first.resolution.needs_creator and question is not None
    (category,) = question["categories"]
    assert category["op"] == "caption"
    events = [
        ("user", None),
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [{"key": category["key"], "media_ids": ["m5"]}],
                }
            },
        ),
    ]
    resolver_agent = _wire(monkeypatch, [_caption(text)])
    second = await _turn(turn2_request or request, fold_clip_selections(events))
    return second, resolver_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("text", _CREATOR_TEXTS)
async def test_tapped_creator_caption_settles_and_is_not_asked_again(monkeypatch, text) -> None:
    second, resolver_agent = await _ask_then_tap(monkeypatch, text)
    resolver_agent.assert_not_called()  # failure 4: settled before the resolver
    resolution = second.resolution
    assert not resolution.needs_creator and resolution.clip_question is None  # failure 1
    (intent,) = resolution.intents
    assert intent.status == "resolved"
    assert intent.media_ids() == ["m5"]
    assert (intent.caption_text, intent.caption_grounding) == (text, "creator_text")  # 2


@pytest.mark.asyncio
async def test_tapped_creator_caption_still_needs_the_request_to_contain_it(monkeypatch) -> None:
    """Failure 3: the creator-copy fence still checks the words against the request."""
    second, _ = await _ask_then_tap(
        monkeypatch, "3 · 20 minutes of darkness", turn2_request="Rule 3 is about the boat."
    )
    assert second.resolution.needs_creator
    assert all(i.caption_text is None for i in second.resolution.intents)
