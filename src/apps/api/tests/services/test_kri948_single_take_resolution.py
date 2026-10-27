"""Missing long-take metadata is checked before another one-choice picker.

Failure modes: missing records suppress vision, a provider failure becomes a
creator question, or adding another video removes a genuine selection choice.
"""

import pytest

from app.schemas.clip_intents import ClipIntent
from tests.services.test_kri779_single_take_moments import (
    SECOND_TAKE,
    TAKE,
    _patch,
    _resolve,
    _take,
)

pytestmark = pytest.mark.asyncio


async def test_a_moment_missing_from_the_record_is_checked_before_a_single_take_picker(monkeypatch):
    intent = ClipIntent(intent_id="queue", op="order", attribute="the queue")
    transport = _patch(
        monkeypatch,
        {"intents": [{"intent_id": "queue"}]},
        vision={"the queue": ("yes", 0.95, "people waiting under the awning")},
    )
    result = await _resolve([intent], [_take()], request="show the queue")
    assert transport.vision_prompts
    assert result.status == "resolved" and result.clip_question is None
    assert result.intents[0].media_ids() == [TAKE]


async def test_a_deferred_moment_check_does_not_ask_the_creator_to_tap_the_only_video(monkeypatch):
    intent = ClipIntent(intent_id="queue", op="order", attribute="the queue")
    _patch(monkeypatch, {"intents": [{"intent_id": "queue"}]})
    result = await _resolve([intent], [_take()], request="show the queue", max_vision_requeries=0)
    assert result.status == "pending"
    assert result.clip_question is None


async def test_two_takes_keep_a_real_selection_question(monkeypatch):
    intent = ClipIntent(intent_id="queue", op="order", attribute="the queue")
    transport = _patch(monkeypatch, {"intents": [{"intent_id": "queue"}]})
    result = await _resolve([intent], [_take(), _take(SECOND_TAKE)], request="show the queue")
    assert result.status == "needs_creator"
    assert result.clip_question["categories"][0]["candidate_media_ids"] == [TAKE, SECOND_TAKE]
    assert not transport.vision_prompts


async def test_an_unknown_moment_asks_for_time_instead_of_another_single_choice_tap(monkeypatch):
    intent = ClipIntent(intent_id="queue", op="order", attribute="the queue")
    _patch(monkeypatch, {"intents": [{"intent_id": "queue"}]}, vision={})
    result = await _resolve([intent], [_take()], request="show the queue")
    assert result.status == "needs_creator"
    assert result.clip_question is None
    assert "About when" in result.question
    assert "the queue" in result.question
