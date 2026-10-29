"""Metadata-only selections over the existing creator-owned media catalogue."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    MediaAsset,
    MediaAssetSource,
    MediaCollection,
    MediaCollectionMember,
    MediaCollectionStory,
)
from app.schemas.media_collections import (
    CollectionEvent,
    CollectionMember,
    CollectionOut,
    CollectionWrite,
)
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
    # Heal a stored original-role binding in memory; the next committing flow persists it.
    # The revision is untouched: the creator's selection did not change, only its source.
    selected = members(row)
    bound = await analysis_bound_members(db, owner, selected)
    if bound != selected:
        row.selection_json = {
            **(row.selection_json or {}),
            "members": [member.model_dump(mode="json") for member in bound],
        }
    return row


async def analysis_bound_members(
    db: AsyncSession, owner: uuid.UUID, selected: list[CollectionMember]
) -> list[CollectionMember]:
    """Bind a device-only video to the analysis proxy that holds its facts.

    Attaching the full original later (KRI-822) leaves the analysis, moments and phone
    execution on the proxy, so a member bound to that original would never be indexed or
    offered. Same rule as `media_library_analysis._requested_source`.
    """
    if not selected:
        return selected
    bound_ids = [member.source_id for member in selected]
    originals = set(
        (
            await db.scalars(
                select(MediaAssetSource.asset_id)
                .join(MediaAsset, MediaAsset.id == MediaAssetSource.asset_id)
                .where(
                    MediaAssetSource.user_id == owner,
                    MediaAssetSource.id.in_(bound_ids),
                    MediaAssetSource.role == "original",
                    MediaAsset.user_id == owner,
                    MediaAsset.kind == "video",
                    MediaAsset.original_availability == "device_only",
                )
            )
        ).all()
    )
    if not originals:
        return selected
    proxies: dict[uuid.UUID, uuid.UUID] = {}
    for proxy in await db.scalars(
        select(MediaAssetSource)
        .where(
            MediaAssetSource.user_id == owner,
            MediaAssetSource.asset_id.in_(originals),
            MediaAssetSource.role == "analysis_proxy",
            MediaAssetSource.status == "available",
        )
        .order_by(MediaAssetSource.created_at.desc(), MediaAssetSource.id.desc())
    ):
        proxies.setdefault(proxy.asset_id, proxy.id)
    return [
        member.model_copy(update={"source_id": proxies[member.asset_id]})
        if member.asset_id in proxies
        else member
        for member in selected
    ]


def members(row: MediaCollection) -> list[CollectionMember]:
    return [
        CollectionMember.model_validate(value)
        for value in (row.selection_json or {}).get("members", [])
    ]


def events(row: MediaCollection) -> list[CollectionEvent]:
    return [
        CollectionEvent.model_validate(value)
        for value in (row.selection_json or {}).get("events", [])
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


def footage_in_event(story: MediaCollectionStory, selected: list[CollectionMember]) -> bool:
    """Whether every clip the idea cites is still a member of the idea's event."""
    current = {str(member.source_id): member.group for member in selected}
    group = story.story_json.get("group")
    return all(
        value["source_id"] in current and (group is None or current[value["source_id"]] == group)
        for value in story.moments_json
    )


async def carry_forward_stories(
    db: AsyncSession,
    owner: uuid.UUID,
    identifier: uuid.UUID,
    *,
    from_revision: int,
    to_revision: int,
    selected: list[CollectionMember],
) -> None:
    """Move the ideas the write left standing to the new revision.

    Ideas are pinned to the collection revision they were found against, so any write would
    orphan them all. An idea whose own clips are all still in its event keeps its evidence,
    whatever else the event gained or lost. Only rows that were current can move: an older
    revision's idea was already left behind by an earlier write.
    """
    rows = await db.scalars(
        select(MediaCollectionStory)
        .where(
            MediaCollectionStory.user_id == owner,
            MediaCollectionStory.collection_id == identifier,
            MediaCollectionStory.collection_revision == from_revision,
        )
        .with_for_update()
    )
    for row in rows:
        if footage_in_event(row, selected):
            row.collection_revision = to_revision


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
        events=events(row),
    )


def capture_time(asset: MediaAsset) -> datetime | None:
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
        if (timestamp := capture_time(sources[member.asset_id][0])) is not None
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
    requested = await analysis_bound_members(db, owner, body.members)
    sources = await eligible_sources(db, owner, requested)
    if len(sources) != len(requested):
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
    grouped = _group(requested, sources, members(row) if row else [])
    stored_events = events(row) if row else []
    new_events = stored_events if body.events is None else body.events
    # Opening the native collection saves its current selection again. Compare the
    # effective content, not the request digest (which contains expected_revision).
    # A stale identical snapshot is safe to acknowledge; it cannot overwrite data.
    if (
        row is not None
        and body.expected_revision <= row.revision
        and row.name == body.name.strip()
        and members(row) == grouped
        and stored_events == new_events
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
    previous_revision = row.revision
    row.name = body.name.strip()
    row.revision += 1
    row.selection_json = {
        "request_digest": digest,
        "members": [member.model_dump(mode="json") for member in grouped],
        "events": [value.model_dump(mode="json") for value in new_events],
    }
    await db.flush()
    await carry_forward_stories(
        db,
        owner,
        identifier,
        from_revision=previous_revision,
        to_revision=row.revision,
        selected=grouped,
    )
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
