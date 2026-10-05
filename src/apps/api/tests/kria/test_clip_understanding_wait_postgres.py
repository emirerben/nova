"""A chat turn waits for clip analysis already in flight instead of asking for a resend.

Prod thread b2a41da6 (2026-10-04): 7 phone clips attached 10:55:03-10:55:26Z, message at
10:55:37Z, reply "I'm still checking some of your clips ... Send it again" at 10:56:02Z,
background analysis of all 7 done at 10:56:03Z. The turn judged a clip snapshot taken
before its ~20 s Main Creator call and never looked again.

These tests use the real Postgres row, the real turn re-read (`planner._reload_intent_clips`)
and the real background writer (`kria_clip_understanding._store`).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.models import ContentPlan, CreationThread, Persona, PlanItem
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.clip_understanding import understanding_incomplete
from app.services.creator_sessions import load_intent_clips_for_item
from app.tasks import kria_clip_understanding as background
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

ANALYSIS = {"understanding": {"summary": "a man in a navy t-shirt bowls a strike"}}
REQUEST = '5. The video of the guy with glasses in the navy T-shirt bowling: "...and Eren"'


def _phone_clips(thread_id: uuid.UUID) -> list[dict]:
    return [
        {
            "media_id": f"analysis-proxy-ios-{name}.mp4",
            "gcs_path": f"users/u/creation-threads/{thread_id}/analysis-proxy-ios-{name}.mp4",
            "kind": "video",
            "duration_s": 4.0,
        }
        for name in ("F1BB989E", "0416212B")
    ]


def _project_with_phone_clips() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        db.get(PlanItem, item_id).clip_assignments = _phone_clips(thread_id)
        db.commit()
    return user_id, thread_id, item_id


def _land_analysis(item_id: uuid.UUID, thread_id: uuid.UUID) -> None:
    """What `analyze_kria_clips` writes when each clip's Gemini analysis returns."""
    for clip in _phone_clips(thread_id):
        assert background._store(item_id, {**clip, "generation": "", "analysis": ANALYSIS})


def _wire(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(
        intents=[
            PlannedClipIntent(
                intent_id="navy-bowler",
                op="label",
                attribute="the guy with glasses in the navy T-shirt bowling",
                source_quote="The video of the guy with glasses in the navy T-shirt bowling",
            )
        ]
    )
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    monkeypatch.setattr(service, "UNDERSTANDING_POLL_S", 0.05)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return resolver


async def _plan(db, clips, *, user_id, item_id, wait_s):  # noqa: ANN001, ANN202
    return await service.plan_and_resolve_clip_intents(
        creator_request=REQUEST,
        latest_user_message=None,
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
        require_clip_understanding=True,
        refresh_clips=lambda: planner._reload_intent_clips(db, item_id=item_id, creator_id=user_id),
        understanding_wait_until=time.monotonic() + wait_s,
    )


async def _turn_start_snapshot(db, item_id: uuid.UUID) -> list[IntentClip]:  # noqa: ANN001
    """The clips `_load_creator_inputs` captures before the Main Creator call."""
    item = await db.get(PlanItem, item_id)
    plan = await db.get(ContentPlan, item.content_plan_id)
    persona = await db.get(Persona, plan.persona_id)
    snapshot = await load_intent_clips_for_item(db, item, persona)
    await db.rollback()
    return snapshot


@pytest.mark.asyncio
async def test_turn_resolves_once_background_analysis_lands_during_the_wait(monkeypatch):
    resolver = _wire(monkeypatch)
    user_id, thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            snapshot = await _turn_start_snapshot(db, item_id)
            assert all(understanding_incomplete(c.analysis) for c in snapshot)

            async def _background() -> None:
                await asyncio.sleep(0.3)
                await asyncio.to_thread(_land_analysis, item_id, thread_id)

            lander = asyncio.create_task(_background())
            result = await _plan(db, snapshot, user_id=user_id, item_id=item_id, wait_s=10)
            await lander
    finally:
        await async_engine.dispose()

    assert result.resolution.status != "pending", result.resolution.diagnostics
    resolver.assert_awaited_once()
    resolved_clips = resolver.await_args.kwargs["clips"]
    assert [c.media_id for c in resolved_clips] == [c.media_id for c in snapshot]
    assert not any(understanding_incomplete(c.analysis) for c in resolved_clips)


@pytest.mark.asyncio
async def test_analysis_that_landed_during_the_main_creator_call_needs_no_wait(monkeypatch):
    """Exactly the prod race: analysis done before the gate, but after the snapshot."""
    resolver = _wire(monkeypatch)
    user_id, thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            snapshot = await _turn_start_snapshot(db, item_id)
            await asyncio.to_thread(_land_analysis, item_id, thread_id)
            result = await _plan(db, snapshot, user_id=user_id, item_id=item_id, wait_s=0)
    finally:
        await async_engine.dispose()

    assert result.resolution.status != "pending", result.resolution.diagnostics
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_analysis_that_does_not_land_in_time_still_replies_pending(monkeypatch):
    resolver = _wire(monkeypatch)
    user_id, _thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            snapshot = await _turn_start_snapshot(db, item_id)
            result = await _plan(db, snapshot, user_id=user_id, item_id=item_id, wait_s=0.3)
    finally:
        await async_engine.dispose()

    assert result.resolution.status == "pending"
    assert result.resolution.diagnostics["unanalysed_clips"] == 2
    assert result.resolution.diagnostics["understanding_wait_s"] >= 0.3
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_reread_refuses_another_creators_item():
    user_id, _thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            stranger = await planner._reload_intent_clips(
                db, item_id=item_id, creator_id=uuid.uuid4()
            )
            mine = await planner._reload_intent_clips(db, item_id=item_id, creator_id=user_id)
    finally:
        await async_engine.dispose()

    assert stranger == []
    assert len(mine) == 2
