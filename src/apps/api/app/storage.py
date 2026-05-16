"""GCS storage abstraction: presigned PUT URL generation and public-read upload."""

import datetime
import json

from google.cloud import storage as gcs
from google.oauth2 import service_account

from app.config import settings

_client: gcs.Client | None = None


def _get_client() -> gcs.Client:
    """Build a GCS client with a 3-tier credential chain:

    1. File path  (GOOGLE_APPLICATION_CREDENTIALS) — local dev
    2. JSON string (GOOGLE_SERVICE_ACCOUNT_JSON)   — Fly.io / containers
    3. Application Default Credentials              — GCE / GKE / Cloud Run
    """
    global _client
    if _client is None:
        project = settings.gcloud_project or None
        if settings.google_application_credentials:
            creds = service_account.Credentials.from_service_account_file(
                settings.google_application_credentials
            )
        elif settings.google_service_account_json.strip():
            raw = settings.google_service_account_json.strip()
            try:
                info = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is set but contains invalid JSON"
                ) from exc
            try:
                creds = service_account.Credentials.from_service_account_info(info)
            except (ValueError, KeyError) as exc:
                raise RuntimeError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON contains valid JSON but is not a "
                    "valid service account key (missing required fields)"
                ) from exc
        else:
            creds = None  # triggers ADC inside gcs.Client
        _client = gcs.Client(project=project, credentials=creds)
    return _client


def presigned_put_url(
    user_id: str,
    job_id: str,
    filename: str = "raw.mp4",
    content_type: str = "video/mp4",
) -> tuple[str, str]:
    """Return (signed_upload_url, gcs_object_path) for client-side direct upload.

    Client uploads directly to GCS — API never touches video bytes (OOM prevention).
    The signed URL enforces the given content_type; client must send the same header.
    """
    object_path = f"{user_id}/{job_id}/{filename}"
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)

    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=15),
        method="PUT",
        content_type=content_type,
    )
    return url, object_path


def upload_public_read(local_path: str, object_path: str, content_type: str = "video/mp4") -> str:
    """Upload a local file to GCS and return a signed URL valid for 1 day.

    URL TTL matches the bucket lifecycle rule (infra/gcs-lifecycle.json): per-job
    objects under dev-user/ and music-jobs/ are deleted at age 1 day, so a longer
    URL TTL would point at a 404. Uses signed URLs instead of ACLs — compatible
    with uniform bucket-level access.
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.upload_from_filename(local_path, content_type=content_type)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(days=1),
        method="GET",
    )


def upload_bytes_public_read(data: bytes, object_path: str, content_type: str = "image/jpeg") -> str:  # noqa: E501
    """Upload raw bytes to GCS and return a signed URL valid for 1 day."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.upload_from_string(data, content_type=content_type)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(days=1),
        method="GET",
    )


def download_to_file(object_path: str, local_path: str) -> None:
    """Download a GCS object to a local path (worker use only)."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.download_to_filename(local_path)


def copy_object_signed_url(
    src_object_path: str, dst_object_path: str
) -> str:
    """Server-side copy a GCS object to a new key, returns signed URL for the copy.

    Uses bucket.copy_blob (server-side rewrite) so we don't pay egress + re-upload
    bandwidth when the source file is identical to the destination. Avoids the
    cost of `download → upload` for jobs that produce two outputs from the same
    bytes (e.g. single_video templates where template_output and
    template_base are byte-identical).
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    src_blob = bucket.blob(src_object_path)
    dst_blob = bucket.copy_blob(src_blob, bucket, dst_object_path)
    return dst_blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(days=1),
        method="GET",
    )


def object_exists(object_path: str) -> bool:
    """Check whether a GCS object exists. Used for GCS path validation."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.exists()


