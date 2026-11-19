"""Admin fleet report across agents and models.

GET /admin/agents/report?days=14 — per agent and model: calls, settled cost, latency p50/p95,
the share of runs outside ``SUCCESS_OUTCOMES`` and the fallback share; chat-turn outcomes from
the edit receipts; and the paid-call ledger by provider and feature. See
``app.services.fleet_report`` for what each section covers.

Auth: X-Admin-Token header (same gate as the rest of admin.py). Via the CLI:
``python scripts/admin.py --prod GET agents/report``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.routes.admin import _require_admin
from app.services.fleet_report import FleetReport, build_fleet_report

router = APIRouter()


@router.get("/report", response_model=FleetReport)
async def fleet_report(
    days: int = Query(14, ge=1, le=90),
    environment: str | None = Query(None, max_length=40),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> FleetReport:
    return await build_fleet_report(
        db,
        days=days,
        environment=environment,
        retention_days=settings.agent_run_retention_days,
    )
