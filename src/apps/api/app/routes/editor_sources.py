"""Admission endpoints for sources added from the phone guided editor."""

from __future__ import annotations

import copy
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import storage
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.kria.media_sources import MediaUploadContract
from app.models import CreationThread, CreationThreadUploadReservation, Job, PlanItemAsset
from app.routes.generative_jobs import (
    _guided_v2_revision,
    _phone_editor_media_available,
    variant_render_baseline,
)
from app.routes.plan_items import _load_owned_item
from app.schemas.guided_edit_revision import GuidedEditorSource
from app.services.phone_editor_sources import (
    EDITOR_SOURCES_FIELD,
    MAX_IMPORTS,
    begin_attempt,
    editor_source_bindings,
    editor_visual_bindings,
    lease_expired,
)
from app.services.phone_sources import (
    PHONE_SOURCES_FIELD,
    PHONE_VISUALS_FIELD,
    PhoneSourceBinding,
    PhoneVisualBinding,
)
from app.tasks.editor_sources import prepare_phone_editor_source, reservation_lock_busy

router = APIRouter()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditorSourceRequest(_StrictModel):
    client_import_id: uuid.UUID
    base_generation: str = Field(min_length=1, max_length=160)
    guided_revision_number: int = Field(ge=1)
    source_kind: Literal["footage", "visual"]
    source_id: str = Field(min_length=1, max_length=160)


class EditorSourceOut(_StrictModel):
    import_id: uuid.UUID
    status: Literal["preparing", "ready", "failed"]
    source_id: str
    source_index: int | None = None
    source: GuidedEditorSource | None = None
    error: str | None = None
    reason_code: str | None = None
    retryable: bool = False


def _variant(job: Job, variant_id: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in (job.assembly_plan or {}).get("variants") or []
            if isinstance(row, dict) and row.get("variant_id") == variant_id
        ),
        None,
    )


def _registry(variant: dict[str, Any]) -> dict[str, Any]:
    raw = variant.get(EDITOR_SOURCES_FIELD)
    return copy.deepcopy(raw) if isinstance(raw, dict) else {"imports": {}, "sources": []}


def _response(
    import_id: uuid.UUID, record: dict[str, Any], variant: dict[str, Any]
) -> EditorSourceOut:
    source = record.get("source") if record.get("status") == "ready" else None
    return EditorSourceOut(
        import_id=import_id,
        status=record["status"],
        source_id=str(record["source_id"]),
        source_index=record.get("source_index"),
        source=source,
        error=record.get("error"),
        reason_code=record.get("reason_code"),
        retryable=bool(record.get("retryable")),
    )


def _fence(job: Job, variant: dict[str, Any], body: EditorSourceRequest) -> None:
    revision = _guided_v2_revision(job, variant) or {}
    if body.base_generation != variant_render_baseline(variant):
        raise HTTPException(status_code=409, detail="baseline_conflict")
    if body.guided_revision_number != revision.get("revision_number"):
        raise HTTPException(status_code=409, detail="GUIDED_REVISION_STALE")


