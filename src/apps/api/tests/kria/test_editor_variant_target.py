"""A native editor save targets the owned variant, independent of chat selection."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.kria.drafts import editor_variant_target
from app.kria.runtime import RuntimeFailure


def target_rows():
    owner, item_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        current_job_id=job_id,
        content_plan_id=uuid.uuid4(),
        idea="Saved video",
        edit_format="montage",
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=owner,
        content_plan_item_id=item_id,
        assembly_plan={"variants": [{"variant_id": "edited", "render_generation_id": "g1"}]},
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=Mock()), add=Mock(), flush=AsyncMock())
    return owner, item, job, db


@pytest.mark.asyncio
async def test_explicit_owned_variant_ignores_selected_chat_variant():
    owner, item, job, db = target_rows()
    thread = SimpleNamespace(active_job_id=job.id, state={"selected_variant_id": "other"})
    db.execute.return_value.scalar_one_or_none.return_value = thread
    target = await editor_variant_target(
        db, item=item, job=job, creator_id=owner, variant_key="edited"
    )
    assert target.variant_key == "edited"
    assert target.generation_id == "g1"
    assert thread.state == {"selected_variant_id": "other"}
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["owner", "item", "job", "variant"])
async def test_target_mismatches_fail_before_thread_lookup(mismatch):
    owner, item, job, db = target_rows()
    variant = "edited"
    if mismatch == "owner":
        job.user_id = uuid.uuid4()
    elif mismatch == "item":
        job.content_plan_item_id = uuid.uuid4()
    elif mismatch == "job":
        item.current_job_id = uuid.uuid4()
    else:
        variant = "missing"
    with pytest.raises(RuntimeFailure):
        await editor_variant_target(db, item=item, job=job, creator_id=owner, variant_key=variant)
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_target_links_a_thread_without_rendering():
    owner, item, job, db = target_rows()
    db.execute.return_value.scalar_one_or_none.return_value = None
    target = await editor_variant_target(
        db, item=item, job=job, creator_id=owner, variant_key="edited"
    )
    assert target.thread.active_plan_item_id == item.id
    assert target.thread.active_job_id == job.id
    assert target.thread.creator_id == owner
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_thread_with_different_job_is_stale():
    owner, item, job, db = target_rows()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        active_job_id=uuid.uuid4()
    )
    with pytest.raises(RuntimeFailure):
        await editor_variant_target(db, item=item, job=job, creator_id=owner, variant_key="edited")
    db.add.assert_not_called()
