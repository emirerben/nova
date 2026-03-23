import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import WaitlistSignup

router = APIRouter()


def get_real_ip(request: Request) -> str:
    """Rate limit by real IP; X-Forwarded-For aware (works behind nginx/Caddy)."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host or "127.0.0.1"


class WaitlistRequest(BaseModel):
    email: EmailStr


@router.post("/api/waitlist", status_code=201)
@limiter.limit("5/minute", key_func=get_real_ip)
async def join_waitlist(
    request: Request,
    body: WaitlistRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    normalized = body.email.lower().strip()
    signup = WaitlistSignup(email=normalized)
    db.add(signup)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already_registered")
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
