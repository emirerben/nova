"""Authenticated TikTok OAuth, Direct Post, status, media, and webhook routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

import redis.asyncio as redis_async
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import Job, OAuthToken, Persona, TikTokPublication
from app.services import tiktok_client
from app.services.tiktok_lifecycle import (
    visibility_after_draft_inbox,
    visibility_after_draft_post,
)
from app.services.tiktok_publishable import (
    PublishableOutputError,
    job_is_terminal_ready,
    resolve_publishable_output,
)
from app.services.tiktok_tokens import active_access_token
from app.services.token_crypto import TokenCryptoError, decrypt_token, encrypt_token

log = structlog.get_logger()
router = APIRouter()

_OAUTH_STATE_TTL = 600
_WEBHOOK_FRESHNESS_S = 300
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
_SCOPES = [
    "user.info.basic",
    "video.publish",
    "video.upload",
]
_ANALYTICS_SCOPES = {"user.info.basic", "video.list"}
_CONSENT_VERSION = "2026-08-11"
_MEDIA_VERIFICATION_FILENAME = "tiktok9a2bMaksajhuoYRL3P7tSex7MrV8z5lg.txt"
_MEDIA_VERIFICATION_CONTENT = "tiktok-developers-site-verification=9a2bMaksajhuoYRL3P7tSex7MrV8z5lg"


class _RedactTikTokMediaAccessFilter(logging.Filter):
    """Keep the bearer media token out of Uvicorn's default access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            if args[2].startswith("/tiktok/media/"):
                redacted = list(args)
                redacted[2] = "/tiktok/media/[redacted]"
                record.args = tuple(redacted)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTikTokMediaAccessFilter())


def _redis():
    return redis_async.from_url(settings.redis_url, decode_responses=True)


def _beta_user(user_id: uuid.UUID) -> bool:
    return str(user_id) in set(settings.tiktok_publishing_beta_user_ids)


def _publishing_available(user_id: uuid.UUID) -> bool:
    return settings.tiktok_publishing_enabled and (
        settings.tiktok_content_posting_audited or _beta_user(user_id)
    )


def _connection_available(user_id: uuid.UUID) -> bool:
    return (
        _beta_user(user_id)
        or settings.tiktok_publishing_enabled
        or settings.tiktok_performance_sync_enabled
    )


class TikTokConnectionResponse(BaseModel):
    available: bool
    connected: bool
    status: str
    account: dict[str, Any] | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    can_publish: bool = False
    can_upload_draft: bool = False
    can_analyze: bool = False
    audited: bool = False
    beta: bool = False
    last_synced_at: datetime | None = None
    learned_post_count: int = 0


class OAuthStartResponse(BaseModel):
    authorization_url: str


class OAuthStartBody(BaseModel):
    return_to: str | None = None


class PublishOptionsResponse(BaseModel):
    preview_url: str
    source_revision: str
    variant_id: str | None
    duration_s: float | None
    creator_nickname: str
    privacy_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_duration_s: int
    suggested_title: str
    audited: bool
    consent_version: str
    can_direct_post: bool
    can_upload_draft: bool


