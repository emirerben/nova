"""Advisory locks shared by native identity linking and account erasure."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


def auth_email_lock_key(email: str) -> str:
    return f"native-auth:email:{email.lower()}"


def auth_identity_lock_key(provider: str, subject: str) -> str:
    return f"native-auth:identity:{provider}:{subject}"


def account_lifecycle_lock_key(user_id: uuid.UUID) -> str:
    return f"account-lifecycle:{user_id}"


async def acquire_auth_locks(db: AsyncSession, *keys: str) -> None:
    for key in sorted(set(keys)):
        await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
