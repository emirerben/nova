"""User authentication for the plan proxy.

Auth flow (Google-only, admin-proxy pattern):
1. User signs in via Google in the Next.js frontend (NextAuth).
2. NextAuth signIn callback POSTs the verified email + name to
   POST /auth/google-upsert (gated by INTERNAL_API_KEY).
3. The API find-or-creates the users row and returns its UUID.
4. NextAuth stores the UUID in the JWT as token.userId.
5. The Next.js plan proxy (/api/plan/[...path]) reads the server-side
   session, attaches X-User-Id + Authorization: Bearer <INTERNAL_API_KEY>,
   and forwards to this API.
6. get_current_user reads X-User-Id (already validated by the Next.js gate).

Legacy public routes (generative/template/music jobs) use
get_current_user_or_synthetic which falls back to the synthetic dev user
when no X-User-Id header is present, so existing unauthenticated flows
keep working unchanged.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User

SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_INTERNAL_API_KEY_HEADER = "authorization"


def ensure_job_owner(job_user_id: uuid.UUID | None, current_user: "User") -> None:
    """Raise 404 when job_user_id belongs to a real user other than current_user.

    Jobs owned by the synthetic dev user (or with no owner) stay reachable by
    UUID — the documented transitional model for the anonymous public flows.
    404 (not 403) so a forbidden job is indistinguishable from a missing one.
    """
    if job_user_id is None or job_user_id == SYNTHETIC_USER_ID:
        return
    if job_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")


def _verify_internal_key(authorization: str | None) -> None:
    """Raise 401 unless the bearer token matches INTERNAL_API_KEY.

    SECURITY CONTRACT (fail-closed): `INTERNAL_API_KEY` MUST be set in every
    environment that serves the strict plan routes. The X-User-Id header is the
    *only* thing identifying the user, and it is trusted because the Next.js plan
    proxy attaches it ONLY after verifying the server-side NextAuth session. The
    internal key is what proves "this request came from our proxy, not a forged
    direct call."

    If the server key is unset this REJECTS (it does not bypass) — a deployment
    that forgets the secret returns 401 rather than trusting a forged X-User-Id.
    This is safe for local dev: the Next.js plan proxy and dev-login provider both
    already refuse to forward without INTERNAL_API_KEY, so any local flow that
    reaches a strict route already has the key configured.
    """
    expected = settings.internal_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Server auth not configured",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal key")


async def get_current_user(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Require an authenticated user.

    Reads the X-User-Id header injected by the Next.js plan proxy (which
    already verified the NextAuth session server-side).  Returns the User
    row; raises 401 if missing or not found.
    """
    _verify_internal_key(authorization)

    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    try:
        uid = uuid.UUID(x_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user id",
        )

    from sqlalchemy import select

    row = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return row


async def get_current_user_or_synthetic(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Return the authenticated user, or the synthetic dev user as a fallback.

    Legacy public routes (generative/template/music jobs) use this so
    unauthenticated callers keep working during the auth rollout.
    """
    if not x_user_id:
        # No auth header — return the synthetic dev user without a DB hit.
        from app.models import User as UserModel

        synthetic = UserModel()
        synthetic.id = SYNTHETIC_USER_ID
        synthetic.email = "synthetic-mvp@nova.internal"
        synthetic.name = "Dev"
        synthetic.auth_provider = "synthetic"
        synthetic.onboarding_status = "complete"
        return synthetic

    return await get_current_user(x_user_id=x_user_id, authorization=authorization, db=db)


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentUserOrSynthetic = Annotated[User, Depends(get_current_user_or_synthetic)]

__all__ = [
    "SYNTHETIC_USER_ID",
    "CurrentUser",
    "CurrentUserOrSynthetic",
    "ensure_job_owner",
    "get_current_user",
    "get_current_user_or_synthetic",
]
