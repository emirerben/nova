"""KRI-1117 S19 against a real database: the Save-time pin only takes this item's ready pool.

Only GCS transport is substituted. The query scope and the owner recheck inside
`bind_phone_visual_assets` are the guarantee, so they must run for real.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import storage
from app.database import AsyncSessionLocal, sync_session
from app.models import Job, PlanItem, PlanItemAsset
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.services.phone_speaker_background import (
    pin_pool_visual_for_background,
    pin_pool_visual_for_background_sync,
)
from tests.kria.test_render_retry_postgres import seed_failed

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
)


def _seed_pool_row(*, item_id, user_id, status="ready", plan_item_for_path=None) -> uuid.UUID:
    asset_id = uuid.uuid4()
    path_item = plan_item_for_path or item_id
    with sync_session() as db:
        db.add(
            PlanItemAsset(
                id=asset_id,
                user_id=user_id,
                plan_item_id=item_id,
                kind="image",
                gcs_path=f"users/{user_id}/plan/{path_item}/pool/{asset_id}.png",
                gcs_generation="42",
                status=status,
            )
        )
        db.commit()
    return asset_id


async def _pin(case, item_id, media_id):
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, case.job_id)
        await pin_pool_visual_for_background(
            db, job, plan_item_id=item_id, media_id=str(media_id), kind="image"
        )
        return job.assembly_plan


@pytest.fixture
def case(monkeypatch):
    seeded = seed_failed()
    with sync_session() as db:
        seeded.item_id = db.get(Job, seeded.job_id).content_plan_item_id

    def download(_path, local, *, generation):
        assert generation == "42"
        Path(local).write_bytes(PNG)

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    return seeded


@pytest.mark.asyncio
async def test_this_items_ready_row_is_pinned_at_its_exact_generation(case):
    media_id = _seed_pool_row(item_id=case.item_id, user_id=case.user_id)
    plan = await _pin(case, case.item_id, media_id)
    (row,) = plan[PHONE_VISUALS_FIELD]
    assert row["media_id"] == str(media_id)
    assert row["generation"] == "42"
    assert row["sha256"] == hashlib.sha256(PNG).hexdigest()


@pytest.mark.asyncio
async def test_a_row_of_another_plan_item_is_refused_even_for_the_same_owner(case):
    with sync_session() as db:
        other_item = db.get(PlanItem, case.item_id)
        other_id = uuid.uuid4()
        clone = PlanItem(
            **{
                column.name: getattr(other_item, column.name)
                for column in PlanItem.__table__.columns
                if column.name not in {"id", "created_at", "updated_at"}
            }
        )
        clone.id = other_id
        db.add(clone)
        db.commit()
    foreign = _seed_pool_row(item_id=other_id, user_id=case.user_id)
    with pytest.raises(HTTPException) as refused:
        await _pin(case, case.item_id, foreign)
    assert refused.value.detail == {"code": "speaker_background_media_not_in_pool"}


@pytest.mark.asyncio
async def test_a_row_still_analysing_is_refused(case):
    media_id = _seed_pool_row(item_id=case.item_id, user_id=case.user_id, status="analyzing")
    with pytest.raises(HTTPException) as refused:
        await _pin(case, case.item_id, media_id)
    assert refused.value.detail == {"code": "speaker_background_media_not_in_pool"}


@pytest.mark.asyncio
async def test_a_pool_path_that_is_not_this_items_namespace_is_refused(case):
    """The row says it is ready in this item, but its object lives under another item's prefix."""
    media_id = _seed_pool_row(
        item_id=case.item_id, user_id=case.user_id, plan_item_for_path=uuid.uuid4()
    )
    with pytest.raises(HTTPException) as refused:
        await _pin(case, case.item_id, media_id)
    assert refused.value.detail == {"code": "speaker_background_media_unavailable"}


def _pin_sync(case, item_id, media_id, kind="image"):
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        pin_pool_visual_for_background_sync(
            db, job, plan_item_id=item_id, media_id=str(media_id), kind=kind
        )
        return job.assembly_plan


def test_the_approval_tasks_sync_pin_takes_this_items_ready_row_at_its_exact_generation(case):
    media_id = _seed_pool_row(item_id=case.item_id, user_id=case.user_id)
    plan = _pin_sync(case, case.item_id, media_id)
    (row,) = plan[PHONE_VISUALS_FIELD]
    assert (row["media_id"], row["generation"]) == (str(media_id), "42")
    # Pinned once: a second call leaves the receipts alone.
    assert _pin_sync(case, case.item_id, media_id)[PHONE_VISUALS_FIELD] == [row]


def test_the_sync_pin_refuses_what_the_async_pin_refuses_and_stores_nothing(case):
    still_analysing = _seed_pool_row(item_id=case.item_id, user_id=case.user_id, status="analyzing")
    wrong_kind = _seed_pool_row(item_id=case.item_id, user_id=case.user_id)
    for media_id, kind in (
        (uuid.uuid4(), "image"),
        (still_analysing, "image"),
        (wrong_kind, "video"),
    ):
        with pytest.raises(HTTPException) as refused:
            _pin_sync(case, case.item_id, media_id, kind)
        assert refused.value.detail == {"code": "speaker_background_media_not_in_pool"}
    with sync_session() as db:
        assert PHONE_VISUALS_FIELD not in (db.get(Job, case.job_id).assembly_plan or {})


@pytest.mark.asyncio
async def test_the_copilots_pool_lists_only_this_items_ready_photos_and_videos(case):
    from app.kria.planner import _background_pool_rows

    ready = _seed_pool_row(item_id=case.item_id, user_id=case.user_id)
    _seed_pool_row(item_id=case.item_id, user_id=case.user_id, status="analyzing")
    _seed_pool_row(item_id=case.item_id, user_id=case.user_id, status="failed")
    with sync_session() as db:
        item = db.get(PlanItem, case.item_id)
        clone = PlanItem(
            **{
                c.name: getattr(item, c.name)
                for c in PlanItem.__table__.columns
                if c.name not in {"id", "created_at", "updated_at"}
            }
        )
        clone.id = uuid.uuid4()
        db.add(clone)
        db.commit()
        other_item = clone.id
    _seed_pool_row(item_id=other_item, user_id=case.user_id)

    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, case.item_id)
        rows = await _background_pool_rows(db, item)
    assert [row["id"] for row in rows] == [str(ready)]
    assert rows[0]["kind"] == "image"
