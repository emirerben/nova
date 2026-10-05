"""iPhone clip vision answers survive the turn, so "go ahead" finishes the checks.

Prod thread D1FDCA87 (2026-10-05, La Mercè, 8 phone clips): turn 1 needed more than the
18-call vision budget and replied "I'm still checking some of your clips". Every phone
clip is a raw `clip_assignments` row, and the answer cache wrote only pool assets, so
the follow-up turn started from zero. It finished only because that turn's planner
happened to emit fewer intents.

These tests use the real Postgres row, the real cache writer
(`persist_clip_intent_vision_answers`), the real clip loader
(`load_intent_clips_for_item`) and the real resolver with fake agents.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_question import ClipQuestionOutput
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.models import ContentPlan, CreationThread, Persona, PlanItem
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_answers import persist_clip_intent_vision_answers
from app.services.clip_intent_resolution import ANSWERS_KEY, resolve_clip_intents_for_turn
from app.services.creator_sessions import load_intent_clips_for_item
from app.tasks import kria_clip_understanding as background
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

NAMES = ("F1BB989E", "0416212B", "9C2D7A11")
ANALYSIS = {"understanding": {"summary": "a human tower in a square, crowd below"}}
REQUEST = "Chapter 1 · a helmeted child at the very top of a tall human tower · Watch the top."


def _phone_clips(thread_id: uuid.UUID, *, generation: str = "1759657402") -> list[dict]:
    return [
        {
            "media_id": f"analysis-proxy-ios-{name}.mp4",
            "gcs_path": f"users/u/creation-threads/{thread_id}/analysis-proxy-ios-{name}.mp4",
            "kind": "video",
            "duration_s": 4.0,
            "storage_generation": f"{generation}{index}",
            "analysis": ANALYSIS,
        }
        for index, name in enumerate(NAMES)
    ]


def _project_with_phone_clips() -> tuple[uuid.UUID, uuid.UUID]:
    _user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        db.get(PlanItem, item_id).clip_assignments = _phone_clips(thread_id)
        db.commit()
    return thread_id, item_id


def _wire_agents(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The resolver wants every clip checked; returns the clips vision was asked about."""
    asked: list[str] = []

    def _resolver(self, input, *, ctx=None):  # noqa: A002, ANN001, ANN202
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="chapter_1",
                    needs_vision=[
                        ResolverVisionQuestion(
                            media=clip.alias, question="Is a child at the top of the tower?"
                        )
                        for clip in input.clips
                    ],
                )
            ]
        )

    async def _vision(candidate, clip, **_kwargs):  # noqa: ANN001, ANN202
        asked.append(clip.media_id)
        return ClipQuestionOutput(answer="yes", confidence=0.9, evidence="child on top")

    monkeypatch.setattr(resolution, "default_client", lambda: None)
    monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolver)
    monkeypatch.setattr(resolution, "_run_vision_candidate", _vision)
    return asked


async def _clips(db, item_id: uuid.UUID):  # noqa: ANN001, ANN202
    item = await db.get(PlanItem, item_id, populate_existing=True)
    plan = await db.get(ContentPlan, item.content_plan_id)
    persona = await db.get(Persona, plan.persona_id)
    clips = await load_intent_clips_for_item(db, item, persona)
    await db.rollback()
    return clips


async def _turn(db, item_id: uuid.UUID):  # noqa: ANN001, ANN202
    """One Kria turn's clip-intent work: resolve with a 2-call budget, then cache."""
    result = await resolve_clip_intents_for_turn(
        intents=[ClipIntent(intent_id="chapter_1", op="include", attribute="Chapter 1")],
        creator_request=REQUEST,
        clips=await _clips(db, item_id),
        run_context=RunContext(),
        max_vision_requeries=2,
        vision_deadline_s=30,
    )
    item = await db.get(PlanItem, item_id, populate_existing=True)
    await persist_clip_intent_vision_answers(db, item, result.vision_answers, strict=True)
    await db.commit()
    return result


