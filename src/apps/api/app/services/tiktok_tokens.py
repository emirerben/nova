"""Transactional TikTok access-token loading and rotation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import OAuthToken
from app.services import tiktok_client
from app.services.token_crypto import decrypt_token, encrypt_token

_SKEW = timedelta(minutes=5)


async def active_access_token(db: AsyncSession, user_id: uuid.UUID) -> tuple[OAuthToken, str]:
    row = (
        await db.execute(
            select(OAuthToken)
            .where(
                OAuthToken.user_id == user_id,
                OAuthToken.platform == "tiktok",
                OAuthToken.status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.access_token is None:
        raise LookupError("TikTok is not connected")
    now = datetime.now(UTC)
    if row.expires_at is None or row.expires_at > now + _SKEW:
        return row, decrypt_token(row.access_token)
    if row.refresh_token is None or (
        row.refresh_expires_at is not None and row.refresh_expires_at <= now
    ):
        row.status = "reconnect_required"
        await db.commit()
        raise LookupError("TikTok must be reconnected")
    try:
        refreshed = await run_in_threadpool(
            tiktok_client.refresh_access_token, decrypt_token(row.refresh_token)
        )
    except tiktok_client.TikTokAPIError as exc:
        if not exc.retryable:
            row.status = "reconnect_required"
            await db.commit()
            raise LookupError("TikTok must be reconnected") from exc
        raise
    _apply_refresh(row, refreshed, now)
    await db.commit()
    return row, refreshed.access_token


def active_access_token_sync(session: Session, user_id: uuid.UUID) -> tuple[OAuthToken, str]:
    row = session.execute(
        select(OAuthToken)
        .where(
            OAuthToken.user_id == user_id,
            OAuthToken.platform == "tiktok",
            OAuthToken.status == "active",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.access_token is None:
        raise LookupError("TikTok is not connected")
    now = datetime.now(UTC)
    if row.expires_at is None or row.expires_at > now + _SKEW:
        return row, decrypt_token(row.access_token)
    if row.refresh_token is None or (
        row.refresh_expires_at is not None and row.refresh_expires_at <= now
    ):
        row.status = "reconnect_required"
        session.commit()
        raise LookupError("TikTok must be reconnected")
    try:
        refreshed = tiktok_client.refresh_access_token(decrypt_token(row.refresh_token))
    except tiktok_client.TikTokAPIError as exc:
        if not exc.retryable:
            row.status = "reconnect_required"
            session.commit()
            raise LookupError("TikTok must be reconnected") from exc
        raise
    _apply_refresh(row, refreshed, now)
    session.commit()
    return row, refreshed.access_token


def _apply_refresh(row: OAuthToken, refreshed: tiktok_client.TokenPayload, now: datetime) -> None:
    row.access_token = encrypt_token(refreshed.access_token)
    if refreshed.refresh_token:
        row.refresh_token = encrypt_token(refreshed.refresh_token)
    row.expires_at = now + timedelta(seconds=refreshed.expires_in)
    if refreshed.refresh_expires_in:
        row.refresh_expires_at = now + timedelta(seconds=refreshed.refresh_expires_in)
    row.scopes = refreshed.scopes
    row.platform_account_id = refreshed.open_id
    row.status = "active"
