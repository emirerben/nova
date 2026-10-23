"""Metadata-only selections over the existing creator-owned media catalogue."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, MediaAssetSource, MediaCollection, MediaCollectionMember
from app.schemas.media_collections import CollectionMember, CollectionOut, CollectionWrite
from app.services.media_library_ingest import lock_library_owner


async def owned_collection(
    db: AsyncSession, owner: uuid.UUID, identifier: uuid.UUID, *, revision: int | None = None
) -> MediaCollection:
    row = await db.get(MediaCollection, identifier, populate_existing=True)
    if row is None or row.user_id != owner:
        raise HTTPException(404, "Collection not found")
    if revision is not None and row.revision != revision:
        raise HTTPException(
            409,
            {
                "code": "collection_changed",
                "message": "This collection changed. Refresh before continuing.",
            },
        )
    return row


def members(row: MediaCollection) -> list[CollectionMember]:
    return [
        CollectionMember.model_validate(value)
        for value in (row.selection_json or {}).get("members", [])
    ]


async def eligible_sources(
    db: AsyncSession, owner: uuid.UUID, selected: list[CollectionMember]
) -> dict[uuid.UUID, tuple[MediaAsset, MediaAssetSource]]:
    if not selected:
        return {}
    wanted = {member.asset_id: member.source_id for member in selected}
    rows = (
        await db.execute(
            select(MediaAsset, MediaAssetSource)
            .join(MediaAssetSource, MediaAssetSource.asset_id == MediaAsset.id)
            .where(
                MediaAsset.user_id == owner,
                MediaAssetSource.user_id == owner,
                MediaAsset.id.in_(wanted),
                MediaAssetSource.id.in_(wanted.values()),
                MediaAsset.archived_at.is_(None),
                MediaAssetSource.status == "available",
                MediaAssetSource.role.in_(["original", "analysis_proxy"]),
            )
        )
    ).all()
    return {
        asset.id: (asset, source) for asset, source in rows if wanted.get(asset.id) == source.id
    }


def collection_out(row: MediaCollection, eligible: dict) -> CollectionOut:
    selected = members(row)
    return CollectionOut(
        id=row.id,
        name=row.name,
        revision=row.revision,
        members=selected,
        unavailable_asset_ids=[
            member.asset_id for member in selected if member.asset_id not in eligible
        ],
    )


def _capture_time(asset: MediaAsset) -> datetime | None:
    # These are existing, creator-authorized capture facts, never AI guesses.
    context = asset.creator_context or {}
    value = context.get("capture_time")
    if value is None:
        # Native proxy receipts already carry optional device capture context.
        metadata = asset.metadata_json or {}
        proxy = (metadata.get("upload_contract") or {}).get("proxy") or {}
        value = ((proxy.get("original") or {}).get("capture") or {}).get("capture_time")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _group(
    selected: list[CollectionMember], sources: dict, previous: list[CollectionMember]
) -> list[CollectionMember]:
    previous_groups = {(member.asset_id, member.source_id): member.group for member in previous}
    dated = sorted(
        (timestamp, str(member.asset_id), member.asset_id)
        for member in selected
        if (timestamp := _capture_time(sources[member.asset_id][0])) is not None
    )
    groups = {}
    last = None
    event = 0
    for timestamp, _, identifier in dated:
        if last is None or timestamp - last > timedelta(hours=4):
            event += 1
        groups[identifier] = f"Event {event}"
        last = timestamp
    return [
        member.model_copy(
            update={
                "group": member.group
                or previous_groups.get((member.asset_id, member.source_id))
                or groups.get(member.asset_id, "Selected collection")
            }
        )
        for member in selected
    ]


async def write_collection(
    db: AsyncSession, owner: uuid.UUID, identifier: uuid.UUID, body: CollectionWrite
) -> CollectionOut:
    await lock_library_owner(db, owner)
    row = await db.get(MediaCollection, identifier, populate_existing=True)
    if row is not None and row.user_id != owner:
        raise HTTPException(404, "Collection not found")
    digest = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    if row is not None and (row.selection_json or {}).get("request_digest") == digest:
        return collection_out(row, await eligible_sources(db, owner, members(row)))
    sources = await eligible_sources(db, owner, body.members)
    if len(sources) != len(body.members):
        owned_ids = set(
            (
                await db.scalars(
                    select(MediaAsset.id).where(
                        MediaAsset.user_id == owner,
                        MediaAsset.id.in_([m.asset_id for m in body.members]),
                    )
                )
            ).all()
        )
        if any(member.asset_id not in owned_ids for member in body.members):
            raise HTTPException(404, "Media asset not found")
        raise HTTPException(
            409,
            {
                "code": "collection_media_unavailable",
                "message": (
                    "Selected media is unavailable or its source changed. Remove or repair it."
                ),
            },
        )
    grouped = _group(body.members, sources, members(row) if row else [])
    # Opening the native collection saves its current selection again. Compare the
    # effective content, not the request digest (which contains expected_revision).
    # A stale identical snapshot is safe to acknowledge; it cannot overwrite data.
    if (
        row is not None
        and body.expected_revision <= row.revision
        and row.name == body.name.strip()
        and members(row) == grouped
    ):
        return collection_out(row, sources)
    if (row.revision if row is not None else 0) != body.expected_revision:
        raise HTTPException(
            409,
            {
                "code": "collection_changed",
                "message": "This collection changed. Refresh before continuing.",
            },
        )
    if row is None:
        row = MediaCollection(id=identifier, user_id=owner, name=body.name, revision=0)
        db.add(row)
    row.name = body.name.strip()
    row.revision += 1
    row.selection_json = {
        "request_digest": digest,
        "members": [member.model_dump(mode="json") for member in grouped],
    }
    await db.flush()
    await db.execute(
        delete(MediaCollectionMember).where(
            MediaCollectionMember.collection_id == identifier,
            MediaCollectionMember.user_id == owner,
        )
    )
    db.add_all(
        [
            MediaCollectionMember(collection_id=identifier, asset_id=member.asset_id, user_id=owner)
            for member in grouped
        ]
    )
    await db.commit()
    return collection_out(row, sources)
