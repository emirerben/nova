"""GCS storage abstraction: presigned PUT URL generation and public-read upload."""

import datetime
import json

from google.cloud import storage as gcs
from google.oauth2 import service_account

from app.config import settings

_client: gcs.Client | None = None


def get_gcp_credentials(
    scopes: list[str] | None = None,
) -> service_account.Credentials | None:
    """Return GCP service-account credentials using the project-wide 3-tier chain:

    1. File path  (GOOGLE_APPLICATION_CREDENTIALS) — local dev
    2. JSON string (GOOGLE_SERVICE_ACCOUNT_JSON)   — Fly.io / containers
    3. Returns None                                 — caller falls through to ADC

    Pass ``scopes`` when the calling SDK does not add them automatically.  The
    Cloud Vision gRPC client needs ``https://www.googleapis.com/auth/cloud-platform``
    explicitly; GCS manages its own scopes internally, so pass ``None`` there.
    """
    if settings.google_application_credentials:
        creds = service_account.Credentials.from_service_account_file(
            settings.google_application_credentials
        )
        return creds.with_scopes(scopes) if scopes else creds
    elif settings.google_service_account_json.strip():
        raw = settings.google_service_account_json.strip()
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but contains invalid JSON"
            ) from exc
        try:
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        except (ValueError, KeyError) as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON contains valid JSON but is not a "
                "valid service account key (missing required fields)"
            ) from exc
        return creds
    else:
        return None  # caller falls through to ADC


def _get_client() -> gcs.Client:
    """Build a GCS client using the project-wide credential chain (see get_gcp_credentials)."""
    global _client
    if _client is None:
        project = settings.gcloud_project or None
        creds = get_gcp_credentials()  # GCS SDK manages its own scopes
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


def presigned_put_url_for_plan_item(
    user_id: str,
    plan_item_id: str,
    filename: str,
    content_type: str = "video/mp4",
) -> tuple[str, str]:
    """Signed PUT URL for an authenticated content-plan upload.

    Lands under `users/{user_id}/plan/{plan_item_id}/...` — a PERSISTENT prefix
    NOT matched by the 24h GCS delete rule (infra/gcs-lifecycle.json), unlike the
    `dev-user/*` paths from presigned_put_url. Allowlisted in
    admin_music._ALLOWED_CLIP_PREFIXES so the render pipeline accepts it.
    """
    object_path = f"users/{user_id}/plan/{plan_item_id}/{filename}"
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=15),
        method="PUT",
        content_type=content_type,
    )
    return url, object_path


def presigned_put_url_for_plan_seed(
    user_id: str,
    plan_id: str,
    filename: str,
    content_type: str = "video/mp4",
) -> tuple[str, str]:
    """Signed PUT URL for the content-plan activation seed (T8).

    Lands under `users/{user_id}/plan/{plan_id}/seed/...` — the same PERSISTENT
    `users/` namespace as themed per-item uploads (NOT swept by the 24h GCS rule,
    allowlisted in admin_music._ALLOWED_CLIP_PREFIXES), but keyed by plan rather
    than item: the seed batch is uploaded once before any item is chosen, then
    clip_plan_matcher assigns clips to items.
    """
    object_path = f"users/{user_id}/plan/{plan_id}/seed/{filename}"
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=15),
        method="PUT",
        content_type=content_type,
    )
    return url, object_path


def presigned_put_url_for_plan_pool(
    user_id: str,
    plan_id: str,
    filename: str,
    content_type: str = "video/mp4",
) -> tuple[str, str]:
    """Signed PUT URL for the post-activation footage pool ("dump the trip").

    Lands under `users/{user_id}/plan-pool/{plan_id}/...` — the same PERSISTENT
    `users/` namespace as themed and seed uploads (NOT swept by the 24h GCS
    rule, accepted by build_generative_job's users/ allowlist). Pool clips are
    matched across PENDING plan items by match_pool_clips; matched items
    reference these paths directly (no GCS copy, same trust argument as the
    activation seed).
    """
    object_path = f"users/{user_id}/plan-pool/{plan_id}/{filename}"
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


def upload_bytes_public_read(
    data: bytes, object_path: str, content_type: str = "image/jpeg"
) -> str:  # noqa: E501
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


def signed_get_url(object_path: str, expiration_minutes: int = 5) -> str:
    """Generate a short-lived signed GET URL for the API to stream-probe a GCS
    object without downloading it. ffmpeg/ffprobe accept https:// URLs and
    range-request only the moov atom, so a 400 MB clip is probed in ~1-2s.

    Default TTL is 5 minutes — long enough for a sequence of preflight probes
    on a 20-clip upload, short enough that a leaked URL is useless almost
    immediately.
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="GET",
    )


def copy_object_signed_url(src_object_path: str, dst_object_path: str) -> str:
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


def copy_object(src_object_path: str, dst_object_path: str) -> None:
    """Server-side copy a GCS object to a new key (no signed URL).

    Same `bucket.copy_blob` (server-side rewrite) mechanics as
    `copy_object_signed_url` — no egress + re-upload bandwidth — for callers
    that only need the durable copy to exist (e.g. the generative clip-editor's
    per-job source snapshots), not a playback URL for it.
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    src_blob = bucket.blob(src_object_path)
    bucket.copy_blob(src_blob, bucket, dst_object_path)


def object_exists(object_path: str) -> bool:
    """Check whether a GCS object exists. Used for GCS path validation."""
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.exists()
