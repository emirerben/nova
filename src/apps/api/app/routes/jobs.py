"""POST /jobs, GET /jobs/:id/status"""

import json
import uuid

import redis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Job, JobClip
from app.tasks.orchestrate import orchestrate_job

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _redis_client


log = structlog.get_logger()
router = APIRouter()


class CreateJobRequest(BaseModel):
    job_id: str  # from /uploads/presigned response
    raw_storage_path: str
    platforms: list[str]


class ClipStatus(BaseModel):
    id: str
    rank: int
    hook_score: float
    engagement_score: float
    combined_score: float
    start_s: float
    end_s: float
    hook_text: str | None
    render_status: str
    video_path: str | None
    thumbnail_path: str | None
    duration_s: float | None
    platform_copy: dict | None
    copy_status: str
    post_status: dict | None


class JobStatusResponse(BaseModel):
    id: str
    status: str
    clips: list[ClipStatus]
    error_detail: str | None
    created_at: str
    updated_at: str
    # Drive import progress (only present when status == "importing")
    import_progress_pct: float | None = None
    drive_filename: str | None = None
    drive_file_size_bytes: int | None = None


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(
    body: CreateJobRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Job).where(Job.id == uuid.UUID(body.job_id)))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "queued":
        raise HTTPException(status_code=409, detail=f"Job already in state: {job.status}")

    # Enqueue Celery task
    orchestrate_job.apply_async(args=[str(job.id)], task_id=str(job.id))

    log.info("job_enqueued", job_id=str(job.id))
    return {"job_id": str(job.id), "status": "queued"}


@router.get("/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    result = await db.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    clips_result = await db.execute(
        select(JobClip).where(JobClip.job_id == uuid.UUID(job_id)).order_by(JobClip.rank)
    )
    clips = clips_result.scalars().all()

    # Only return top-3 ranked clips to the client (hold 4-9 for re-roll)
    visible_clips = [c for c in clips if c.rank <= 3]

    # Read Drive import progress from Redis (only when importing)
    import_progress_pct = None
    drive_filename = None
    drive_file_size_bytes = None

    if job.status == "importing":
        try:
            r = _get_redis()
            raw = r.get(f"import:progress:{job_id}")
            if raw:
                progress = json.loads(raw)
                import_progress_pct = progress.get("progress_pct")
        except Exception:
            pass  # Redis unavailable — progress just won't show

        if job.probe_metadata:
            drive_filename = job.probe_metadata.get("drive_filename")
            drive_file_size_bytes = job.probe_metadata.get("drive_file_size_bytes")

    return JobStatusResponse(
        id=str(job.id),
        status=job.status,
        clips=[
            ClipStatus(
                id=str(c.id),
                rank=c.rank,
                hook_score=c.hook_score,
                engagement_score=c.engagement_score,
                combined_score=c.combined_score,
                start_s=c.start_s,
                end_s=c.end_s,
                hook_text=c.hook_text,
                render_status=c.render_status,
                video_path=c.video_path,
                thumbnail_path=c.thumbnail_path,
                duration_s=c.duration_s,
                platform_copy=c.platform_copy,
                copy_status=c.copy_status,
                post_status=c.post_status,
            )
            for c in visible_clips
        ],
        error_detail=job.error_detail,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        import_progress_pct=import_progress_pct,
        drive_filename=drive_filename,
        drive_file_size_bytes=drive_file_size_bytes,
    )
