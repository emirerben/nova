"""Background entrypoint for reusable media-library source analysis."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.config import settings
from app.database import sync_session
from app.models import MediaAsset, MediaAssetUploadReservation
from app.services.job_storage_paths import project_media_reference_lock_key
from app.services.media_library_analysis import (
    ensure_media_asset_analysis,
    media_asset_analysis_status,
)
from app.worker import celery_app


@celery_app.task(
    bind=True, name="tasks.analyze_media_asset", max_retries=0, soft_time_limit=600, time_limit=660
)
def analyze_media_asset_task(self: Any, asset_id: str, owner_id: str) -> dict[str, Any]:
    del self
    if not settings.media_library_enabled:
        return {"asset_id": str(asset_id), "status": "disabled"}
    try:
        identifier, owner = uuid.UUID(asset_id), uuid.UUID(owner_id)
    except (ValueError, TypeError):
        return {"asset_id": str(asset_id), "status": "invalid"}
    with sync_session() as db:
        db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner))))
        asset = db.scalar(
            select(MediaAsset).where(MediaAsset.id == identifier, MediaAsset.user_id == owner)
        )
        if asset is None:
            return {"asset_id": asset_id, "status": "missing"}
        asset.status = "processing"
        transcribe_audio = asset.kind == "video" and asset.metadata_json.get("has_audio") is True
        db.commit()
    result = ensure_media_asset_analysis(asset_id=str(asset_id), owner_id=str(owner_id))
    if result and transcribe_audio:
        # Transcription has its own lease/version and failure state; semantic facts survive.
        ensure_media_asset_analysis(asset_id=asset_id, owner_id=owner_id, analyzer="transcript")
    with sync_session() as db:
        db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner))))
        # Serialize projection updates and reread durable state after another worker finishes.
        db.scalar(select(MediaAsset).where(MediaAsset.id == identifier).with_for_update())
        status = media_asset_analysis_status(asset_id=asset_id, owner_id=owner_id)
        db.execute(
            update(MediaAsset)
            .where(MediaAsset.id == identifier, MediaAsset.user_id == owner)
            .values(status=status)
        )
        db.commit()
    return {"asset_id": str(asset_id), "status": status}


# KRI-820: a stale pending/processing library asset is re-sent at most this many
# times. An input that keeps stalling (hard kill, poison file) then ends as a
# visible `failed` instead of being re-dispatched every sweep forever.
MEDIA_ANALYSIS_MAX_REDISPATCHES = 3
_REDISPATCH_COUNT_KEY = "_analysis_redispatches"


@celery_app.task(name="tasks.reconcile_media_library_analysis")
def reconcile_media_library_analysis() -> int:
    """Bounded durable-publication recovery after a broker failure or worker crash."""
    if not settings.media_library_enabled:
        return 0
    sent = 0
    with sync_session() as db:
        rows = db.scalars(
            select(MediaAssetUploadReservation)
            .where(
                MediaAssetUploadReservation.analysis_dispatch_pending.is_(True),
                MediaAssetUploadReservation.status == "finalized",
            )
            .order_by(MediaAssetUploadReservation.created_at)
            .limit(50)
        ).all()
        stale = db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.status.in_(["pending", "processing"]),
                MediaAsset.updated_at < datetime.now(UTC) - timedelta(minutes=15),
            )
            .order_by(MediaAsset.updated_at)
            .limit(50)
        ).all()
        for owner in sorted({row.user_id for row in [*rows, *stale]}, key=str):
            db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner))))
        for row in rows:
            analyze_media_asset_task.delay(str(row.asset_id), str(row.user_id))
            row.analysis_dispatch_pending = False
            sent += 1
        stale_cutoff = datetime.now(UTC) - timedelta(minutes=15)
        for asset in stale:
            # Re-read under the owner lock: a worker that finished in between
            # must never be degraded to failed or re-sent (KRI-820).
            db.refresh(asset, with_for_update=True)
            if asset.status not in {"pending", "processing"} or asset.updated_at >= stale_cutoff:
                continue
            metadata = dict(asset.metadata_json or {})
            redispatches = int(metadata.get(_REDISPATCH_COUNT_KEY) or 0)
            if redispatches >= MEDIA_ANALYSIS_MAX_REDISPATCHES:
                asset.status = "failed"
                asset.updated_at = datetime.now(UTC)
                continue
            metadata[_REDISPATCH_COUNT_KEY] = redispatches + 1
            asset.metadata_json = metadata
            analyze_media_asset_task.delay(str(asset.id), str(asset.user_id))
            asset.updated_at = datetime.now(UTC)
            sent += 1
        db.commit()
    return sent
