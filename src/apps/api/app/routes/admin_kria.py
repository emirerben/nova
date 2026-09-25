"""Admin lookup for one complete, privacy-safe Kria control-plane trace."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.database import get_db
from app.routes.admin import _require_admin
from app.services.kria_trace import (
    reconciliation_actions,
    resolve_kria_trace,
    trace_alerts,
    trace_metrics,
)

router = APIRouter(dependencies=[Depends(_require_admin)])


class KriaTraceResponse(BaseModel):
    trace: dict[str, Any]
    metrics: dict[str, Any]
    alerts: list[dict[str, Any]]
    recovery_actions: list[dict[str, Any]]


def _optional_uuid(value: str | None, *, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_{field}") from exc


@router.get("/trace", response_model=KriaTraceResponse)
async def get_kria_trace(
    db: Annotated[AsyncSession, Depends(get_db)],
    thread_id: str | None = Query(default=None),
    turn_id: str | None = Query(default=None),
) -> KriaTraceResponse:
    if (thread_id is None) == (turn_id is None):
        raise HTTPException(status_code=422, detail="provide_exactly_one_trace_identity")
    trace = await resolve_kria_trace(
        db,
        thread_id=_optional_uuid(thread_id, field="thread_id"),
        turn_id=_optional_uuid(turn_id, field="turn_id"),
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="kria_trace_not_found")
    return KriaTraceResponse(
        trace=trace,
        metrics=trace_metrics(trace),
        alerts=trace_alerts(trace),
        recovery_actions=reconciliation_actions(trace),
    )


@router.post("/reconcile/dry-run", response_model=KriaTraceResponse)
async def dry_run_kria_reconcile(
    db: Annotated[AsyncSession, Depends(get_db)],
    thread_id: str | None = Query(default=None),
    turn_id: str | None = Query(default=None),
) -> KriaTraceResponse:
    """Inspect exact recovery actions; deliberately performs no mutation or publish."""

    return await get_kria_trace(db=db, thread_id=thread_id, turn_id=turn_id)


class PhoneRenderConfigResponse(BaseModel):
    phone_rendering_enabled: bool
    montage_unified_plan_enabled: bool
    # The device-parity capability allowlist as this process reads it (a Fly secret in
    # prod). The code default is empty, so an empty list here means the secret is unset.
    verified_features: list[str]
    default_verified_features: list[str]
    # Capabilities a text layer can add on its own (animation phases, backgrounds,
    # caption pop, karaoke); a unified montage's labels need none of them (KRI-209).
    authored_text_verified: bool


@router.get("/phone-render-config", response_model=PhoneRenderConfigResponse)
async def get_phone_render_config() -> PhoneRenderConfigResponse:
    """Read-only: the phone-render feature allowlist the running API actually uses.

    Lets an operator confirm `authoredText` (and the rest) with
    `python scripts/admin.py --prod GET kria/phone-render-config` instead of `fly ssh`.
    Names only: capability strings, never a secret value.
    """
    default = Settings.model_fields["phone_render_verified_features"].get_default(
        call_default_factory=True
    )
    verified = sorted(str(feature) for feature in settings.phone_render_verified_features)
    return PhoneRenderConfigResponse(
        phone_rendering_enabled=bool(settings.phone_rendering_enabled),
        montage_unified_plan_enabled=bool(settings.montage_unified_plan_enabled),
        verified_features=verified,
        default_verified_features=sorted(default),
        authored_text_verified="authoredText" in verified,
    )
