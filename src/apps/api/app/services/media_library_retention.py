"""Retention helpers for immutable media-library source generations.

Library objects participate in the existing storage-retention manifest.  This
module deliberately has no task or storage-provider calls: it supplies the
database facts needed while building and executing that manifest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.config import settings
from app.models import MediaAssetReference, MediaAssetSource, StorageRetentionEntry


@dataclass(frozen=True)
class LibrarySourceCandidate:
    source_id: uuid.UUID
    creator_id: uuid.UUID
    object_path: str
    object_generation: str
    byte_count: int
    eligible_at: datetime
    action: str
    reason: str


def is_durable_library_path(path: object, owner_id: uuid.UUID) -> bool:
    """Accept only owner-scoped durable namespaces, never transient uploads."""
    if not isinstance(path, str) or not path or ".." in path.split("/"):
        return False
    owner = str(owner_id)
    return path.startswith(f"users/{owner}/") or path.startswith(f"{owner}/")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def source_last_activity(source: MediaAssetSource) -> datetime:
    """The source's own use clock governs expiry, independent of its asset."""
    return _as_utc(source.last_used_at or source.created_at)


def source_is_protected(db: Any, source: MediaAssetSource) -> bool:
    """A reference to this source or an asset-wide reference pins its bytes."""
    return (
        db.scalar(
            select(MediaAssetReference.id)
            .where(
                MediaAssetReference.user_id == source.user_id,
                MediaAssetReference.asset_id == source.asset_id,
                or_(
                    MediaAssetReference.source_id == source.id,
                    MediaAssetReference.source_id.is_(None),
                ),
            )
            .limit(1)
        )
        is not None
    )


def path_is_adopted_by_library(db: Any, path: str) -> bool:
    """Job/temporary cleanup must leave durable library ownership to retention."""
    return (
        db.scalar(
            select(MediaAssetSource.id)
            .where(
                MediaAssetSource.object_path == path,
                MediaAssetSource.status.in_(("available", "missing", "deleting")),
            )
            .limit(1)
        )
        is not None
    )


def source_is_delete_eligible(
    db: Any,
    source: MediaAssetSource,
    *,
    now: datetime,
) -> bool:
    """Recheck both use and live references under the shared owner lock."""
    if source.status != "available" or not is_durable_library_path(
        source.object_path, source.user_id
    ):
        return False
    if source_is_protected(db, source):
        return False
    return now >= source_last_activity(source) + timedelta(
        days=settings.storage_retention_inactive_days
    )


def prior_warning_at(db: Any, source: MediaAssetSource) -> datetime | None:
    """Return a pending warning from this source's current inactivity window."""
    warning_threshold = source_last_activity(source) + timedelta(
        days=settings.storage_retention_warning_days
    )
    return db.scalar(
        select(StorageRetentionEntry.created_at)
        .where(
            StorageRetentionEntry.creator_id == source.user_id,
            StorageRetentionEntry.object_path == source.object_path,
            StorageRetentionEntry.object_generation == source.object_generation,
            StorageRetentionEntry.action == "warn",
            StorageRetentionEntry.status == "pending",
            StorageRetentionEntry.created_at >= warning_threshold,
        )
        .order_by(StorageRetentionEntry.created_at.asc())
        .limit(1)
    )


def source_candidates(
    db: Any,
    *,
    now: datetime,
    limit: int,
    cursor: tuple[datetime, uuid.UUID] | None = None,
) -> tuple[list[LibrarySourceCandidate], tuple[datetime, uuid.UUID] | None, bool]:
    """Return a fair bounded page of catalogue-only source candidates.

    The cursor is source last-use time plus source ID.  A source referenced by
    an editable consumer is intentionally skipped; library membership itself
    is never treated as a pin.
    """
    bounded = max(0, int(limit))
    if bounded == 0:
        return [], cursor, False
    activity = func.coalesce(MediaAssetSource.last_used_at, MediaAssetSource.created_at)
    query = select(MediaAssetSource).where(MediaAssetSource.status == "available")
    if cursor is not None:
        query = query.where(
            or_(
                activity > cursor[0],
                and_(activity == cursor[0], MediaAssetSource.id > cursor[1]),
            )
        )
    rows = db.scalars(
        query.order_by(activity.asc(), MediaAssetSource.id.asc()).limit(bounded)
    ).all()
    wrapped = False
    if cursor is not None and not rows:
        wrapped = True
        rows = db.scalars(
            select(MediaAssetSource)
            .where(MediaAssetSource.status == "available")
            .order_by(activity.asc(), MediaAssetSource.id.asc())
            .limit(bounded)
        ).all()

    result: list[LibrarySourceCandidate] = []
    for source in rows:
        if not is_durable_library_path(source.object_path, source.user_id):
            continue
        last_used = source_last_activity(source)
        delete_at = last_used + timedelta(days=settings.storage_retention_inactive_days)
        warning_delete_at = max(delete_at, now + timedelta(days=7))
        warning_at = delete_at - timedelta(
            days=settings.storage_retention_inactive_days - settings.storage_retention_warning_days
        )
        if source_is_protected(db, source):
            continue
        warned_at = prior_warning_at(db, source)
        if now >= delete_at:
            if warned_at is None:
                result.append(
                    LibrarySourceCandidate(
                        source.id,
                        source.user_id,
                        source.object_path,
                        source.object_generation,
                        source.byte_count,
                        warning_delete_at,
                        "warn",
                        "inactive_library_source_warning",
                    )
                )
            elif now >= max(delete_at, _as_utc(warned_at) + timedelta(days=7)):
                result.append(
                    LibrarySourceCandidate(
                        source.id,
                        source.user_id,
                        source.object_path,
                        source.object_generation,
                        source.byte_count,
                        max(delete_at, _as_utc(warned_at) + timedelta(days=7)),
                        "delete",
                        "inactive_library_source",
                    )
                )
        elif now >= warning_at and warned_at is None:
            result.append(
                LibrarySourceCandidate(
                    source.id,
                    source.user_id,
                    source.object_path,
                    source.object_generation,
                    source.byte_count,
                    warning_delete_at,
                    "warn",
                    "inactive_library_source_warning",
                )
            )
    next_cursor = (source_last_activity(rows[-1]), rows[-1].id) if rows else cursor
    return result, next_cursor, wrapped


def mark_source_deleting(db: Any, entry: StorageRetentionEntry, *, now: datetime) -> bool:
    """Fence fresh library use before irreversible remote deletion."""
    source = db.scalar(
        select(MediaAssetSource)
        .where(
            MediaAssetSource.user_id == entry.creator_id,
            MediaAssetSource.object_path == entry.object_path,
            MediaAssetSource.object_generation == entry.object_generation,
        )
        .with_for_update()
    )
    if source is None:
        return False
    if not source_is_delete_eligible(db, source, now=now):
        return False
    source.status = "deleting"
    return True


def finish_source_deletion(
    db: Any,
    entry: StorageRetentionEntry,
    *,
    deleted: bool,
    missing: bool = False,
) -> None:
    """Retain source/analysis rows while recording the cloud-byte outcome."""
    source = db.scalar(
        select(MediaAssetSource)
        .where(
            MediaAssetSource.user_id == entry.creator_id,
            MediaAssetSource.object_path == entry.object_path,
            MediaAssetSource.object_generation == entry.object_generation,
        )
        .with_for_update()
    )
    if source is not None and source.status == "deleting":
        source.status = "deleted" if deleted else "missing" if missing else "available"
