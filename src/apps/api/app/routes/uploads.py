"""Upload endpoints: presigned URLs, Google Drive import (single + batch)."""

import json
import re
import uuid

import structlog
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.database import get_db
from app.models import Job

log = structlog.get_logger()
router = APIRouter()

ALLOWED_ASPECT_RATIOS = {"16:9", "9:16"}

ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/x-msvideo"}

# Drive imports also accept application/octet-stream (Drive may report this for valid videos)
DRIVE_ALLOWED_MIME = ALLOWED_CONTENT_TYPES | {"application/octet-stream"}

# Google Drive file IDs: alphanumeric + hyphen/underscore, typically 20-50 chars
DRIVE_FILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{10,60}$")


_fernet: Fernet | None = None
_fernet_valid: bool | None = None


def _validate_fernet_key() -> bool:
    """Check if the token_encryption_key is a valid Fernet key. Cached after first call."""
    global _fernet, _fernet_valid
    if _fernet_valid is not None:
        return _fernet_valid
    if not settings.token_encryption_key:
        _fernet_valid = False
        return False
    try:
        _fernet = Fernet(settings.token_encryption_key.encode())
        _fernet_valid = True
        return True
    except (ValueError, Exception):
        _fernet_valid = False
        return False


def _encrypt_token(token: str) -> str:
    """Encrypt a Google access token with Fernet for safe storage in Redis."""
    if _fernet is None:
        _validate_fernet_key()
    if _fernet is None:
        raise ValueError("Fernet key not configured")
    return _fernet.encrypt(token.encode()).decode()


_redis_client_uploads = None


def _get_redis():
    """Get a shared Redis client."""
    global _redis_client_uploads
    if _redis_client_uploads is None:
        import redis

        _redis_client_uploads = redis.from_url(settings.redis_url)
    return _redis_client_uploads


ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "webm", "mkv"}


class PresignedRequest(BaseModel):
    filename: str
    file_size_bytes: int
    duration_s: float
    aspect_ratio: str  # "16:9" | "9:16"
    platforms: list[str]
    content_type: str = "video/mp4"


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
            detail="Only 16:9 (landscape) and 9:16 (vertical) aspect ratios are supported in v1. Use landscape or vertical video.",  # noqa: E501
        )

    allowed_platforms = {"instagram", "youtube", "tiktok"}
    invalid = set(body.platforms) - allowed_platforms
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown platforms: {invalid}",
        )

    if body.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported content type: {body.content_type}",
        )

    # TODO: auth — replace with real user_id from JWT
    user_id = "dev-user"

    job_id = str(uuid.uuid4())

    try:
        upload_url, gcs_path = storage.presigned_put_url(
            user_id, job_id, content_type=body.content_type
        )  # noqa: E501
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


# ── Google Drive Import (single file) ─────────────────────────────────────────


class DriveImportRequest(BaseModel):
    drive_file_id: str
    filename: str
    file_size_bytes: int
    compress: bool = False  # Compress to 720p for faster testing
    mime_type: str
    platforms: list[str]
    google_access_token: str

    @field_validator("drive_file_id")
    @classmethod
    def validate_drive_file_id(cls, v: str) -> str:
        if not DRIVE_FILE_ID_PATTERN.match(v):
            raise ValueError("Invalid Google Drive file ID format")
        return v


class DriveImportResponse(BaseModel):
    job_id: str
    status: str


@router.post(
    "/drive-import", response_model=DriveImportResponse, status_code=status.HTTP_202_ACCEPTED
)
async def import_from_drive(
    body: DriveImportRequest,
    db: AsyncSession = Depends(get_db),
) -> DriveImportResponse:
    """Import a video from Google Drive. The file is downloaded server-side to GCS."""
    if not _validate_fernet_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Drive import not configured — token_encryption_key is missing or invalid",
        )

    # Compression shrinks ~10x, so allow larger inputs when enabled
    size_limit = settings.max_upload_bytes * 10 if body.compress else settings.max_upload_bytes
    if body.file_size_bytes > size_limit:
        limit_gb = size_limit // (1024**3)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File exceeds {limit_gb}GB limit ({body.file_size_bytes} bytes)",
        )

    if body.mime_type not in DRIVE_ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported file type: {body.mime_type}. Supported: MP4, MOV, AVI.",
        )

    allowed_platforms = {"instagram", "youtube", "tiktok"}
    invalid = set(body.platforms) - allowed_platforms
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown platforms: {invalid}",
        )

    if not body.platforms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one platform must be selected",
        )

    # TODO: auth — replace with real user_id from JWT
    user_id = "dev-user"
    job_id = str(uuid.uuid4())
    gcs_path = f"{user_id}/{job_id}/raw.mp4"

    # Encrypt the access token before it touches Redis
    encrypted_token = _encrypt_token(body.google_access_token)

    job = Job(
        id=uuid.UUID(job_id),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        status="importing",
        raw_storage_path=gcs_path,
        selected_platforms=body.platforms,
        probe_metadata={
            "drive_filename": body.filename,
            "drive_file_size_bytes": body.file_size_bytes,
        },
    )
    db.add(job)
    await db.commit()

    try:
        from app.tasks.drive_import import import_from_drive as import_task

        import_task.apply_async(
            args=[job_id, body.drive_file_id, encrypted_token, gcs_path],
            kwargs={"compress": body.compress},
        )
    except Exception as exc:
        # If Celery enqueue fails, mark job as failed so it doesn't stay stuck in "importing"
        job.status = "processing_failed"
        job.error_detail = f"Failed to start import: {str(exc)[:200]}"
        await db.commit()
        log.error("drive_import_enqueue_failed", job_id=job_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Import service unavailable — try again",
        ) from exc

    log.info("drive_import_enqueued", job_id=job_id, filename=body.filename)
    return DriveImportResponse(job_id=job_id, status="importing")