async def _owned_proxy_reservation(
    db: AsyncSession, *, user_id: uuid.UUID, source_id: str, item_id: uuid.UUID
) -> CreationThreadUploadReservation:
    try:
        row = (
            await db.execute(
                select(CreationThreadUploadReservation)
                .join(
                    CreationThread, CreationThread.id == CreationThreadUploadReservation.thread_id
                )
                .where(
                    CreationThreadUploadReservation.creator_id == user_id,
                    CreationThread.creator_id == user_id,
                    CreationThread.active_plan_item_id == item_id,
                    CreationThreadUploadReservation.media_id == source_id,
                )
                .with_for_update(of=CreationThreadUploadReservation, nowait=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if not reservation_lock_busy(exc):
            raise
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail={"code": "source_reservation_busy", "retryable": True},
        ) from exc
    if row is None:
        raise HTTPException(status_code=409, detail="source_reservation_missing")
    try:
        contract = MediaUploadContract.model_validate(row.upload_contract or {})
    except ValueError as exc:
        raise HTTPException(422, detail="source_reservation_invalid") from exc
    if (
        contract.purpose != "analysis_proxy"
        or contract.proxy is None
        or contract.proxy.original.kind != "video"
    ):
        raise HTTPException(status_code=422, detail="source_reservation_invalid")
    return row


async def _publish(db: AsyncSession, job: Job, variant_id: str, key: str, token: str) -> None:
    try:
        await run_in_threadpool(
            prepare_phone_editor_source.delay, str(job.id), variant_id, key, token
        )
    except Exception:
        # A failed publish is recoverable; a delivered task's later success is
        # fenced by this attempt status, so uncertain broker outcomes are safe.
        from app.tasks.editor_sources import _fail

        await run_in_threadpool(
            _fail,
            str(job.id),
            variant_id,
            key,
            token,
            code="source_dispatch_failed",
            retryable=True,
        )


def _require_available(job: Job, variant: dict, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
    if job.user_id != user_id or job.content_plan_item_id != item_id:
        raise HTTPException(404, detail="Variant not found")
    if not _phone_editor_media_available(job, variant):
        raise HTTPException(404, detail="Phone editor media is unavailable")


def _existing_ready_source(
    job: Job, variant: dict, body: EditorSourceRequest
) -> tuple[int, dict] | None:
    """Reuse canonical source and immutable receipt without an upload reservation.

    Normal project attachment consumes reservations. A source already bound to
    this edit must not need a new upload simply to gain another placement.
    """
    sources = (_guided_v2_revision(job, variant) or {}).get("sources", [])
    match = next(
        ((index, row) for index, row in enumerate(sources) if row["media_id"] == body.source_id),
        None,
    )
    if match is None:
        return None
    index, source = match
    expected_lane = "clip" if body.source_kind == "footage" else "asset"
    if source["lane"] != expected_lane:
        raise HTTPException(409, detail="editor_source_identity_conflict")
    assembly = job.assembly_plan or {}
    try:
        if body.source_kind == "footage":
            bindings = [
                PhoneSourceBinding.model_validate(row)
                for row in assembly.get(PHONE_SOURCES_FIELD, [])
            ]
            bindings.extend(editor_source_bindings(variant))
            candidates = [binding for binding in bindings if binding.media_id == body.source_id]
            valid = all(
                binding.proxy_path == source["gcs_path"]
                and binding.generation == source["generation"]
                for binding in candidates
            )
        else:
            bindings = [
                PhoneVisualBinding.model_validate(row)
                for row in assembly.get(PHONE_VISUALS_FIELD, [])
            ]
            bindings.extend(editor_visual_bindings(variant))
            candidates = [binding for binding in bindings if binding.media_id == body.source_id]
            valid = all(
                binding.gcs_path == source["gcs_path"]
                and binding.generation == source["generation"]
                and binding.kind == source["kind"]
                for binding in candidates
            )
        if not candidates:
            return None
        if not valid or any(binding != candidates[0] for binding in candidates):
            raise ValueError("conflicting receipts")
    except ValueError as exc:
        raise HTTPException(409, detail="editor_source_identity_conflict") from exc
    return index, source


@router.post("/{item_id}/variants/{variant_id}/editor-sources", response_model=EditorSourceOut)
async def create_editor_source(
    item_id: str,
    variant_id: str,
    body: EditorSourceRequest,
    user: CurrentUser,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> EditorSourceOut:
    if not getattr(settings, "phone_editor_media_enabled", False):
        raise HTTPException(status_code=404, detail="Phone editor media is unavailable")
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    if item.current_job_id is None:
        raise HTTPException(status_code=404, detail="No render to edit yet")
    # Visual identity is already known from the request. Lock it between the
    # owning item and the job, matching the creation graph's canonical order.
    asset = None
    if body.source_kind == "visual":
        try:
            asset_id = uuid.UUID(body.source_id)
        except ValueError as exc:
            raise HTTPException(422, detail="visual_not_ready") from exc
        asset = await db.get(PlanItemAsset, asset_id, with_for_update=True, populate_existing=True)
    job = (
        await db.execute(select(Job).where(Job.id == item.current_job_id).with_for_update())
    ).scalar_one_or_none()
    if job is None or (variant := _variant(job, variant_id)) is None:
        raise HTTPException(status_code=404, detail="Variant not found")
    _require_available(job, variant, user.id, item.id)
    registry = _registry(variant)
    imports = registry.setdefault("imports", {})
    key = str(body.client_import_id)
    existing = imports.get(key)
    payload = body.model_dump(mode="json")
    if isinstance(existing, dict):
        comparison = {
            field: existing.get(field) for field in payload if field != "client_import_id"
        }
        if comparison != {
            field: value for field, value in payload.items() if field != "client_import_id"
        }:
            raise HTTPException(status_code=409, detail="import_id_reused")
        if (existing.get("status") == "failed" and existing.get("retryable")) or lease_expired(
            existing
        ):
            _fence(job, variant, body)
            token = begin_attempt(existing)
            imports[key] = existing
            variant[EDITOR_SOURCES_FIELD] = registry
            flag_modified(job, "assembly_plan")
            await db.commit()
            await _publish(db, job, variant_id, key, token)
        await db.refresh(job)
        variant = _variant(job, variant_id)
        existing = _registry(variant)["imports"][key]
        result = _response(body.client_import_id, existing, variant)
        if result.status == "preparing":
            response.status_code = status.HTTP_202_ACCEPTED
        return result
    _fence(job, variant, body)
    if len(imports) >= MAX_IMPORTS:
        raise HTTPException(422, detail="editor_import_limit")
    record: dict[str, Any] = {
        **{field: value for field, value in payload.items() if field != "client_import_id"},
        "status": "preparing",
        "error": None,
        "reason_code": None,
        "retryable": False,
        "source_index": None,
        "user_id": str(user.id),
    }
    ready = _existing_ready_source(job, variant, body)
    if body.source_kind == "footage" and ready is None:
        reservation = await _owned_proxy_reservation(
            db, user_id=user.id, source_id=body.source_id, item_id=item.id
        )
        record["reservation_id"] = str(reservation.id)
    elif body.source_kind == "visual":
        if (
            asset is None
            or asset.plan_item_id != item.id
            or asset.user_id != user.id
            or asset.status != "ready"
        ):
            raise HTTPException(status_code=422, detail="visual_not_ready")
        if asset.kind not in {"image", "video"}:
            raise HTTPException(status_code=422, detail="visual_kind_unsupported")
        if ready is not None and (
            asset.gcs_path != ready[1]["gcs_path"]
            or str(asset.gcs_generation or "") != ready[1]["generation"]
            or asset.kind != ready[1]["kind"]
        ):
            raise HTTPException(409, detail="editor_source_identity_conflict")
    if ready is not None:
        index, source = ready
        record.update(status="ready", source_index=index, source=source)
        imports[key] = record
        variant[EDITOR_SOURCES_FIELD] = registry
        flag_modified(job, "assembly_plan")
        await db.commit()
        return _response(body.client_import_id, record, variant)
    token = begin_attempt(record)
    imports[key] = record
    variant[EDITOR_SOURCES_FIELD] = registry
    flag_modified(job, "assembly_plan")
    await db.commit()
    await _publish(db, job, variant_id, key, token)
    await db.refresh(job)
    variant = _variant(job, variant_id)
    record = _registry(variant)["imports"][key]
    if record["status"] == "preparing":
        response.status_code = status.HTTP_202_ACCEPTED
    return _response(body.client_import_id, record, variant)


@router.get(
    "/{item_id}/variants/{variant_id}/editor-sources/{import_id}", response_model=EditorSourceOut
)
async def get_editor_source(
    item_id: str,
    variant_id: str,
    import_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EditorSourceOut:
    if not getattr(settings, "phone_editor_media_enabled", False):
        raise HTTPException(status_code=404, detail="Phone editor media is unavailable")
    item = await _load_owned_item(item_id, user.id, db)
    job = await db.get(Job, item.current_job_id) if item.current_job_id else None
    variant = _variant(job, variant_id) if job else None
    if job is not None and variant is not None:
        _require_available(job, variant, user.id, item.id)
    record = _registry(variant)["imports"].get(str(import_id)) if variant else None
    if not isinstance(record, dict):
        raise HTTPException(status_code=404, detail="Editor source import not found")
    if lease_expired(record):
        record.update(
            status="failed",
            error="source_prepare_expired",
            reason_code="source_prepare_expired",
            retryable=True,
        )
    return _response(import_id, record, variant)


async def validate_editor_sources(
    db: AsyncSession,
    *,
    job: Job,
    variant: dict[str, Any],
    used_media_ids: set[str] | None = None,
) -> None:
    """Cheap Save-time revalidation of already admitted immutable receipts."""
    registry = _registry(variant)
    for row in registry.get("sources") or []:
        if not isinstance(row, dict) or row.get("status") != "ready":
            continue
        if used_media_ids is not None and row.get("media_id") not in used_media_ids:
            continue
        if isinstance(row.get("source_binding"), dict):
            binding = row["source_binding"]
            # The durable receipt replaces the temporary upload reservation:
            # attaching project media may legitimately consume that reservation.
            try:
                metadata = await run_in_threadpool(
                    storage.object_metadata,
                    binding.get("proxy_path"),
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail="editor_source_unavailable") from exc
            if str(getattr(metadata, "generation", "") or "") != binding.get("generation"):
                raise HTTPException(status_code=409, detail="editor_source_generation_stale")
        elif isinstance(row.get("visual_binding"), dict):
            binding = row["visual_binding"]
            asset = await db.get(PlanItemAsset, binding.get("media_id"))
            if (
                asset is None
                or asset.plan_item_id != job.content_plan_item_id
                or asset.user_id != job.user_id
                or asset.status != "ready"
                or asset.kind != binding.get("kind", "image")
                or asset.gcs_path != binding.get("gcs_path")
                or str(asset.gcs_generation or "") != binding.get("generation")
            ):
                raise HTTPException(status_code=409, detail="editor_visual_unavailable")
