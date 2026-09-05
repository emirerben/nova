"""GCS storage abstraction: presigned PUT URL generation and public-read upload."""

import datetime
import json
import mimetypes
import re
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote

from billiard.exceptions import SoftTimeLimitExceeded
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage as gcs
from google.oauth2 import service_account

from app.config import settings

_client: gcs.Client | None = None


def _uses_local_storage() -> bool:
    return settings.storage_provider.strip().lower() == "local"


def local_object_path(object_path: str) -> Path:
    """Resolve one fixture-owned object inside the configured local root.

    Local storage is deliberately double-gated.  A production process cannot
    activate it by changing only STORAGE_PROVIDER, and object names may never
    escape the configured root.
    """

    if not _uses_local_storage() or not settings.e2e_fixtures:
        raise RuntimeError("Local storage requires STORAGE_PROVIDER=local and E2E_FIXTURES=true")
    root_value = settings.local_storage_root.strip()
    if not root_value:
        raise RuntimeError("LOCAL_STORAGE_ROOT is required for local fixture storage")
    normalized = PurePosixPath(object_path)
    if (
        normalized.is_absolute()
        or not normalized.parts
        or any(part in {"", ".", ".."} for part in normalized.parts)
    ):
        raise ValueError("Unsafe local storage object path")
    root = Path(root_value).expanduser().resolve()
    candidate = root.joinpath(*normalized.parts).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Unsafe local storage object path")
    return candidate


def _local_object_url(object_path: str) -> str:
    base = settings.local_storage_base_url.rstrip("/")
    if not base:
        raise RuntimeError("LOCAL_STORAGE_BASE_URL is required for local fixture storage")
    return f"{base}/{quote(object_path, safe='/')}"


def _local_metadata(object_path: str) -> "ObjectMetadata":
    path = local_object_path(object_path)
    if not path.is_file():
        raise FileNotFoundError(object_path)
    stat = path.stat()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    generation = str(stat.st_mtime_ns)
    return ObjectMetadata(
        path=object_path,
        generation=generation,
        etag=f'"local-{generation}-{stat.st_size}"',
        size=stat.st_size,
        content_type=content_type,
        md5_hash=None,
    )


@dataclass(frozen=True)
class ObjectMetadata:
    path: str
    generation: str
    etag: str | None
    size: int
    content_type: str
    md5_hash: str | None = None


PrefixDeletionStatus = Literal["verified_empty", "partial", "unavailable"]

# Maintenance callers pass a request timeout while holding ownership locks.
# Keep that path to one small storage page and at most two delete RPCs. A
# successful final re-list is still required before durable debt may clear.
VERIFIED_PREFIX_DELETE_OBJECT_CAP = 2


@dataclass(frozen=True)
class PrefixDeletionResult:
    """Bounded outcome for one list-delete-relist prefix cleanup attempt.

    ``deleted`` counts objects whose deletion completed (including an object
    concurrently removed after the first listing). ``remaining`` is ``None``
    when the final listing could not prove the prefix's state. Callers with a
    durable receipt must clear it only when ``status == "verified_empty"``.
    """

    status: PrefixDeletionStatus
    listed: int = 0
    deleted: int = 0
    failed: int = 0
    remaining: int | None = None

    @property
    def verified_empty(self) -> bool:
        return self.status == "verified_empty"


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
    if _uses_local_storage():
        raise RuntimeError(
            "Local fixture storage does not support browser-signed uploads; "
            "use the server-side fixture import/proxy path instead"
        )
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


