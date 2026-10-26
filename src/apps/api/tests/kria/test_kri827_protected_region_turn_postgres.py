"""KRI-827: the creator's mark of what to keep text off is a trust boundary.

A chat turn carries ``protected_region`` (a box on one frame of one clip). Failure modes:
F16 a mark naming media outside this creator's item (another account's media id) is
accepted and later tracked; the client stamps its own generation so a swap goes unnoticed;
The mark the server keeps is the one the approval binding pins (`fold_protected_regions`).
Needs the ``nova_test`` Postgres (CI's test-api job); the pure contract checks live in
`test_kri827_protected_targets.py`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.protected_targets import fold_protected_regions
from app.kria.runtime import RuntimeFailure, submit_turn
from app.models import CreationThread, CreationThreadEvent, PlanItem
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

BOX = (0.3, 0.42, 0.7, 0.64)


def _body(revision: int, media_id: str) -> SubmitTurnBody:
    return SubmitTurnBody(
        message="This is the plate",
        client_event_id=f"mark-{uuid.uuid4().hex}",
        expected_thread_revision=revision,
        protected_region={"label": "plate", "media_id": media_id, "source_t_s": 1.5, "box": BOX},
    )


def _seed() -> tuple[uuid.UUID, uuid.UUID, int]:
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.clip_assignments = [{"media_id": "clip-1", "storage_generation": "g7"}]
        db.commit()
        revision = thread.revision
    return user_id, thread_id, revision


@pytest.mark.asyncio
async def test_the_creators_mark_is_kept_with_the_servers_generation() -> None:
    user_id, thread_id, revision = _seed()
    try:
        async with AsyncSessionLocal() as db:
            await submit_turn(
                db, thread_id=thread_id, creator_id=user_id, body=_body(revision, "clip-1")
            )
        with sync_session() as db:
            rows = db.execute(
                select(CreationThreadEvent.role, CreationThreadEvent.payload).where(
                    CreationThreadEvent.thread_id == thread_id
                )
            ).all()
        [mark] = fold_protected_regions(rows)
        assert mark == {
            "label": "plate",
            "media_id": "clip-1",
            "source_t_s": 1.5,
            "box": list(BOX),
            "generation": "g7",
        }
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_mark_on_media_outside_the_creators_item_is_refused() -> None:
    user_id, thread_id, revision = _seed()
    try:
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await submit_turn(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    body=_body(revision, "someone-elses-clip"),
                )
        assert caught.value.status_code == 404
        assert caught.value.code == "protected_region_media_not_found"
    finally:
        await async_engine.dispose()