@pytest.mark.asyncio
async def test_follow_up_turn_only_checks_what_the_budget_left_unchecked(monkeypatch):
    asked = _wire_agents(monkeypatch)
    _thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            first = await _turn(db, item_id)
            first_asked = list(asked)
            second = await _turn(db, item_id)
    finally:
        await async_engine.dispose()

    assert first.status == "pending", first.diagnostics
    assert first.diagnostics["vision_calls"] == 2
    assert first.diagnostics["vision_over_cap"] == 1
    assert len(first_asked) == 2

    # The follow-up re-checks nothing it already knows: one call, then resolved.
    assert asked[2:] == [
        m for m in (f"analysis-proxy-ios-{n}.mp4" for n in NAMES) if m not in first_asked
    ]
    assert second.status == "resolved", second.diagnostics
    assert second.diagnostics["vision_cached"] == 2
    assert {a.media_id for a in second.intents[0].assignments} == {
        f"analysis-proxy-ios-{n}.mp4" for n in NAMES
    }


@pytest.mark.asyncio
async def test_cached_answers_keep_the_clip_record_and_survive_late_analysis(monkeypatch):
    _wire_agents(monkeypatch)
    thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            await _turn(db, item_id)
    finally:
        await async_engine.dispose()

    with sync_session() as db:
        rows = db.get(PlanItem, item_id).clip_assignments
    cached = [row for row in rows if row["analysis"].get(ANSWERS_KEY)]
    assert len(cached) == 2
    for row in cached:
        assert row["analysis"]["understanding"] == ANALYSIS["understanding"]
        (answer,) = row["analysis"][ANSWERS_KEY].values()
        assert answer["answer"] == "yes"
        assert answer["generation"] == row["storage_generation"]

    # The background analyzer merges into the same analysis and keeps the answers.
    clip = _phone_clips(thread_id)[0]
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        item.clip_assignments = [
            {**row, "analysis": {**row["analysis"], "understanding": {}}}
            if row["media_id"] == clip["media_id"]
            else row
            for row in item.clip_assignments
        ]
        db.commit()
    assert background._store(
        item_id, {**clip, "generation": clip["storage_generation"], "analysis": ANALYSIS}
    )
    with sync_session() as db:
        row = next(
            r
            for r in db.get(PlanItem, item_id).clip_assignments
            if r["media_id"] == clip["media_id"]
        )
    assert row["analysis"]["understanding"] == ANALYSIS["understanding"]
    assert (ANSWERS_KEY in row["analysis"]) == any(
        r["media_id"] == clip["media_id"] for r in cached
    )


@pytest.mark.asyncio
async def test_a_replaced_clip_never_inherits_the_old_answer(monkeypatch):
    """An answer is only kept for the storage generation that produced it."""
    asked = _wire_agents(monkeypatch)
    thread_id, item_id = _project_with_phone_clips()
    try:
        async with AsyncSessionLocal() as db:
            result = await resolve_clip_intents_for_turn(
                intents=[ClipIntent(intent_id="chapter_1", op="include", attribute="Chapter 1")],
                creator_request=REQUEST,
                clips=await _clips(db, item_id),
                run_context=RunContext(),
                max_vision_requeries=3,
                vision_deadline_s=30,
            )
            # The creator replaced every clip while vision ran.
            with sync_session() as sync_db:
                sync_db.get(PlanItem, item_id).clip_assignments = _phone_clips(
                    thread_id, generation="1759657999"
                )
                sync_db.commit()
            item = await db.get(PlanItem, item_id, populate_existing=True)
            await persist_clip_intent_vision_answers(db, item, result.vision_answers, strict=True)
            await db.commit()
    finally:
        await async_engine.dispose()

    assert len(asked) == 3
    with sync_session() as db:
        rows = db.get(PlanItem, item_id).clip_assignments
    assert not any(ANSWERS_KEY in row["analysis"] for row in rows)


@pytest.mark.asyncio
async def test_a_failed_cache_write_never_fails_the_turn(monkeypatch):
    """KRI-291 still holds: phone-clip cache bookkeeping is best effort, even when strict."""
    _wire_agents(monkeypatch)
    _thread_id, item_id = _project_with_phone_clips()

    def _boom(*_args, **_kwargs):  # noqa: ANN202
        raise RuntimeError("facade refused")

    monkeypatch.setattr("app.services.plan_item_media.mutate_plan_item_media", _boom)
    try:
        async with AsyncSessionLocal() as db:
            result = await _turn(db, item_id)
    finally:
        await async_engine.dispose()

    assert result.vision_answers  # answered this turn, just not cached
    with sync_session() as db:
        rows = db.get(PlanItem, item_id).clip_assignments
    assert not any(ANSWERS_KEY in row["analysis"] for row in rows)