def signed_put_url(
    object_path: str,
    content_type: str,
    file_size_bytes: int,
    expiration_minutes: int = 15,
) -> str:
    """Sign an exact GCS object path for a browser PUT.

    Route code owns path construction and authorization. Keeping that decision at
    the HTTP boundary lets this helper stay a small signing primitive while still
    pinning the exact Content-Type and byte count into the V4 signature. The
    generation precondition makes the random object key create-only, so the URL
    cannot overwrite a source after job validation.
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
        headers={
            "content-length": str(file_size_bytes),
            "x-goog-if-generation-match": "0",
        },
    )


def signed_put_url_legacy(
    object_path: str,
    content_type: str,
    file_size_bytes: int,
    expiration_minutes: int = 15,
) -> str:
    """Sign a PUT compatible with clients that send only Content-Type.

    Browsers and the relay set Content-Length automatically but deployed pre-0.28
    code cannot add the new generation header. Keep the exact byte ceiling in
    the signature. Callers must use a lifecycle-covered staging key and promote
    only the verified generation into persistent storage.
    """
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
        headers={"content-length": str(file_size_bytes)},
    )


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


def presigned_put_url_for_media_overlay(
    user_id: str,
    plan_item_id: str,
    filename: str,
    content_type: str = "video/mp4",
) -> tuple[str, str]:
    """Signed PUT URL for a media-overlay card asset.

    Lands under `users/{user_id}/plan/{plan_item_id}/overlays/...` — the same
    PERSISTENT `users/` namespace as themed uploads (NOT swept by the 24h GCS
    delete rule). This ensures overlay assets survive past the 24h lifecycle so
    a later re-render (e.g. swap-song) can re-apply the same cards.

    Accepted content types: images (jpeg/png/webp/heic/heif) and short video clips
    (mp4/quicktime). The route layer validates the content_type before calling
    this function.
    """
    object_path = f"users/{user_id}/plan/{plan_item_id}/overlays/{filename}"
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=15),
        method="PUT",
        content_type=content_type,
    )
    return url, object_path


def upload_local_file(local_path: str, object_path: str, content_type: str) -> None:
    """Server-side upload of a local file to an exact GCS object path.

    Used by the pool-asset proxy upload (plans/005): the API streams the browser's
    multipart file to disk, then pushes it here — no browser↔GCS CORS involved.
    """
    if _uses_local_storage():
        destination = local_object_path(object_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, destination)
        return
    bucket = _get_client().bucket(settings.storage_bucket)
    bucket.blob(object_path).upload_from_filename(local_path, content_type=content_type)


def presigned_put_url_for_sfx(
    user_id: str,
    plan_item_id: str,
    filename: str,
    content_type: str = "audio/mpeg",
) -> tuple[str, str]:
    """Signed PUT URL for a user-uploaded sound-effect asset.

    Lands under `users/{user_id}/plan/{plan_item_id}/sfx/...` — the same
    PERSISTENT `users/` namespace as overlay assets (NOT swept by the 24h GCS
    delete rule). This ensures SFX assets survive past the 24h lifecycle so a
    later re-render can re-apply the same effects.

    Accepted content types: audio files (mp3/mp4/wav/aac/ogg). The route layer
    validates the content_type before calling this function.
    """
    object_path = f"users/{user_id}/plan/{plan_item_id}/sfx/{filename}"
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
    if _uses_local_storage():
        upload_local_file(local_path, object_path, content_type)
        return _local_object_url(object_path)
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
    if _uses_local_storage():
        destination = local_object_path(object_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return _local_object_url(object_path)
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
    if _uses_local_storage():
        shutil.copy2(local_object_path(object_path), local_path)
        return
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    blob.download_to_filename(local_path)


def download_generation_to_file(
    object_path: str,
    local_path: str,
    *,
    generation: str,
) -> None:
    """Download exactly the storage generation previously validated by a worker."""
    if _uses_local_storage():
        metadata = _local_metadata(object_path)
        if metadata.generation != str(generation):
            raise FileNotFoundError(object_path)
        shutil.copy2(local_object_path(object_path), local_path)
        return
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path, generation=int(generation))
    blob.download_to_filename(local_path)


def delete_object_best_effort(object_path: str) -> bool:
    """Delete a GCS object, swallowing every failure (returns False on any error).

    For superseded render outputs: reburn/re-transcribe upload a NEW key and repoint
    the variant entry, leaving the old blob unreachable — under `generative-jobs/*`
    (lifecycle-exempt) it would otherwise persist forever. Deletion is cleanup, never
    correctness: a failure only costs storage, so it must never fail the caller.
    """
    try:
        if _uses_local_storage():
            local_object_path(object_path).unlink(missing_ok=True)
            return True
        bucket = _get_client().bucket(settings.storage_bucket)
        bucket.blob(object_path).delete()
        return True
    except NotFound:
        # Deletion is idempotently complete when the object never arrived or
        # was already removed by an earlier cleanup attempt.
        return True
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        return False


def delete_object_generation(object_path: str, *, generation: str) -> None:
    """Delete exactly the generation validated by registration.

    Unlike best-effort cleanup, callers use this to enforce a security boundary:
    a replaced object must never cause deletion of newer, unvalidated bytes.
    """
    if _uses_local_storage():
        metadata = _local_metadata(object_path)
        if metadata.generation != str(generation):
            raise PreconditionFailed("Local fixture generation changed")
        local_object_path(object_path).unlink()
        return
    bucket = _get_client().bucket(settings.storage_bucket)
    bucket.blob(object_path, generation=int(generation)).delete()


def delete_object_generation_best_effort(object_path: str, *, generation: str) -> bool:
    """Idempotently delete one immutable generation without touching replacements."""
    try:
        delete_object_generation(object_path, generation=generation)
        return True
    except NotFound:
        return True
    except Exception:  # noqa: BLE001 — caller retains a durable cleanup claim
        return False


def _validate_delete_prefix(prefix: str) -> str:
    normalized = prefix.strip()
    if len(normalized.strip("/")) < 3:
        raise ValueError(f"Refusing to delete an unsafe prefix: {prefix!r}")
    return normalized


def delete_prefix_verified(
    prefix: str,
    *,
    timeout_s: float | None = None,
) -> PrefixDeletionResult:
    """Delete a prefix and prove the result with a successful empty re-list.

    Storage transport failures are represented by ``unavailable`` instead of
    being confused with an already-empty prefix. A successful re-list with
    objects still present is ``partial``. The GCS SDK's implicit retries can
    optionally be disabled with ``timeout_s`` so durable maintenance callers
    retain control of their own retry and lease budgets.

    When ``timeout_s`` is supplied, one attempt is capped at
    ``VERIFIED_PREFIX_DELETE_OBJECT_CAP`` objects and a total deadline of
    ``timeout_s * (cap + 2)`` (list + deletes + proof re-list). Larger prefixes
    return ``partial`` and are drained by later durable sweeps. Without a
    timeout, the historical all-at-once behavior is preserved for callers that
    do not hold database locks.
    """
    normalized = _validate_delete_prefix(prefix)
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    object_cap = VERIFIED_PREFIX_DELETE_OBJECT_CAP if timeout_s is not None else None
    deadline = (
        time.monotonic() + timeout_s * (VERIFIED_PREFIX_DELETE_OBJECT_CAP + 2)
        if timeout_s is not None
        else None
    )

    def request_timeout() -> float | None:
        if deadline is None:
            return timeout_s
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("verified prefix deletion budget exhausted")
        assert timeout_s is not None
        return min(timeout_s, remaining)

    if _uses_local_storage():
        try:
            directory = local_object_path(normalized.rstrip("/"))
            listed_paths: list[Path] = []
            if directory.exists():
                for path in directory.rglob("*"):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("verified prefix deletion budget exhausted")
                    if path.is_file():
                        listed_paths.append(path)
                        if object_cap is not None and len(listed_paths) > object_cap:
                            break
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — status is the durable retry contract
            return PrefixDeletionResult(status="unavailable", remaining=None)

        deleted = 0
        failed = 0
        paths_to_delete = listed_paths if object_cap is None else listed_paths[:object_cap]
        for path in paths_to_delete:
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("verified prefix deletion budget exhausted")
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                deleted += 1
            except SoftTimeLimitExceeded:
                raise
            except Exception:  # noqa: BLE001 — verify below and retain receipt
                failed += 1

        try:
            remaining_paths: list[Path] = []
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("verified prefix deletion budget exhausted")
            if directory.exists():
                for path in directory.rglob("*"):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("verified prefix deletion budget exhausted")
                    if path.is_file():
                        remaining_paths.append(path)
                        if object_cap is not None:
                            break
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — deletion may have succeeded, proof did not
            return PrefixDeletionResult(
                status="unavailable",
                listed=len(listed_paths),
                deleted=deleted,
                failed=failed,
                remaining=None,
            )

        remaining = len(remaining_paths)
        if remaining == 0 and directory.exists():
            # Directories are not storage objects. Remove them only as local
            # fixture hygiene; failure does not invalidate the empty proof.
            try:
                for path in sorted(
                    (path for path in directory.rglob("*") if path.is_dir()),
                    key=lambda value: len(value.parts),
                    reverse=True,
                ):
                    path.rmdir()
                directory.rmdir()
            except OSError:
                pass
        return PrefixDeletionResult(
            status="verified_empty" if remaining == 0 else "partial",
            listed=len(listed_paths),
            deleted=deleted,
            failed=failed,
            remaining=remaining,
        )

    bucket = _get_client().bucket(settings.storage_bucket)
    list_kwargs: dict[str, object] = {"prefix": normalized}
    if object_cap is not None:
        # max_results + matching page_size ensures the iterator cannot amplify
        # one bounded attempt into multiple list RPCs.
        list_kwargs.update(max_results=object_cap + 1, page_size=object_cap + 1)
    if timeout_s is not None:
        try:
            list_kwargs.update(timeout=request_timeout(), retry=None)
        except TimeoutError:
            return PrefixDeletionResult(status="unavailable", remaining=None)
    try:
        listed_blobs = list(bucket.list_blobs(**list_kwargs))
    except SoftTimeLimitExceeded:
        raise
    except Exception:  # noqa: BLE001 — unavailable is distinct from empty
        return PrefixDeletionResult(status="unavailable", remaining=None)

    deleted = 0
    failed = 0
    blobs_to_delete = listed_blobs if object_cap is None else listed_blobs[:object_cap]
    for blob in blobs_to_delete:
        try:
            delete_kwargs = (
                {} if timeout_s is None else {"timeout": request_timeout(), "retry": None}
            )
            blob.delete(**delete_kwargs)
            deleted += 1
        except NotFound:
            # A concurrent cleanup completed this exact listed object.
            deleted += 1
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — final re-list decides partial vs complete
            failed += 1

    try:
        proof_kwargs: dict[str, object] = {"prefix": normalized}
        if object_cap is not None:
            # Empty/non-empty is the only proof needed after a bounded pass.
            proof_kwargs.update(max_results=1, page_size=1)
        if timeout_s is not None:
            proof_kwargs.update(timeout=request_timeout(), retry=None)
        remaining = sum(1 for _ in bucket.list_blobs(**proof_kwargs))
    except SoftTimeLimitExceeded:
        raise
    except Exception:  # noqa: BLE001 — never claim completion without the proof
        return PrefixDeletionResult(
            status="unavailable",
            listed=len(listed_blobs),
            deleted=deleted,
            failed=failed,
            remaining=None,
        )

    return PrefixDeletionResult(
        status="verified_empty" if remaining == 0 else "partial",
        listed=len(listed_blobs),
        deleted=deleted,
        failed=failed,
        remaining=remaining,
    )


# Descriptive alias for call sites that frame this as an outcome-bearing
# replacement for the legacy best-effort helper.
delete_prefix_with_status = delete_prefix_verified


def delete_prefix_best_effort(prefix: str) -> int:
    """Delete every object under a prefix with the historical one-pass contract.

    Existing fire-and-forget callers keep their integer return type and single
    listing. New durable callers must use :func:`delete_prefix_verified` and
    retain their receipt unless it reports ``verified_empty``.
    """
    normalized = _validate_delete_prefix(prefix)
    if _uses_local_storage():
        directory = local_object_path(normalized.rstrip("/"))
        if not directory.exists():
            return 0
        files = [path for path in directory.rglob("*") if path.is_file()]
        for path in files:
            path.unlink(missing_ok=True)
        for path in sorted(
            (path for path in directory.rglob("*") if path.is_dir()),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            path.rmdir()
        directory.rmdir()
        return len(files)
    bucket = _get_client().bucket(settings.storage_bucket)
    deleted = 0
    try:
        for blob in bucket.list_blobs(prefix=normalized):
            try:
                blob.delete()
                deleted += 1
            except Exception:  # noqa: BLE001 — best-effort per-object cleanup
                continue
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        pass
    return deleted


def delete_prefix_once(prefix: str, *, timeout_s: float) -> int:
    """Delete a bounded prefix, raising so durable callers can retry failures."""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if len(prefix.strip("/")) < 3:
        raise ValueError(f"Refusing to delete an unsafe prefix: {prefix!r}")
    if _uses_local_storage():
        directory = local_object_path(prefix.rstrip("/"))
        if not directory.exists():
            return 0
        files = [path for path in directory.rglob("*") if path.is_file()]
        for path in files:
            path.unlink(missing_ok=True)
        for path in sorted(
            (path for path in directory.rglob("*") if path.is_dir()),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            path.rmdir()
        directory.rmdir()
        return len(files)
    bucket = _get_client().bucket(settings.storage_bucket)
    deleted = 0
    for blob in bucket.list_blobs(prefix=prefix, timeout=timeout_s, retry=None):
        try:
            blob.delete(timeout=timeout_s, retry=None)
        except NotFound:
            pass
        deleted += 1
    return deleted


def signed_get_url(object_path: str, expiration_minutes: int = 5) -> str:
    """Generate a short-lived signed GET URL for the API to stream-probe a GCS
    object without downloading it. ffmpeg/ffprobe accept https:// URLs and
    range-request only the moov atom, so a 400 MB clip is probed in ~1-2s.

    Default TTL is 5 minutes — long enough for a sequence of preflight probes
    on a 20-clip upload, short enough that a leaked URL is useless almost
    immediately.
    """
    if _uses_local_storage():
        if not local_object_path(object_path).is_file():
            raise FileNotFoundError(object_path)
        return _local_object_url(object_path)
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="GET",
    )


def signed_download_url(
    object_path: str,
    filename: str,
    expiration_minutes: int = 360,
) -> str:
    """Generate a signed GET that streams as a browser attachment.

    The response header, not the cross-origin ``download`` attribute, owns the
    filename on mobile browsers. Strip path/control/punctuation characters so a
    variant id can never become a malformed Content-Disposition header.
    """
    safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "", filename.replace("..", ""))
    safe_filename = safe_filename.lstrip(".")[:120] or "kria-video.mp4"
    if _uses_local_storage():
        if not local_object_path(object_path).is_file():
            raise FileNotFoundError(object_path)
        return f"{_local_object_url(object_path)}?download={quote(safe_filename)}"
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="GET",
        response_disposition=f'attachment; filename="{safe_filename}"',
    )


def copy_object_signed_url(src_object_path: str, dst_object_path: str) -> str:
    """Server-side copy a GCS object to a new key, returns signed URL for the copy.

    Uses bucket.copy_blob (server-side rewrite) so we don't pay egress + re-upload
    bandwidth when the source file is identical to the destination. Avoids the
    cost of `download → upload` for jobs that produce two outputs from the same
    bytes (e.g. single_video templates where template_output and
    template_base are byte-identical).
    """
    if _uses_local_storage():
        copy_object(src_object_path, dst_object_path)
        return _local_object_url(dst_object_path)
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
    if _uses_local_storage():
        destination = local_object_path(dst_object_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_object_path(src_object_path), destination)
        return
    bucket = _get_client().bucket(settings.storage_bucket)
    src_blob = bucket.blob(src_object_path)
    bucket.copy_blob(src_blob, bucket, dst_object_path)


def object_metadata(object_path: str) -> ObjectMetadata:
    """Return immutable identity and HTTP metadata for an owned GCS object."""
    if _uses_local_storage():
        return _local_metadata(object_path)
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.get_blob(object_path)
    if blob is None or blob.generation is None:
        raise FileNotFoundError(object_path)
    return ObjectMetadata(
        path=object_path,
        generation=str(blob.generation),
        etag=blob.etag,
        size=int(blob.size or 0),
        content_type=blob.content_type or "video/mp4",
        md5_hash=blob.md5_hash,
    )


def object_metadata_once(object_path: str, *, timeout_s: float) -> ObjectMetadata:
    """Return metadata with one bounded GCS request and no SDK retry.

    Maintenance audits own their retry/fail-closed policy. Disabling implicit
    retries prevents one unavailable source from consuming the whole rollout
    deadline while still proving the object has non-zero bytes.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if _uses_local_storage():
        return _local_metadata(object_path)
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.get_blob(object_path, timeout=timeout_s, retry=None)
    if blob is None or blob.generation is None:
        raise FileNotFoundError(object_path)
    return ObjectMetadata(
        path=object_path,
        generation=str(blob.generation),
        etag=blob.etag,
        size=int(blob.size or 0),
        content_type=blob.content_type or "video/mp4",
        md5_hash=blob.md5_hash,
    )


