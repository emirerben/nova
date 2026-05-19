"""Clip-level endpoints — currently just pre-emptive analysis.

POST /clips/prefetch-analyze
    Fire-and-forget: the frontend hits this immediately after each clip's
    presigned PUT completes. The server downloads, hashes, Gemini-uploads,
    and analyses the clip, writing the result into the same Redis cache
    that `_analyze_clips_with_cache` reads during orchestration. On submit
    the orchestrator finds a warm entry and skips Gemini entirely.

    The endpoint returns 202 within milliseconds — the work runs in the
    background. Failures are silent (best-effort by contract; the
    orchestrator does the same work if the cache is cold).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.limiter import limiter
from app.models import VideoTemplate
from app.routes.waitlist import get_real_ip
from app.services.clip_prefetch import (
    in_flight_count,
    is_valid_prefetch_path,
    schedule_prefetch,
)

log = structlog.get_logger()
router = APIRouter()


class PrefetchAnalyzeRequest(BaseModel):
    # GCS object path produced by /presigned-urls or the Drive batch import.
    # Validated against a strict regex in the service layer so a caller
    # can't trick us into downloading arbitrary objects from our bucket.
    gcs_path: str = Field(..., min_length=1, max_length=512)
    # Template the user is currently viewing. We use the template's
    # filter_hint as part of the cache key — clips analysed for template A
    # are NOT reusable for template B because best_moments may emphasise
    # different visual cues per template. Match the orchestrator's key
    # exactly so the warm entry actually hits at submit time.
    template_id: str = Field(..., min_length=1, max_length=128)


class PrefetchAnalyzeResponse(BaseModel):
    status: str
    # Whether this exact gcs_path is already being prefetched in this
    # process. Useful for the frontend to suppress its retry-with-backoff
    # loop on duplicate fires (e.g. user double-clicks Generate).
    duplicate: bool = False


@router.post(
    "/prefetch-analyze",
    response_model=PrefetchAnalyzeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("60/minute", key_func=get_real_ip)
async def prefetch_analyze(
    request: Request,
    body: PrefetchAnalyzeRequest,
    db: AsyncSession = Depends(get_db),
) -> PrefetchAnalyzeResponse:
    """Schedule pre-emptive Gemini analysis for a freshly-uploaded clip.

    Rate limit: 60/min/IP. A typical 20-clip job fires 20 calls in a
    burst of seconds — well under. The cap is for abuse, not real usage.
    """
    # Whitelist-validate the path BEFORE any DB or scheduling work — cheap
    # rejection of obviously-bad input. If a caller can spam invalid paths,
    # they should hit the rate limit fast, not exhaust DB pool slots.
    if not is_valid_prefetch_path(body.gcs_path):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="gcs_path must be a valid upload-batch path",
        )

    # Look up template's filter_hint. A non-existent template is a hard
    # error — the frontend should never send a stale id. We use the recipe's
    # clip_filter_hint with the same "" default the orchestrator uses
    # (template_orchestrate.py: `getattr(recipe, "clip_filter_hint", "") or ""`).
    result = await db.execute(
        select(VideoTemplate).where(VideoTemplate.id == body.template_id)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )

    recipe = template.recipe_cached or {}
    filter_hint = (recipe.get("clip_filter_hint") or "") if isinstance(recipe, dict) else ""

    # Mixed-media templates (photo slots) skip Gemini at orchestrate time,
    # so prefetching them is pure waste. Don't enqueue.
    slots = recipe.get("slots", []) if isinstance(recipe, dict) else []
    if any(s.get("media_type") == "photo" for s in slots if isinstance(s, dict)):
        log.info(
            "prefetch_skip_mixed_media_template",
            template_id=body.template_id,
            gcs_path=body.gcs_path,
        )
        return PrefetchAnalyzeResponse(status="skipped_mixed_media")

    scheduled = await schedule_prefetch(body.gcs_path, filter_hint)
    log.info(
        "prefetch_enqueued",
        gcs_path=body.gcs_path,
        template_id=body.template_id,
        scheduled=scheduled,
        in_flight=in_flight_count(),
    )
    return PrefetchAnalyzeResponse(
        status="enqueued" if scheduled else "duplicate",
        duplicate=not scheduled,
    )
