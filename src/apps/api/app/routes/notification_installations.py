"""Mobile-only registration. Targets come from server policy, never the client."""

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import VerifiedMobile
from app.database import get_db
from app.services.notification_delivery import (
    NotificationPolicy,
    RegistrationConflict,
    notification_policy,
    register_installation,
    revoke_installation,
)
from app.services.token_crypto import TokenCryptoError

router = APIRouter()
Policy = Annotated[NotificationPolicy, Depends(notification_policy)]


class RegistrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: SecretStr
    enabled: bool = False


class InstallationOut(BaseModel):
    id: uuid.UUID
    enabled: bool
    registration_version: int


class CapabilitiesOut(BaseModel):
    available: bool


@router.get("/notification-installations/capabilities", response_model=CapabilitiesOut)
async def capabilities(principal: VerifiedMobile, policy: Policy) -> CapabilitiesOut:
    return CapabilitiesOut(available=policy.allows(principal.user_id))


@router.put("/notification-installations/{installation_id}", response_model=InstallationOut)
async def register(
    installation_id: uuid.UUID,
    request: Request,
    principal: VerifiedMobile,
    policy: Policy,
    db: AsyncSession = Depends(get_db),
) -> InstallationOut:
    if not policy.allows(principal.user_id):
        raise HTTPException(404, "Notifications unavailable")
    try:
        body = RegistrationBody.model_validate(await request.json())
        if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,256}", body.token.get_secret_value()):
            raise ValueError("Invalid token")
    except (ValidationError, ValueError, UnicodeError):
        # FastAPI's default validation response can echo the rejected token input.
        raise HTTPException(422, "Invalid notification registration") from None
    try:
        row = await db.run_sync(
            lambda session: register_installation(
                session,
                principal,
                installation_id,
                body.token,
                body.enabled,
                policy,
                datetime.now(UTC),
            )
        )
        output = InstallationOut(
            id=row.id, enabled=row.enabled, registration_version=row.registration_version
        )
        await db.commit()
        return output
    except (RegistrationConflict, IntegrityError):
        await db.rollback()
        raise HTTPException(409, "Registration unavailable") from None
    except TokenCryptoError:
        await db.rollback()
        raise HTTPException(503, "Notification registration unavailable") from None


@router.delete("/notification-installations/{installation_id}", status_code=204)
async def revoke(
    installation_id: uuid.UUID, principal: VerifiedMobile, db: AsyncSession = Depends(get_db)
) -> Response:
    try:
        await db.run_sync(
            lambda session: revoke_installation(
                session, principal, installation_id, datetime.now(UTC)
            )
        )
        await db.commit()
    except RegistrationConflict:
        await db.rollback()
        raise HTTPException(409, "Registration unavailable") from None
    return Response(status_code=204)