class CreatePublicationBody(BaseModel):
    job_id: uuid.UUID
    variant_id: str | None = None
    source_revision: str = Field(min_length=32, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    delivery_mode: Literal["direct_post", "draft_upload"] = "direct_post"
    title: str = Field(default="", max_length=2200)
    privacy_level: str = Field(min_length=1, max_length=80)
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    brand_content_toggle: bool = False
    brand_organic_toggle: bool = False
    is_aigc: bool = False
    music_usage_confirmed: bool
    draft_handoff_confirmed: bool = False
    consent_version: str = _CONSENT_VERSION


class PublicationResponse(BaseModel):
    id: str
    job_id: str
    variant_id: str | None
    delivery_mode: str
    title: str
    privacy_level: str
    allow_comment: bool
    allow_duet: bool
    allow_stitch: bool
    creator_nickname: str | None
    processing_status: str
    visibility_status: str
    public_at: datetime | None
    retryable: bool
    failure_code: str | None
    failure_detail: str | None
    latest_metrics: dict[str, Any] | None
    metrics_synced_at: datetime | None
    evaluation_metrics: dict[str, Any] | None
    evaluation_captured_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _publication_response(row: TikTokPublication) -> PublicationResponse:
    creator_nickname = (row.creator_info_snapshot or {}).get("creator_nickname")
    return PublicationResponse(
        id=str(row.id),
        job_id=str(row.job_id),
        variant_id=row.variant_id,
        delivery_mode=row.delivery_mode or "direct_post",
        title=row.title,
        privacy_level=row.privacy_level,
        allow_comment=row.allow_comment,
        allow_duet=row.allow_duet,
        allow_stitch=row.allow_stitch,
        creator_nickname=creator_nickname if isinstance(creator_nickname, str) else None,
        processing_status=row.processing_status,
        visibility_status=row.visibility_status,
        public_at=row.public_at,
        retryable=row.retryable,
        failure_code=row.failure_code,
        failure_detail=row.failure_detail,
        latest_metrics=row.latest_metrics,
        metrics_synced_at=row.metrics_synced_at,
        evaluation_metrics=row.evaluation_metrics,
        evaluation_captured_at=row.evaluation_captured_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/connection", response_model=TikTokConnectionResponse)
async def connection(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TikTokConnectionResponse:
    token = (
        await db.execute(
            select(OAuthToken).where(OAuthToken.user_id == user.id, OAuthToken.platform == "tiktok")
        )
    ).scalar_one_or_none()
    scopes = set(token.scopes or []) if token else set()
    learned_count = await db.scalar(
        select(func.count(TikTokPublication.id)).where(
            TikTokPublication.user_id == user.id,
            TikTokPublication.visibility_status == "public",
            TikTokPublication.evaluation_metrics.is_not(None),
        )
    )
    connected = bool(token and token.status == "active" and token.access_token)
    return TikTokConnectionResponse(
        available=_connection_available(user.id),
        connected=connected,
        status=token.status if token else "disconnected",
        account=token.account_metadata if token else None,
        granted_scopes=sorted(scopes),
        can_publish=connected and _publishing_available(user.id) and "video.publish" in scopes,
        can_upload_draft=(
            connected
            and _publishing_available(user.id)
            and settings.tiktok_draft_upload_enabled
            and "video.upload" in scopes
        ),
        can_analyze=(
            connected
            and settings.tiktok_performance_sync_enabled
            and _ANALYTICS_SCOPES.issubset(scopes)
        ),
        audited=settings.tiktok_content_posting_audited,
        beta=_beta_user(user.id),
        last_synced_at=token.last_synced_at if token else None,
        learned_post_count=learned_count or 0,
    )


def _safe_oauth_return_to(value: str | None) -> str | None:
    """Allow OAuth to return only to owned in-app publishing surfaces."""

    if not value or len(value) > 1024:
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith("//") or "\\" in parsed.path:
        return None
    path = parsed.path.rstrip("/") or "/"
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    if path != "/library" and not path.startswith("/plan/items/"):
        return None
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{path}{query}"


@router.post("/oauth/start", response_model=OAuthStartResponse)
async def oauth_start(user: CurrentUser, body: OAuthStartBody | None = None) -> OAuthStartResponse:
    if not _connection_available(user.id):
        raise HTTPException(status_code=404, detail="TikTok integration is not available")
    state_value = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "user_id": str(user.id),
            "return_to": _safe_oauth_return_to(body.return_to if body else None),
        },
        separators=(",", ":"),
    )
    client = _redis()
    try:
        await client.setex(f"tiktok:oauth:{state_value}", _OAUTH_STATE_TTL, payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail="TikTok connection is temporarily unavailable"
        ) from exc
    finally:
        await client.aclose()
    try:
        url = tiktok_client.authorization_url(state_value, _SCOPES)
    except tiktok_client.TikTokAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return OAuthStartResponse(authorization_url=url)


def _callback_redirect(return_to: str | None = None, **params: str) -> RedirectResponse:
    configured = settings.tiktok_web_app_url
    parsed = urlparse(configured)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    allowed_origins = {value.rstrip("/") for value in settings.allowed_origins}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or origin not in allowed_origins
    ):
        origin = "http://localhost:3000"
    destination = _safe_oauth_return_to(return_to) or "/library"
    base = f"{origin}{destination}"
    separator = "&" if "?" in base else "?"
    return RedirectResponse(f"{base}{separator}{urlencode(params)}", status_code=303)


