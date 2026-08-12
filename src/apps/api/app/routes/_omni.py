"""Authenticated Omni generated-asset job contracts and state helpers.

The generated video is deliberately kept out of the Director's structured
operation call. Accepting an Omni suggestion creates a separate async job;
only a completed, normalized asset yields an editor operation.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Job

log = structlog.get_logger()

OmniAction = Literal["generate_insert", "restyle_segment"]
OmniStatus = Literal[
    "queued",
    "generating",
    "normalizing",
    "ready",
    "failed",
    "cancellation_requested",
    "cancelled",
]


class OmniAssetStartBody(BaseModel):
    suggestion_id: str = Field(min_length=1, max_length=100)
    draft_revision: str = Field(min_length=1, max_length=100)
    action: OmniAction
    prompt: str = Field(min_length=1, max_length=500)
    insert_at_s: float = Field(ge=0.0, le=60.0)
    duration_s: float = Field(ge=3.0, le=10.0)
    source_clip_index: int | None = Field(default=None, ge=0)
    source_start_s: float | None = Field(default=None, ge=0.0)
    source_end_s: float | None = Field(default=None, ge=0.0)
    reference_clip_index: int | None = Field(default=None, ge=0)
    reference_frame_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_source_contract(self) -> OmniAssetStartBody:
        if self.action == "restyle_segment":
            if (
                self.source_clip_index is None
                or self.source_start_s is None
                or self.source_end_s is None
                or self.source_end_s <= self.source_start_s
            ):
                raise ValueError("restyle_segment requires one explicit source segment")
            if self.source_end_s - self.source_start_s > 10.0:
                raise ValueError("restyle source segment cannot exceed 10 seconds")
            if self.reference_clip_index is not None or self.reference_frame_s is not None:
                raise ValueError("restyle_segment does not accept a reference frame")
        elif any(
            value is not None
            for value in (self.source_clip_index, self.source_start_s, self.source_end_s)
        ):
            raise ValueError("generate_insert does not accept a source segment")
        if (self.reference_clip_index is None) != (self.reference_frame_s is None):
            raise ValueError("reference clip and frame time must be provided together")
        return self


class InsertGeneratedAssetOperation(BaseModel):
    op: Literal["insert_generated_asset"] = "insert_generated_asset"
    asset_id: str
    clip_index: int = Field(ge=0)
    insert_at_s: float = Field(ge=0.0)
    duration_s: float = Field(gt=0.0, le=10.0)


class ReplaceGeneratedSegmentOperation(BaseModel):
    op: Literal["replace_generated_segment"] = "replace_generated_segment"
    asset_id: str
    clip_index: int = Field(ge=0)
    source_clip_index: int = Field(ge=0)
    source_start_s: float = Field(ge=0.0)
    source_end_s: float = Field(gt=0.0)
    duration_s: float = Field(gt=0.0, le=10.0)


GeneratedAssetOperation = InsertGeneratedAssetOperation | ReplaceGeneratedSegmentOperation


class OmniAssetResponse(BaseModel):
    asset_id: str
    status: OmniStatus
    progress: float = Field(ge=0.0, le=1.0)
    model: str
    error: str | None = None
    operation: GeneratedAssetOperation | None = None


class OmniAssetClaimBody(BaseModel):
    draft_revision: str = Field(min_length=1, max_length=100)


def _records(job: Job) -> tuple[dict, dict]:
    assembly = copy.deepcopy(job.assembly_plan or {})
    records = assembly.get("omni_generated_assets")
    if not isinstance(records, dict):
        records = {}
        assembly["omni_generated_assets"] = records
    return assembly, records


def omni_response(record: dict) -> OmniAssetResponse:
    operation = record.get("operation")
    return OmniAssetResponse(
        asset_id=str(record["asset_id"]),
        status=str(record.get("status") or "failed"),  # type: ignore[arg-type]
        progress=max(0.0, min(1.0, float(record.get("progress") or 0.0))),
        model=str(record.get("model") or settings.edit_omni_model),
        error=str(record["error"])[:300] if record.get("error") else None,
        operation=(
            (
                ReplaceGeneratedSegmentOperation.model_validate(operation)
                if operation.get("op") == "replace_generated_segment"
                else InsertGeneratedAssetOperation.model_validate(operation)
            )
            if isinstance(operation, dict)
            else None
        ),
    )


def _variant_timeline_slots(variant: dict) -> list[dict]:
    slots: list[dict] = []
    for key in ("ai_timeline", "user_timeline"):
        timeline = variant.get(key)
        if isinstance(timeline, dict):
            slots.extend(slot for slot in timeline.get("slots") or [] if isinstance(slot, dict))
    return slots


def _source_durations(job: Job, variant: dict) -> dict[int, float]:
    durations: dict[int, float] = {}
    for slot in _variant_timeline_slots(variant):
        try:
            index = int(slot["clip_index"])
            duration_s = float(slot.get("source_duration_s") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if duration_s > 0:
            durations[index] = max(durations.get(index, 0.0), duration_s)
    _, records = _records(job)
    for record in records.values():
        operation = record.get("operation") if isinstance(record, dict) else None
        if not isinstance(operation, dict):
            continue
        try:
            index = int(operation["clip_index"])
            duration_s = float(operation["duration_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if duration_s > 0:
            durations[index] = max(durations.get(index, 0.0), duration_s)
    return durations


def _variant_duration_s(variant: dict) -> float:
    timeline = variant.get("user_timeline") or variant.get("ai_timeline")
    slots = timeline.get("slots") if isinstance(timeline, dict) else []
    return sum(
        float(slot.get("duration_s") or 0.0)
        for slot in slots or []
        if isinstance(slot, dict) and not slot.get("removed")
    )


async def _lock_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = (
        await db.execute(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")
    if job.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cancelled_job_immutable",
        )
    return job


async def start_omni_asset(
    job: Job,
    variant_id: str,
    body: OmniAssetStartBody,
    db: AsyncSession,
) -> OmniAssetResponse:
    if not settings.omni_generated_video_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="omni_generated_video_not_enabled",
        )
    job = await _lock_job(db, job.id)
    variant = next(
        (
            candidate
            for candidate in (job.assembly_plan or {}).get("variants") or []
            if isinstance(candidate, dict) and candidate.get("variant_id") == variant_id
        ),
        None,
    )
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    clip_paths = list((job.all_candidates or {}).get("clip_paths") or [])
    for index in (body.source_clip_index, body.reference_clip_index):
        if index is not None and index >= len(clip_paths):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="omni_source_clip_not_found",
            )
    durations = _source_durations(job, variant)
    for index, at_s in (
        (body.source_clip_index, body.source_end_s),
        (body.reference_clip_index, body.reference_frame_s),
    ):
        if index is None or at_s is None:
            continue
        source_duration_s = durations.get(index)
        if source_duration_s is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="omni_source_duration_unavailable",
            )
        if at_s > source_duration_s or (
            index == body.reference_clip_index and at_s >= source_duration_s
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="omni_source_time_out_of_bounds",
            )
    variant_duration_s = _variant_duration_s(variant)
    if variant_duration_s <= 0 or body.insert_at_s > variant_duration_s:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="omni_insert_time_out_of_bounds",
        )
    asset_id = str(uuid.uuid4())
    assembly, records = _records(job)
    record = {
        "asset_id": asset_id,
        "created_at": datetime.now(UTC).isoformat(),
        "suggestion_id": body.suggestion_id,
        "draft_revision": body.draft_revision,
        "status": "queued",
        "progress": 0.02,
        "model": settings.edit_omni_model,
        "action": body.action,
        "prompt": body.prompt,
        "insert_at_s": round(body.insert_at_s, 3),
        "duration_s": round(body.duration_s, 3),
        "source_clip_index": body.source_clip_index,
        "source_start_s": body.source_start_s,
        "source_end_s": body.source_end_s,
        "reference_clip_index": body.reference_clip_index,
        "reference_frame_s": body.reference_frame_s,
        "source_references": [
            clip_paths[index]
            for index in (body.source_clip_index, body.reference_clip_index)
            if index is not None
        ],
        "source_slot_fingerprint": (
            {
                "clip_index": body.source_clip_index,
                "start_s": body.source_start_s,
                "end_s": body.source_end_s,
            }
            if body.action == "restyle_segment"
            else None
        ),
        "provider_interaction_id": None,
        "storage_path": None,
        "operation": None,
        "error": None,
    }
    records[asset_id] = record
    job.assembly_plan = assembly
    await db.commit()

    try:
        from app.tasks.omni_generate import generate_omni_asset  # noqa: PLC0415

        generate_omni_asset.apply_async(
            kwargs={"job_id": str(job.id), "asset_id": asset_id},
            task_id=f"omni-{asset_id}",
        )
    except Exception as exc:
        # The enqueue happens after the queued record commits. Cancellation can
        # win in that gap, so never reuse the pre-commit ORM snapshot here.
        # Re-lock and merge only this asset attempt; a cancelled Job tombstone is
        # immutable even on broker failure recovery.
        locked_job = (
            await db.execute(
                select(Job)
                .where(Job.id == job.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked_job is not None and locked_job.status != "cancelled":
            assembly, records = _records(locked_job)
            current = records.get(asset_id)
            if isinstance(current, dict):
                current.update(
                    status="failed",
                    progress=0.0,
                    error="queue_unavailable",
                )
                locked_job.assembly_plan = assembly
                await db.commit()
            else:
                await db.rollback()
        else:
            await db.rollback()
        log.warning("omni_asset.enqueue_failed", job_id=str(job.id), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="omni_generation_queue_unavailable",
        ) from exc
    return omni_response(record)


def get_omni_asset(job: Job, asset_id: str) -> OmniAssetResponse:
    if job.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cancelled_job_immutable",
        )
    _, records = _records(job)
    record = records.get(asset_id)
    if not isinstance(record, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="omni_asset_not_found")
    return omni_response(record)


async def cancel_omni_asset(
    job: Job,
    asset_id: str,
    db: AsyncSession,
) -> OmniAssetResponse:
    job = await _lock_job(db, job.id)
    assembly, records = _records(job)
    record = records.get(asset_id)
    if not isinstance(record, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="omni_asset_not_found")
    if record.get("status") == "ready":
        storage_path = str(record.get("storage_path") or "")
        operation = record.get("operation")
        released = operation is None
        if isinstance(operation, dict):
            try:
                clip_index = int(operation["clip_index"])
            except (KeyError, TypeError, ValueError):
                clip_index = -1
            candidates = copy.deepcopy(job.all_candidates or {})
            clip_paths = list(candidates.get("clip_paths") or [])
            referenced = any(
                int(slot.get("clip_index", -1)) == clip_index
                for variant in (assembly.get("variants") or [])
                if isinstance(variant, dict)
                for slot in _variant_timeline_slots(variant)
                if not slot.get("removed")
            )
            if (
                not referenced
                and clip_index == len(clip_paths) - 1
                and clip_index >= 0
                and clip_paths[clip_index] == storage_path
            ):
                clip_paths.pop()
                candidates["clip_paths"] = clip_paths
                job.all_candidates = candidates
                released = True
        record.update(
            status="cancelled",
            progress=0.0,
            storage_path=None if released else storage_path,
            output_url=None if released else record.get("output_url"),
            operation=None if released else operation,
            release_pending=not released,
            error=None,
        )
        job.assembly_plan = assembly
        await db.commit()
        if storage_path and released:
            from app.storage import delete_object_best_effort  # noqa: PLC0415

            await asyncio.to_thread(delete_object_best_effort, storage_path)
        return omni_response(record)
    if record.get("status") in {"ready", "failed", "cancelled"}:
        return omni_response(record)
    interaction_id = record.get("provider_interaction_id")
    # A queued task with no provider interaction can be terminalized now.
    # The worker also treats `cancelled` as terminal if it wins the revoke race.
    record["status"] = "cancellation_requested" if interaction_id else "cancelled"
    record["progress"] = min(0.95, float(record.get("progress") or 0.0)) if interaction_id else 0.0
    job.assembly_plan = assembly
    await db.commit()

    from app.worker import celery_app  # noqa: PLC0415

    await asyncio.to_thread(
        celery_app.control.revoke,
        f"omni-{asset_id}",
        terminate=False,
    )
    if interaction_id and settings.gemini_api_key:
        try:
            from google import genai  # type: ignore[import]  # noqa: PLC0415

            client = genai.Client(api_key=settings.gemini_api_key)
            await asyncio.to_thread(
                client.interactions.cancel,
                str(interaction_id),
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.info(
                "omni_asset.provider_cancel_failed",
                job_id=str(job.id),
                asset_id=asset_id,
                error=str(exc)[:200],
            )
    return omni_response(record)


async def claim_omni_asset(
    job: Job,
    asset_id: str,
    body: OmniAssetClaimBody,
    db: AsyncSession,
) -> OmniAssetResponse:
    """Atomically make one ready asset available to the editor clip pool."""
    job = await _lock_job(db, job.id)
    assembly, records = _records(job)
    record = records.get(asset_id)
    if not isinstance(record, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="omni_asset_not_found")
    if record.get("status") != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="omni_asset_not_ready",
        )
    if record.get("draft_revision") != body.draft_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="omni_draft_revision_mismatch",
        )
    if record.get("operation") is not None:
        return omni_response(record)
    storage_path = str(record.get("storage_path") or "")
    duration_s = float(record.get("normalized_duration_s") or 0.0)
    if not storage_path or duration_s <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="omni_asset_incomplete",
        )
    candidates = copy.deepcopy(job.all_candidates or {})
    clip_paths = list(candidates.get("clip_paths") or [])
    if storage_path in clip_paths:
        clip_index = clip_paths.index(storage_path)
    else:
        clip_paths.append(storage_path)
        clip_index = len(clip_paths) - 1
    candidates["clip_paths"] = clip_paths
    job.all_candidates = candidates
    record["claimed_at"] = datetime.now(UTC).isoformat()
    if record.get("action") == "restyle_segment":
        record["operation"] = {
            "op": "replace_generated_segment",
            "asset_id": asset_id,
            "clip_index": clip_index,
            "source_clip_index": int(record["source_clip_index"]),
            "source_start_s": float(record["source_start_s"]),
            "source_end_s": float(record["source_end_s"]),
            "duration_s": duration_s,
        }
    else:
        record["operation"] = {
            "op": "insert_generated_asset",
            "asset_id": asset_id,
            "clip_index": clip_index,
            "insert_at_s": float(record["insert_at_s"]),
            "duration_s": duration_s,
        }
    job.assembly_plan = assembly
    await db.commit()
    return omni_response(record)
