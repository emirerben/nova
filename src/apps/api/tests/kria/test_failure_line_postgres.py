"""KRI-470: the generic 'render didn't finish' line is held back ONLY while a runtime-v2
observer can still post the real reason. Real SQL (scratch Postgres), no mocked `execute`.

Failure modes: the observer never posts (v2 flipped off, a stuck turn, the sweep's 50-row
cap) and `reconcile_render_state` has already moved the session to `failed`, so a
suppression with no bound loses the failure line permanently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.models import (
    CreationThread,
    CreatorAgentEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.services import creator_sessions
from app.services.creator_sessions import reconcile_render_state
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

GENERIC = "That render didn't finish. Your confirmed plan is saved."


def _seed_failed(*, dispatched_age: timedelta | None) -> tuple[uuid.UUID, uuid.UUID]:
    """A failed Job on a rendering session; `dispatched_age=None` = no v2 execution."""
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
        job = Job(
            user_id=user_id,
            status="processing_failed",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            assembly_plan={"variants": []},
            failure_reason="phone_plan_unsupported",
        )
        db.add(job)
        db.flush()
        if dispatched_age is not None:
            from app.models import CreationThreadEvent

            source = CreationThreadEvent(
                thread_id=thread_id,
                sequence=2,
                revision=3,
                client_event_id=f"f-{uuid.uuid4().hex}",
                role="user",
                event_type="user_message",
                content="Render",
                payload=None,
            )
            db.add(source)
            thread.revision = source.revision
            db.flush()
            turn = CreatorAgentTurn(
                thread_id=thread_id,
                session_id=session_id,
                source_event_id=source.id,
                client_event_id=source.client_event_id,
                request_digest=uuid.uuid4().hex,
                status="observing",
            )
            db.add(turn)
            db.flush()
            then = datetime.now(UTC) - dispatched_age
            db.add(
                CreatorAgentExecution(
                    session_id=session_id,
                    turn_id=turn.id,
                    idempotency_key=f"f-{uuid.uuid4().hex}",
                    request_digest=uuid.uuid4().hex,
                    expected_revision=3,
                    tool_name="render.request",
                    tool_version=1,
                    risk="approval_required",
                    target_thread_id=thread_id,
                    target_job_id=job.id,
                    target_ownership_epoch=0,
                    status="dispatched",
                    started_at=then,
                    dispatched_at=then,
                )
            )
        item.current_job_id = job.id
        session.phase = "rendering"
        session.target_job_id = job.id
        thread.active_job_id = job.id
        db.commit()
    return user_id, session_id


async def _reconcile_and_lines(session_id: uuid.UUID) -> list[str]:
    try:
        async with AsyncSessionLocal() as db:
            session = await db.get(CreatorAgentSession, session_id, with_for_update=True)
            assert await reconcile_render_state(db, session) is True
            assert session.phase == "failed"
            await db.commit()
        with sync_session() as db:
            rows = db.execute(
                select(CreatorAgentEvent.payload).where(
                    CreatorAgentEvent.session_id == session_id,
                    CreatorAgentEvent.event_type == "assistant_render_failed",
                )
            ).scalars()
            return [row["message"] for row in rows]
    finally:
        await async_engine.dispose()


@pytest.fixture(autouse=True)
def _v2_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.mark.asyncio
async def test_a_fresh_dispatched_execution_holds_the_generic_line_back() -> None:
    _, session_id = _seed_failed(dispatched_age=timedelta(seconds=20))
    assert await _reconcile_and_lines(session_id) == []


@pytest.mark.asyncio
async def test_a_stale_dispatched_execution_no_longer_holds_it_back() -> None:
    window = creator_sessions.OBSERVER_FAILURE_WINDOW
    _, session_id = _seed_failed(dispatched_age=window + timedelta(minutes=5))
    assert await _reconcile_and_lines(session_id) == [GENERIC]


@pytest.mark.asyncio
async def test_runtime_v2_off_posts_the_generic_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", False)
    _, session_id = _seed_failed(dispatched_age=timedelta(seconds=20))
    assert await _reconcile_and_lines(session_id) == [GENERIC]


@pytest.mark.asyncio
async def test_no_execution_posts_the_generic_line() -> None:
    _, session_id = _seed_failed(dispatched_age=None)
    assert await _reconcile_and_lines(session_id) == [GENERIC]


def test_the_window_is_a_few_minutes_not_unbounded() -> None:
    assert timedelta(minutes=1) <= creator_sessions.OBSERVER_FAILURE_WINDOW <= timedelta(minutes=10)
