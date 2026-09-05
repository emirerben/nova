"""Normalization and ownership checks for persisted job storage paths.

Keep these checks outside route modules so background jobs and maintenance
scripts can enforce the same ownership boundary without importing the API
surface (or creating a route/service import cycle).
"""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import unquote, urlparse

from app.config import settings
from app.models import Job

# Job-scoped, lifecycle-exempt home for extracted posters. Named here rather
# than in services/template_poster.py so cleanup paths can bound a delete to
# this prefix without importing the ffmpeg/storage-heavy poster module.
JOB_POSTER_PATH_PREFIX = "job-posters/{job_id}/"

JOB_OUTPUT_PREFIXES = (
    "generative-jobs/{job_id}/",
    "jobs/{job_id}/",
    "music-jobs/{job_id}/",
    "auto-music-jobs/{job_id}/",
    # Posters live off the video prefixes so a GCS lifecycle rule written for
    # the source video cannot delete the thumbnail (see JOB_POSTER_PREFIX in
    # services/template_poster.py). Legacy sibling poster keys stay readable
    # through the video prefixes above; the stored path is authoritative.
    JOB_POSTER_PATH_PREFIX,
)


def project_media_reference_lock_key(user_id: uuid.UUID) -> int:
    """Stable positive BIGINT key for owner-scoped PostgreSQL advisory locks."""

    digest = hashlib.sha256(f"project-media:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def job_input_path_matches_owner(path: object, user_id: uuid.UUID) -> bool:
    """Reject another account's recognizable persistent upload namespaces.

    Legacy curated/anonymous prefixes such as ``slot-uploads/`` have no owner
    encoded in their key and remain compatible. Every namespace that does
    encode an owner must match the authenticated caller.
    """

    candidate = normalize_job_storage_path(path)
    if candidate is None:
        return False
    expected = str(user_id)
    parts = candidate.split("/")
    if parts[0] == "users" and len(parts) >= 2:
        return parts[1] == expected
    if parts[:2] == ["voiceover-uploads", "direct"] and len(parts) >= 3:
        return parts[2] == expected
    if (
        parts[0] == "dev-user"
        and len(parts) >= 3
        and parts[2] in {"generative", "plan-pool", "plan-pool-reservations"}
    ):
        try:
            encoded_owner = str(uuid.UUID(parts[1]))
        except ValueError:
            return True
        return encoded_owner == expected
    if parts[0] == "dev-user":
        return True
    try:
        encoded_owner = str(uuid.UUID(parts[0]))
    except ValueError:
        return True
    return encoded_owner == expected


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
