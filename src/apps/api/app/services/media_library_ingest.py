"""Verified, owner-serialized library admission. Originals and proxies stay distinct."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from fastapi import HTTPException
from google.api_core.exceptions import NotFound
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.kria.media_sources import MediaUploadContract
from app.models import (
    MediaAsset,
    MediaAssetReference,
    MediaAssetSource,
    MediaAssetUploadReservation,
    TemporaryMediaUpload,
    User,
)
from app.services.job_storage_paths import project_media_reference_lock_key

log = structlog.get_logger()
ACTIVE_ASSET_LIMIT = 500
_URL_TTL = timedelta(minutes=15)
_ALLOWED = {
    "image": {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"},
    "video": {"video/mp4"},
    "audio": {"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/aac"},
}
_BYTE_LIMIT = {"image": 25 * 1024**2, "video": 512 * 1024**2, "audio": 200 * 1024**2}
# A creator-supplied full original (KRI-822) is a real camera file: HEVC .mov
# or .mp4, bounded like plan-item Visuals uploads (routes/plan_items.py).
_ORIGINAL_VIDEO_TYPES = {"video/mp4", "video/quicktime"}
_ORIGINAL_VIDEO_LIMIT = 4 * 1024**3


async def lock_library_owner(db: AsyncSession, owner_id: uuid.UUID) -> None:
    if not settings.media_library_enabled:
        raise HTTPException(404, "Media library is not enabled")
    await db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner_id))))
    if await db.scalar(select(User.id).where(User.id == owner_id)) is None:
        raise HTTPException(404, "Account not found")


async def _make_catalogue_room(db: AsyncSession, owner_id: uuid.UUID) -> None:
    """Archive oldest entries in the admission transaction, under the owner fence.

    This only changes catalogue visibility. Sources, facts and existing post
    references retain their identities and follow the existing retention rules.
    """
    active = await db.scalar(
        select(func.count())
        .select_from(MediaAsset)
        .where(MediaAsset.user_id == owner_id, MediaAsset.archived_at.is_(None))
    )
    excess = int(active or 0) - ACTIVE_ASSET_LIMIT + 1
    if excess <= 0:
        return
    oldest = (
        await db.scalars(
            select(MediaAsset)
            .where(MediaAsset.user_id == owner_id, MediaAsset.archived_at.is_(None))
            .order_by(MediaAsset.created_at, MediaAsset.id)
            .limit(excess)
        )
    ).all()
    archived_at = datetime.now(UTC)
    for asset in oldest:
        asset.archived_at = archived_at


def validate_upload_request(payload: dict, *, legacy: bool = False) -> MediaUploadContract:
    if payload.get("attach_original"):
        return _validate_original_attachment(payload)
    kind = payload["kind"]
    supported = _ALLOWED.get(kind, set()) | (
        {"video/quicktime", "video/webm"} if legacy and kind == "video" else set()
    )
    if payload["content_type"] not in supported:
        raise HTTPException(422, "Unsupported media type")
    limit = 4 * 1024**3 if legacy and kind == "video" else _BYTE_LIMIT[kind]
    if not 0 < payload["file_size_bytes"] <= limit:
        raise HTTPException(422, "Media exceeds the per-file limit")
    contract = MediaUploadContract.model_validate(payload.get("upload_contract") or {})
    if kind == "video" and contract.purpose != "analysis_proxy" and not legacy:
        raise HTTPException(
            422, "Phone videos require an analysis proxy bound to the local original"
        )
    if contract.proxy is not None and contract.proxy.original.kind != kind:
        raise HTTPException(422, "Proxy original kind does not match the uploaded media")
    return contract


def _validate_original_attachment(payload: dict) -> MediaUploadContract:
    """The full original of an owned video; never a proxy, never a new asset."""
    contract = MediaUploadContract.model_validate(payload.get("upload_contract") or {})
    if (
        payload["kind"] != "video"
        or not payload.get("repair_asset_id")
        or contract.purpose != "cloud_render_source"
        or contract.proxy is not None
    ):
        raise HTTPException(
            422, "An original upload needs an owned video and its full, unconverted bytes"
        )
    if payload["content_type"] not in _ORIGINAL_VIDEO_TYPES:
        raise HTTPException(422, "Unsupported media type")
    if not 0 < payload["file_size_bytes"] <= _ORIGINAL_VIDEO_LIMIT:
        raise HTTPException(422, "Media exceeds the per-file limit")
    return contract


def _require_declared_original(asset: MediaAsset, byte_count: int) -> None:
    if not asset.original_declared_sha256 or asset.original_byte_count is None:
        raise HTTPException(422, "This video has no declared original to restore")
    if byte_count != asset.original_byte_count:
        raise HTTPException(422, "This file is not the original of this saved video")


async def _repair_target(db: AsyncSession, owner_id: uuid.UUID, payload: dict) -> MediaAsset | None:
    if not payload.get("repair_asset_id"):
        return None
    asset = await load_library_asset(db, owner_id, uuid.UUID(str(payload["repair_asset_id"])))
    if asset.archived_at is not None or asset.kind != payload["kind"]:
        raise HTTPException(409, "Repair requires an active asset of the same media kind")
    if payload.get("attach_original"):
        # Size is checked before a PUT is signed; the full sha256 at finalize.
        _require_declared_original(asset, payload["file_size_bytes"])
        return asset
    if asset.kind == "video" and payload["upload_contract"] != asset.metadata_json.get(
        "upload_contract"
    ):
        raise HTTPException(422, "Repair proxy must preserve the immutable original binding")
    return asset


async def _reservation_capacity(db, owner_id, payload, *, exclude_reservation=None):
    await _repair_target(db, owner_id, payload)
    # Reserving a PUT never displaces saved media. Bound unfinished uploads
    # separately; only verified unique admission rotates the active catalogue.
    pending_query = (
        select(func.count())
        .select_from(MediaAssetUploadReservation)
        .where(
            MediaAssetUploadReservation.user_id == owner_id,
            MediaAssetUploadReservation.status == "reserved",
            MediaAssetUploadReservation.expires_at > datetime.now(UTC),
        )
    )
    if exclude_reservation is not None:
        pending_query = pending_query.where(MediaAssetUploadReservation.id != exclude_reservation)
    pending = await db.scalar(pending_query)
    if int(pending or 0) >= ACTIVE_ASSET_LIMIT:
        raise HTTPException(
            409, "Too many unfinished library uploads; finish or retry an existing import"
        )


async def reserve_library_upload(
    db: AsyncSession, owner_id: uuid.UUID, payload: dict
) -> MediaAssetUploadReservation:
    if not payload.get("attach_original"):
        # Keep pre-KRI-822 retry identities byte-identical to stored requests.
        payload = {key: value for key, value in payload.items() if key != "attach_original"}
    validate_upload_request(payload)
    await lock_library_owner(db, owner_id)
    row = await db.scalar(
        select(MediaAssetUploadReservation)
        .where(
            MediaAssetUploadReservation.user_id == owner_id,
            MediaAssetUploadReservation.client_upload_id == payload["client_upload_id"],
        )
        .with_for_update()
    )
    if row is not None:
        if row.request_json != payload:
            raise HTTPException(409, "Upload identity reused for different media")
        # Finalized targets never receive another signed PUT.
        if row.status == "finalized":
            await db.commit()
            return row
        receipt = await db.scalar(
            select(TemporaryMediaUpload)
            .where(TemporaryMediaUpload.object_path == row.object_path)
            .with_for_update()
        )
        if (
            receipt is None
            or receipt.status != "reserved"
            or receipt.cleanup_claimed_at is not None
        ):
            raise HTTPException(409, "Upload target is no longer available; start a new import")
        await _reservation_capacity(db, owner_id, payload, exclude_reservation=row.id)
        row.expires_at = datetime.now(UTC) + _URL_TTL
        receipt.retention_expires_at = datetime.now(UTC) + timedelta(days=1)
    else:
        await _reservation_capacity(db, owner_id, payload)
        identifier = uuid.uuid4()
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/heic": "heic",
            "image/heif": "heif",
            "video/mp4": "mp4",
            "video/quicktime": "mov",
        }.get(payload["content_type"], "audio")
        prefix = (
            "analysis-proxy-"
            if payload["upload_contract"].get("purpose") == "analysis_proxy"
            else "source-"
        )
        path = f"users/{owner_id}/media-library/{identifier}/{prefix}{identifier}.{extension}"
        row = MediaAssetUploadReservation(
            id=identifier,
            user_id=owner_id,
            client_upload_id=payload["client_upload_id"],
            request_json=payload,
            object_path=path,
            expires_at=datetime.now(UTC) + _URL_TTL,
        )
        db.add(row)
        db.add(
            TemporaryMediaUpload(
                user_id=owner_id,
                object_path=path,
                purpose="media_library",
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    await db.commit()
    return row


def verified_source_metadata(
    path: str, generation: str, kind: str, payload: dict, *, legacy: bool = False
) -> dict:
    """Hash all pinned bytes and probe media before registration; no AI calls."""
    contract = validate_upload_request(payload, legacy=legacy)
    try:
        before = storage.object_metadata(path)
        if str(before.generation) != generation or before.size != payload["file_size_bytes"]:
            raise ValueError("Uploaded source generation or size changed")
        with tempfile.TemporaryDirectory(prefix="library-verify-") as directory:
            local = Path(directory) / "source"
            storage.download_generation_to_file(path, str(local), generation=generation)
            if local.stat().st_size != payload["file_size_bytes"]:
                raise ValueError("Uploaded source length does not match")
            digest = hashlib.sha256()
            with local.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            facts = {}
            if kind == "image":
                import pillow_heif
                from PIL import Image

                pillow_heif.register_heif_opener()
                with Image.open(local) as image:
                    image.load()
                    facts = {"width": image.width, "height": image.height, "format": image.format}
                    formats = {
                        "image/jpeg": {"JPEG"},
                        "image/png": {"PNG"},
                        "image/webp": {"WEBP"},
                        "image/heic": {"HEIF"},
                        "image/heif": {"HEIF"},
                    }
                    if image.format not in formats[payload["content_type"]]:
                        raise ValueError("Uploaded image bytes do not match their media type")
            else:
                completed = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_format",
                        "-show_streams",
                        "-of",
                        "json",
                        str(local),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                probe = json.loads(completed.stdout)
                streams = probe.get("streams", [])
                videos = [
                    stream
                    for stream in streams
                    if stream.get("codec_type") == "video"
                    and not stream.get("disposition", {}).get("attached_pic")
                ]
                audio = any(stream.get("codec_type") == "audio" for stream in streams)
                duration = float(probe.get("format", {}).get("duration", 0))
                if not math.isfinite(duration) or not 0 < duration <= 1800:
                    raise ValueError("Media must have a supported, finite duration")
                if kind == "video" and not videos or kind == "audio" and (not audio or videos):
                    raise ValueError("Uploaded media has the wrong stream kind")
                facts = {"duration_s": duration, "has_audio": audio}
                if videos:
                    facts.update(
                        {"width": int(videos[0]["width"]), "height": int(videos[0]["height"])}
                    )
                if contract.proxy is not None:
                    from fractions import Fraction

                    contract.proxy.verify_registered(duration, audio)
                    if kind == "video":
                        video = videos[0] if videos else {}
                        frame_rate = float(Fraction(video.get("avg_frame_rate", "0/1")))
                        rotation = float(video.get("tags", {}).get("rotate", 0))
                        for side in video.get("side_data_list", []):
                            rotation = float(side.get("rotation", rotation))
                        if (
                            video.get("codec_name") != "h264"
                            or not 1 <= frame_rate <= 30
                            or abs(frame_rate - float(contract.proxy.frame_rate or 0)) > 0.5
                            or rotation % 360 != 0
                        ):
                            raise ValueError(
                                "Video proxy requires H.264, matching FPS and baked orientation"
                            )
                    if videos and (
                        facts["width"] != contract.proxy.width
                        or facts["height"] != contract.proxy.height
                    ):
                        raise ValueError("Proxy dimensions do not match")
            if str(storage.object_metadata(path).generation) != generation:
                raise ValueError("Media changed during verification")
        return {
            "uploaded_sha256": digest.hexdigest(),
            "byte_count": before.size,
            "content_type": payload["content_type"],
            "generation": generation,
            "role": "analysis_proxy" if contract.proxy else "original",
            "metadata": facts,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "Uploaded bytes are invalid or unavailable") from exc


async def register_verified_source(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    kind: str,
    object_path: str,
    generation: str,
    uploaded_sha256: str,
    byte_count: int,
    content_type: str,
    role: str,
    original_declared_sha256: str | None = None,
    original_byte_count: int | None = None,
    metadata: dict | None = None,
    repair_asset_id: uuid.UUID | None = None,
    attach_original: bool = False,
) -> tuple[MediaAsset, MediaAssetSource]:
    """Admission seam for verified existing upload paths; caller owns commit.

    ``attach_original`` (KRI-822) adds the full original to an owned asset whose
    declared original identity equals the verified uploaded bytes.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", uploaded_sha256):
        raise HTTPException(422, "A verified full source hash is required")
    await lock_library_owner(db, owner_id)
    repair = None
    if repair_asset_id is not None:
        repair = await load_library_asset(db, owner_id, repair_asset_id)
        if repair.archived_at is not None or repair.kind != kind:
            raise HTTPException(409, "Repair requires an active asset of the same media kind")
        if attach_original:
            _require_declared_original(repair, byte_count)
            if role != "original" or uploaded_sha256 != repair.original_declared_sha256:
                raise HTTPException(422, "This file is not the original of this saved video")
            original_declared_sha256 = repair.original_declared_sha256
            original_byte_count = repair.original_byte_count
        elif role == "original":
            known = await db.scalar(
                select(MediaAssetSource.id).where(
                    MediaAssetSource.asset_id == repair.id,
                    MediaAssetSource.user_id == owner_id,
                    MediaAssetSource.role == role,
                    MediaAssetSource.uploaded_sha256 == uploaded_sha256,
                    MediaAssetSource.byte_count == byte_count,
                )
            )
            if known is None:
                raise HTTPException(422, "Repair bytes must match the verified original")
        elif (
            not original_declared_sha256
            or repair.original_declared_sha256 != original_declared_sha256
            or repair.original_byte_count != original_byte_count
        ):
            raise HTTPException(422, "Repair must preserve the declared original identity")
    # Device declarations alone never establish uploaded-byte identity.
    match = await db.execute(
        select(MediaAsset, MediaAssetSource)
        .join(MediaAssetSource, MediaAssetSource.asset_id == MediaAsset.id)
        .where(
            MediaAsset.user_id == owner_id,
            MediaAsset.kind == kind,
            (MediaAsset.id == repair_asset_id) if repair_asset_id else True,
            MediaAsset.archived_at.is_(None),
            MediaAsset.original_declared_sha256.is_not_distinct_from(original_declared_sha256),
            MediaAsset.original_byte_count.is_not_distinct_from(original_byte_count),
            MediaAssetSource.user_id == owner_id,
            MediaAssetSource.uploaded_sha256 == uploaded_sha256,
            MediaAssetSource.role == role,
            MediaAssetSource.status.in_(["available", "missing", "deleted"]),
        )
        .order_by(
            (MediaAssetSource.status == "available").desc(),
            MediaAsset.created_at,
            MediaAssetSource.created_at.desc(),
        )
        .limit(1)
    )
    existing = match.first()
    if existing is not None and existing[1].status == "available":
        current_source = existing[1]
        try:
            current = await asyncio.to_thread(storage.object_metadata, current_source.object_path)
        except (NotFound, FileNotFoundError):
            current = None
        except Exception as exc:
            raise HTTPException(503, "Cannot verify the existing cloud copy; retry") from exc
        if (
            current is not None
            and str(current.generation) == current_source.object_generation
            and current.size == current_source.byte_count
        ):
            if attach_original:
                existing[0].original_availability = "cloud_backed"
            return existing[0], current_source
        current_source.status = "missing"
    if existing is None and repair is None:
        await _make_catalogue_room(db, owner_id)
    asset = (
        existing[0]
        if existing is not None
        else repair
        if repair is not None
        else MediaAsset(
            id=uuid.uuid4(),
            user_id=owner_id,
            kind=kind,
            original_declared_sha256=original_declared_sha256,
            original_byte_count=original_byte_count,
            original_availability="cloud_backed" if role == "original" else "device_only",
            metadata_json=metadata or {},
            status="pending",
            # Transaction start can precede another import's commit while
            # waiting on the owner fence. Age new entries at actual admission.
            created_at=func.clock_timestamp(),
        )
    )
    if repair is not None:
        # A changed proxy hash does not match the previous source above. Check
        # the prior default without rewriting any existing consumer's pin.
        previous = await db.scalar(
            select(MediaAssetSource)
            .where(
                MediaAssetSource.asset_id == repair.id,
                MediaAssetSource.user_id == owner_id,
                MediaAssetSource.role == role,
                MediaAssetSource.status == "available",
            )
            .order_by(MediaAssetSource.created_at.desc(), MediaAssetSource.id.desc())
            .limit(1)
        )
        if previous is not None:
            try:
                old_copy = await asyncio.to_thread(storage.object_metadata, previous.object_path)
            except (NotFound, FileNotFoundError):
                previous.status = "missing"
            except Exception as exc:
                # The new copy is verified; a transient old-copy lookup cannot
                # establish absence or invalidate the old post references.
                log.warning("library_repair_prior_copy_unverified", error_type=type(exc).__name__)
            else:
                if (
                    str(old_copy.generation) != previous.object_generation
                    or old_copy.size != previous.byte_count
                ):
                    previous.status = "missing"
    asset.status = "pending"
    source = MediaAssetSource(
        id=uuid.uuid4(),
        user_id=owner_id,
        asset_id=asset.id,
        role=role,
        object_path=object_path,
        object_generation=generation,
        uploaded_sha256=uploaded_sha256,
        byte_count=byte_count,
        content_type=content_type,
        status="available",
    )
    if attach_original:
        # Only after the full bytes matched the declared original identity.
        asset.original_availability = "cloud_backed"
    db.add(asset)
    await db.flush()
    db.add(source)
    await db.flush()
    return asset, source


