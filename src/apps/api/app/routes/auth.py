"""Google upsert endpoint — called by the Next.js NextAuth signIn callback.

The Next.js frontend handles all Google OAuth interactions via NextAuth.
When a user first signs in, NextAuth's server-side signIn callback POSTs
the verified email + name here.  We find-or-create the users row and
return the UUID so NextAuth can embed it in the session JWT.

This endpoint is gated by INTERNAL_API_KEY (server-to-server only).
It is never called from the browser.
"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User

log = structlog.get_logger(__name__)
router = APIRouter()


class GoogleUpsertRequest(BaseModel):
    email: EmailStr
    name: str | None = None


class GoogleUpsertResponse(BaseModel):
    user_id: str
    created: bool


def _require_internal_key(request: Request) -> None:
    if not settings.internal_api_key:
        return  # unconfigured = open in local dev
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {settings.internal_api_key}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal key")


@router.post("/google-upsert", response_model=GoogleUpsertResponse)
async def google_upsert(
    body: GoogleUpsertRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GoogleUpsertResponse:
    _require_internal_key(request)

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is not None:
        log.info("auth.google_upsert.existing", user_id=str(user.id), email=body.email)
        return GoogleUpsertResponse(user_id=str(user.id), created=False)

    user = User(
        id=uuid.uuid4(),
        email=body.email,
        name=body.name,
        auth_provider="google",
        onboarding_status="pending",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    log.info("auth.google_upsert.created", user_id=str(user.id), email=body.email)
    return GoogleUpsertResponse(user_id=str(user.id), created=True)
