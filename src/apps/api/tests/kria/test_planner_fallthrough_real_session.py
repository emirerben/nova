"""Regression: copilot-first declining must fall through to extraction on a REAL session.

`_plan_editor_revision` rolls the session back before its model call, which expires
every loaded ORM row. The first copilot-first release then read `persona`/`item`
attributes from async code on the extract-first path and crashed every declined turn
with MissingGreenlet (2026-09-30, "I couldn't finish that step"). Mocked sessions
cannot expire rows, so this runs against the test Postgres.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.creator_agent import AskUser
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.models import CreationThread, Job, PlanItem
from tests.kria.test_creative_brief import _ASK, _MANIFEST, _upd
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        job = Job(
            id=uuid.uuid4(),
            user_id=user_id,
            status="variants_ready",
            mode="content_plan",
            raw_storage_path="users/t/raw.mp4",
            selected_platforms=[],
        )
        db.add(job)
        db.flush()
        item = db.get(PlanItem, item_id)
        item.current_job_id = job.id
        db.commit()
    return user_id, thread_id, item_id


def _response(outcome: str, ops: list[dict] | None = None):
    return SimpleNamespace(
        intent="edit" if ops else "clarify",
        ops=ops or [],
        reply="r",
        outcome=outcome,
        rejection_reasons=[],
        unmet_requests=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,message,response",
    [
        (
            "clarification",
            "tighten the pacing a bit",
            _response("clarification"),
        ),
        (
            "unsupported",
            "tighten the pacing a bit",
            _response("unsupported"),
        ),
        (
            "structural",
            "remove clip 4",
            _response("proposed", [{"op": "remove_clip", "slot_index": 3}]),
        ),
        (
            "replan_wording",
            "Make it a completely different vibe, use only the best 3 clips",
            _response("proposed", [{"op": "edit_text", "bar_index": 0, "text": "x"}]),
        ),
    ],
)
async def test_declined_fast_path_falls_through_without_missing_greenlet(
    monkeypatch: pytest.MonkeyPatch, case: str, message: str, response
) -> None:
    user_id, thread_id, item_id = _seed()
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", True)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item_id)})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    target = SimpleNamespace(
        job_id=uuid.uuid4(),
        snapshot={"text_bars": [], "allowed_op_families": ["text", "clip"]},
        conversation=[],
    )
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(return_value=response))
    calls = []

    async def creator(inputs, **_kw):  # noqa: ANN001, ANN202
        calls.append(inputs)
        return SimpleNamespace(action=AskUser(**_ASK), brief_updates=[_upd("select", "global")])

    monkeypatch.setattr(planner, "_call_main_creator", creator)
    # Same construction as the runtime: an unpooled engine that lives for this loop.
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            result = await planner.plan_live_turn(
                db,
                thread_id=thread_id,
                item_id=item_id,
                creator_id=user_id,
                user_message=message,
            )
    finally:
        await engine.dispose()
    assert calls, f"{case}: the extraction must run after the fast path declined"
    assert result.defer_brief is False
    assert result.brief_route in {"replan", "editor_ops"}


@pytest.mark.asyncio
async def test_deferred_brief_extraction_runs_on_a_real_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id = _seed()
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item_id)})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    target = SimpleNamespace(
        job_id=uuid.uuid4(),
        snapshot={"text_bars": [], "allowed_op_families": ["text"]},
        conversation=[],
    )
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))

    async def creator(_inputs, **_kw):  # noqa: ANN001, ANN202
        return SimpleNamespace(
            action=AskUser(**_ASK), brief_updates=[_upd("text", "title", literal="N")]
        )

    monkeypatch.setattr(planner, "_call_main_creator", creator)
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            updates, route = await planner.extract_deferred_brief(
                db,
                thread_id=thread_id,
                item_id=item_id,
                creator_id=user_id,
                user_message="Change the title to N",
            )
    finally:
        await engine.dispose()
    assert [u.literal for u in updates] == ["N"] and route == "editor_ops"
