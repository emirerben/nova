"""Normalization and ownership checks for persisted job storage paths.

Keep these checks outside route modules so background jobs and maintenance
scripts can enforce the same ownership boundary without importing the API
surface (or creating a route/service import cycle).
"""

from __future__ import annotations

import uuid
from urllib.parse import unquote, urlparse

from app.config import settings
from app.models import Job

JOB_OUTPUT_PREFIXES = (
    "generative-jobs/{job_id}/",
    "jobs/{job_id}/",
    "music-jobs/{job_id}/",
    "auto-music-jobs/{job_id}/",
)


def normalize_job_storage_path(path: object) -> str | None:
    """Normalize a stored object key or an owned GCS browser URL."""
    if not isinstance(path, str):
        return None
    candidate = path.strip().lstrip("/")
    if "://" in candidate:
        parsed = urlparse(candidate)
        bucket_prefix = f"/{settings.storage_bucket}/"
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"storage.googleapis.com", "storage.cloud.google.com"}
            or not parsed.path.startswith(bucket_prefix)
        ):
            return None
        candidate = unquote(parsed.path[len(bucket_prefix) :]).lstrip("/")
    if not candidate or ".." in candidate.split("/"):
        return None
    return candidate


def job_output_path(path: object, job_id: uuid.UUID) -> str | None:
    """Return an object key only when it is under a job output prefix."""
    candidate = normalize_job_storage_path(path)
    if candidate is None:
        return None
    if any(candidate.startswith(prefix.format(job_id=job_id)) for prefix in JOB_OUTPUT_PREFIXES):
        return candidate
    return None


def owned_job_output_path(path: object, job: Job) -> str | None:
    """Return a normalized output key only when it belongs to ``job``.

    Most newer pipelines use an explicit mode prefix. The default/auto
    uploader historically used ``{user_id}/{job_id}/...``; accept that exact
    prefix so those rows remain readable and deletable without granting access
    to a broader user prefix.
    """
    candidate = normalize_job_storage_path(path)
    if candidate is None:
        return None
    if job_output_path(candidate, job.id) is not None:
        return candidate
    if candidate.startswith(f"{job.user_id}/{job.id}/"):
        return candidate
    return None
