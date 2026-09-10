"""Owner- and revision-fenced final uploads from the phone renderer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.kria.device_render import (
    DeviceAssetDownloadBody,
    DeviceAssetDownloadOut,
    DeviceExportCompleteBody,
    DeviceExportCompleteOut,
    DeviceExportReservationBody,
    DeviceExportReservationOut,
    DeviceRenderIdentity,
    DeviceRenderStatus,
    require_current_request,
)
from app.kria.render_assets import LibraryRenderAsset
from app.limiter import limiter
from app.models import ContentPlan, Job, PlanItem, TemporaryMediaUpload
from app.services.content_plan_persona import PlanPersonaOwnershipError, load_owned_plan_persona
from app.services.device_render import device_record, device_status, save_device_record
from app.services.job_storage_paths import project_media_reference_lock_key
from app.services.render_library import catalog_path, inspect_library_asset

router = APIRouter()


async def _owned_job(db: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    # The owner advisory lock is shared with temporary-upload cleanup/account erasure.
    await db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(user_id))))
    job = (
        await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Job not found")
    initial_item_id = job.content_plan_item_id
    locked_epoch = None
    if initial_item_id is not None:
        item = await db.get(PlanItem, job.content_plan_item_id)
        if item is None:
            raise HTTPException(409, "Project changed")
        plan = await db.get(
            ContentPlan, item.content_plan_id, with_for_update=True, populate_existing=True
        )
        if plan is None or plan.user_id != user_id:
            raise HTTPException(404, "Job not found")
        try:
            await load_owned_plan_persona(db, plan, for_update=True)
        except PlanPersonaOwnershipError as exc:
            raise HTTPException(409, "Project changed") from exc
        item = await db.get(PlanItem, item.id, with_for_update=True, populate_existing=True)
        if (
            item is None
            or item.current_job_id != job_id
            or int(job.content_plan_ownership_epoch or 0) != int(plan.ownership_epoch or 0)
        ):
            raise HTTPException(409, "Project changed")
        locked_epoch = int(plan.ownership_epoch or 0)
    job = await db.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.user_id != user_id:
        raise HTTPException(404, "Job not found")
    if job.content_plan_item_id != initial_item_id or (
        locked_epoch is not None and int(job.content_plan_ownership_epoch or 0) != locked_epoch
    ):
        raise HTTPException(409, "Project changed")
    if job.status == "cancelled":
        raise HTTPException(409, "Render cancelled")
    return job


def _record(job: Job, identity: DeviceRenderIdentity) -> tuple[dict, DeviceRenderStatus]:
    try:
        record = device_record(job, identity.variant_id)
        status = device_status(job, identity.variant_id)
        require_current_request(status, identity)
    except KeyError as exc:
        raise HTTPException(404, "Device recipe unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Device recipe changed") from exc
    if identity.job_id != job.id:
        raise HTTPException(409, "Device recipe changed")
    variant = next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants", [])
            if v.get("variant_id") == identity.variant_id
        ),
        None,
    )
    if variant is None or str(variant.get("render_generation_id") or "") != record.get(
        "base_generation"
    ):
        raise HTTPException(409, "Device recipe changed")
    return record, status


@router.get("/jobs/{job_id}/device-render", response_model=DeviceRenderStatus)
async def get_device_render(
    job_id: uuid.UUID,
    user: CurrentUser,
    variant_id: str = Query(..., min_length=1, max_length=160),
    db: AsyncSession = Depends(get_db),
) -> DeviceRenderStatus:
    job = await _owned_job(db, user.id, job_id)
    try:
        status = device_status(job, variant_id)
    except KeyError as exc:
        raise HTTPException(404, "Device recipe unavailable") from exc
    _record(job, status.request.identity)
    if not settings.phone_rendering_enabled and status.phase == "awaiting_device":
        return status.model_copy(
            update={
                "phase": "needs_attention",
                "reason": "Phone rendering is temporarily unavailable",
            }
        )
    return status


@router.post("/jobs/{job_id}/device-render/assets", response_model=DeviceAssetDownloadOut)
@limiter.limit("30/minute")
async def download_device_asset(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceAssetDownloadBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceAssetDownloadOut:
    if not settings.phone_rendering_enabled:
        raise HTTPException(404, "Phone rendering is unavailable")
    user_id = user.id
    job = await _owned_job(db, user_id, job_id)
    _, status = _record(job, body.identity)
    manifest = getattr(status.request.recipe, "asset_manifest", None)
    asset = next((a for a in manifest.assets if a.id == body.asset_id), None) if manifest else None
    if not isinstance(asset, LibraryRenderAsset):
        raise HTTPException(404, "Library asset unavailable")
    try:
        path = await catalog_path(db, asset.catalog, asset.catalog_id)
        await db.rollback()
        actual = await asyncio.to_thread(
            inspect_library_asset,
            path,
            asset_id=asset.id,
            catalog=asset.catalog,
            catalog_id=asset.catalog_id,
        )
        if actual != asset:
            raise ValueError("library generation changed")
        job = await _owned_job(db, user_id, job_id)
        _record(job, body.identity)
        if await catalog_path(db, asset.catalog, asset.catalog_id) != path:
            raise ValueError("catalog changed")
        url = await asyncio.to_thread(
            storage.signed_get_url_for_generation,
            path,
            generation=asset.generation,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "Library asset unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Library asset changed; refresh the recipe") from exc
    return DeviceAssetDownloadOut(
        asset_id=asset.id,
        download_url=url,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


@router.post("/jobs/{job_id}/device-render/uploads", response_model=DeviceExportReservationOut)
@limiter.limit("10/minute")
async def reserve_device_export(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceExportReservationBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceExportReservationOut:
    if not settings.phone_rendering_enabled:
        raise HTTPException(404, "Phone rendering is unavailable")
    job = await _owned_job(db, user.id, job_id)
    record, status = _record(job, body.identity)
    if status.phase not in {"awaiting_device", "syncing"}:
        raise HTTPException(409, "Render no longer accepts uploads")
    attempt = str(body.attempt_id)
    attempts = record["attempts"]
    previous = attempts.get(attempt)
    if previous and (previous["size"] != body.file_size_bytes or previous["sha256"] != body.sha256):
        raise HTTPException(409, "Upload attempt reused for different bytes")
    if not previous and len(attempts) >= 5:
        raise HTTPException(409, "Too many export attempts for this recipe")
    path = f"{user.id}/{job.id}/device/{body.attempt_id}.mp4"
    now = datetime.now(UTC)
    cleanup = (
        await db.execute(
            select(TemporaryMediaUpload)
            .where(TemporaryMediaUpload.object_path == path)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cleanup is None:
        cleanup = TemporaryMediaUpload(
            user_id=user.id,
            object_path=path,
            purpose="device_export",
            status="reserved",
            retention_expires_at=now + timedelta(hours=24),
        )
        db.add(cleanup)
    elif (
        cleanup.user_id != user.id
        or cleanup.status != "reserved"
        or cleanup.retention_expires_at <= now
    ):
        raise HTTPException(409, "Upload reservation expired")
    attempts[attempt] = {"path": path, "size": body.file_size_bytes, "sha256": body.sha256}
    status.phase = "syncing"
    record["status"] = status.model_dump(mode="json")
    save_device_record(job, body.identity.variant_id, record)
    await db.commit()
    url = await asyncio.to_thread(storage.signed_put_url, path, "video/mp4", body.file_size_bytes)
    return DeviceExportReservationOut(
        attempt_id=body.attempt_id,
        upload_url=url,
        upload_headers={"Content-Type": "video/mp4", "x-goog-if-generation-match": "0"},
        expires_at=now + timedelta(minutes=15),
    )


def _verify_export(
    path: str, generation: str, expected_size: int, expected_sha256: str, status: DeviceRenderStatus
) -> None:
    with tempfile.TemporaryDirectory(prefix="kria_device_export_") as directory:
        local = Path(directory) / "export.mp4"
        storage.download_generation_to_file(path, str(local), generation=generation)
        if local.stat().st_size != expected_size:
            raise ValueError("export size mismatch")
        with local.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected_sha256:
                raise ValueError("export checksum mismatch")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(local)],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        probe = json.loads(result.stdout)
        video = [s for s in probe["streams"] if s["codec_type"] == "video"]
        audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        recipe = status.request.recipe
        if (
            len(video) != 1
            or video[0].get("codec_name") != "h264"
            or video[0].get("width") != recipe.canvas.width
            or video[0].get("height") != recipe.canvas.height
        ):
            raise ValueError("export video format mismatch")
        stream = video[0]
        try:
            actual_fps = float(Fraction(str(stream.get("avg_frame_rate", "0/1"))))
            rotation = float((stream.get("tags") or {}).get("rotate", 0))
            rotations = [
                float(entry["rotation"])
                for entry in stream.get("side_data_list", [])
                if "rotation" in entry
            ] + [rotation]
        except (ValueError, ZeroDivisionError, TypeError) as exc:
            raise ValueError("export video format mismatch") from exc
        if (
            stream.get("pix_fmt") != "yuv420p"
            or abs(actual_fps - recipe.frame_rate) > 0.01
            or any(not math.isfinite(value) or value % 360 != 0 for value in rotations)
        ):
            raise ValueError("export video format mismatch")
        requires_audio = (
            recipe.schema_version == 2
            or recipe.audio.music_asset_id is not None
            or any(track.kind == "audio" and track.clips for track in recipe.tracks)
        )
        if (
            (requires_audio and not audio)
            or len(audio) > 1
            or any(s.get("codec_name") != "aac" for s in audio)
        ):
            raise ValueError("export audio format mismatch")
        duration = float(probe["format"]["duration"])
        if not math.isfinite(duration) or abs(duration - recipe.duration) > max(
            0.1, 2 / recipe.frame_rate
        ):
            raise ValueError("export duration mismatch")


@router.post("/jobs/{job_id}/device-render/complete", response_model=DeviceExportCompleteOut)
@limiter.limit("5/minute")
async def complete_device_export(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceExportCompleteBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceExportCompleteOut:
    user_id = user.id
    job = await _owned_job(db, user_id, job_id)
    record, status = _record(job, body.identity)
    attempt_id = str(body.attempt_id)
    if status.phase == "published":
        if record.get("published_attempt") != attempt_id:
            raise HTTPException(409, "Another export was published")
        return DeviceExportCompleteOut(identity=body.identity)
    if status.phase != "syncing":
        raise HTTPException(409, "Render no longer accepts completion")
    attempt = record["attempts"].get(attempt_id)
    if attempt is None:
        raise HTTPException(409, "Export was not reserved")
    # Do not hold database locks while downloading/probing the MP4. Reacquire the full fence below.
    await db.rollback()
    try:
        metadata = await asyncio.to_thread(storage.object_metadata, attempt["path"])
        if (
            metadata.size != attempt["size"]
            or metadata.content_type != "video/mp4"
            or not metadata.generation
        ):
            raise ValueError("export metadata mismatch")
        await asyncio.to_thread(
            _verify_export,
            attempt["path"],
            str(metadata.generation),
            attempt["size"],
            attempt["sha256"],
            status,
        )
    except FileNotFoundError as exc:
        raise HTTPException(409, "Export upload has not finished") from exc
    except (ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "Export does not match the approved recipe") from exc
    url = await asyncio.to_thread(storage.signed_get_url, attempt["path"])
    job = await _owned_job(db, user_id, job_id)
    record, status = _record(job, body.identity)
    if status.phase == "published":
        if record.get("published_attempt") != attempt_id:
            raise HTTPException(409, "Another export was published")
        return DeviceExportCompleteOut(identity=body.identity)
    if status.phase != "syncing":
        raise HTTPException(409, "Render no longer accepts completion")
    cleanup = (
        await db.execute(
            select(TemporaryMediaUpload)
            .where(TemporaryMediaUpload.object_path == attempt["path"])
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        cleanup is None
        or cleanup.user_id != user_id
        or cleanup.status != "reserved"
        or cleanup.retention_expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Upload reservation expired")
    status.phase = "published"
    record.update(
        status=status.model_dump(mode="json"),
        published_attempt=attempt_id,
        base_generation=attempt_id,
    )
    save_device_record(job, body.identity.variant_id, record)
    assembly = dict(job.assembly_plan)
    assembly["variants"] = [
        {
            **v,
            "ok": True,
            "render_status": "ready",
            "render_generation_id": attempt_id,
            "render_finished_at": datetime.now(UTC).isoformat(),
            "video_path": attempt["path"],
            "output_url": url,
            "render_destination": "phone",
            "duration_s": status.request.recipe.duration,
        }
        if v.get("variant_id") == body.identity.variant_id
        else v
        for v in assembly["variants"]
    ]
    job.assembly_plan = assembly
    if all(v.get("ok") for v in assembly["variants"]):
        job.status = "variants_ready"
        job.current_phase = None
        job.finished_at = datetime.now(UTC)
    cleanup.status = "attached"
    await db.commit()
    return DeviceExportCompleteOut(identity=body.identity)