def copy_object_generation(
    src_object_path: str,
    dst_object_path: str,
    *,
    source_generation: str,
) -> ObjectMetadata:
    """Copy exactly one source generation, failing if the render changed."""
    if _uses_local_storage():
        metadata = _local_metadata(src_object_path)
        if metadata.generation != str(source_generation):
            raise PreconditionFailed("Local fixture generation changed")
        destination = local_object_path(dst_object_path)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_object_path(src_object_path), destination)
        return _local_metadata(dst_object_path)
    bucket = _get_client().bucket(settings.storage_bucket)
    src_blob = bucket.blob(src_object_path, generation=int(source_generation))
    try:
        bucket.copy_blob(
            src_blob,
            bucket,
            dst_object_path,
            if_source_generation_match=int(source_generation),
            if_generation_match=0,
        )
    except PreconditionFailed:
        # A prior attempt may have copied successfully but lost its response.
        # Destination names are reservation-unique and never client-writable.
        pass
    return object_metadata(dst_object_path)


def iter_object_range(
    object_path: str,
    *,
    start: int = 0,
    end: int | None = None,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Stream a GCS byte range in bounded chunks (``end`` is inclusive)."""
    if start < 0 or chunk_size <= 0:
        raise ValueError("invalid object range")
    if _uses_local_storage():
        with local_object_path(object_path).open("rb") as handle:
            handle.seek(start)
            remaining = None if end is None else end - start + 1
            while remaining is None or remaining > 0:
                requested = chunk_size if remaining is None else min(chunk_size, remaining)
                chunk = handle.read(requested)
                if not chunk:
                    break
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)
        return
    blob = _get_client().bucket(settings.storage_bucket).blob(object_path)
    cursor = start
    while end is None or cursor <= end:
        chunk_end = cursor + chunk_size - 1
        if end is not None:
            chunk_end = min(chunk_end, end)
        requested = chunk_end - cursor + 1
        chunk = blob.download_as_bytes(start=cursor, end=chunk_end)
        if not chunk:
            break
        yield chunk
        cursor += len(chunk)
        if len(chunk) < requested:
            break


def object_exists(object_path: str) -> bool:
    """Check whether a GCS object exists. Used for GCS path validation."""
    if _uses_local_storage():
        return local_object_path(object_path).is_file()
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.exists()


def object_exists_once(object_path: str, *, timeout_s: float) -> bool:
    """Check existence with one bounded GCS request and no automatic retry.

    Durable callers already own retry policy. Disabling the SDK retry prevents
    one slow object from consuming an entire Celery maintenance deadline.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if _uses_local_storage():
        return local_object_path(object_path).is_file()
    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.exists(timeout=timeout_s, retry=None)


def delete_object_once(object_path: str, *, timeout_s: float) -> bool:
    """Delete one object with a bounded request; missing is already complete.

    Unlike ``delete_object_best_effort``, transport and task-timeout exceptions
    propagate so a durable receipt can stay committed for the next sweep.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if _uses_local_storage():
        local_object_path(object_path).unlink(missing_ok=True)
        return True
    bucket = _get_client().bucket(settings.storage_bucket)
    try:
        bucket.blob(object_path).delete(timeout=timeout_s, retry=None)
    except NotFound:
        return True
    return True
