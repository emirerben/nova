"""Pin a pool visual for a Talk-to-camera background at Save time (KRI-1117).

The compiler only accepts visuals the job has already pinned (`PhoneVisualBinding`: the
exact generation, hashed). The worker pins the ones a render shows; a creator who picks a
different pool photo or video in the editor needs it pinned first. `bind_phone_visual_assets`
is the one receipt path (it rechecks owner, item, status and the pool namespace before it
downloads anything); this module is the async Save-side caller of it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models import PlanItemAsset
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import pinned_visual


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": code})


async def pin_pool_visual_for_background(
    db: AsyncSession,
    job: Any,
    *,
    plan_item_id: uuid.UUID,
    media_id: str,
    kind: str,
) -> None:
    """Make ``media_id`` a pinned visual of ``job``, or raise a 422 naming why not.

    Only a ready row of THIS plan item's pool with the asked kind qualifies (S19): an id
    from another item or user, a made-up id, or a row still analysing is refused with
    ``speaker_background_media_not_in_pool``. An already pinned visual is left alone. The
    new receipt is added to ``job.assembly_plan`` in one dict replacement; the caller's
    transaction commits it with the Save, and a refused Save rolls it back.
    """
    plan = job.assembly_plan or {}
    if pinned_visual(plan, media_id) is not None:
        return
    try:
        asset_id = uuid.UUID(media_id)
    except ValueError as exc:
        raise _refuse("speaker_background_media_not_in_pool") from exc
    row = (
        await db.execute(
            select(PlanItemAsset).where(
                PlanItemAsset.id == asset_id, PlanItemAsset.plan_item_id == plan_item_id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.status != "ready" or row.kind != kind:
        raise _refuse("speaker_background_media_not_in_pool")
    from app.database import sync_session  # noqa: PLC0415
    from app.services.phone_visuals import bind_phone_visual_assets  # noqa: PLC0415

    pins = {str(row.id): (row.kind, row.gcs_path, str(row.gcs_generation or ""))}
    try:
        bound = await run_in_threadpool(
            bind_phone_visual_assets, sync_session, job_id=str(job.id), pins=pins
        )
    except ValueError as exc:
        raise _refuse("speaker_background_media_unavailable") from exc
    rows = [*(plan.get(PHONE_VISUALS_FIELD) or []), *(v.model_dump(mode="json") for v in bound)]
    job.assembly_plan = {**plan, PHONE_VISUALS_FIELD: rows}
