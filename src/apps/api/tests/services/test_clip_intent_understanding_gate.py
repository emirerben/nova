"""KRI-282 L2: hold the turn while background clip analysis is incomplete."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, IntentResolution


def _wire(monkeypatch):  # noqa: ANN001, ANN202
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(
        intents=[
            PlannedClipIntent(
                intent_id="sport", op="label", attribute="sport", source_quote="label each sport"
            )
        ]
    )
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return resolver


def _clips(*analyses):  # noqa: ANN002, ANN202
    return [IntentClip(media_id=f"m{i}", kind="video", analysis=a) for i, a in enumerate(analyses)]


async def _plan(clips, **kw):  # noqa: ANN001, ANN003, ANN202
    return await service.plan_and_resolve_clip_intents(
        creator_request="label each sport",
        latest_user_message=None,
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
        **kw,
    )


@pytest.mark.asyncio
async def test_unanalysed_clip_returns_pending_not_a_creator_question(monkeypatch):
    resolver = _wire(monkeypatch)
    clips = _clips({"understanding": {"summary": "five a side football"}}, {"source": "probe_only"})
    result = await _plan(clips, require_clip_understanding=True)
    assert result.resolution.status == "pending"
    assert not result.resolution.needs_creator and result.resolution.question is None
    assert result.resolution.diagnostics["unanalysed_clips"] == 1
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_fully_analysed_clips_reach_the_resolver(monkeypatch):
    resolver = _wire(monkeypatch)
    clips = _clips(
        {"understanding": {"summary": "football"}},
        {"understanding_attempts": 3},  # settled: analysed-but-empty never blocks a turn
    )
    await _plan(clips, require_clip_understanding=True)
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_is_opt_in(monkeypatch):
    resolver = _wire(monkeypatch)
    await _plan(_clips(None), require_clip_understanding=False)
    resolver.assert_awaited_once()
