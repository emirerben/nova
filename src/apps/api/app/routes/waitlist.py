import hmac

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import WaitlistSignup

log = structlog.get_logger()
router = APIRouter()

# Max length for UTM params to prevent abuse
UTM_MAX_LENGTH = 256


def get_real_ip(request: Request) -> str:
    """Rate limit by real IP; X-Forwarded-For aware (works behind nginx/Caddy)."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host or "127.0.0.1"


def _truncate_utm(value: str | None) -> str | None:
    """Truncate UTM param to max length, or return None if empty."""
    if not value:
        return None
    return value[:UTM_MAX_LENGTH] or None


class WaitlistRequest(BaseModel):
    email: EmailStr


@router.post("/api/waitlist", status_code=201)
@limiter.limit("5/minute", key_func=get_real_ip)
async def join_waitlist(
    request: Request,
    body: WaitlistRequest,
    db: AsyncSession = Depends(get_db),
    utm_source: str | None = Query(None),
    utm_medium: str | None = Query(None),
    utm_campaign: str | None = Query(None),
) -> dict:
    normalized = body.email.lower().strip()
    signup = WaitlistSignup(
        email=normalized,
        utm_source=_truncate_utm(utm_source),
        utm_medium=_truncate_utm(utm_medium),
        utm_campaign=_truncate_utm(utm_campaign),
    )
    db.add(signup)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already_registered")

    # Fire-and-forget confirmation email
    try:
        from app.tasks.email import send_waitlist_confirmation  # noqa: PLC0415
        send_waitlist_confirmation.delay(normalized)
    except Exception as exc:
        log.warning("confirmation_email_dispatch_failed", email=normalized, error=str(exc))

    return {"message": "success"}


@router.get("/api/admin/waitlist")
async def list_waitlist(
    x_admin_secret: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if not x_admin_secret or not hmac.compare_digest(
        x_admin_secret, settings.waitlist_admin_secret
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    result = await db.execute(
        select(WaitlistSignup).order_by(WaitlistSignup.created_at.desc())
    )
    signups = result.scalars().all()
    return [
        {
            "id": s.id,
            "email": s.email,
            "created_at": s.created_at.isoformat(),
            "invited_at": s.invited_at.isoformat() if s.invited_at else None,
        }
        for s in signups
    ]
