"""Celery tasks: import_from_drive, batch_import_from_drive.

Downloads video files from Google Drive to GCS using an OAuth access token.
The token is Fernet-encrypted in Celery task args (never plaintext in Redis).

Single import flow:
  import_from_drive(job_id, drive_file_id, encrypted_token, gcs_path)
    → decrypt token → stream download → ffprobe validate → upload GCS
    → set status "queued" → enqueue orchestrate_job

Batch import flow (for template mode):
  batch_import_from_drive(batch_id, files_meta, encrypted_token, gcs_paths)
    → decrypt token → download each file → upload each to GCS
    → write progress to Redis per file
"""

import json
import os
import subprocess
import tempfile
import time
import uuid

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job
from app.worker import celery_app

log = structlog.get_logger()

DRIVE_DOWNLOAD_URL = "https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
DRIVE_METADATA_URL = "https://www.googleapis.com/drive/v3/files/{file_id}?fields=size,mimeType,name"

# httpx timeout: read=60s to handle large file streaming, connect=10s
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=None, pool=10.0)

# Progress update interval (seconds)
PROGRESS_INTERVAL_S = 5

# Max retries for 5xx errors
MAX_RETRIES = 1
RETRY_DELAY_S = 5


def _get_redis():
    """Get a Redis client from the Celery broker connection."""
    import redis

    return redis.from_url(settings.redis_url)


def _decrypt_token(encrypted_token: str) -> str:
    """Decrypt a Fernet-encrypted Google access token.

    Uses ttl=3600 so tokens older than 1 hour are rejected (defense-in-depth).
    """
    f = Fernet(settings.token_encryption_key.encode())
    try:
        return f.decrypt(encrypted_token.encode(), ttl=3600).decode()
    except InvalidToken as exc:
        raise ValueError("Google access token expired or invalid") from exc