async def track_library_duplicate(db: AsyncSession, owner_id: uuid.UUID, object_path: str) -> None:
    """Keep the unused verified upload discoverable after canonical rebinding."""
    receipt = await db.scalar(
        select(TemporaryMediaUpload).where(TemporaryMediaUpload.object_path == object_path)
    )
    if receipt is None:
        db.add(
            TemporaryMediaUpload(
                user_id=owner_id,
                object_path=object_path,
                purpose="library_duplicate",
                retention_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )


async def preserve_rejected_uploads(
    db: AsyncSession, owner_id: uuid.UUID, thread_id: uuid.UUID, paths: list[str]
) -> None:
    """Rollback the rejected attachment, then commit only cleanup ownership.

    All physical uploads in a rejected batch are abandoned, including its
    first uncommitted canonical source. Already-bound bytes remain protected
    by cleanup's locked reference recheck; claimed targets are never revived.
    """
    await db.rollback()
    from app.services.job_storage_paths import lock_project_media_owner

    await lock_project_media_owner(db, owner_id)
    if await db.scalar(select(User.id).where(User.id == owner_id)) is None:
        await db.rollback()
        return
    prefix = f"users/{owner_id}/creation-threads/{thread_id}/"
    for path in sorted(set(paths)):
        if not path.startswith(prefix) or ".." in path.split("/"):
            raise ValueError("Rejected upload path does not belong to its thread")
        await track_library_duplicate(db, owner_id, path)
    await db.commit()


def dispatch_library_analysis(asset_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    from app.tasks.media_library import analyze_media_asset_task

    analyze_media_asset_task.delay(str(asset_id), str(owner_id))


async def _publish_reservation(db: AsyncSession, row: MediaAssetUploadReservation) -> None:
    if not row.analysis_dispatch_pending:
        return
    try:
        await asyncio.to_thread(dispatch_library_analysis, row.asset_id, row.user_id)
    except Exception as exc:
        log.warning(
            "library_analysis_publication_deferred",
            reservation_id=str(row.id),
            error_type=type(exc).__name__,
        )
        return
    row.analysis_dispatch_pending = False
    await db.commit()


async def finalize_library_upload(
    db: AsyncSession, owner_id: uuid.UUID, reservation_id: uuid.UUID, client_event_id: str
) -> tuple[MediaAsset, MediaAssetSource]:
    row = await db.scalar(
        select(MediaAssetUploadReservation).where(
            MediaAssetUploadReservation.id == reservation_id,
            MediaAssetUploadReservation.user_id == owner_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Upload reservation not found")
    if row.status == "finalized":
        asset, source = await resolve_library_source(
            db, owner_id, row.asset_id, row.source_id, allow_archived=True, allow_missing=True
        )
        await db.commit()
        await _publish_reservation(db, row)
        return asset, source
    path, payload = row.object_path, dict(row.request_json)
    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(409, "Upload reservation expired; renew before finalizing")
    # No long transaction while downloading and probing.
    await db.commit()
    try:
        metadata = await asyncio.to_thread(storage.object_metadata, path)
    except Exception as exc:
        raise HTTPException(409, "Upload is not available") from exc
    proof = await asyncio.to_thread(
        verified_source_metadata, path, str(metadata.generation), payload["kind"], payload
    )
    await lock_library_owner(db, owner_id)
    row = await db.scalar(
        select(MediaAssetUploadReservation)
        .where(
            MediaAssetUploadReservation.id == reservation_id,
            MediaAssetUploadReservation.user_id == owner_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, "Upload reservation not found")
    if row.status == "finalized":
        asset, source = await resolve_library_source(
            db, owner_id, row.asset_id, row.source_id, allow_archived=True, allow_missing=True
        )
        await db.commit()
        await _publish_reservation(db, row)
        return asset, source
    receipt = await db.scalar(
        select(TemporaryMediaUpload)
        .where(TemporaryMediaUpload.user_id == owner_id, TemporaryMediaUpload.object_path == path)
        .with_for_update()
    )
    if (
        row.expires_at <= datetime.now(UTC)
        or receipt is None
        or receipt.status != "reserved"
        or receipt.cleanup_claimed_at is not None
    ):
        raise HTTPException(409, "Upload cleanup has started; import the media again")
    current = await asyncio.to_thread(storage.object_metadata, path)
    if str(current.generation) != proof["generation"] or current.size != proof["byte_count"]:
        raise HTTPException(409, "Uploaded source changed")
    contract = MediaUploadContract.model_validate(payload["upload_contract"])
    original = contract.proxy.original if contract.proxy else None
    asset, source = await register_verified_source(
        db,
        owner_id=owner_id,
        kind=payload["kind"],
        object_path=path,
        generation=proof["generation"],
        uploaded_sha256=proof["uploaded_sha256"],
        byte_count=proof["byte_count"],
        content_type=proof["content_type"],
        role=proof["role"],
        original_declared_sha256=original.sha256 if original else None,
        original_byte_count=original.byte_count if original else None,
        metadata={
            **proof["metadata"],
            "filename": payload["filename"],
            "upload_contract": payload["upload_contract"],
        },
        repair_asset_id=uuid.UUID(payload["repair_asset_id"])
        if payload.get("repair_asset_id")
        else None,
        attach_original=bool(payload.get("attach_original")),
    )
    if source.object_path == path:
        receipt.status = "adopted"
    # Duplicate upload receipts stay reserved for bounded cleanup.
    row.asset_id, row.source_id, row.status = asset.id, source.id, "finalized"
    row.client_event_id, row.analysis_dispatch_pending = client_event_id, True
    await db.commit()
    await _publish_reservation(db, row)
    return asset, source


async def load_library_asset(
    db: AsyncSession, owner_id: uuid.UUID, asset_id: uuid.UUID
) -> MediaAsset:
    asset = await db.scalar(
        select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.user_id == owner_id)
    )
    if asset is None:
        raise HTTPException(404, "Media asset not found")
    return asset


async def resolve_library_source(
    db: AsyncSession,
    owner_id: uuid.UUID,
    asset_id: uuid.UUID,
    source_id: uuid.UUID | None = None,
    kind: str | None = None,
    allow_archived: bool = False,
    allow_missing: bool = False,
    prefer_role: str | None = None,
) -> tuple[MediaAsset, MediaAssetSource]:
    asset = await load_library_asset(db, owner_id, asset_id)
    if (asset.archived_at is not None and not allow_archived) or (
        kind is not None and kind != asset.kind
    ):
        raise HTTPException(409, "Media asset is unavailable for this selection")
    query = select(MediaAssetSource).where(
        MediaAssetSource.asset_id == asset.id,
        MediaAssetSource.user_id == owner_id,
        MediaAssetSource.role.in_(["original", "analysis_proxy"]),
    )
    if source_id is not None:
        query = query.where(MediaAssetSource.id == source_id)
    if not allow_missing:
        query = query.where(MediaAssetSource.status == "available")
    ordering = [(MediaAssetSource.role == prefer_role).desc()] if prefer_role else []
    source = await db.scalar(
        query.order_by(
            *ordering,
            (MediaAssetSource.status == "available").desc(),
            MediaAssetSource.created_at.desc(),
            MediaAssetSource.id.desc(),
        ).limit(1)
    )
    if source is None:
        raise HTTPException(409, "Cloud copy is unavailable; repair this asset from its original")
    return asset, source


async def reference_library_source(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    asset_id: uuid.UUID,
    source_id: uuid.UUID,
    creation_thread_id: uuid.UUID | None = None,
    plan_item_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> MediaAssetReference:
    """Keep a consumer reference in the same transaction as its source binding."""
    await lock_library_owner(db, owner_id)
    consumers = {
        "creation_thread_id": creation_thread_id,
        "plan_item_id": plan_item_id,
        "job_id": job_id,
    }
    selected = [(key, value) for key, value in consumers.items() if value is not None]
    if len(selected) != 1:
        raise ValueError("Exactly one media consumer is required")
    key, value = selected[0]
    row = await db.scalar(
        select(MediaAssetReference).where(
            MediaAssetReference.user_id == owner_id,
            MediaAssetReference.asset_id == asset_id,
            MediaAssetReference.source_id == source_id,
            getattr(MediaAssetReference, key) == value,
        )
    )
    if row is None:
        row = MediaAssetReference(
            user_id=owner_id, asset_id=asset_id, source_id=source_id, **{key: value}
        )
        db.add(row)
        await db.flush()
    return row


async def archive_library_asset(
    db: AsyncSession, owner_id: uuid.UUID, asset_id: uuid.UUID
) -> MediaAsset:
    await lock_library_owner(db, owner_id)
    asset = await load_library_asset(db, owner_id, asset_id)
    asset.archived_at = asset.archived_at or datetime.now(UTC)
    await db.commit()
    return asset


async def retry_library_asset(
    db: AsyncSession, owner_id: uuid.UUID, asset_id: uuid.UUID
) -> MediaAsset:
    await lock_library_owner(db, owner_id)
    asset, _ = await resolve_library_source(db, owner_id, asset_id)
    asset.status = "pending"
    # The upload receipt is a durable publication outbox, even after finalize.
    rows = (
        await db.scalars(
            select(MediaAssetUploadReservation).where(
                MediaAssetUploadReservation.user_id == owner_id,
                MediaAssetUploadReservation.asset_id == asset_id,
            )
        )
    ).all()
    for row in rows:
        row.analysis_dispatch_pending = True
    await db.commit()
    if rows:
        await _publish_reservation(db, rows[0])
    else:
        try:
            await asyncio.to_thread(dispatch_library_analysis, asset.id, owner_id)
        except Exception as exc:
            raise HTTPException(503, "Analysis could not be queued; retry in a moment") from exc
    return asset
