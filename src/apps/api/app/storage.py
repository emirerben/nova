"""GCS storage abstraction: presigned PUT URL generation and public-read upload."""

import datetime

from google.cloud import storage as gcs
from google.oauth2 import service_account

from app.config import settings

_client: gcs.Client | None = None


def _get_client() -> gcs.Client:
    global _client
    if _client is None:
        project = settings.gcloud_project or None
        if settings.google_application_credentials:
            creds = service_account.Credentials.from_service_account_file(
                settings.google_application_credentials
            )
            _client = gcs.Client(project=project, credentials=creds)
        else:
            _client = gcs.Client(project=project)
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
    """Upload a local file to GCS and return a signed URL valid for 7 days.

    Uses signed URLs instead of ACLs — compatible with uniform bucket-level access.
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.upload_from_filename(local_path, content_type=content_type)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(days=7),
        method="GET",
    )


def upload_bytes_public_read(data: bytes, object_path: str, content_type: str = "image/jpeg") -> str:  # noqa: E501
    """Upload raw bytes to GCS and return a signed URL valid for 7 days."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.upload_from_string(data, content_type=content_type)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(days=7),
        method="GET",
    )


def download_to_file(object_path: str, local_path: str) -> None:
    """Download a GCS object to a local path (worker use only)."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.download_to_filename(local_path)


def object_exists(object_path: str) -> bool:
    """Check whether a GCS object exists. Used for GCS path validation."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.exists()