def _compress_for_testing(input_path: str, output_path: str) -> None:
    """Transcode video to 720p for faster testing. ~10x smaller files."""
    result = subprocess.run(
        [
            "ffmpeg", "-i", input_path,
            "-vf", "scale=-2:720",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", output_path,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise ValueError(f"Compression failed: {result.stderr.strip()[:200]}")


def _ffprobe_duration(filepath: str) -> float:
    """Run ffprobe to get video duration in seconds."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            filepath,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed: {result.stderr.strip()}")
    return float(result.stdout.strip())


def _stream_download(
    access_token: str,
    file_id: str,
    dest_path: str,
    total_bytes: int,
    redis_key: str | None = None,
) -> None:
    """Stream download a file from Google Drive to a local path.

    Updates progress in Redis every PROGRESS_INTERVAL_S seconds.
    Retries once on 5xx errors.
    """
    url = DRIVE_DOWNLOAD_URL.format(file_id=file_id)
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.stream(
                "GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as resp:
                if resp.status_code == 401:
                    raise ValueError("Google Drive access denied — token may have expired")
                if resp.status_code == 403:
                    raise ValueError(
                        "Google Drive access denied — you may not have permission to this file"
                    )
                if resp.status_code == 404:
                    raise ValueError(
                        "File not found on Google Drive — it may have been deleted or moved"
                    )
                resp.raise_for_status()

                downloaded = 0
                last_progress_time = time.monotonic()

                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):  # 1MB chunks
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Throttled progress updates to Redis
                        if (
                            redis_key
                            and (time.monotonic() - last_progress_time) >= PROGRESS_INTERVAL_S
                        ):
                            pct = (
                                round((downloaded / total_bytes) * 100, 1) if total_bytes > 0 else 0
                            )
                            r = _get_redis()
                            r.set(
                                redis_key,
                                json.dumps({"progress_pct": pct, "bytes_downloaded": downloaded}),
                                ex=3600,
                            )
                            last_progress_time = time.monotonic()

            return  # success
        except httpx.ReadTimeout:
            if attempt < MAX_RETRIES:
                log.warning("drive_download_timeout_retry", attempt=attempt)
                time.sleep(RETRY_DELAY_S)
                continue
            raise ValueError(
                "Google Drive download timed out. "
                "File may be too large or connection too slow."
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500 and attempt < MAX_RETRIES:
                log.warning(
                    "drive_download_5xx_retry", status=exc.response.status_code, attempt=attempt
                )
                time.sleep(RETRY_DELAY_S)
                continue
            raise


def _upload_to_gcs(local_path: str, gcs_path: str, content_type: str = "video/mp4") -> None:
    """Upload a local file to GCS (same pattern as storage.upload_public_read)."""
    from app.storage import _get_client

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(gcs_path)
    blob.upload_from_filename(local_path, content_type=content_type)


def _cleanup_gcs_blob(gcs_path: str) -> None:
    """Attempt to delete a GCS blob (best-effort cleanup on failure)."""
    try:
        from app.storage import _get_client

        bucket = _get_client().bucket(settings.storage_bucket)
        blob = bucket.blob(gcs_path)
        if blob.exists():
            blob.delete()
    except Exception:
        log.warning("gcs_cleanup_failed", gcs_path=gcs_path)


@celery_app.task(
    name="tasks.import_from_drive", bind=True, max_retries=0, time_limit=1200, soft_time_limit=1140
)
def import_from_drive(
    self,
    job_id: str,
    drive_file_id: str,
    encrypted_token: str,
    gcs_path: str,
    compress: bool = False,
) -> None:
    """Download a single file from Google Drive to GCS, then enqueue orchestrate_job."""
    redis_key = f"import:progress:{job_id}"
    log.info("drive_import_start", job_id=job_id, drive_file_id=drive_file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "raw.mp4")
        try:
            # Decrypt the access token
            access_token = _decrypt_token(encrypted_token)

            # Get declared file size from job metadata for progress tracking
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job is None:
                    log.error("job_not_found", job_id=job_id)
                    return
                declared_size = (job.probe_metadata or {}).get("drive_file_size_bytes", 0)

            # Stream download from Drive
            _stream_download(access_token, drive_file_id, local_path, declared_size, redis_key)

            # Validate with ffprobe: check duration and format
            duration = _ffprobe_duration(local_path)
            if duration > settings.max_duration_s:
                raise ValueError(
                    f"Video is {duration:.0f}s long, exceeds {settings.max_duration_s:.0f}s limit. "
                    "Try a shorter video."
                )

            actual_size = os.path.getsize(local_path)
            if actual_size > settings.max_upload_bytes:
                raise ValueError(
                    f"Downloaded file is {actual_size / (1024**3):.1f} GB, "
                    f"exceeds {settings.max_upload_bytes / (1024**3):.0f} GB limit."
                )

            # Compress for testing (720p, ~10x smaller)
            if compress:
                compressed_path = os.path.join(tmpdir, "compressed.mp4")
                log.info("drive_import_compressing", job_id=job_id)
                _compress_for_testing(local_path, compressed_path)
                os.remove(local_path)
                os.rename(compressed_path, local_path)
                log.info(
                    "drive_import_compressed",
                    job_id=job_id,
                    size_mb=round(os.path.getsize(local_path) / (1024**2)),
                )

            # Upload to GCS
            log.info("drive_import_uploading_gcs", job_id=job_id, gcs_path=gcs_path)
            _upload_to_gcs(local_path, gcs_path)

            # Atomic handoff: set status + enqueue orchestrate in one try block
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job is None:
                    return
                job.status = "queued"
                db.commit()

                from app.tasks.orchestrate import orchestrate_job

                orchestrate_job.apply_async(args=[job_id], task_id=job_id)

            log.info("drive_import_complete", job_id=job_id)

        except Exception as exc:
            log.error("drive_import_failed", job_id=job_id, error=str(exc))
            _cleanup_gcs_blob(gcs_path)
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job is not None:
                    job.status = "processing_failed"
                    job.error_detail = str(exc)[:500]
                    db.commit()
        finally:
            # Always clean up Redis progress key
            try:
                r = _get_redis()
                r.delete(redis_key)
            except Exception:
                pass


@celery_app.task(
    name="tasks.batch_import_from_drive",
    bind=True,
    max_retries=0,
    time_limit=2400,
    soft_time_limit=2340,
)
def batch_import_from_drive(
    self,
    batch_id: str,
    files_meta: list[dict],
    encrypted_token: str,
    gcs_paths: list[str],
    compress: bool = False,
) -> None:
    """Download multiple files from Google Drive to GCS (for template mode).

    Progress and final status are stored in Redis key batch:progress:{batch_id}.
    The frontend polls this key via GET /uploads/drive-import-batch/{batch_id}/status.
    """
    redis_key = f"batch:progress:{batch_id}"
    log.info("batch_import_start", batch_id=batch_id, file_count=len(files_meta))

    r = _get_redis()
    completed_paths: list[str] = []
    errors: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            access_token = _decrypt_token(encrypted_token)
        except ValueError as exc:
            r.set(
                redis_key,
                json.dumps(
                    {
                        "status": "failed",
                        "total": len(files_meta),
                        "completed": 0,
                        "current_file": None,
                        "gcs_paths": [],
                        "errors": [str(exc)],
                    }
                ),
                ex=3600,
            )
            return

        total = len(files_meta)

        for i, (file_meta, gcs_path) in enumerate(zip(files_meta, gcs_paths)):
            filename = file_meta.get("filename", f"clip_{i:03d}.mp4")
            file_id = file_meta["drive_file_id"]
            declared_size = file_meta.get("file_size_bytes", 0)
            local_path = os.path.join(tmpdir, f"clip_{i:03d}.mp4")

            # Update progress
            r.set(
                redis_key,
                json.dumps(
                    {
                        "status": "importing",
                        "total": total,
                        "completed": len(completed_paths),
                        "current_file": filename,
                        "gcs_paths": completed_paths,
                        "errors": errors,
                    }
                ),
                ex=3600,
            )

            try:
                _stream_download(access_token, file_id, local_path, declared_size)

                # Validate duration
                duration = _ffprobe_duration(local_path)
                if duration > settings.max_duration_s:
                    raise ValueError(f"{filename}: video is {duration:.0f}s, exceeds limit")

                # Compress for testing (720p, ~10x smaller)
                if compress:
                    compressed_path = os.path.join(tmpdir, f"compressed_{i:03d}.mp4")
                    _compress_for_testing(local_path, compressed_path)
                    os.remove(local_path)
                    os.rename(compressed_path, local_path)

                actual_size = os.path.getsize(local_path)
                if actual_size > settings.max_upload_bytes:
                    raise ValueError(
                        f"{filename}: file too large ({actual_size / (1024**3):.1f} GB)"
                    )

                _upload_to_gcs(local_path, gcs_path)
                completed_paths.append(gcs_path)
                log.info(
                    "batch_file_imported",
                    batch_id=batch_id,
                    file=filename,
                    index=i + 1,
                    total=total,
                )

            except Exception as exc:
                log.error("batch_file_failed", batch_id=batch_id, file=filename, error=str(exc))
                errors.append(f"{filename}: {str(exc)[:200]}")
                _cleanup_gcs_blob(gcs_path)

            finally:
                # Clean up local temp file for this clip
                if os.path.exists(local_path):
                    os.remove(local_path)

        # Write final status
        if len(completed_paths) == total:
            final_status = "complete"
        elif len(completed_paths) > 0:
            final_status = "partial_failure"
        else:
            final_status = "failed"

        r.set(
            redis_key,
            json.dumps(
                {
                    "status": final_status,
                    "total": total,
                    "completed": len(completed_paths),
                    "current_file": None,
                    "gcs_paths": completed_paths,
                    "errors": errors,
                }
            ),
            ex=3600,
        )

        log.info(
            "batch_import_complete",
            batch_id=batch_id,
            status=final_status,
            completed=len(completed_paths),
            total=total,
        )
