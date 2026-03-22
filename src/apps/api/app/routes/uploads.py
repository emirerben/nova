"""POST /uploads/presigned — returns a GCS signed PUT URL for client-side direct upload."""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.database import get_db
from app.models import Job

log = structlog.get_logger()
router = APIRouter()

ALLOWED_ASPECT_RATIOS = {"16:9", "9:16"}


class PresignedRequest(BaseModel):
    filename: str
    file_size_bytes: int
    duration_s: float
    aspect_ratio: str  # "16:9" | "9:16"
    platforms: list[str]


class PresignedResponse(BaseModel):
    upload_url: str
    job_id: str
    gcs_path: str


@router.post("/presigned", response_model=PresignedResponse, status_code=status.HTTP_201_CREATED)
async def create_presigned_upload(
    request: Request,
    body: PresignedRequest,
    db: AsyncSession = Depends(get_db),
) -> PresignedResponse:
    # Validation happens here — before the client wastes bandwidth
    max_bytes = 4 * 1024 * 1024 * 1024
    if body.file_size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File exceeds 4GB limit ({body.file_size_bytes} bytes)",
        )

    if body.duration_s > 1800:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Video exceeds 30-minute limit",
        )

    if body.aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only 16:9 (landscape) and 9:16 (vertical) aspect ratios are supported in v1. Use landscape or vertical video.",
        )

    allowed_platforms = {"instagram", "youtube", "tiktok"}
    invalid = set(body.platforms) - allowed_platforms
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown platforms: {invalid}",
        )

    # TODO: auth — replace with real user_id from JWT
    user_id = "dev-user"

    job_id = str(uuid.uuid4())

    try:
        upload_url, gcs_path = storage.presigned_put_url(user_id, job_id)
    except Exception as exc:
        log.error("presigned_url_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload service unavailable — try again",
        ) from exc

    job = Job(
        id=uuid.UUID(job_id),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),  # TODO: real user
        status="queued",
        raw_storage_path=gcs_path,
        selected_platforms=body.platforms,
    )
    db.add(job)
    await db.commit()

    log.info("presigned_url_created", job_id=job_id, platforms=body.platforms)
    return PresignedResponse(upload_url=upload_url, job_id=job_id, gcs_path=gcs_path)
