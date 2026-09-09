"""POST /presigned-urls — batch presigned upload for template-mode clips.

Unlike POST /uploads/presigned (which creates a Job per file), this endpoint
returns signed PUT URLs without job creation. The frontend uploads all files
first, then creates one template job via POST /template-jobs.

Flow:
  [Client] POST /presigned-urls {files: [{filename, content_type, file_size_bytes}]}
           ──▶ validate each file (type, size, count ≤ 20)
           ──▶ generate signed GCS PUT URL per file
           ◀── {urls: [{upload_url, gcs_path}]}
"""

import uuid

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app import storage
from app.auth import CurrentUserOrSynthetic

log = structlog.get_logger()
router = APIRouter()

MAX_FILES_PER_BATCH = 20
MAX_BYTES_PER_FILE = 4 * 1024 * 1024 * 1024  # 4GB
ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime"}


class FileMetadata(BaseModel):
    filename: str
    content_type: str
    file_size_bytes: int


class BatchPresignedRequest(BaseModel):
    files: list[FileMetadata]


class PresignedUrlItem(BaseModel):
    upload_url: str
    gcs_path: str


class BatchPresignedResponse(BaseModel):
    urls: list[PresignedUrlItem]


@router.post("", response_model=BatchPresignedResponse, status_code=status.HTTP_200_OK)
async def create_batch_presigned(
    body: BatchPresignedRequest,
    current_user: CurrentUserOrSynthetic,
) -> BatchPresignedResponse:
    """Generate signed GCS PUT URLs for a batch of clip files.

    No job or DB row is created — this is purely for upload authorisation.
    The caller uploads directly to GCS, then creates a template job separately.
    """
    # ── Validate batch ──────────────────────────────────────────────────────
    if len(body.files) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one file is required",
        )

    if len(body.files) > MAX_FILES_PER_BATCH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Maximum {MAX_FILES_PER_BATCH} files per batch",
        )

    # ── Validate each file ──────────────────────────────────────────────────
    for i, f in enumerate(body.files):
        if f.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File {i}: unsupported content type '{f.content_type}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}",
            )
        if f.file_size_bytes > MAX_BYTES_PER_FILE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File {i}: exceeds 4GB limit ({f.file_size_bytes} bytes)",
            )
        if f.file_size_bytes <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File {i}: file_size_bytes must be positive",
            )

    # ── Generate signed URLs ────────────────────────────────────────────────
    user_id = str(current_user.id)
    batch_id = uuid.uuid4().hex[:12]

    try:
        urls: list[PresignedUrlItem] = []
        for i, f in enumerate(body.files):
            # Unattached uploads live only in the 24-hour lifecycle namespace.
            # POST /template-jobs promotes the exact generation after an owned
            # Job row exists.
            ext = f.filename.rsplit(".", 1)[-1] if "." in f.filename else "mp4"
            safe_ext = "mov" if ext.lower() == "mov" else "mp4"
            gcs_path = f"staging/{user_id}/batch-{batch_id}/clip_{i:03d}.{safe_ext}"
            upload_url = storage.signed_put_url_legacy(
                gcs_path,
                f.content_type,
                f.file_size_bytes,
            )
            urls.append(PresignedUrlItem(upload_url=upload_url, gcs_path=gcs_path))
    except Exception as exc:
        log.error("batch_presigned_failed", error=str(exc), batch_size=len(body.files))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload service unavailable — try again",
        ) from exc

    log.info("batch_presigned_created", batch_id=batch_id, file_count=len(urls))
    return BatchPresignedResponse(urls=urls)
