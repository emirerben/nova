"""TikTok Direct Post submission, reconciliation, analytics sync, and cleanup."""

from __future__ import annotations

import hashlib
import json
import secrets
import statistics
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select

from app import storage
from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.tiktok_analysis import (
    TIKTOK_ANALYZER_PROMPT_VERSION,
    TikTokAnalyzerInput,
)
from app.agents.tiktok_analyzer import TikTokAnalyzerAgent
from app.config import settings
from app.database import sync_session
from app.models import OAuthToken, Persona, TikTokPublication
from app.services import tiktok_client
from app.services.tiktok_lifecycle import (
    visibility_after_draft_inbox,
    visibility_after_draft_post,
)
from app.services.tiktok_tokens import active_access_token_sync
from app.worker import celery_app

log = structlog.get_logger()

_POLL_INTERVAL = timedelta(seconds=30)
_EVALUATION_AGE = timedelta(hours=72)
_EVALUATION_MAX_AGE = timedelta(hours=84)
_SYNC_INTERVAL = timedelta(hours=12)
_ANALYSIS_MIN_VIDEOS = 5
_MAX_RETRIES = 3


@celery_app.task(
    name="app.tasks.tiktok.submit_tiktok_publication",
    bind=True,
    max_retries=0,
    soft_time_limit=110,
    time_limit=120,
)
def submit_tiktok_publication(self, publication_id: str) -> None:  # noqa: ANN001
    publication_uuid = uuid.UUID(publication_id)
    with sync_session() as session:
        row = session.execute(
            select(TikTokPublication)
            .where(TikTokPublication.id == publication_uuid)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or row.processing_status not in {"queued", "failed"}:
            return
        if row.processing_status == "failed" and not _can_submit_failed(
            row.retryable, row.retry_count
        ):
            return
        if (row.delivery_mode or "direct_post") == "draft_upload" and not (
            settings.tiktok_draft_upload_enabled
        ):
            row.processing_status = "failed"
            row.failure_code = "draft_upload_disabled"
            row.failure_detail = "TikTok draft upload is temporarily unavailable"
            row.retryable = False
            row.next_poll_at = None
            session.commit()
            return
        row.processing_status = "snapshotting"
        row.next_poll_at = None
        row.retryable = False
        row.failure_code = None
        row.failure_detail = None
        session.commit()
        source_object_path = row.source_object_path
        source_generation = row.source_generation

    snapshot_path = f"tiktok-publish/{publication_id}.mp4"
    try:
        storage.copy_object_generation(
            source_object_path,
            snapshot_path,
            source_generation=source_generation,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(
            publication_uuid, "snapshot_failed", "Could not prepare the exact video", retryable=True
        )
        log.warning("tiktok.snapshot_failed", publication_id=publication_id, error=str(exc)[:200])
        return

    media_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with sync_session() as session:
        row = session.execute(
            select(TikTokPublication)
            .where(TikTokPublication.id == publication_uuid)
            .with_for_update()
        ).scalar_one()
        if row.processing_status != "snapshotting":
            storage.delete_object_best_effort(snapshot_path)
            return
        row.snapshot_object_path = snapshot_path
        row.media_token_hash = hashlib.sha256(media_token.encode()).hexdigest()
        row.media_expires_at = now + timedelta(hours=2)
        row.processing_status = "submitting"
        session.commit()
        user_id = row.user_id
        delivery_mode = row.delivery_mode or "direct_post"
        post_info = _post_info(row) if delivery_mode == "direct_post" else None

    media_url = f"{settings.tiktok_media_base_url.rstrip('/')}/{publication_id}/{media_token}.mp4"
    try:
        with sync_session() as session:
            _, access_token = active_access_token_sync(session, user_id)
        if delivery_mode == "draft_upload":
            publish_id = tiktok_client.initialize_draft_upload(
                access_token,
                media_url=media_url,
            )
        else:
            publish_id = tiktok_client.initialize_direct_post(
                access_token,
                post_info=post_info or {},
                media_url=media_url,
            )
    except tiktok_client.TikTokAPIError as exc:
        if exc.ambiguous:
            _mark_unknown(publication_uuid)
        else:
            _fail(publication_uuid, exc.code, str(exc), retryable=exc.retryable)
        return
    except Exception as exc:  # noqa: BLE001
        _fail(publication_uuid, "submission_failed", str(exc)[:300], retryable=False)
        return

    with sync_session() as session:
        row = session.execute(
            select(TikTokPublication)
            .where(TikTokPublication.id == publication_uuid)
            .with_for_update()
        ).scalar_one()
        row.tiktok_publish_id = publish_id
        if row.processing_status != "submitting":
            session.commit()
            return
        row.processing_status = "processing"
        row.next_poll_at = datetime.now(UTC) + _POLL_INTERVAL
        session.commit()


@celery_app.task(
    name="app.tasks.tiktok.poll_tiktok_publications", soft_time_limit=50, time_limit=60
)
def poll_tiktok_publications() -> int:
    now = datetime.now(UTC)
    poll_ids: list[uuid.UUID] = []
    submit_ids: list[uuid.UUID] = []
    deauthorization_user_ids: list[uuid.UUID] = []
    with sync_session() as session:
        rows = (
            session.execute(
                select(TikTokPublication)
                .where(
                    or_(
                        TikTokPublication.processing_status == "processing",
                        (
                            (TikTokPublication.processing_status == "complete")
                            & (TikTokPublication.visibility_status == "unknown")
                        ),
                    ),
                    TikTokPublication.tiktok_publish_id.is_not(None),
                    TikTokPublication.next_poll_at <= now,
                    TikTokPublication.created_at >= now - timedelta(days=7),
                )
                .order_by(TikTokPublication.next_poll_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        poll_ids = [row.id for row in rows]
        for row in rows:
            # Claim the row before dispatch so overlapping beat processes do
            # not enqueue duplicate status requests.
            row.next_poll_at = now + timedelta(minutes=2)

        stale_before = now - timedelta(minutes=5)
        recovery_rows = (
            session.execute(
                select(TikTokPublication)
                .where(
                    or_(
                        (
                            (TikTokPublication.processing_status == "queued")
                            & or_(
                                TikTokPublication.next_poll_at <= now,
                                (
                                    TikTokPublication.next_poll_at.is_(None)
                                    & (TikTokPublication.updated_at <= stale_before)
                                ),
                            )
                        ),
                        (
                            (TikTokPublication.processing_status == "snapshotting")
                            & (TikTokPublication.updated_at <= stale_before)
                        ),
                        (
                            (TikTokPublication.processing_status == "submitting")
                            & (TikTokPublication.updated_at <= stale_before)
                        ),
                        (
                            (TikTokPublication.processing_status == "failed")
                            & TikTokPublication.retryable.is_(True)
                            & or_(
                                TikTokPublication.next_poll_at <= now,
                                (
                                    TikTokPublication.next_poll_at.is_(None)
                                    & (TikTokPublication.updated_at <= stale_before)
                                ),
                            )
                        ),
                    ),
                    TikTokPublication.created_at >= now - timedelta(days=7),
                )
                .order_by(TikTokPublication.updated_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for row in recovery_rows:
            if _recover_stale_publication(row, now):
                submit_ids.append(row.id)

        deauthorization_user_ids = (
            session.execute(
                select(OAuthToken.user_id)
                .join(TikTokPublication, TikTokPublication.user_id == OAuthToken.user_id)
                .where(
                    OAuthToken.platform == "tiktok",
                    OAuthToken.status == "revoked",
                    or_(
                        TikTokPublication.snapshot_object_path.is_not(None),
                        TikTokPublication.source_object_path != "redacted",
                        TikTokPublication.latest_metrics.is_not(None),
                        TikTokPublication.evaluation_metrics.is_not(None),
                        TikTokPublication.title != "",
                    ),
                )
                .distinct()
                .limit(50)
            )
            .scalars()
            .all()
        )
        session.commit()
    for publication_id in poll_ids:
        _safe_dispatch(poll_tiktok_publication, str(publication_id))
    for publication_id in submit_ids:
        _safe_dispatch(submit_tiktok_publication, str(publication_id))
    for user_id in deauthorization_user_ids:
        _safe_dispatch(cleanup_tiktok_deauthorization, str(user_id))
    return len(poll_ids) + len(submit_ids) + len(deauthorization_user_ids)


@celery_app.task(name="app.tasks.tiktok.poll_tiktok_publication", soft_time_limit=25, time_limit=30)
def poll_tiktok_publication(publication_id: str) -> None:
    _poll_one(uuid.UUID(publication_id))


def _poll_one(publication_id: uuid.UUID) -> None:
    with sync_session() as session:
        row = session.get(TikTokPublication, publication_id)
        if row is None or not row.tiktok_publish_id:
            return
        user_id = row.user_id
        publish_id = row.tiktok_publish_id
    try:
        with sync_session() as session:
            _, access_token = active_access_token_sync(session, user_id)
        payload = tiktok_client.fetch_publish_status(access_token, publish_id)
    except tiktok_client.TikTokAPIError as exc:
        with sync_session() as session:
            row = session.get(TikTokPublication, publication_id)
            if row:
                if row.visibility_status == "removed":
                    row.next_poll_at = None
                else:
                    row.next_poll_at = datetime.now(UTC) + timedelta(minutes=2)
                if not exc.retryable and row.processing_status != "complete":
                    row.failure_code = exc.code
                session.commit()
        return
    status_value = str(payload.get("status") or "")
    should_retry = False
    with sync_session() as session:
        row = session.execute(
            select(TikTokPublication)
            .where(TikTokPublication.id == publication_id)
            .with_for_update()
        ).scalar_one()
        if row.visibility_status == "removed":
            row.next_poll_at = None
            session.commit()
            return
        post_ids = (
            payload.get("publicaly_available_post_id")
            or payload.get("publicly_available_post_id")
            or []
        )
        if isinstance(post_ids, str):
            post_ids = [post_ids]
        if post_ids:
            row.tiktok_post_id = str(post_ids[0])
            row.visibility_status = "public"
            row.public_at = row.public_at or datetime.now(UTC)
        if (
            status_value == "SEND_TO_USER_INBOX"
            and (row.delivery_mode or "direct_post") == "draft_upload"
        ):
            visibility_status = visibility_after_draft_inbox(
                row.visibility_status,
                row.processing_status,
            )
            row.processing_status = "complete"
            row.visibility_status = visibility_status
            row.next_poll_at = None
        elif status_value == "PUBLISH_COMPLETE":
            row.processing_status = "complete"
            if (row.delivery_mode or "direct_post") == "draft_upload":
                row.visibility_status = visibility_after_draft_post(row.visibility_status)
                row.next_poll_at = None
            elif row.visibility_status == "public":
                row.next_poll_at = None
            elif row.privacy_level == "SELF_ONLY":
                row.visibility_status = "private"
                row.next_poll_at = None
            else:
                # Completion means TikTok finished ingestion, not that the post
                # is public. Continue a slower fallback poll while waiting for
                # moderation or a webhook visibility event.
                row.next_poll_at = datetime.now(UTC) + timedelta(minutes=2)
        elif status_value == "FAILED":
            if row.processing_status == "complete":
                row.next_poll_at = None
                session.commit()
                return
            reason = str(payload.get("fail_reason") or "publish_failed")
            row.processing_status = "failed"
            row.failure_code = reason[:100]
            row.failure_detail = (
                "TikTok could not receive this draft"
                if (row.delivery_mode or "direct_post") == "draft_upload"
                else "TikTok could not publish this video"
            )
            row.retryable = (
                reason in {"internal", "video_pull_failed"} and row.retry_count < _MAX_RETRIES
            )
            if row.retryable:
                row.retry_count += 1
                should_retry = True
            row.next_poll_at = datetime.now(UTC) + timedelta(minutes=1) if row.retryable else None
        else:
            row.next_poll_at = datetime.now(UTC) + _POLL_INTERVAL
        session.commit()
    if should_retry:
        _safe_dispatch(submit_tiktok_publication, str(publication_id), countdown=60)


@celery_app.task(
    name="app.tasks.tiktok.schedule_tiktok_account_syncs", soft_time_limit=50, time_limit=60
)
def schedule_tiktok_account_syncs() -> int:
    if not settings.tiktok_performance_sync_enabled:
        return 0
    now = datetime.now(UTC)
    due = now - _SYNC_INTERVAL
    with sync_session() as session:
        rows = (
            session.execute(
                select(OAuthToken)
                .where(
                    OAuthToken.platform == "tiktok",
                    OAuthToken.status == "active",
                    (OAuthToken.last_synced_at.is_(None) | (OAuthToken.last_synced_at <= due)),
                    (
                        OAuthToken.sync_lease_expires_at.is_(None)
                        | (OAuthToken.sync_lease_expires_at <= now)
                    ),
                )
                .limit(50)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        user_ids = [row.user_id for row in rows]
        for row in rows:
            row.sync_lease_expires_at = now + timedelta(minutes=15)
        session.commit()
    for user_id in user_ids:
        sync_tiktok_account.delay(str(user_id))
    return len(user_ids)


@celery_app.task(
    name="app.tasks.tiktok.sync_tiktok_account",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=210,
    time_limit=240,
)
def sync_tiktok_account(self, user_id: str) -> None:  # noqa: ANN001
    if not settings.tiktok_performance_sync_enabled:
        _clear_sync_lease(uuid.UUID(user_id))
        return
    user_uuid = uuid.UUID(user_id)
    try:
        with sync_session() as session:
            token_row, access_token = active_access_token_sync(session, user_uuid)
            if not {"user.info.basic", "video.list"}.issubset(set(token_row.scopes or [])):
                token_row.sync_lease_expires_at = None
                session.commit()
                return
        account = tiktok_client.user_info(access_token)
        videos = tiktok_client.list_videos(access_token, limit=30)
    except tiktok_client.TikTokAPIError as exc:
        if exc.retryable:
            _extend_sync_lease(user_uuid)
            raise self.retry(exc=exc) from exc
        log.warning("tiktok.sync_failed", user_id=user_id, code=exc.code)
        _clear_sync_lease(user_uuid)
        return
    except LookupError:
        _clear_sync_lease(user_uuid)
        return

    normalized = _normalize_videos(videos)
    now = datetime.now(UTC)
    with sync_session() as session:
        token_row = session.execute(
            select(OAuthToken)
            .where(OAuthToken.user_id == user_uuid, OAuthToken.platform == "tiktok")
            .with_for_update()
        ).scalar_one_or_none()
        if token_row is None or token_row.status != "active":
            return
        token_row.account_metadata = _account_metadata(account)
        token_row.last_synced_at = now
        token_row.sync_lease_expires_at = None
        publications = (
            session.execute(select(TikTokPublication).where(TikTokPublication.user_id == user_uuid))
            .scalars()
            .all()
        )
        by_post_id = {row.tiktok_post_id: row for row in publications if row.tiktok_post_id}
        for video in normalized:
            publication = by_post_id.get(video.get("video_id"))
            if publication is None:
                continue
            metrics = _metrics(video)
            publication.latest_metrics = metrics
            publication.metrics_synced_at = now
            if (
                publication.visibility_status == "public"
                and publication.public_at
                and _within_evaluation_window(publication.public_at, now)
                and publication.evaluation_metrics is None
            ):
                publication.evaluation_metrics = {
                    **metrics,
                    "window_hours": round((now - publication.public_at).total_seconds() / 3600, 1),
                }
                publication.evaluation_captured_at = now

        persona = session.execute(
            select(Persona).where(Persona.user_id == user_uuid).with_for_update()
        ).scalar_one_or_none()
        video_fingerprint = _analysis_fingerprint(normalized)
        previous_fingerprint = None
        mature_fingerprint_changed = False
        mature_publications: list[TikTokPublication] = []
        if persona:
            profile = dict(persona.tiktok_profile or {})
            previous_sync = dict(profile.get("official_sync") or {})
            previous_fingerprint = previous_sync.get("analysis_fingerprint")
            mature_publications = [
                row
                for row in publications
                if row.visibility_status == "public" and row.evaluation_metrics is not None
            ]
            mature_fingerprint = _mature_fingerprint(mature_publications)
            mature_fingerprint_changed = mature_fingerprint != previous_sync.get(
                "mature_fingerprint"
            )
            fingerprint = hashlib.sha256(
                f"{video_fingerprint}:{mature_fingerprint}".encode()
            ).hexdigest()
            profile["official_sync"] = {
                "account": _account_metadata(account),
                "videos": normalized,
                "median_views": _median(
                    [float(v["view_count"]) for v in normalized if v.get("view_count") is not None]
                ),
                "synced_at": now.isoformat(),
                "analysis_fingerprint": fingerprint,
                "mature_fingerprint": mature_fingerprint,
                "linked_public_mature_count": len(mature_publications),
                "edit_correlations": _edit_correlations(mature_publications),
                "last_style_derived_at": previous_sync.get("last_style_derived_at"),
            }
            persona.tiktok_profile = profile
        else:
            fingerprint = video_fingerprint
        session.commit()
        persona_id = str(persona.id) if persona else None

    if (
        persona_id
        and len(normalized) >= _ANALYSIS_MIN_VIDEOS
        and fingerprint != previous_fingerprint
    ):
        _run_official_analysis(
            uuid.UUID(persona_id),
            normalized,
            account,
            mature_publications,
            mature_fingerprint_changed=mature_fingerprint_changed,
        )


def _run_official_analysis(
    persona_id: uuid.UUID,
    videos: list[dict[str, Any]],
    account: dict[str, Any],
    mature_publications: list[TikTokPublication],
    *,
    mature_fingerprint_changed: bool,
) -> None:
    correlations = _edit_correlations(mature_publications)
    try:
        output = TikTokAnalyzerAgent(default_client()).run(
            TikTokAnalyzerInput(
                handle=str(account.get("display_name") or ""),
                follower_count=_int(account.get("follower_count")),
                median_views=_median(
                    [float(v["view_count"]) for v in videos if v.get("view_count") is not None]
                ),
                videos=videos,
                edit_correlations=correlations,
            ),
            ctx=RunContext(job_id=None),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tiktok.official_analysis_failed", persona_id=str(persona_id), error=str(exc)[:200]
        )
        return
    should_derive_style = False
    with sync_session() as session:
        persona = session.execute(
            select(Persona).where(Persona.id == persona_id).with_for_update()
        ).scalar_one_or_none()
        if persona is None:
            return
        profile = dict(persona.tiktok_profile or {})
        analysis = output.analysis.model_dump()
        analysis["summary_for_prompts"] = _summary_with_edit_correlations(analysis)
        analysis["provenance"] = {
            "source": "tiktok_display_api",
            "linked_post_count": len(mature_publications),
            "correlation_only": True,
        }
        profile["official_analysis"] = analysis
        official_sync = dict(profile.get("official_sync") or {})
        last_derived_raw = official_sync.get("last_style_derived_at")
        try:
            last_derived = datetime.fromisoformat(last_derived_raw) if last_derived_raw else None
        except (TypeError, ValueError):
            last_derived = None
        should_derive_style = (
            settings.user_style_enabled
            and len(mature_publications) >= 5
            and bool(correlations)
            and mature_fingerprint_changed
            and (persona.style or {}).get("status") != "edited"
            and (last_derived is None or datetime.now(UTC) - last_derived >= timedelta(days=7))
        )
        if should_derive_style:
            official_sync["last_style_derived_at"] = datetime.now(UTC).isoformat()
            profile["official_sync"] = official_sync
        persona.tiktok_profile = profile
        session.commit()
    if should_derive_style:
        from app.tasks.style_build import derive_user_style

        derive_user_style.delay(str(persona_id))


def _clear_sync_lease(user_id: uuid.UUID) -> None:
    with sync_session() as session:
        row = session.execute(
            select(OAuthToken)
            .where(OAuthToken.user_id == user_id, OAuthToken.platform == "tiktok")
            .with_for_update()
        ).scalar_one_or_none()
        if row:
            row.sync_lease_expires_at = None
            session.commit()


def _extend_sync_lease(user_id: uuid.UUID) -> None:
    with sync_session() as session:
        row = session.execute(
            select(OAuthToken)
            .where(OAuthToken.user_id == user_id, OAuthToken.platform == "tiktok")
            .with_for_update()
        ).scalar_one_or_none()
        if row:
            row.sync_lease_expires_at = datetime.now(UTC) + timedelta(minutes=15)
            session.commit()


@celery_app.task(
    name="app.tasks.tiktok.cleanup_tiktok_deauthorization",
    soft_time_limit=110,
    time_limit=120,
)
def cleanup_tiktok_deauthorization(user_id: str) -> int:
    """Minimize revoked-account data after credential erasure is committed."""
    from app.routes.tiktok import _minimize_disconnected_publication

    user_uuid = uuid.UUID(user_id)
    with sync_session() as session:
        publications = (
            session.execute(
                select(TikTokPublication)
                .where(
                    TikTokPublication.user_id == user_uuid,
                    or_(
                        TikTokPublication.source_object_path != "redacted",
                        TikTokPublication.latest_metrics.is_not(None),
                        TikTokPublication.evaluation_metrics.is_not(None),
                        TikTokPublication.title != "",
                    ),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for publication in publications:
            _minimize_disconnected_publication(publication)
        session.commit()

        snapshots = (
            session.execute(
                select(TikTokPublication)
                .where(
                    TikTokPublication.user_id == user_uuid,
                    TikTokPublication.snapshot_object_path.is_not(None),
                )
                .limit(25)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for publication in snapshots:
            storage.delete_object_best_effort(publication.snapshot_object_path)
            publication.snapshot_object_path = None
        session.commit()
        remaining = session.scalar(
            select(TikTokPublication.id)
            .where(
                TikTokPublication.user_id == user_uuid,
                or_(
                    TikTokPublication.snapshot_object_path.is_not(None),
                    TikTokPublication.source_object_path != "redacted",
                    TikTokPublication.latest_metrics.is_not(None),
                    TikTokPublication.evaluation_metrics.is_not(None),
                    TikTokPublication.title != "",
                ),
            )
            .limit(1)
        )
    if remaining:
        _safe_dispatch(cleanup_tiktok_deauthorization, user_id, countdown=5)
    return len(snapshots)


@celery_app.task(
    name="app.tasks.tiktok.cleanup_tiktok_publications", soft_time_limit=50, time_limit=60
)
def cleanup_tiktok_publications() -> int:
    now = datetime.now(UTC)
    deleted = 0
    with sync_session() as session:
        rows = (
            session.execute(
                select(TikTokPublication).where(TikTokPublication.snapshot_object_path.is_not(None))
            )
            .scalars()
            .all()
        )
        for row in rows:
            terminal_aged = row.processing_status in {
                "complete",
                "failed",
                "submission_unknown",
            } and row.updated_at < now - timedelta(hours=24)
            absolute_aged = row.created_at < now - timedelta(days=7)
            if not terminal_aged and not absolute_aged:
                continue
            if row.snapshot_object_path:
                storage.delete_object_best_effort(row.snapshot_object_path)
                deleted += 1
            row.snapshot_object_path = None
            row.media_token_hash = None
            row.media_expires_at = None
        session.commit()
        revoked_users = (
            session.execute(
                select(OAuthToken.user_id).where(
                    OAuthToken.platform == "tiktok", OAuthToken.status == "revoked"
                )
            )
            .scalars()
            .all()
        )
        if revoked_users:
            audit_rows = (
                session.execute(
                    select(TikTokPublication).where(
                        TikTokPublication.user_id.in_(revoked_users),
                        TikTokPublication.updated_at < now - timedelta(days=30),
                    )
                )
                .scalars()
                .all()
            )
            for row in audit_rows:
                row.tiktok_publish_id = None
                row.tiktok_post_id = None
                row.title = ""
                row.creator_info_snapshot = None
                row.source_object_path = "redacted"
                row.source_generation = "redacted"
                row.source_etag = None
                row.edit_signature = {}
                row.latest_metrics = None
                row.evaluation_metrics = None
            session.commit()
    return deleted


def _post_info(row: TikTokPublication) -> dict[str, Any]:
    return {
        "title": row.title,
        "privacy_level": row.privacy_level,
        "disable_comment": not row.allow_comment,
        "disable_duet": not row.allow_duet,
        "disable_stitch": not row.allow_stitch,
        "brand_content_toggle": row.brand_content_toggle,
        "brand_organic_toggle": row.brand_organic_toggle,
        "is_aigc": row.is_aigc,
    }


def _fail(publication_id: uuid.UUID, code: str, detail: str, *, retryable: bool) -> None:
    schedule_retry = False
    retry_count = 0
    with sync_session() as session:
        row = session.get(TikTokPublication, publication_id)
        if row is None or row.processing_status in {
            "complete",
            "processing",
            "submission_unknown",
        }:
            return
        if row.failure_code == "authorization_removed":
            return
        row.processing_status = "failed"
        row.failure_code = code[:100]
        row.failure_detail = detail[:500]
        row.retryable = retryable and row.retry_count < _MAX_RETRIES
        if row.retryable:
            row.retry_count += 1
            retry_count = row.retry_count
            schedule_retry = True
            row.next_poll_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 30 * (2 ** (retry_count - 1)))
            )
        else:
            row.next_poll_at = None
        session.commit()
    if schedule_retry:
        _safe_dispatch(
            submit_tiktok_publication,
            str(publication_id),
            countdown=min(300, 30 * (2 ** (retry_count - 1))),
        )


def _mark_unknown(publication_id: uuid.UUID) -> None:
    with sync_session() as session:
        row = session.get(TikTokPublication, publication_id)
        if row and row.processing_status == "submitting":
            row.processing_status = "submission_unknown"
            row.failure_code = "submission_timeout"
            row.failure_detail = (
                "TikTok may have received this delivery. Check TikTok before trying again."
            )
            row.retryable = False
            row.next_poll_at = None
            session.commit()


def _safe_dispatch(task: Any, *args: str, countdown: int = 0) -> None:
    """Let the durable DB sweep recover when the broker is temporarily unavailable."""
    try:
        task.apply_async(args=list(args), countdown=countdown)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tiktok.task_dispatch_deferred",
            task=getattr(task, "name", repr(task)),
            error=str(exc)[:200],
        )


def _can_submit_failed(retryable: bool, retry_count: int) -> bool:
    return retryable and retry_count <= _MAX_RETRIES


def _recover_stale_publication(row: TikTokPublication, now: datetime) -> bool:
    """Return whether a safely retryable stale state should be submitted again."""
    if row.processing_status == "submitting":
        row.processing_status = "submission_unknown"
        row.failure_code = "submission_worker_lost"
        row.failure_detail = (
            "TikTok may have received this delivery. Check TikTok before trying again."
        )
        row.retryable = False
        row.next_poll_at = None
        return False
    if row.processing_status == "snapshotting":
        row.processing_status = "queued"
    row.next_poll_at = now + timedelta(minutes=2)
    return True


def _normalize_videos(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views = [float(v["view_count"]) for v in videos if _number(v.get("view_count")) is not None]
    median = _median(views)
    normalized = []
    for video in videos:
        raw_views = _number(video.get("view_count"))
        view_count = int(raw_views) if raw_views is not None else None
        like_count = _int(video.get("like_count"))
        comment_count = _int(video.get("comment_count"))
        share_count = _int(video.get("share_count"))
        engagement = None
        if view_count and view_count > 0:
            engagement = round((like_count + comment_count + share_count) / view_count, 6)
        created = _int(video.get("create_time"))
        normalized.append(
            {
                "video_id": str(video.get("id") or ""),
                "caption": str(video.get("video_description") or video.get("title") or "")[:300],
                "hashtags": [],
                "view_count": view_count,
                "like_count": like_count,
                "comment_count": comment_count,
                "share_count": share_count,
                "repost_count": None,
                "engagement_rate": engagement,
                "view_index": round(view_count / median, 3) if view_count and median else None,
                "duration": _int(video.get("duration")),
                "upload_date": datetime.fromtimestamp(created, UTC).strftime("%Y%m%d")
                if created
                else None,
                "webpage_url": None,
            }
        )
    return sorted(normalized, key=lambda item: item.get("view_count") or 0, reverse=True)


def _analysis_fingerprint(videos: list[dict[str, Any]]) -> str:
    stable = {
        "prompt_version": TIKTOK_ANALYZER_PROMPT_VERSION,
        "input_version": "official-display-v1",
        "videos": [
            {
                "id": video.get("video_id"),
                "view_band": round(float(video.get("view_index") or 0), 1),
                "engagement_band": round(float(video.get("engagement_rate") or 0), 2),
            }
            for video in videos
        ],
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _summary_with_edit_correlations(analysis: dict[str, Any]) -> str:
    summary = str(analysis.get("summary_for_prompts") or "").strip()
    patterns = [
        str(value).strip()
        for value in analysis.get("edit_patterns_observed") or []
        if str(value).strip()
    ][:4]
    if patterns:
        appendix = (
            "Observed Nova edit correlations (low confidence; linked public Nova posts; "
            "72–84-hour window; association only): " + "; ".join(patterns)
        )
        if summary and len(appendix) < 1200:
            summary_budget = 1200 - len(appendix) - 1
            summary = f"{summary[:summary_budget].rstrip()}\n{appendix}"
        else:
            summary = appendix
    return summary[:1200]


def _within_evaluation_window(public_at: datetime, now: datetime) -> bool:
    age = now - public_at
    return _EVALUATION_AGE <= age <= _EVALUATION_MAX_AGE


def _mature_fingerprint(rows: list[TikTokPublication]) -> str:
    stable = [
        {
            "id": str(row.id),
            "signature_version": row.edit_signature_version,
            "signature": row.edit_signature,
            "evaluation": row.evaluation_metrics,
        }
        for row in sorted(rows, key=lambda item: str(item.id))
    ]
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _edit_correlations(rows: list[TikTokPublication]) -> list[dict[str, Any]]:
    if len(rows) < 5:
        return []
    fields = ("archetype", "duration_bucket", "text_mode", "music", "style_family")
    output: list[dict[str, Any]] = []
    for field in fields:
        buckets: dict[str, list[float]] = {}
        for row in rows:
            value = str((row.edit_signature or {}).get(field, "unknown"))
            metrics = row.evaluation_metrics or {}
            views = _number(metrics.get("view_count"))
            if views is not None:
                buckets.setdefault(value, []).append(views)
        supported = {value: values for value, values in buckets.items() if len(values) >= 3}
        if len(supported) < 2:
            continue
        ranked = sorted(supported.items(), key=lambda item: statistics.fmean(item[1]), reverse=True)
        winner, winner_values = ranked[0]
        runner, runner_values = ranked[1]
        winner_mean = statistics.fmean(winner_values)
        runner_mean = statistics.fmean(runner_values)
        if runner_mean <= 0:
            continue
        ratio = winner_mean / runner_mean
        if ratio < 1.2:
            continue
        output.append(
            {
                "feature": field,
                "observed_value": winner,
                "comparison_value": runner,
                "view_ratio": round(ratio, 2),
                "sample_size": len(winner_values) + len(runner_values),
                "window_hours": "72-84",
                "provenance": "linked_nova_public_posts",
                "confidence": "low",
                "language": "correlation_only",
            }
        )
    return output[:4]


def _metrics(video: dict[str, Any]) -> dict[str, Any]:
    return {
        key: video.get(key)
        for key in (
            "view_count",
            "like_count",
            "comment_count",
            "share_count",
            "engagement_rate",
            "view_index",
        )
    }


def _account_metadata(account: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "open_id",
        "display_name",
        "avatar_url",
        "profile_deep_link",
        "is_verified",
        "follower_count",
        "following_count",
        "likes_count",
        "video_count",
    }
    return {key: account[key] for key in allowed if account.get(key) is not None}


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None else 0
