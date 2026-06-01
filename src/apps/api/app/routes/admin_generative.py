"""Admin endpoint for the generative-edits overview.

GET /admin/generative — recent generative jobs with a per-variant summary.

Generative jobs are plain ``Job`` rows (``mode == "generative"``) whose per-variant
render state lives in ``Job.assembly_plan["variants"]`` — the generic /admin/jobs list
defers that JSONB, so this tailored endpoint materializes it (plus the clip set from
``all_candidates``) to drive the dedicated /admin/generative dashboard. Detail/analysis
(variant tiles, agent runs, pipeline trace) is handled by the existing
/admin/jobs/{id}/debug view, which this list links into.

Auth: X-Admin-Token header (same gate as the rest of admin.py).
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job
from app.routes.admin import _require_admin

log = structlog.get_logger()

router = APIRouter()


class AdminGenerativeVariant(BaseModel):
    variant_id: str
    text_mode: str | None = None
    track_title: str | None = None
    render_status: str | None = None
    ok: bool | None = None
    error: str | None = None
    # The archetype that actually rendered this variant (Lane D). None on montage
    # variants (the default path doesn't stamp it).
    resolved_archetype: str | None = None


class AdminGenerativeListItem(BaseModel):
    job_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    error_detail: str | None = None
    clip_count: int
    variants: list[AdminGenerativeVariant]
    # Plan-declared format vs what actually rendered. A mismatch (e.g. declared
    # talking_head, resolved montage) is the at-a-glance signal that dispatch fell
    # back — the trace event carries the reason.
    edit_format: str | None = None
    resolved_archetype: str | None = None


class AdminGenerativeListResponse(BaseModel):
    items: list[AdminGenerativeListItem]
    total: int


def _variant_summaries(job: Job) -> list[AdminGenerativeVariant]:
    """Per-variant rows from assembly_plan, defensively filtered.

    Mirrors the guard the detail page uses: a mid-flight job can carry a partial or
    odd assembly_plan, so only entries that are dicts with a usable variant_id count.
    """
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    raw = plan.get("variants")
    if not isinstance(raw, list):
        return []
    out: list[AdminGenerativeVariant] = []
    for v in raw:
        if not isinstance(v, dict) or not isinstance(v.get("variant_id"), str):
            continue
        out.append(
            AdminGenerativeVariant(
                variant_id=v["variant_id"],
                text_mode=v.get("text_mode"),
                track_title=v.get("track_title"),
                render_status=v.get("render_status"),
                ok=v.get("ok"),
                error=v.get("error"),
                resolved_archetype=v.get("resolved_archetype"),
            )
        )
    return out


def _clip_count(job: Job) -> int:
    cand = job.all_candidates if isinstance(job.all_candidates, dict) else {}
    paths = cand.get("clip_paths")
    return len(paths) if isinstance(paths, list) else 0


def _declared_edit_format(job: Job) -> str | None:
    cand = job.all_candidates if isinstance(job.all_candidates, dict) else {}
    fmt = cand.get("edit_format")
    return fmt if isinstance(fmt, str) else None


def _resolved_archetype(job: Job) -> str | None:
    """The archetype that rendered this job — the first variant that stamped one."""
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    raw = plan.get("variants")
    if not isinstance(raw, list):
        return None
    for v in raw:
        if isinstance(v, dict) and isinstance(v.get("resolved_archetype"), str):
            return v["resolved_archetype"]
    return None


@router.get("", response_model=AdminGenerativeListResponse)
async def list_generative_jobs(
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> AdminGenerativeListResponse:
    """Recent generative jobs (newest first) with a per-variant summary."""
    result = await db.execute(
        select(Job).where(Job.mode == "generative").order_by(Job.created_at.desc()).limit(limit)
    )
    jobs = result.scalars().all()

    items: list[AdminGenerativeListItem] = []
    for job in jobs:
        items.append(
            AdminGenerativeListItem(
                job_id=str(job.id),
                status=job.status,
                created_at=job.created_at,
                updated_at=job.updated_at,
                error_detail=job.error_detail,
                clip_count=_clip_count(job),
                variants=_variant_summaries(job),
                edit_format=_declared_edit_format(job),
                resolved_archetype=_resolved_archetype(job),
            )
        )
    return AdminGenerativeListResponse(items=items, total=len(items))
