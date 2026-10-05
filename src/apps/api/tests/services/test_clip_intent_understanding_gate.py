"""KRI-282 L2: hold the turn while background clip analysis is incomplete."""

import time
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


ANALYSED = {"understanding": {"summary": "a man bowling in a navy t-shirt"}}


def _refresher(*rounds):  # noqa: ANN002, ANN202
    """Each call returns the next round of clips; the last round repeats."""
    calls = []

    async def _refresh():  # noqa: ANN202
        calls.append(len(calls))
        return rounds[min(len(calls) - 1, len(rounds) - 1)]

    return _refresh, calls


@pytest.mark.asyncio
async def test_turn_waits_for_analysis_in_flight_then_resolves_with_fresh_records(monkeypatch):
    """Prod thread b2a41da6: the reply went out at 10:56:02Z, analysis landed 10:56:03Z."""
    monkeypatch.setattr(service, "UNDERSTANDING_POLL_S", 0.01)
    resolver = _wire(monkeypatch)
    stale = _clips(None, None)
    refresh, calls = _refresher(_clips(ANALYSED, None), _clips(ANALYSED, ANALYSED))

    result = await _plan(
        stale,
        require_clip_understanding=True,
        refresh_clips=refresh,
        understanding_wait_until=time.monotonic() + 5,
    )

    assert result.resolution.status != "pending"
    resolver.assert_awaited_once()
    seen = resolver.await_args.kwargs["clips"]
    assert [clip.analysis for clip in seen] == [ANALYSED, ANALYSED]
    assert len(calls) == 2, "stops polling as soon as every clip is understood"


@pytest.mark.asyncio
async def test_no_wait_budget_still_rereads_the_stale_snapshot_once(monkeypatch):
    resolver = _wire(monkeypatch)
    refresh, calls = _refresher(_clips(ANALYSED))

    await _plan(
        _clips(None),
        require_clip_understanding=True,
        refresh_clips=refresh,
        understanding_wait_until=None,
    )

    assert len(calls) == 1
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_analysis_that_never_lands_replies_pending_after_the_bounded_wait(monkeypatch):
    monkeypatch.setattr(service, "UNDERSTANDING_POLL_S", 0.01)
    resolver = _wire(monkeypatch)
    refresh, calls = _refresher(_clips(None, ANALYSED))

    result = await _plan(
        _clips(None, None),
        require_clip_understanding=True,
        refresh_clips=refresh,
        understanding_wait_until=time.monotonic() + 0.05,
    )

    assert result.resolution.status == "pending"
    assert result.resolution.error_code == "clip_understanding_incomplete"
    assert result.resolution.diagnostics["unanalysed_clips"] == 1
    assert result.resolution.diagnostics["understanding_wait_s"] >= 0.0
    assert len(calls) > 1
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_replaced_clip_keeps_its_pinned_record(monkeypatch):
    """Analysis of a re-uploaded object (new generation) never stands in for the old one."""
    resolver = _wire(monkeypatch)
    pinned = [IntentClip(media_id="m0", kind="video", analysis=None, gcs_path="a", generation="1")]
    replaced = [
        IntentClip(media_id="m0", kind="video", analysis=ANALYSED, gcs_path="a", generation="2")
    ]
    refresh, _ = _refresher(replaced)

    result = await _plan(
        pinned,
        require_clip_understanding=True,
        refresh_clips=refresh,
        understanding_wait_until=None,
    )

    assert result.resolution.status == "pending"
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_failed_reread_replies_pending_instead_of_failing_the_turn(monkeypatch):
    resolver = _wire(monkeypatch)
    refresh = AsyncMock(side_effect=RuntimeError("db went away"))

    result = await _plan(
        _clips(None),
        require_clip_understanding=True,
        refresh_clips=refresh,
        understanding_wait_until=time.monotonic() + 5,
    )

    assert result.resolution.status == "pending"
    refresh.assert_awaited_once()
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_understood_clips_never_trigger_a_reread(monkeypatch):
    resolver = _wire(monkeypatch)
    refresh = AsyncMock()

    await _plan(
        _clips(ANALYSED),
        require_clip_understanding=True,
        refresh_clips=refresh,
        understanding_wait_until=time.monotonic() + 5,
    )

    refresh.assert_not_awaited()
    resolver.assert_awaited_once()
