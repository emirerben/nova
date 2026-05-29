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


def _verify_internal_key(authorization: str | None) -> None:
    """Raise 401 if the bearer token doesn't match INTERNAL_API_KEY.

    SECURITY CONTRACT: `INTERNAL_API_KEY` MUST be set as a Fly secret in any
    non-local deployment. The X-User-Id header is the *only* thing identifying
    the user, and it is trusted because the Next.js plan proxy attaches it ONLY
    after verifying the server-side NextAuth session. The internal key is what
    proves "this request came from our proxy, not a forged direct call."

    When the key is empty (local dev convenience), this check is bypassed, which
    means a direct request carrying a forged X-User-Id would be trusted. That is
    acceptable for Phase 1 (no plan routes carry per-user data yet; legacy routes
    already attribute everything to the synthetic user) BUT is a fail-open footgun.
    Phase 2 (plan routes with real user data) MUST harden this to fail closed when
    the key is unset. Until then: never deploy without INTERNAL_API_KEY set.
    """
    if not settings.internal_api_key:
        return  # key not configured: local-dev bypass (see SECURITY CONTRACT above)
    expected = f"Bearer {settings.internal_api_key}"
    if authorization != expected:
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
