"""Independent per-post pool projections of shared, verified library originals."""

import asyncio
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import PlanItemAsset
from app.services.media_library_analysis import get_media_asset_analysis
from app.services.media_library_ingest import reference_library_source, resolve_library_source
from app.services.pool_asset_preview import needs_preview


async def project_library_visual(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    item_id: uuid.UUID,
    asset_id: uuid.UUID,
    source_id: uuid.UUID | None = None,
    maximum: int = 100,
) -> PlanItemAsset:
    asset, source = await resolve_library_source(db, owner_id, asset_id, source_id)
    if asset.kind not in {"image", "video"} or source.role != "original":
        raise HTTPException(
            422, "Select videos as footage to preserve their local original binding"
        )
    existing = await db.scalar(
        select(PlanItemAsset).where(
            PlanItemAsset.plan_item_id == item_id,
            PlanItemAsset.user_id == owner_id,
            PlanItemAsset.library_source_id == source.id,
            PlanItemAsset.status.notin_(["deduped", "cleanup_pending"]),
        )
    )
    if existing is not None:
        return existing
    count = await db.scalar(
        select(func.count())
        .select_from(PlanItemAsset)
        .where(PlanItemAsset.plan_item_id == item_id, PlanItemAsset.status != "deduped")
    )
    if int(count or 0) >= maximum:
        raise HTTPException(409, "This post has reached its visual asset limit")
    try:
        metadata = await asyncio.to_thread(storage.object_metadata, source.object_path)
        if str(metadata.generation) != source.object_generation:
            raise ValueError("Library source generation changed")
    except Exception as exc:
        raise HTTPException(
            409, "Cloud copy is unavailable; repair the asset from its original"
        ) from exc
    facts = await asyncio.to_thread(
        get_media_asset_analysis,
        owner_id=str(owner_id),
        asset_id=str(asset.id),
        source_id=str(source.id),
    )
    ready = facts is not None and not needs_preview(
        asset.kind, source.content_type, source.object_path
    )
    row = PlanItemAsset(
        id=uuid.uuid4(),
        user_id=owner_id,
        plan_item_id=item_id,
        kind=asset.kind,
        library_asset_id=asset.id,
        library_source_id=source.id,
        gcs_path=source.object_path,
        gcs_generation=source.object_generation,
        content_fingerprint=f"library-sha256:{source.uploaded_sha256}",
        upload_content_type=source.content_type,
        upload_size_bytes=source.byte_count,
        source_filename=str(asset.metadata_json.get("filename") or "Library media"),
        status="ready" if ready else "uploaded",
        media_status="ready" if ready else "pending",
        analysis=facts,
        aspect=facts.get("aspect") if facts else None,
        duration_s=asset.metadata_json.get("duration_s"),
        user_context="",
    )
    db.add(row)
    await db.flush()
    await reference_library_source(
        db, owner_id=owner_id, asset_id=asset.id, source_id=source.id, plan_item_id=item_id
    )
    return row


async def adopt_uploaded_visual(db: AsyncSession, asset: PlanItemAsset) -> None:
    """Register the existing verified upload, without sending any new bytes."""
    from app.services.media_library_ingest import (
        register_verified_source,
        track_library_duplicate,
        verified_source_metadata,
    )

    if asset.library_asset_id is not None or not asset.gcs_generation:
        return
    payload = {
        "kind": asset.kind,
        "content_type": asset.upload_content_type,
        "file_size_bytes": asset.upload_size_bytes,
        "upload_contract": {"purpose": "cloud_render_source", "proxy": None},
    }
    metadata = await asyncio.to_thread(storage.object_metadata, asset.gcs_path)
    payload["content_type"], payload["file_size_bytes"] = metadata.content_type, metadata.size
    proof = await asyncio.to_thread(
        verified_source_metadata,
        asset.gcs_path,
        str(asset.gcs_generation),
        asset.kind,
        payload,
        legacy=True,
    )
    library, source = await register_verified_source(
        db,
        owner_id=asset.user_id,
        kind=asset.kind,
        object_path=asset.gcs_path,
        generation=str(asset.gcs_generation),
        uploaded_sha256=proof["uploaded_sha256"],
        byte_count=proof["byte_count"],
        content_type=proof["content_type"],
        role="original",
        metadata={
            **proof["metadata"],
            "filename": asset.source_filename,
            "upload_contract": payload["upload_contract"],
        },
    )
    if source.object_path != asset.gcs_path:
        # The verified duplicate stays discoverable for cleanup after rebinding.
        await track_library_duplicate(db, asset.user_id, asset.gcs_path)
        asset.gcs_path, asset.gcs_generation = source.object_path, source.object_generation
    asset.library_asset_id, asset.library_source_id = library.id, source.id
    await reference_library_source(
        db,
        owner_id=asset.user_id,
        asset_id=library.id,
        source_id=source.id,
        plan_item_id=asset.plan_item_id,
    )
