"""Per-item progress for a collection, derived in ONE place.

The client renders this and nothing else: it never infers "still working" from which index
bucket an asset landed in. The state comes from `MediaAsset.status`, the analysis rows
(semantic facts and transcript), the recorded failure, and the last index pass.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, MediaAssetAnalysis
from app.schemas.media_collections import CollectionItemOut, CollectionItemsOut
from app.services import media_library_analysis as facts
from app.services.media_collections import eligible_sources, members
from app.services.media_moments import INDEX_OUTCOME_KEY

# Never improves on retry: the item is shown as excluded, not as a failure.
EXCLUDED_REASONS = frozenset({"unsupported_type", "too_large", "too_long", "access_revoked"})
# Attempts after which a transient failure stops being retried automatically.
MAX_ATTEMPTS = 3
_ACTIVE = frozenset({"queued", "analyzing"})


@dataclass(frozen=True, slots=True)
class ItemSignals:
    """Everything the state depends on, already read. No I/O below this line."""

    kind: str
    eligible: bool
    source_role: str | None
    asset_status: str
    metadata: dict
    byte_count: int | None
    fact_status: str | None  # semantic analysis row: queued|analyzing|ready|failed|None
    transcript_status: str | None


@dataclass(frozen=True, slots=True)
class DerivedState:
    state: str
    reason: str | None = None
    retryable: bool = False
    attempts: int = 0


def derive_item_state(signals: ItemSignals) -> DerivedState:
    """The single source of truth for a collection item's state.

    needs_retry: failed for a transient reason with automatic attempts left (retryable).
    failed: gave up (attempts exhausted, or nothing usable); retrying is still allowed
        through the retry route but the server will not do it on its own.
    excluded: can never be analysed or used (type, size, length, access).
    """
    failure = signals.metadata.get(facts.FAILURE_KEY)
    failure = failure if isinstance(failure, dict) else {}
    attempts = int(failure.get("attempts") or 0)

    if not signals.eligible:
        return DerivedState("excluded", "access_revoked")
    if signals.kind not in {"video", "image"}:
        return DerivedState("excluded", "unsupported_type")
    if signals.kind == "video" and signals.source_role != "analysis_proxy":
        # Only the proxy runs on the phone; an original-only video cannot become a story.
        return DerivedState("excluded", "unsupported_type")
    excluded = facts.analysis_exclusion_reason(signals.kind, signals.metadata, signals.byte_count)
    if excluded is not None:
        return DerivedState("excluded", excluded)

    if signals.asset_status == "failed":
        reason = failure.get("reason") or "provider_error"
        if reason in EXCLUDED_REASONS:
            return DerivedState("excluded", reason, attempts=attempts)
        if reason in facts.TRANSIENT_FAILURE_REASONS and attempts < MAX_ATTEMPTS:
            return DerivedState("needs_retry", reason, True, attempts)
        return DerivedState("failed", reason, False, attempts)

    if signals.fact_status in _ACTIVE or signals.asset_status == "processing":
        return DerivedState("analyzing", attempts=attempts)
    if signals.fact_status == "failed":
        # The task has not recorded the outcome on the asset yet: still the same attempt.
        return DerivedState("analyzing", attempts=attempts)
    if signals.asset_status == "pending" and signals.fact_status != "ready":
        # `uploading` is a device-side state: a member's bytes are on the server by definition.
        return DerivedState("queued", attempts=attempts)

    if signals.transcript_status in _ACTIVE:
        return DerivedState("analyzing", attempts=attempts)
    outcome = signals.metadata.get(INDEX_OUTCOME_KEY)
    if signals.kind == "video" and isinstance(outcome, dict) and outcome.get("outcome") == "none":
        return DerivedState("failed", "no_usable_moment")
    return DerivedState("ready")


async def collection_items(db: AsyncSession, owner: uuid.UUID, collection) -> CollectionItemsOut:
    selected = members(collection)
    if not selected:
        return CollectionItemsOut(revision=collection.revision, items=[])
    ids = [member.asset_id for member in selected]
    assets = {
        asset.id: asset
        for asset in (
            await db.scalars(
                select(MediaAsset).where(MediaAsset.user_id == owner, MediaAsset.id.in_(ids))
            )
        ).all()
    }
    available = await eligible_sources(db, owner, selected)

    keys: dict[tuple[uuid.UUID, str | None], facts.MediaAssetFactKey] = {}
    for asset_id, (asset, source) in available.items():
        ref = facts._Source(
            str(asset.id),
            str(source.id),
            str(owner),
            asset.kind,
            source.role,
            source.object_path,
            source.object_generation,
            source.uploaded_sha256 or "",
            source.status,
        )
        keys[(asset_id, None)] = facts._key(ref, None)
        if asset.kind == "video":
            keys[(asset_id, "transcript")] = facts._key(ref, "transcript")
    statuses: dict[tuple, str] = {}
    if keys:
        rows = await db.execute(
            select(
                MediaAssetAnalysis.source_identity,
                MediaAssetAnalysis.analyzer,
                MediaAssetAnalysis.model,
                MediaAssetAnalysis.prompt_version,
                MediaAssetAnalysis.schema_version,
                MediaAssetAnalysis.status,
            ).where(
                MediaAssetAnalysis.user_id == owner,
                MediaAssetAnalysis.source_identity.in_(
                    {key.source_identity for key in keys.values()}
                ),
            )
        )
        statuses = {tuple(row[:5]): row[5] for row in rows}

    def fact_status(asset_id: uuid.UUID, analyzer: str | None) -> str | None:
        key = keys.get((asset_id, analyzer))
        if key is None:
            return None
        return statuses.get(
            (key.source_identity, key.analyzer, key.model, key.prompt_version, key.schema_version)
        )

    items: list[CollectionItemOut] = []
    for member in selected:
        asset = assets.get(member.asset_id)
        if asset is None:
            continue  # Deleted outright: there is no tile left to describe.
        pair = available.get(member.asset_id)
        derived = derive_item_state(
            ItemSignals(
                kind=asset.kind,
                eligible=pair is not None,
                source_role=pair[1].role if pair else None,
                asset_status=asset.status,
                metadata=asset.metadata_json or {},
                byte_count=asset.original_byte_count,
                fact_status=fact_status(asset.id, None),
                transcript_status=fact_status(asset.id, "transcript"),
            )
        )
        items.append(
            CollectionItemOut(
                asset_id=asset.id,
                kind=asset.kind,
                group=member.group,
                state=derived.state,
                reason=derived.reason,
                retryable=derived.retryable,
                attempts=derived.attempts,
                updated_at=asset.updated_at or datetime.min,
            )
        )
    return CollectionItemsOut(revision=collection.revision, items=items)
