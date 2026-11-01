"""Pin a pool visual for a Talk-to-camera background at Save time (KRI-1117).

The compiler only accepts visuals the job has already pinned (`PhoneVisualBinding`: the
exact generation, hashed). The worker pins the ones a render shows; a creator who picks a
different pool photo or video in the editor needs it pinned first. `bind_phone_visual_assets`
is the one receipt path (it rechecks owner, item, status and the pool namespace before it
downloads anything); this module is the Save-side caller of it, for the HTTP Save route and
for the two chat apply paths (the async copilot edit and the sync approval task), which compile
the same `speaker_background` section.

A clip's tilt backdrop of kind ``media`` (KRI-1139) names a pool photo or video the same way, so
its Save pins through the same receipt path under its own refusal codes
(``clip_backdrop_media_*``): the receipt is the reference storage retention honours.
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
    "speaker_background_media_unavailable": (
        "Couldn't read that photo or video",
        "Bu fotoğraf ya da videoyu okuyamadım",
    ),
    "clip_backdrop_media_unavailable": (
        "Couldn't put a photo or video behind a clip: it isn't available right now",
        "Bir klibin arkasına fotoğraf ya da video koyamadım: şu anda kullanılamıyor",
    ),
    "clip_backdrop_media_not_in_pool": (
        "Couldn't use that photo or video behind a clip: it isn't in this video's Visuals",
        "Bu fotoğraf ya da videoyu klibin arkasına koyamadım: "
        "bu videonun görselleri arasında değil",
    ),
    "clip_backdrop_media_unreadable": (
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


def _pool_pin(media_id: str, kind: str | None, row: Any) -> dict[str, tuple[str, str, str]] | None:
    """The one pin a ready row of the asked kind yields, else None (S19).

    ``kind`` None accepts a photo or a video (a clip backdrop names the id only).
    """
    if row is None or row.status != "ready":
        return None
    if row.kind != (kind if kind is not None else row.kind) or row.kind not in {"image", "video"}:
        return None
    return {str(row.id): (row.kind, row.gcs_path, str(row.gcs_generation or ""))}


def _with_receipts(plan: dict, bound: Any) -> dict:
    rows = [*(plan.get(PHONE_VISUALS_FIELD) or []), *(v.model_dump(mode="json") for v in bound)]
    return {**plan, PHONE_VISUALS_FIELD: rows}


def _asset_uuid(media_id: str, not_in_pool: str) -> uuid.UUID:
    try:
        return uuid.UUID(media_id)
    except ValueError as exc:
        raise _refuse(not_in_pool) from exc


async def pin_pool_visual_for_background(
    db: AsyncSession,
    job: Any,
    *,
    plan_item_id: uuid.UUID,
    media_id: str,
    kind: str | None,
    not_in_pool: str = "speaker_background_media_not_in_pool",
    unavailable: str = "speaker_background_media_unavailable",
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
        await db.execute(_scoped_row_query(plan_item_id, _asset_uuid(media_id, not_in_pool)))
    ).scalar_one_or_none()
    pins = _pool_pin(media_id, kind, row)
    if pins is None:
        raise _refuse(not_in_pool)
    from app.database import sync_session  # noqa: PLC0415
    from app.services.phone_visuals import bind_phone_visual_assets  # noqa: PLC0415

    try:
        bound = await run_in_threadpool(
            bind_phone_visual_assets, sync_session, job_id=str(job.id), pins=pins
        )
    except ValueError as exc:
        raise _refuse(unavailable) from exc
    job.assembly_plan = _with_receipts(plan, bound)


def pin_pool_visual_for_background_sync(
    db: Session,
    job: Any,
    *,
    plan_item_id: uuid.UUID,
    media_id: str,
    kind: str | None,
    not_in_pool: str = "speaker_background_media_not_in_pool",
    unavailable: str = "speaker_background_media_unavailable",
) -> None:
    """`pin_pool_visual_for_background` for a worker holding a sync session (the approval task)."""
    plan = job.assembly_plan or {}
    if pinned_visual(plan, media_id) is not None:
        return
    row = db.execute(
        _scoped_row_query(plan_item_id, _asset_uuid(media_id, not_in_pool))
    ).scalar_one_or_none()
    pins = _pool_pin(media_id, kind, row)
    if pins is None:
        raise _refuse(not_in_pool)
    from app.database import sync_session  # noqa: PLC0415
    from app.services.phone_visuals import bind_phone_visual_assets  # noqa: PLC0415

    try:
        bound = bind_phone_visual_assets(sync_session, job_id=str(job.id), pins=pins)
    except ValueError as exc:
        raise _refuse(unavailable) from exc
    job.assembly_plan = _with_receipts(plan, bound)


def background_pin_request(job: Any, variant_id: str, payload: Any) -> tuple[str, str] | None:
    """``(media_id, kind)`` a Save that SETS a background needs pinned first, else None.

    Only a Save the validator can accept is worth a download and a hash: a clear, an
    untouched lane, a closed gate or a variant that cannot carry one reach
    `_validate_speaker_background_section` and get their own refusal there. The one rule the
    HTTP Save route and the chat apply paths (async copilot edit, sync approval task) share.
    """
    if "speaker_background" not in getattr(payload, "model_fields_set", ()):
        return None
    if getattr(payload, "speaker_background", None) is None:
        return None
    from app.routes.generative_jobs import (  # noqa: PLC0415
        _find_variant,
        _speaker_background_shape_ok,
    )
    from app.services.phone_rollout import speaker_background_unavailable_reason  # noqa: PLC0415

    variant = _find_variant(job, variant_id) or {}
    if not _speaker_background_shape_ok(job, variant):
        return None
    if speaker_background_unavailable_reason() is not None:
        return None
    return payload.speaker_background.media_id, payload.speaker_background.kind


def backdrop_pin_requests(job: Any, variant_id: str, payload: Any) -> list[str]:
    """Pool ids a Save that SETS a clip's media backdrop needs pinned first (KRI-1139).

    Like `background_pin_request`, only a Save the gate can accept is worth a download: a
    closed gate, a cloud variant or a slot that leaves its tilt alone reach
    `_require_clip_tilt_gates` and get their own refusal there.
    """
    slots = getattr(payload, "timeline_slots", None)
    if not slots:
        return []
    from app.routes.generative_jobs import _find_variant, _tilt_media_asset  # noqa: PLC0415
    from app.services.phone_rollout import clip_backdrop_media_supported  # noqa: PLC0415

    variant = _find_variant(job, variant_id) or {}
    if variant.get("render_destination") != "device":
        return []
    if not clip_backdrop_media_supported(require_client=False):
        return []
    ids: list[str] = []
    for slot in slots:
        if slot.removed or "tilt" not in slot.model_fields_set:
            continue
        media_id = _tilt_media_asset(slot.tilt)
        if media_id is not None and media_id not in ids:
            ids.append(media_id)
    return ids


async def pin_background_for_commit(
    db: AsyncSession, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    """Pin the pool visuals ``payload`` sets as the speaker background or a clip backdrop."""
    request = background_pin_request(job, variant_id, payload)
    if request is not None:
        await pin_pool_visual_for_background(
            db, job, plan_item_id=plan_item_id, media_id=request[0], kind=request[1]
        )
    for media_id in backdrop_pin_requests(job, variant_id, payload):
        await pin_pool_visual_for_background(
            db,
            job,
            plan_item_id=plan_item_id,
            media_id=media_id,
            kind=None,
            not_in_pool="clip_backdrop_media_not_in_pool",
            unavailable="clip_backdrop_media_unreadable",
        )


def pin_background_for_commit_sync(
    db: Session, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    """`pin_background_for_commit` for the sync approval task."""
    request = background_pin_request(job, variant_id, payload)
    if request is not None:
        pin_pool_visual_for_background_sync(
            db, job, plan_item_id=plan_item_id, media_id=request[0], kind=request[1]
        )
    for media_id in backdrop_pin_requests(job, variant_id, payload):
        pin_pool_visual_for_background_sync(
            db,
            job,
            plan_item_id=plan_item_id,
            media_id=media_id,
            kind=None,
            not_in_pool="clip_backdrop_media_not_in_pool",
            unavailable="clip_backdrop_media_unreadable",
        )