@router.get("/oauth/callback")
async def oauth_callback(
    state_value: str = Query(alias="state"),
    code: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    client = _redis()
    try:
        raw = await client.getdel(f"tiktok:oauth:{state_value}")
    except Exception:
        raw = None
    finally:
        await client.aclose()
    if not raw:
        return _callback_redirect(tiktok="error", reason="expired_state")
    try:
        state_payload = json.loads(raw)
        return_to = _safe_oauth_return_to(state_payload.get("return_to"))
    except (TypeError, ValueError):
        return _callback_redirect(tiktok="error", reason="expired_state")
    if error or not code:
        return _callback_redirect(return_to, tiktok="error", reason="access_denied")
    try:
        user_id = uuid.UUID(str(state_payload["user_id"]))
        token_payload = await run_in_threadpool(tiktok_client.exchange_code, code)
        account = await run_in_threadpool(tiktok_client.user_info, token_payload.access_token)
        now = datetime.now(UTC)
        row = (
            await db.execute(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id, OAuthToken.platform == "tiktok"
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = OAuthToken(user_id=user_id, platform="tiktok")
            db.add(row)
        row.access_token = encrypt_token(token_payload.access_token)
        row.refresh_token = (
            encrypt_token(token_payload.refresh_token) if token_payload.refresh_token else None
        )
        row.expires_at = now + timedelta(seconds=token_payload.expires_in)
        row.refresh_expires_at = (
            now + timedelta(seconds=token_payload.refresh_expires_in)
            if token_payload.refresh_expires_in
            else None
        )
        row.platform_account_id = token_payload.open_id
        row.scopes = token_payload.scopes
        row.account_metadata = _sanitize_account(account)
        row.status = "active"
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _callback_redirect(return_to, tiktok="error", reason="account_already_connected")
    except tiktok_client.TikTokAPIError as exc:
        await db.rollback()
        log.warning(
            "tiktok_oauth_failed",
            error_code=exc.code,
            status_code=exc.status_code,
        )
        return _callback_redirect(return_to, tiktok="error", reason="connection_failed")
    except (ValueError, KeyError, TokenCryptoError):
        await db.rollback()
        return _callback_redirect(return_to, tiktok="error", reason="connection_failed")
    return _callback_redirect(return_to, tiktok="connected")


@router.delete("/connection", status_code=204)
async def disconnect(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> Response:
    row = (
        await db.execute(
            select(OAuthToken)
            .where(OAuthToken.user_id == user.id, OAuthToken.platform == "tiktok")
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row:
        if row.access_token:
            try:
                await run_in_threadpool(
                    tiktok_client.revoke_access, decrypt_token(row.access_token)
                )
            except Exception:  # noqa: BLE001 — local erasure must still proceed
                pass
        _erase_connection(row)
        await _purge_connected_profile(db, user.id)
        await _cancel_unsubmitted_publications(db, user.id)
        await db.commit()
        _dispatch_deauthorization_cleanup(user.id)
    return Response(status_code=204)


@router.get("/publish-options", response_model=PublishOptionsResponse)
async def publish_options(
    user: CurrentUser,
    job_id: uuid.UUID,
    variant_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> PublishOptionsResponse:
    if not _publishing_available(user.id):
        raise HTTPException(status_code=404, detail="TikTok publishing is not available")
    job = await _owned_job(db, user.id, job_id)
    try:
        output = await run_in_threadpool(resolve_publishable_output, job, variant_id)
        token_row, access_token = await active_access_token(db, user.id)
        granted_scopes = set(token_row.scopes or [])
        can_direct_post = "video.publish" in granted_scopes
        can_upload_draft = settings.tiktok_draft_upload_enabled and "video.upload" in granted_scopes
        if not (can_direct_post or can_upload_draft):
            raise HTTPException(
                status_code=409, detail="Reconnect TikTok to grant Content Posting access"
            )
        creator = (
            await run_in_threadpool(tiktok_client.creator_info, access_token)
            if can_direct_post
            else {}
        )
    except PublishableOutputError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except tiktok_client.TikTokAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    options = [str(v) for v in creator.get("privacy_level_options") or []]
    if not settings.tiktok_content_posting_audited:
        options = [value for value in options if value == "SELF_ONLY"] or ["SELF_ONLY"]
    maximum = int(creator.get("max_video_post_duration_sec") or 60)
    if output.duration_s and output.duration_s > maximum:
        raise HTTPException(status_code=409, detail=f"Video exceeds TikTok's {maximum}s limit")
    account_name = (token_row.account_metadata or {}).get("display_name")
    creator_nickname = str(creator.get("creator_nickname") or account_name or "TikTok creator")
    return PublishOptionsResponse(
        preview_url=output.preview_url,
        source_revision=output.source_revision,
        variant_id=output.variant_id,
        duration_s=output.duration_s,
        creator_nickname=creator_nickname,
        privacy_options=options,
        comment_disabled=bool(creator.get("comment_disabled")),
        duet_disabled=bool(creator.get("duet_disabled")),
        stitch_disabled=bool(creator.get("stitch_disabled")),
        max_duration_s=maximum,
        suggested_title=_suggested_title(job),
        audited=settings.tiktok_content_posting_audited,
        consent_version=_CONSENT_VERSION,
        can_direct_post=can_direct_post,
        can_upload_draft=can_upload_draft,
    )


@router.post("/publications", response_model=PublicationResponse, status_code=202)
async def create_publication(
    body: CreatePublicationBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PublicationResponse:
    if not _publishing_available(user.id):
        raise HTTPException(status_code=404, detail="TikTok publishing is not available")
    if not body.music_usage_confirmed:
        raise HTTPException(status_code=400, detail="Music usage confirmation is required")
    if body.delivery_mode == "draft_upload" and not settings.tiktok_draft_upload_enabled:
        raise HTTPException(status_code=404, detail="TikTok draft upload is not available")
    if body.delivery_mode == "draft_upload" and not body.draft_handoff_confirmed:
        raise HTTPException(
            status_code=400,
            detail="Confirm that you will finish this draft inside TikTok",
        )
    # Validate the Job before the idempotency fast path: cancellation also
    # suppresses retries of an older queued/failed publication receipt.
    job = await _owned_job(db, user.id, body.job_id)
    request_hash = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = (
        await db.execute(
            select(TikTokPublication).where(
                TikTokPublication.user_id == user.id,
                TikTokPublication.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=409, detail="Idempotency key was reused with different content"
            )
        if existing.processing_status == "queued" or (
            existing.processing_status == "failed" and existing.retryable
        ):
            _dispatch_publication(existing.id)
        return _publication_response(existing)

    try:
        output = await run_in_threadpool(resolve_publishable_output, job, body.variant_id)
        token_row, access_token = await active_access_token(db, user.id)
        required_scope = "video.upload" if body.delivery_mode == "draft_upload" else "video.publish"
        if required_scope not in set(token_row.scopes or []):
            raise HTTPException(
                status_code=409,
                detail=f"Reconnect TikTok to grant {required_scope} access",
            )
        creator = (
            {}
            if body.delivery_mode == "draft_upload"
            else await run_in_threadpool(tiktok_client.creator_info, access_token)
        )
    except PublishableOutputError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except tiktok_client.TikTokAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if output.source_revision != body.source_revision:
        raise HTTPException(
            status_code=409, detail="The video changed; review the latest render before publishing"
        )
    if body.delivery_mode == "direct_post":
        _validate_post_settings(body, creator)
    if body.consent_version != _CONSENT_VERSION:
        raise HTTPException(
            status_code=409, detail="Review the latest TikTok consent before posting"
        )
    try:
        current_meta = await run_in_threadpool(storage.object_metadata, output.object_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="The approved render is no longer available"
        ) from exc
    if current_meta.generation != output.generation or current_meta.etag != output.etag:
        raise HTTPException(
            status_code=409, detail="The video changed; review the latest render before publishing"
        )

    # Re-lock immediately before inserting the durable publication receipt.
    # The creator-info and metadata calls above intentionally run without a DB
    # lock; this second fence prevents a concurrent rerender/cancellation from
    # slipping between the initial validation and the publication commit.
    job = await _owned_job(db, user.id, body.job_id, for_update=True)

    row = TikTokPublication(
        user_id=user.id,
        job_id=job.id,
        variant_id=output.variant_id,
        idempotency_key=body.idempotency_key,
        request_hash=request_hash,
        delivery_mode=body.delivery_mode,
        source_object_path=output.object_path,
        source_generation=output.generation,
        source_etag=output.etag,
        edit_signature=output.edit_signature,
        title=("" if body.delivery_mode == "draft_upload" else body.title.strip()),
        privacy_level=(
            "TIKTOK_DRAFT" if body.delivery_mode == "draft_upload" else body.privacy_level
        ),
        allow_comment=body.delivery_mode == "direct_post" and body.allow_comment,
        allow_duet=body.delivery_mode == "direct_post" and body.allow_duet,
        allow_stitch=body.delivery_mode == "direct_post" and body.allow_stitch,
        brand_content_toggle=(body.delivery_mode == "direct_post" and body.brand_content_toggle),
        brand_organic_toggle=(body.delivery_mode == "direct_post" and body.brand_organic_toggle),
        is_aigc=body.delivery_mode == "direct_post" and body.is_aigc,
        music_usage_confirmed=body.music_usage_confirmed,
        consent_version=body.consent_version,
        consented_at=datetime.now(UTC),
        creator_info_snapshot=_sanitize_creator_info(creator),
        next_poll_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(
                select(TikTokPublication).where(
                    TikTokPublication.user_id == user.id,
                    TikTokPublication.idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one()
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency key conflict")
        if existing.processing_status == "queued" or (
            existing.processing_status == "failed" and existing.retryable
        ):
            _dispatch_publication(existing.id)
        return _publication_response(existing)
    await db.refresh(row)
    _dispatch_publication(row.id)
    return _publication_response(row)


@router.get("/publications", response_model=list[PublicationResponse])
async def list_publications(
    user: CurrentUser,
    job_id: uuid.UUID | None = None,
    variant_id: str | None = Query(default=None, max_length=200),
    db: AsyncSession = Depends(get_db),
):
    query = select(TikTokPublication).where(TikTokPublication.user_id == user.id)
    if job_id is not None:
        query = query.where(TikTokPublication.job_id == job_id)
    if variant_id is not None:
        query = query.where(TikTokPublication.variant_id == variant_id)
    rows = (
        (await db.execute(query.order_by(desc(TikTokPublication.created_at)).limit(100)))
        .scalars()
        .all()
    )
    return [_publication_response(row) for row in rows]


@router.get("/publications/receipt", response_model=PublicationResponse | None)
async def get_publication_receipt(
    user: CurrentUser,
    job_id: uuid.UUID,
    variant_id: str | None = Query(default=None, max_length=200),
    db: AsyncSession = Depends(get_db),
) -> PublicationResponse | None:
    query = select(TikTokPublication).where(
        TikTokPublication.user_id == user.id,
        TikTokPublication.job_id == job_id,
    )
    if variant_id is not None:
        query = query.where(TikTokPublication.variant_id == variant_id)
    row = (
        await db.execute(query.order_by(desc(TikTokPublication.created_at)).limit(1))
    ).scalar_one_or_none()
    return _publication_response(row) if row is not None else None


@router.get("/publications/{publication_id}", response_model=PublicationResponse)
async def get_publication(
    publication_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PublicationResponse:
    row = await _owned_publication(db, user.id, publication_id)
    return _publication_response(row)


@router.post("/sync", status_code=202)
async def request_sync(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    if not settings.tiktok_performance_sync_enabled:
        raise HTTPException(status_code=404, detail="TikTok performance sync is not available")
    token = (
        await db.execute(
            select(OAuthToken).where(
                OAuthToken.user_id == user.id,
                OAuthToken.platform == "tiktok",
                OAuthToken.status == "active",
            )
        )
    ).scalar_one_or_none()
    if token is None or not _ANALYTICS_SCOPES.issubset(set(token.scopes or [])):
        raise HTTPException(status_code=409, detail="Reconnect TikTok to grant analytics access")
    client = _redis()
    try:
        accepted = await client.set(f"tiktok:manual-sync:{user.id}", "1", ex=300, nx=True)
    finally:
        await client.aclose()
    if not accepted:
        raise HTTPException(status_code=429, detail="A TikTok sync was requested recently")
    from app.tasks.tiktok import sync_tiktok_account

    sync_tiktok_account.delay(str(user.id))
    return {"status": "queued"}


@router.get(f"/media/{_MEDIA_VERIFICATION_FILENAME}", include_in_schema=False)
async def media_verification() -> Response:
    """Serve TikTok's URL-prefix ownership proof at the exact verified path."""
    return Response(content=_MEDIA_VERIFICATION_CONTENT, media_type="text/plain")


@router.api_route("/media/{publication_id}/{media_token}.mp4", methods=["GET", "HEAD"])
async def media(
    publication_id: uuid.UUID,
    media_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    row = await db.get(TikTokPublication, publication_id)
    if (
        row is None
        or not row.snapshot_object_path
        or not row.media_token_hash
        or not row.media_expires_at
        or row.media_expires_at <= datetime.now(UTC)
        or not hmac.compare_digest(
            hashlib.sha256(media_token.encode()).hexdigest(), row.media_token_hash
        )
    ):
        raise HTTPException(status_code=404, detail="Media not found")
    job_status = await db.scalar(select(Job.status).where(Job.id == row.job_id))
    if job_status == "cancelled":
        raise HTTPException(status_code=404, detail="Media not found")
    try:
        meta = await run_in_threadpool(storage.object_metadata, row.snapshot_object_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc
    start, end, partial = _parse_range(request.headers.get("range"), meta.size)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        # Prevent the global GZip middleware from changing byte offsets or the
        # advertised length of TikTok's range response.
        "Content-Encoding": "identity",
        "Cache-Control": "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{meta.size}"
    if request.method == "HEAD":
        return Response(
            status_code=206 if partial else 200, media_type=meta.content_type, headers=headers
        )
    iterator = storage.iter_object_range(row.snapshot_object_path, start=start, end=end)
    return StreamingResponse(
        iterator, status_code=206 if partial else 200, media_type=meta.content_type, headers=headers
    )


@router.post("/webhook", status_code=204)
async def webhook(
    request: Request,
    tiktok_signature: str | None = Header(default=None, alias="TikTok-Signature"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    raw = await _read_bounded_webhook_body(request)
    timestamp, signature = _verify_webhook(tiktok_signature, raw)
    replay_key = hashlib.sha256(f"{timestamp}:{signature}".encode()).hexdigest()
    client = _redis()
    try:
        fresh = await client.set(
            f"tiktok:webhook:{replay_key}", "1", ex=_WEBHOOK_FRESHNESS_S, nx=True
        )
    finally:
        await client.aclose()
    if not fresh:
        return Response(status_code=204)
    try:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook body") from exc
        action = await _apply_webhook(db, payload)
        await db.commit()
    except Exception:
        # A failed transaction must not consume the replay digest; TikTok's
        # signed retry needs another chance to erase credentials or reconcile.
        client = _redis()
        try:
            await client.delete(f"tiktok:webhook:{replay_key}")
        finally:
            await client.aclose()
        raise
    if isinstance(action, tuple):
        _dispatch_deauthorization_cleanup(action[1])
    elif action:
        _dispatch_publication(action, countdown=60)
    return Response(status_code=204)


async def _read_bounded_webhook_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Webhook body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body is too large")
    return bytes(body)


async def _owned_job(
    db: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Job:
    statement = select(Job).where(Job.id == job_id, Job.user_id == user_id)
    if for_update:
        statement = statement.execution_options(populate_existing=True).with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job_is_terminal_ready(row):
        detail = (
            "Cancelled videos cannot be published"
            if row.status == "cancelled"
            else "The video must finish rendering before it can be published"
        )
        raise HTTPException(status_code=409, detail=detail)
    return row


async def _owned_publication(
    db: AsyncSession, user_id: uuid.UUID, publication_id: uuid.UUID
) -> TikTokPublication:
    row = (
        await db.execute(
            select(TikTokPublication).where(
                TikTokPublication.id == publication_id,
                TikTokPublication.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    return row


def _sanitize_account(account: dict[str, Any]) -> dict[str, Any]:
    allowed = {"open_id", "display_name", "avatar_url", "profile_deep_link", "is_verified"}
    return {key: account[key] for key in allowed if account.get(key) is not None}


def _sanitize_creator_info(creator: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "creator_nickname",
        "privacy_level_options",
        "comment_disabled",
        "duet_disabled",
        "stitch_disabled",
        "max_video_post_duration_sec",
    }
    sanitized = {key: creator[key] for key in allowed if key in creator}
    if not isinstance(sanitized.get("creator_nickname"), str):
        sanitized.pop("creator_nickname", None)
    return sanitized


def _erase_connection(row: OAuthToken) -> None:
    row.access_token = None
    row.refresh_token = None
    row.expires_at = None
    row.refresh_expires_at = None
    row.platform_account_id = None
    row.scopes = []
    row.account_metadata = None
    row.last_synced_at = None
    row.sync_lease_expires_at = None
    row.status = "revoked"


def _dispatch_publication(publication_id: uuid.UUID, *, countdown: int = 0) -> None:
    """Best-effort fast path; the DB recovery sweep is the durable path."""
    from app.tasks.tiktok import submit_tiktok_publication

    try:
        submit_tiktok_publication.apply_async(args=[str(publication_id)], countdown=countdown)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tiktok.publication_dispatch_deferred",
            publication_id=str(publication_id),
            error=str(exc)[:200],
        )


def _dispatch_deauthorization_cleanup(user_id: uuid.UUID) -> None:
    """Best-effort fast path; revoked-account recovery is scanned every minute."""
    from app.tasks.tiktok import cleanup_tiktok_deauthorization

    try:
        cleanup_tiktok_deauthorization.delay(str(user_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tiktok.deauthorization_cleanup_deferred",
            user_id=str(user_id),
            error=str(exc)[:200],
        )


async def _cancel_unsubmitted_publications(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(TikTokPublication)
        .where(
            TikTokPublication.user_id == user_id,
            TikTokPublication.processing_status.in_(["queued", "snapshotting"]),
        )
        .values(
            processing_status="failed",
            retryable=False,
            next_poll_at=None,
            failure_code="authorization_removed",
            failure_detail="TikTok was disconnected before submission",
        )
    )
    await db.execute(
        update(TikTokPublication)
        .where(
            TikTokPublication.user_id == user_id,
            TikTokPublication.processing_status == "submitting",
        )
        .values(
            processing_status="submission_unknown",
            retryable=False,
            next_poll_at=None,
            failure_code="authorization_removed_during_submission",
            failure_detail="TikTok was disconnected while submission was in progress",
        )
    )


def _minimize_disconnected_publication(row: TikTokPublication) -> None:
    """Keep only the 30-day consent/audit record after authorization removal."""
    row.media_token_hash = None
    row.media_expires_at = None
    row.latest_metrics = None
    row.metrics_synced_at = None
    row.evaluation_metrics = None
    row.evaluation_captured_at = None
    row.title = ""
    row.creator_info_snapshot = None
    row.source_object_path = "redacted"
    row.source_generation = "redacted"
    row.source_etag = None
    row.edit_signature = {}
    row.retryable = False
    row.next_poll_at = None
    if row.processing_status in {"queued", "snapshotting"}:
        row.processing_status = "failed"
        row.failure_code = "authorization_removed"
        row.failure_detail = "TikTok was disconnected before submission"
    elif row.processing_status == "submitting":
        row.processing_status = "submission_unknown"
        row.failure_code = "authorization_removed_during_submission"
        row.failure_detail = "TikTok was disconnected while submission was in progress"


async def _purge_connected_profile(db: AsyncSession, user_id: uuid.UUID) -> None:
    persona = (
        await db.execute(select(Persona).where(Persona.user_id == user_id).with_for_update())
    ).scalar_one_or_none()
    if persona is None:
        return
    profile = dict(persona.tiktok_profile or {})
    profile.pop("official_sync", None)
    profile.pop("official_analysis", None)
    persona.tiktok_profile = profile or None
    style = dict(persona.style or {})
    if (
        style.get("status") != "edited"
        and (style.get("derived_from") or {}).get("source") == "tiktok_official"
    ):
        persona.style = None


def _suggested_title(job: Job) -> str:
    plan = job.assembly_plan or {}
    copy = plan.get("platform_copy") or {}
    tiktok = copy.get("tiktok") if isinstance(copy, dict) else None
    if isinstance(tiktok, dict):
        return str(tiktok.get("caption") or "")[:2200]
    return ""


def _validate_post_settings(body: CreatePublicationBody, creator: dict[str, Any]) -> None:
    options = {str(v) for v in creator.get("privacy_level_options") or []}
    if not settings.tiktok_content_posting_audited and body.privacy_level != "SELF_ONLY":
        raise HTTPException(status_code=400, detail="Beta publishing is private-only")
    if body.privacy_level not in options:
        raise HTTPException(
            status_code=400, detail="That privacy option is not currently available"
        )
    disabled = {
        "allow_comment": bool(creator.get("comment_disabled")),
        "allow_duet": bool(creator.get("duet_disabled")),
        "allow_stitch": bool(creator.get("stitch_disabled")),
    }
    for field, unavailable in disabled.items():
        if unavailable and getattr(body, field):
            raise HTTPException(status_code=400, detail=f"TikTok has disabled {field}")
    if body.brand_content_toggle and body.privacy_level == "SELF_ONLY":
        raise HTTPException(status_code=400, detail="Branded content cannot be published privately")


def _parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if size <= 0:
        raise HTTPException(status_code=404, detail="Media is empty")
    if not value:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(status_code=416, detail="Only one byte range is supported")
    raw_start, separator, raw_end = value[6:].partition("-")
    if not separator:
        raise HTTPException(status_code=416, detail="Invalid byte range")
    try:
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
        else:
            suffix = int(raw_end)
            start = max(0, size - suffix)
            end = size - 1
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid byte range") from exc
    if start < 0 or end < start or start >= size:
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    return start, min(end, size - 1), True


def _verify_webhook(header: str | None, raw: bytes) -> tuple[int, str]:
    if not header or not settings.tiktok_client_secret:
        raise HTTPException(status_code=401, detail="Missing webhook signature")
    values = {}
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        values[key] = value
    try:
        timestamp = int(values["t"])
        signature = values["s"]
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook signature") from exc
    if abs(int(time.time()) - timestamp) > _WEBHOOK_FRESHNESS_S:
        raise HTTPException(status_code=401, detail="Stale webhook signature")
    signed = str(timestamp).encode() + b"." + raw
    expected = hmac.new(settings.tiktok_client_secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return timestamp, signature


async def _apply_webhook(
    db: AsyncSession, payload: dict[str, Any]
) -> uuid.UUID | tuple[str, uuid.UUID] | None:
    event = str(payload.get("event") or "")
    content = payload.get("content") or {}
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    if event == "authorization.removed":
        open_id = str(payload.get("user_openid") or content.get("open_id") or "")
        row = (
            await db.execute(
                select(OAuthToken)
                .where(OAuthToken.platform == "tiktok", OAuthToken.platform_account_id == open_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row:
            user_id = row.user_id
            _erase_connection(row)
            await _purge_connected_profile(db, user_id)
            await _cancel_unsubmitted_publications(db, user_id)
            return ("deauthorize", user_id)
        return None
    publish_id = str(content.get("publish_id") or "")
    post_id = str(content.get("post_id") or content.get("video_id") or "")
    clauses = []
    if publish_id:
        clauses.append(TikTokPublication.tiktok_publish_id == publish_id)
    if post_id:
        clauses.append(TikTokPublication.tiktok_post_id == post_id)
    if not clauses:
        return None
    from sqlalchemy import or_

    row = (
        await db.execute(select(TikTokPublication).where(or_(*clauses)).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        return None
    if post_id:
        row.tiktok_post_id = post_id
    if row.visibility_status == "removed" and event == "post.publish.publicly_available":
        return None
    if event == "post.publish.inbox_delivered":
        if (row.delivery_mode or "direct_post") != "draft_upload":
            return None
        visibility_status = visibility_after_draft_inbox(
            row.visibility_status,
            row.processing_status,
        )
        row.processing_status = "complete"
        row.visibility_status = visibility_status
        row.next_poll_at = None
    elif event in {"post.publish.complete", "post.publish.completed"}:
        row.processing_status = "complete"
        if (row.delivery_mode or "direct_post") == "draft_upload":
            # Upload API completion means the creator continued from their
            # inbox and posted in TikTok. Audience is chosen there, so do not
            # invent a private/public visibility value.
            row.visibility_status = visibility_after_draft_post(row.visibility_status)
            row.next_poll_at = None
        elif row.privacy_level == "SELF_ONLY" and row.visibility_status == "unknown":
            row.visibility_status = "private"
            row.next_poll_at = None
        elif row.visibility_status == "unknown":
            row.next_poll_at = datetime.now(UTC) + timedelta(minutes=2)
    elif event == "post.publish.publicly_available":
        row.processing_status = "complete"
        row.visibility_status = "public"
        row.public_at = row.public_at or datetime.now(UTC)
        row.next_poll_at = None
    elif event in {
        "post.publish.no_longer_publicaly_available",
        "post.publish.no_longer_publicly_available",
    }:
        row.visibility_status = "removed"
        row.next_poll_at = None
    elif event == "post.publish.failed":
        if row.processing_status == "complete":
            return None
        reason = str(content.get("fail_reason") or "publish_failed")[:100]
        row.processing_status = "failed"
        row.failure_code = reason
        row.failure_detail = (
            "TikTok could not receive this draft"
            if (row.delivery_mode or "direct_post") == "draft_upload"
            else "TikTok could not publish this video"
        )
        row.retryable = reason in {"internal", "video_pull_failed"} and row.retry_count < 3
        if row.retryable:
            row.retry_count += 1
            row.next_poll_at = datetime.now(UTC) + timedelta(minutes=1)
            return row.id
        row.next_poll_at = None
    return None
