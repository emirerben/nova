"""Background entrypoint for reusable media-library source analysis."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, and_, cast, func, or_, select, update

from app.config import settings
from app.database import sync_session
from app.models import MediaAsset, MediaAssetUploadReservation
from app.services.job_storage_paths import project_media_reference_lock_key
from app.services.media_library_analysis import (
    FAILURE_KEY,
    TRANSIENT_FAILURE_REASONS,
    analysis_exclusion_reason,
    clear_analysis_failure,
    ensure_media_asset_analysis,
    media_asset_analysis_status,
    record_analysis_failure,
)
from app.worker import celery_app

# A transient failure (provider outage, spend pause) is attempted this many times in total,
# whether the retry comes from the task itself or from the reconciler. Then it is terminal.
MEDIA_ANALYSIS_MAX_ATTEMPTS = 3
# Reconciler wait before re-dispatching a failed row: base minutes, doubled per failed attempt.
# Always longer than the task's own countdown, so the two paths rarely overlap.
_RECONCILE_BASE_MINUTES = {"provider_error": 10, "budget_paused": 60}
# A pre-existing failure with no recorded reason is retried once, but only while it is recent.
_LEGACY_FAILURE_WINDOW = timedelta(days=7)


def _task_retry_countdown(reason: str, attempts: int) -> int:
    if reason == "budget_paused":
        return 600 * attempts
    return 60 * 3 ** (attempts - 1)


def _failure(asset: MediaAsset) -> dict[str, Any] | None:
    value = (asset.metadata_json or {}).get(FAILURE_KEY)
    return value if isinstance(value, dict) else None


def _may_retry(asset: MediaAsset) -> bool:
    """A failed asset is revived only while its recorded failure is transient and under budget."""
    failure = _failure(asset)
    return (
        failure is not None
        and failure.get("reason") in TRANSIENT_FAILURE_REASONS
        and int(failure.get("attempts") or 0) < MEDIA_ANALYSIS_MAX_ATTEMPTS
    )


@celery_app.task(
    bind=True,
    name="tasks.analyze_media_asset",
    max_retries=MEDIA_ANALYSIS_MAX_ATTEMPTS,
    soft_time_limit=600,
    time_limit=660,
)
def analyze_media_asset_task(self: Any, asset_id: str, owner_id: str) -> dict[str, Any]:
    if not settings.media_library_enabled:
        return {"asset_id": str(asset_id), "status": "disabled"}
    try:
        identifier, owner = uuid.UUID(asset_id), uuid.UUID(owner_id)
    except (ValueError, TypeError):
        return {"asset_id": str(asset_id), "status": "invalid"}
    started = datetime.now(UTC)
    with sync_session() as db:
        db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner))))
        asset = db.scalar(
            select(MediaAsset).where(MediaAsset.id == identifier, MediaAsset.user_id == owner)
        )
        if asset is None:
            return {"asset_id": asset_id, "status": "missing"}
        # KRI-820: a late copy never revives a capped failure. A transient one under its
        # attempt budget is exactly what a retry is for.
        if asset.status == "failed" and not _may_retry(asset):
            return {"asset_id": asset_id, "status": "failed"}
        rejected = analysis_exclusion_reason(
            asset.kind, asset.metadata_json, asset.original_byte_count
        )
        if rejected is not None:
            asset.status = "failed"
            db.commit()
            record_analysis_failure(
                asset_id=asset_id,
                owner_id=owner_id,
                reason=rejected,
                attempts=MEDIA_ANALYSIS_MAX_ATTEMPTS,
            )
            return {"asset_id": asset_id, "status": "failed"}
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
    if status == "ready":
        clear_analysis_failure(asset_id=asset_id, owner_id=owner_id)
    elif status == "failed":
        # Every failed run is counted, even one that bailed before reaching the provider.
        record_analysis_failure(
            asset_id=asset_id, owner_id=owner_id, reason="provider_error", not_before=started
        )
        with sync_session() as db:
            asset = db.get(MediaAsset, identifier)
            failure = _failure(asset) if asset is not None and _may_retry(asset) else None
        if failure:
            raise self.retry(
                countdown=_task_retry_countdown(failure["reason"], int(failure["attempts"]))
            )
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
        retryable = _retryable_failed_assets(db)
        for owner in sorted({row.user_id for row in [*rows, *stale, *retryable]}, key=str):
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
                if _failure(asset) is None:
                    metadata[FAILURE_KEY] = {
                        "reason": "provider_error",
                        "attempts": MEDIA_ANALYSIS_MAX_ATTEMPTS,
                        "at": datetime.now(UTC).isoformat(),
                    }
                    asset.metadata_json = metadata
                continue
            metadata[_REDISPATCH_COUNT_KEY] = redispatches + 1
            asset.metadata_json = metadata
            analyze_media_asset_task.delay(str(asset.id), str(asset.user_id))
            asset.updated_at = datetime.now(UTC)
            sent += 1
        for asset in retryable:
            db.refresh(asset, with_for_update=True)
            if asset.status != "failed" or not (_may_retry(asset) or _failure(asset) is None):
                continue
            metadata = dict(asset.metadata_json or {})
            if _failure(asset) is None:
                # Legacy failure: spend one recorded attempt so it cannot loop unbounded.
                metadata[FAILURE_KEY] = {
                    "reason": "provider_error",
                    "attempts": 1,
                    "at": datetime.now(UTC).isoformat(),
                }
            asset.metadata_json = metadata
            asset.status = "pending"
            asset.updated_at = datetime.now(UTC)
            analyze_media_asset_task.delay(str(asset.id), str(asset.user_id))
            sent += 1
        db.commit()
    return sent


def _retryable_failed_assets(db: Any) -> list[MediaAsset]:
    """Failed rows whose recorded reason is transient, under budget and past their backoff."""
    failure = MediaAsset.metadata_json[FAILURE_KEY]
    attempts = cast(failure["attempts"].astext, Integer)
    reason = failure["reason"].astext
    recorded = MediaAsset.metadata_json.has_key(FAILURE_KEY)  # noqa: W601
    waits = [
        and_(
            reason == name,
            MediaAsset.updated_at
            < func.now()
            - func.make_interval(
                0, 0, 0, 0, 0, cast(minutes * func.power(2, attempts - 1), Integer)
            ),
        )
        for name, minutes in _RECONCILE_BASE_MINUTES.items()
    ]
    legacy = and_(
        ~recorded,
        MediaAsset.updated_at > datetime.now(UTC) - _LEGACY_FAILURE_WINDOW,
        MediaAsset.updated_at
        < datetime.now(UTC) - timedelta(minutes=_RECONCILE_BASE_MINUTES["provider_error"]),
        func.coalesce(cast(MediaAsset.metadata_json[_REDISPATCH_COUNT_KEY].astext, Integer), 0)
        < MEDIA_ANALYSIS_MAX_REDISPATCHES,
    )
    return list(
        db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.status == "failed",
                MediaAsset.archived_at.is_(None),
                or_(and_(recorded, attempts < MEDIA_ANALYSIS_MAX_ATTEMPTS, or_(*waits)), legacy),
            )
            .order_by(MediaAsset.updated_at)
            .limit(50)
        ).all()
    )
