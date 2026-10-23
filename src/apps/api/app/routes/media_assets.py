"""Authenticated, default-off native media library."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from google.api_core.exceptions import NotFound
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import CurrentUser, NativeClient
from app.config import settings
from app.database import get_db
from app.models import (
    MediaAsset,
    MediaAssetReference,
    MediaAssetSource,
    MediaCollection,
    MediaCollectionMember,
    StorageRetentionEntry,
)
from app.schemas.media_assets import (
    MediaAssetAccessOut,
    MediaAssetFinalizeBody,
    MediaAssetListOut,
    MediaAssetOut,
    MediaAssetSourceOut,
    MediaAssetStorageWarning,
    MediaAssetUploadBody,
    MediaAssetUploadOut,
    MediaAssetUsageOut,
)
from app.services import media_library_ingest as ingest


def _require_library(native_client: NativeClient) -> None:
    if not settings.media_library_enabled:
        raise HTTPException(404, "Media library is not enabled")
    if not native_client:
        raise HTTPException(403, "Media library is available in the iOS app")


router = APIRouter(
    prefix="/me/media-assets", tags=["media-library"], dependencies=[Depends(_require_library)]
)


def _serialize_asset(asset, sources, refs, warnings, facts) -> MediaAssetOut:
    return MediaAssetOut(
        id=asset.id,
        kind=asset.kind,
        status="ready" if facts else asset.status,
        archived_at=asset.archived_at,
        created_at=asset.created_at,
        original_declared_sha256=asset.original_declared_sha256,
        original_byte_count=asset.original_byte_count,
        original_availability=asset.original_availability,
        metadata_json=asset.metadata_json,
        creator_context=asset.creator_context,
        analysis=facts,
        sources=[
            MediaAssetSourceOut(
                id=source.id,
                role=source.role,
                status=source.status,
                byte_count=source.byte_count,
                content_type=source.content_type,
                uploaded_sha256=source.uploaded_sha256,
                generation=source.object_generation,
            )
            for source in sources
        ],
        usage=[
            MediaAssetUsageOut(
                creation_thread_id=ref.creation_thread_id,
                plan_item_id=ref.plan_item_id,
                job_id=ref.job_id,
                source_id=ref.source_id,
                created_at=ref.created_at,
            )
            for ref in refs
        ],
        storage_warnings=warnings,
    )


async def assets_out(db: AsyncSession, assets: list[MediaAsset]) -> list[MediaAssetOut]:
    if not assets:
        return []
    from app.services.media_library_analysis import _Source, get_media_asset_facts_batch

    owner, identifiers = assets[0].user_id, [asset.id for asset in assets]
    sources = (
        await db.scalars(
            select(MediaAssetSource)
            .where(MediaAssetSource.user_id == owner, MediaAssetSource.asset_id.in_(identifiers))
            .order_by(MediaAssetSource.created_at.desc(), MediaAssetSource.id.desc())
        )
    ).all()
    refs = (
        await db.scalars(
            select(MediaAssetReference)
            .where(
                MediaAssetReference.user_id == owner, MediaAssetReference.asset_id.in_(identifiers)
            )
            .order_by(MediaAssetReference.created_at)
        )
    ).all()
    warnings = (
        await db.execute(
            select(
                StorageRetentionEntry.object_path,
                StorageRetentionEntry.object_generation,
                func.max(StorageRetentionEntry.created_at).label("warned_at"),
            )
            .where(
                StorageRetentionEntry.creator_id == owner,
                StorageRetentionEntry.object_path.in_([source.object_path for source in sources]),
                StorageRetentionEntry.action == "warn",
                StorageRetentionEntry.status == "pending",
            )
            .group_by(StorageRetentionEntry.object_path, StorageRetentionEntry.object_generation)
        )
    ).all()
    warning_map = {(row.object_path, row.object_generation): row.warned_at for row in warnings}
    source_map, ref_map = {}, {}
    for source in sources:
        source_map.setdefault(source.asset_id, []).append(source)
    for ref in refs:
        ref_map.setdefault(ref.asset_id, []).append(ref)
    fact_sources = []
    for asset in assets:
        eligible = [
            source
            for source in source_map.get(asset.id, [])
            if source.role in {"original", "analysis_proxy"}
            and source.uploaded_sha256
            and len(source.uploaded_sha256) == 64
        ]
        # KRI-822: a device-only video's facts come from its proxy, not an attached original.
        eligible.sort(
            key=lambda source: (
                source.status != "available",
                asset.original_availability == "device_only" and source.role != "analysis_proxy",
            )
        )
        if eligible:
            source = eligible[0]
            fact_sources.append(
                _Source(
                    str(asset.id),
                    str(source.id),
                    str(owner),
                    asset.kind,
                    source.role,
                    source.object_path,
                    source.object_generation,
                    source.uploaded_sha256,
                    source.status,
                )
            )
    facts = await asyncio.to_thread(get_media_asset_facts_batch, fact_sources)
    results = []
    for asset in assets:
        asset_sources = source_map.get(asset.id, [])
        if asset.original_availability == "device_only":
            # Pre-KRI-822 clients bind the first available source for phone Footage.
            asset_sources = sorted(asset_sources, key=lambda source: source.role == "original")
        asset_warnings = []
        for source in asset_sources:
            last_use = source.last_used_at or source.created_at
            warned = warning_map.get((source.object_path, source.object_generation))
            if (
                warned
                and warned >= last_use + timedelta(days=83)
                and source.status in {"available", "missing"}
            ):
                asset_warnings.append(
                    MediaAssetStorageWarning(
                        source_id=source.id,
                        warned_at=warned,
                        eligible_at=max(last_use + timedelta(days=90), warned + timedelta(days=7)),
                    )
                )
        results.append(
            _serialize_asset(
                asset,
                asset_sources,
                ref_map.get(asset.id, []),
                asset_warnings,
                facts.get(str(asset.id)),
            )
        )
    return results


async def asset_out(db: AsyncSession, asset: MediaAsset) -> MediaAssetOut:
    return (await assets_out(db, [asset]))[0]


@router.post("/uploads", response_model=MediaAssetUploadOut)
async def reserve_upload(
    body: MediaAssetUploadBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> MediaAssetUploadOut:
    row = await ingest.reserve_library_upload(db, user.id, body.model_dump(mode="json"))
    url = None
    if row.status == "reserved":
        try:
            url = await asyncio.to_thread(
                storage.signed_put_url, row.object_path, body.content_type, body.file_size_bytes
            )
        except Exception as exc:
            raise HTTPException(503, "Upload service unavailable; retry this reservation") from exc
    return MediaAssetUploadOut(
        reservation_id=row.id,
        object_path=row.object_path,
        upload_url=url,
        expires_at=row.expires_at,
        upload_contract=body.upload_contract,
        asset_id=row.asset_id,
        upload_headers={"x-goog-if-generation-match": "0"} if url else {},
    )


@router.post("/uploads/{reservation_id}/finalize", response_model=MediaAssetOut)
async def finalize_upload(
    reservation_id: uuid.UUID,
    body: MediaAssetFinalizeBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MediaAssetOut:
    asset, _ = await ingest.finalize_library_upload(
        db, user.id, reservation_id, body.client_event_id
    )
    return await asset_out(db, asset)


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        timestamp, identifier = datetime.fromisoformat(value[0]), uuid.UUID(value[1])
        if timestamp.tzinfo is None:
            raise ValueError("Naive cursor time")
        return timestamp, identifier
    except Exception as exc:
        raise HTTPException(422, "Invalid media library cursor") from exc


@router.get("", response_model=MediaAssetListOut)
async def list_assets(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    query: str | None = Query(None, max_length=200),
    kind: Literal["video", "image", "audio"] | None = None,
    status: Literal["pending", "processing", "ready", "failed"] | None = None,
    collection_id: uuid.UUID | None = None,
) -> MediaAssetListOut:
    statement = select(MediaAsset).where(
        MediaAsset.user_id == user.id, MediaAsset.archived_at.is_(None)
    )
    if kind:
        statement = statement.where(MediaAsset.kind == kind)
    if status:
        statement = statement.where(MediaAsset.status == status)
    if query:
        literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(
            MediaAsset.metadata_json["filename"].astext.ilike(f"%{literal}%", escape="\\")
        )
    if collection_id:
        owned = await db.scalar(
            select(MediaCollection.id).where(
                MediaCollection.id == collection_id, MediaCollection.user_id == user.id
            )
        )
        if owned is None:
            raise HTTPException(404, "Collection not found")
        statement = statement.join(
            MediaCollectionMember,
            and_(
                MediaCollectionMember.asset_id == MediaAsset.id,
                MediaCollectionMember.user_id == user.id,
                MediaCollectionMember.collection_id == collection_id,
            ),
        )
    if cursor:
        timestamp, identifier = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                MediaAsset.created_at < timestamp,
                and_(MediaAsset.created_at == timestamp, MediaAsset.id < identifier),
            )
        )
    rows = (
        await db.scalars(
            statement.order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc()).limit(limit + 1)
        )
    ).all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([last.created_at.isoformat(), str(last.id)]).encode()
            )
            .decode()
            .rstrip("=")
        )
    count = await db.scalar(
        select(func.count())
        .select_from(MediaAsset)
        .where(MediaAsset.user_id == user.id, MediaAsset.archived_at.is_(None))
    )
    return MediaAssetListOut(
        assets=await assets_out(db, list(rows[:limit])),
        next_cursor=next_cursor,
        active_count=count or 0,
    )


@router.get("/{asset_id}", response_model=MediaAssetOut)
async def detail(
    asset_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> MediaAssetOut:
    return await asset_out(db, await ingest.load_library_asset(db, user.id, asset_id))


@router.get("/{asset_id}/sources/{source_id}/access-url", response_model=MediaAssetAccessOut)
async def access_url(
    asset_id: uuid.UUID, source_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> MediaAssetAccessOut:
    await ingest.load_library_asset(db, user.id, asset_id)
    source = await db.scalar(
        select(MediaAssetSource).where(
            MediaAssetSource.asset_id == asset_id,
            MediaAssetSource.user_id == user.id,
            MediaAssetSource.id == source_id,
        )
    )
    if source is None:
        raise HTTPException(404, "Media source not found")
    if source.status != "available":
        raise HTTPException(409, "Cloud copy is unavailable")
    try:
        metadata = await asyncio.to_thread(storage.object_metadata, source.object_path)
        if str(metadata.generation) != source.object_generation:
            await ingest.lock_library_owner(db, user.id)
            source.status = "missing"
            await db.commit()
            raise HTTPException(409, "Cloud copy changed; repair from the original")
        url = await asyncio.to_thread(
            storage.signed_get_url_for_generation,
            source.object_path,
            generation=source.object_generation,
        )
    except (NotFound, FileNotFoundError) as exc:
        await ingest.lock_library_owner(db, user.id)
        source.status = "missing"
        await db.commit()
        raise HTTPException(409, "Cloud copy is missing; repair from the original") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "Cloud access is temporarily unavailable; retry") from exc
    return MediaAssetAccessOut(
        url=url,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        role=source.role,
        generation=source.object_generation,
    )


@router.post("/{asset_id}/retry", response_model=MediaAssetOut)
async def retry(
    asset_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> MediaAssetOut:
    return await asset_out(db, await ingest.retry_library_asset(db, user.id, asset_id))


@router.post("/{asset_id}/archive", response_model=MediaAssetOut)
async def archive(
    asset_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> MediaAssetOut:
    return await asset_out(db, await ingest.archive_library_asset(db, user.id, asset_id))
