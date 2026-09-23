"""PostgreSQL regression: a Visuals upload must not deadlock the chat poll.

Prod, 2026-09-23 10:48 UTC: choosing several photos in an iOS chat failed two
of them with "This edit changed elsewhere".  Each photo's
``POST /plan-items/{id}/assets/upload-urls`` runs ``_load_owned_item_context``
(ContentPlan -> Persona -> PlanItem locks).  The chat's
``GET /creation-threads/{id}?projection=full`` poll had locked the thread and
the PlanItem, written the thread, and then bumped its revision.  Writing a row
again in the same transaction re-runs its ``content_plan_id`` foreign-key
check, which takes ``FOR KEY SHARE`` on the plan.  A plan held ``FOR UPDATE``
blocks that, closing the cycle; Postgres aborted one side with ``deadlock
detected`` and the API returned its retryable 409.
``app.db_locks.CONTENT_PLAN_LOCK`` (``FOR NO KEY UPDATE``) lets the check pass.

The poll transaction replays the prod statement order by hand; the upload side
is the real route helper.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import AsyncSessionLocal, sync_engine
from app.database import engine as async_engine
from app.models import ContentPlan, CreationThread, Persona, PlanItem, User
from app.routes.plan_items import _load_owned_item_context

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(
        f"refusing to write lock-regression fixtures to {_database_name!r}",
        allow_module_level=True,
    )
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT 1"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"nova_test Postgres not reachable: {exc!r}", allow_module_level=True)


@dataclass(frozen=True)
class _Seeded:
    user_id: uuid.UUID
    plan_id: uuid.UUID
    item_id: uuid.UUID
    thread_id: uuid.UUID


@pytest.fixture()
async def seeded(request: pytest.FixtureRequest) -> _Seeded:
    # Each async test gets its own loop; drop connections an earlier loop opened.
    await async_engine.dispose()
    request.addfinalizer(lambda: async_engine.sync_engine.dispose(close=False))
    ids = _Seeded(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(User(id=ids.user_id, email=f"plan-lock-{ids.user_id}@example.test"))
        persona = Persona(
            id=uuid.uuid4(),
            user_id=ids.user_id,
            persona_status="ready",
            persona={"summary": "test creator"},
            questionnaire={},
        )
        db.add(persona)
        await db.flush()
        db.add(
            ContentPlan(
                id=ids.plan_id, user_id=ids.user_id, persona_id=persona.id, plan_status="ready"
            )
        )
        await db.flush()
        db.add(
            PlanItem(
                id=ids.item_id,
                content_plan_id=ids.plan_id,
                position=1,
                idea="narrated story",
                item_status="awaiting_clips",
                clip_gcs_paths=[],
            )
        )
        await db.flush()
        db.add(
            CreationThread(
                id=ids.thread_id,
                creator_id=ids.user_id,
                content_plan_id=ids.plan_id,
                active_plan_item_id=ids.item_id,
                state={},
            )
        )
        await db.commit()
    try:
        yield ids
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(CreationThread).where(CreationThread.id == ids.thread_id))
            await db.execute(delete(PlanItem).where(PlanItem.id == ids.item_id))
            await db.execute(delete(ContentPlan).where(ContentPlan.id == ids.plan_id))
            await db.execute(delete(Persona).where(Persona.user_id == ids.user_id))
            await db.execute(delete(User).where(User.id == ids.user_id))
            await db.commit()
        await async_engine.dispose()


async def _wait_for_lock_wait(backend_pid: int) -> None:
    """Return once ``backend_pid`` is blocked on a row lock."""

    async with AsyncSessionLocal() as observer:
        for _ in range(200):
            waiting = await observer.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": backend_pid},
            )
            await observer.rollback()  # fresh stats snapshot on the next read
            if waiting == "Lock":
                return
            await asyncio.sleep(0.05)
    raise AssertionError("the Visuals reservation never waited on the chat poll's PlanItem lock")


async def test_visuals_reservation_does_not_deadlock_the_chat_poll(seeded: _Seeded) -> None:
    poll_holds_item = asyncio.Event()
    reservation_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def chat_poll() -> None:
        async with AsyncSessionLocal() as db:
            thread = (
                await db.execute(
                    select(CreationThread)
                    .where(CreationThread.id == seeded.thread_id)
                    .with_for_update()
                )
            ).scalar_one()
            await db.get(PlanItem, seeded.item_id, with_for_update=True)
            thread.state = {"synced": True}  # first write of the thread row
            await db.flush()
            poll_holds_item.set()
            # The reservation now holds the plan and waits on this PlanItem.
            await _wait_for_lock_wait(await reservation_pid)
            thread.revision += 1  # the rewrite re-runs the content_plan_id FK check
            await db.flush()
            await db.commit()

    async def visuals_reservation() -> None:
        await poll_holds_item.wait()
        async with AsyncSessionLocal() as db:
            reservation_pid.set_result(int(await db.scalar(text("SELECT pg_backend_pid()"))))
            await _load_owned_item_context(str(seeded.item_id), seeded.user_id, db, for_update=True)
            await db.commit()

    # Before CONTENT_PLAN_LOCK, one of these raised DeadlockDetectedError.
    await asyncio.wait_for(asyncio.gather(chat_poll(), visuals_reservation()), timeout=30)

    async with AsyncSessionLocal() as db:
        revision = await db.scalar(
            select(CreationThread.revision).where(CreationThread.id == seeded.thread_id)
        )
    assert revision == 1
