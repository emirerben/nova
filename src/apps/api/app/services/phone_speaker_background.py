"""Pin a pool visual for a Talk-to-camera background at Save time (KRI-1117).

The compiler only accepts visuals the job has already pinned (`PhoneVisualBinding`: the
exact generation, hashed). The worker pins the ones a render shows; a creator who picks a
different pool photo or video in the editor needs it pinned first. `bind_phone_visual_assets`
is the one receipt path (it rechecks owner, item, status and the pool namespace before it
downloads anything); this module is the Save-side caller of it, for the HTTP Save route and
for the two chat apply paths (the async copilot edit and the sync approval task), which compile
the same `speaker_background` section.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import PlanItemAsset
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import pinned_visual

# Creator-facing words for each refusal of a chat edit that sets a background (S18): an
# honest "couldn't" with the reason, in both reply languages (a test pins that every code
# the Save validator and the pin raise is here). Shared by the v1 copilot edit and the v2
# approval task, which each show it in their own reply.
BACKGROUND_REFUSALS: dict[str, tuple[str, str]] = {
    "speaker_background_disabled": (
        "Couldn't put a photo or video behind you: it isn't available right now",
        "Arkana fotoğraf ya da video koyamadım: şu anda kullanılamıyor",
    ),
    "speaker_background_unsupported": (
        "Couldn't put a photo or video behind you on this edit",
        "Bu düzenlemede arkana fotoğraf ya da video koyamadım",
    ),
    "speaker_background_media_not_in_pool": (
        "Couldn't use that photo or video: it isn't in this video's Visuals",
        "Bu fotoğraf ya da videoyu kullanamadım: bu videonun görselleri arasında değil",
    ),
    "speaker_background_kind_mismatch": (
        "Couldn't use that photo or video as your background",
        "Bu fotoğraf ya da videoyu arka plan olarak kullanamadım",
    ),
    "speaker_background_start_out_of_range": (
        "Couldn't use that start point: it's outside the video",
        "Bu başlangıç noktasını kullanamadım: videonun dışında",
    ),
    # KRI-1162: timed backgrounds / the speaker frame.
    "speaker_background_windows_disabled": (
        "Couldn't change the backgrounds behind you over time: it isn't available right now",
        "Arkandaki görselleri zamanla değiştiremedim: şu anda kullanılamıyor",
    ),
    "speaker_background_window_invalid": (
        "Couldn't use those background times: they overlap or are too short",
        "Bu arka plan sürelerini kullanamadım: üst üste biniyor ya da çok kısa",
    ),
    "speaker_background_media_unavailable": (
        "Couldn't read that photo or video",
        "Bu fotoğraf ya da videoyu okuyamadım",
    ),
}


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": code})


def _scoped_row_query(plan_item_id: uuid.UUID, asset_id: uuid.UUID):
    return select(PlanItemAsset).where(
        PlanItemAsset.id == asset_id, PlanItemAsset.plan_item_id == plan_item_id
    )


def _pool_pin(media_id: str, kind: str, row: Any) -> dict[str, tuple[str, str, str]] | None:
    """The one pin a ready row of the asked kind yields, else None (S19)."""
    if row is None or row.status != "ready" or row.kind != kind:
        return None
    return {str(row.id): (row.kind, row.gcs_path, str(row.gcs_generation or ""))}


def _with_receipts(plan: dict, bound: Any) -> dict:
    rows = [*(plan.get(PHONE_VISUALS_FIELD) or []), *(v.model_dump(mode="json") for v in bound)]
    return {**plan, PHONE_VISUALS_FIELD: rows}


def _asset_uuid(media_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(media_id)
    except ValueError as exc:
        raise _refuse("speaker_background_media_not_in_pool") from exc


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
    row = (
        await db.execute(_scoped_row_query(plan_item_id, _asset_uuid(media_id)))
    ).scalar_one_or_none()
    pins = _pool_pin(media_id, kind, row)
    if pins is None:
        raise _refuse("speaker_background_media_not_in_pool")
    from app.database import sync_session  # noqa: PLC0415
    from app.services.phone_visuals import bind_phone_visual_assets  # noqa: PLC0415

    try:
        bound = await run_in_threadpool(
            bind_phone_visual_assets, sync_session, job_id=str(job.id), pins=pins
        )
    except ValueError as exc:
        raise _refuse("speaker_background_media_unavailable") from exc
    job.assembly_plan = _with_receipts(plan, bound)


def pin_pool_visual_for_background_sync(
    db: Session,
    job: Any,
    *,
    plan_item_id: uuid.UUID,
    media_id: str,
    kind: str,
) -> None:
    """`pin_pool_visual_for_background` for a worker holding a sync session (the approval task)."""
    plan = job.assembly_plan or {}
    if pinned_visual(plan, media_id) is not None:
        return
    row = db.execute(_scoped_row_query(plan_item_id, _asset_uuid(media_id))).scalar_one_or_none()
    pins = _pool_pin(media_id, kind, row)
    if pins is None:
        raise _refuse("speaker_background_media_not_in_pool")
    from app.database import sync_session  # noqa: PLC0415
    from app.services.phone_visuals import bind_phone_visual_assets  # noqa: PLC0415

    try:
        bound = bind_phone_visual_assets(sync_session, job_id=str(job.id), pins=pins)
    except ValueError as exc:
        raise _refuse("speaker_background_media_unavailable") from exc
    job.assembly_plan = _with_receipts(plan, bound)


def background_pin_requests(job: Any, variant_id: str, payload: Any) -> list[tuple[str, str]]:
    """Every ``(media_id, kind)`` a Save that SETS a background needs pinned first.

    The fill first, then each ``windows`` entry (KRI-1162, S35), de-duplicated by id.
    Empty for a Save the validator can accept nothing of: a clear, an untouched lane, a
    closed gate or a variant that cannot carry one reach `_validate_speaker_background_section`
    and get their own refusal there. Window ids are only worth a download while the windows
    gate is open (else the validator refuses the Save as ``speaker_background_windows_disabled``
    / 426 and nothing is pinned). The one rule the HTTP Save route and the chat apply paths
    (async copilot edit, sync approval task) share.
    """
    if "speaker_background" not in getattr(payload, "model_fields_set", ()):
        return []
    section = getattr(payload, "speaker_background", None)
    if section is None:
        return []
    from app.routes.generative_jobs import (  # noqa: PLC0415
        _find_variant,
        _speaker_background_shape_ok,
    )
    from app.services.phone_rollout import (  # noqa: PLC0415
        speaker_background_unavailable_reason,
        speaker_background_windows_unavailable_reason,
    )

    variant = _find_variant(job, variant_id) or {}
    if not _speaker_background_shape_ok(job, variant):
        return []
    if speaker_background_unavailable_reason() is not None:
        return []
    requests: dict[str, str] = {section.media_id: section.kind}
    if (
        getattr(section, "windows", None)
        and speaker_background_windows_unavailable_reason() is None
    ):
        for window in section.windows:
            requests.setdefault(window.media_id, window.kind)
    return list(requests.items())


def background_pin_request(job: Any, variant_id: str, payload: Any) -> tuple[str, str] | None:
    """``(media_id, kind)`` of the FILL a Save that SETS a background needs pinned, else None.

    The first entry of `background_pin_requests` (kept for callers that only know the fill).
    """
    requests = background_pin_requests(job, variant_id, payload)
    return requests[0] if requests else None


async def pin_background_for_commit(
    db: AsyncSession, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    """Pin the pool visuals ``payload`` sets as the background: fill and windows (else no-op)."""
    for media_id, kind in background_pin_requests(job, variant_id, payload):
        await pin_pool_visual_for_background(
            db, job, plan_item_id=plan_item_id, media_id=media_id, kind=kind
        )


def pin_background_for_commit_sync(
    db: Session, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    """`pin_background_for_commit` for the sync approval task."""
    for media_id, kind in background_pin_requests(job, variant_id, payload):
        pin_pool_visual_for_background_sync(
            db, job, plan_item_id=plan_item_id, media_id=media_id, kind=kind
        )
