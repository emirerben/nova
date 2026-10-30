"""KRI-1069: a live turn's media snapshot keeps each Visual's capture time (scratch Postgres).

The render contract dates a selected photo from the turn's snapshot (``snapshot_media``),
which the approval binding pins and dispatch reads. ``_snapshot_visual_rows`` is the read.

Ways this can fail (each pinned below):
1. The photo's ``capture`` blob is not carried, so the contract cannot date it.
2. A row the turn cannot use leaks in: a failed upload, a byte-identical duplicate, another
   account's row on the same item.
3. A photo still being analyzed is left out, so a selected id looks unknown at dispatch.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.brief_binding import snapshot_media
from app.kria.planner import _snapshot_visual_rows
from app.models import ContentPlan, CreationThread, Persona, PlanItem, PlanItemAsset, User
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _asset(item: PlanItem, user_id: uuid.UUID, name: str, **fields) -> PlanItemAsset:
    row_id = uuid.uuid4()
    return PlanItemAsset(
        id=row_id,
        plan_item_id=item.id,
        user_id=user_id,
        gcs_path=f"users/{user_id}/plan/{item.id}/pool/{row_id}.jpg",
        gcs_generation="1",
        kind="image",
        source_filename=name,
        **{"status": "ready", **fields},
    )


def _other_user() -> uuid.UUID:
    user_id = uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        persona_id = uuid.uuid4()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=uuid.uuid4(), user_id=user_id, persona_id=persona_id))
        db.commit()
    return user_id


@pytest.mark.asyncio
async def test_the_turn_snapshot_lists_the_items_visuals_with_their_capture() -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    stranger = _other_user()
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        lineup = _asset(
            item, user_id, "IMG_1038.HEIC", capture={"capture_time": "2026-10-28T20:33:20Z"}
        )
        analyzing = _asset(item, user_id, "IMG_1019.HEIC", status="analyzing")
        db.add_all([lineup, analyzing])
        db.flush()
        db.add_all(
            [
                _asset(item, user_id, "broken.jpg", status="failed"),
                _asset(item, user_id, "copy.jpg", deduplicated_to_asset_id=lineup.id),
                _asset(item, stranger, "theirs.jpg"),
            ]
        )
        db.commit()
        item_id, lineup_id, analyzing_id = item.id, lineup.id, analyzing.id

    async with AsyncSessionLocal() as session:
        item = await session.get(PlanItem, item_id)
        rows = await _snapshot_visual_rows(session, item)
        snapshot = snapshot_media(item, visuals=rows)

    assert {row["media_id"]: row for row in snapshot["visuals"]} == {
        f"asset-{lineup_id}": {
            "media_id": f"asset-{lineup_id}",
            "kind": "image",
            "capture": {"capture_time": "2026-10-28T20:33:20Z"},
            "source_filename": "IMG_1038.HEIC",
        },
        f"asset-{analyzing_id}": {
            "media_id": f"asset-{analyzing_id}",
            "kind": "image",
            "capture": None,
            "source_filename": "IMG_1019.HEIC",
        },
    }