# ── Google Drive Import (batch, for template mode) ────────────────────────────


class DriveFileInfo(BaseModel):
    drive_file_id: str
    filename: str
    file_size_bytes: int
    mime_type: str

    @field_validator("drive_file_id")
    @classmethod
    def validate_drive_file_id(cls, v: str) -> str:
        if not DRIVE_FILE_ID_PATTERN.match(v):
            raise ValueError("Invalid Google Drive file ID format")
        return v


class DriveImportBatchRequest(BaseModel):
    files: list[DriveFileInfo]
    google_access_token: str


class DriveImportBatchResponse(BaseModel):
    batch_id: str
    gcs_paths: list[str]
    status: str


class DriveImportBatchStatus(BaseModel):
    batch_id: str
    status: str  # importing | complete | partial_failure | failed
    total: int
    completed: int
    current_file: str | None
    gcs_paths: list[str]
    errors: list[str]


@router.post(
    "/drive-import-batch",
    response_model=DriveImportBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_batch_from_drive(
    body: DriveImportBatchRequest,
) -> DriveImportBatchResponse:
    """Import multiple files from Google Drive (for template mode clip uploads)."""
    if not _validate_fernet_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Drive import not configured — token_encryption_key is missing or invalid",
        )

    if len(body.files) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one file required",
        )

    if len(body.files) > 20:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Maximum 20 files per batch",
        )

    for f in body.files:
        if f.file_size_bytes > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{f.filename} exceeds {settings.max_upload_bytes // (1024**3)}GB limit",
            )
        if f.mime_type not in DRIVE_ALLOWED_MIME:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{f.filename}: unsupported file type {f.mime_type}",
            )

    # TODO: auth — replace with real user_id from JWT
    user_id = "dev-user"
    batch_id = str(uuid.uuid4())

    # Generate GCS paths (same pattern as batch presigned endpoint)
    gcs_paths = []
    for i, f in enumerate(body.files):
        raw_ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "mp4"
        ext = raw_ext if raw_ext in ALLOWED_EXTENSIONS else "mp4"
        gcs_paths.append(f"{user_id}/batch-{batch_id}/clip_{i:03d}.{ext}")

    encrypted_token = _encrypt_token(body.google_access_token)

    files_meta = [f.model_dump() for f in body.files]

    from app.tasks.drive_import import batch_import_from_drive

    batch_import_from_drive.apply_async(args=[batch_id, files_meta, encrypted_token, gcs_paths])

    log.info("batch_drive_import_enqueued", batch_id=batch_id, file_count=len(body.files))
    return DriveImportBatchResponse(batch_id=batch_id, gcs_paths=gcs_paths, status="importing")


@router.get("/drive-import-batch/{batch_id}/status", response_model=DriveImportBatchStatus)
async def get_batch_import_status(batch_id: str) -> DriveImportBatchStatus:
    """Poll the status of a batch Drive import."""
    # Validate batch_id is a UUID to prevent arbitrary Redis key access
    try:
        uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid batch ID format")

    r = _get_redis()
    raw = r.get(f"batch:progress:{batch_id}")

    if raw is None:
        raise HTTPException(status_code=404, detail="Batch import not found or expired")

    data = json.loads(raw)
    return DriveImportBatchStatus(
        batch_id=batch_id,
        status=data.get("status", "importing"),
        total=data.get("total", 0),
        completed=data.get("completed", 0),
        current_file=data.get("current_file"),
        gcs_paths=data.get("gcs_paths", []),
        errors=data.get("errors", []),
    )
