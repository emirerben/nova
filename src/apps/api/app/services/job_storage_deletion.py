"""Versioned manifests for storage cleanup after a Job row is deleted.

Legacy outbox rows contain ``list[str]`` exact keys. Version 2 adds bounded,
allowlisted prefix entries with quiescence deadlines. The payload remains in
the existing ``JobStorageDeletion.object_paths`` JSONB column so account/job
deletion can atomically persist cleanup ownership without a schema rewrite.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from billiard.exceptions import SoftTimeLimitExceeded

from app.services.job_storage_paths import (
    JOB_OUTPUT_PREFIXES,
    normalize_job_storage_path,
)

JOB_STORAGE_MANIFEST_VERSION = 2
JOB_STORAGE_EXACT_PATH_CAP = 4096
# One Job can carry 32 source-copy and 64 render-generation receipts, plus
# conservative roots. Account erasure must externalize all of them.
JOB_STORAGE_PREFIX_CAP = 128
JOB_STORAGE_PREFIX_ATTEMPT_LIMIT = 2
JOB_STORAGE_EXACT_ATTEMPT_LIMIT = 32
# Celery render hard limit (30m) plus bounded storage-call/termination grace.
# Account erasure cannot wait for a live worker, so it externalizes ownership
# and makes conservative roots due only after this window.
ACCOUNT_ERASURE_STORAGE_QUIESCENCE = timedelta(minutes=35)

ManifestStatus = Literal["completed", "pending", "unavailable"]


class JobStorageManifestError(ValueError):
    """A persisted deletion manifest is malformed or exceeds its bounds."""


@dataclass(frozen=True, order=True)
class JobStoragePrefix:
    prefix: str
    not_before: datetime


@dataclass(frozen=True)
class JobStorageManifest:
    exact_paths: tuple[str, ...] = ()
    prefixes: tuple[JobStoragePrefix, ...] = ()
    legacy: bool = False

    @property
    def empty(self) -> bool:
        return not self.exact_paths and not self.prefixes

    def to_payload(self) -> list[str] | dict[str, Any]:
        if self.legacy and not self.prefixes:
            return list(self.exact_paths)
        return {
            "version": JOB_STORAGE_MANIFEST_VERSION,
            "exact_paths": list(self.exact_paths),
            "prefixes": [
                {
                    "prefix": entry.prefix,
                    "not_before": entry.not_before.astimezone(UTC).isoformat(),
                }
                for entry in self.prefixes
            ],
        }


@dataclass(frozen=True)
class JobStorageManifestCleanupResult:
    status: ManifestStatus
    remaining: JobStorageManifest
    exact_deleted: int = 0
    exact_failed: int = 0
    prefixes_verified: int = 0
    prefixes_retained: int = 0

    @property
    def complete(self) -> bool:
        return self.status == "completed" and self.remaining.empty


def _promote_parent_prefix_deadlines(
    prefix_by_path: dict[str, datetime],
) -> dict[str, datetime]:
    """Prevent a broad parent sweep from preceding an active child lease."""
    promoted = dict(prefix_by_path)
    for parent in promoted:
        descendant_deadlines = [
            deadline for child, deadline in prefix_by_path.items() if child.startswith(parent)
        ]
        if descendant_deadlines:
            promoted[parent] = max(descendant_deadlines)
    return promoted


def _canonical_job_id(job_id: str | uuid.UUID) -> str:
    try:
        return str(job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise JobStorageManifestError("job_id must be a UUID") from exc


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise JobStorageManifestError("prefix not_before must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobStorageManifestError("prefix not_before must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise JobStorageManifestError("prefix not_before must be timezone-aware")
    return parsed.astimezone(UTC)


def _safe_exact_path(value: object, *, job_id: str | uuid.UUID) -> str:
    path = normalize_job_storage_path(value)
    if path is None:
        raise JobStorageManifestError("exact storage path is invalid")

    # Exact manifests also carry shared source objects (for example direct
    # uploads and voiceovers), so they cannot use the prefix manifest's closed
    # allowlist.  Job-output namespaces are different: every key below embeds
    # its owning Job UUID.  Once a Job row has been deleted the manifest is the
    # final ownership boundary, so reject a syntactically valid key from a
    # sibling Job instead of handing it to the destructive cleanup worker.
    canonical_job_id = _canonical_job_id(job_id)
    for template in JOB_OUTPUT_PREFIXES:
        namespace = template.partition("{job_id}")[0]
        if path.startswith(namespace) and not path.startswith(
            template.format(job_id=canonical_job_id)
        ):
            raise JobStorageManifestError("exact storage path belongs to another Job")
    return path


def _allowed_prefix_roots(
    job_id: str | uuid.UUID,
    *,
    user_id: str | uuid.UUID | None = None,
) -> tuple[str, ...]:
    canonical_job_id = _canonical_job_id(job_id)
    roots = [template.format(job_id=canonical_job_id) for template in JOB_OUTPUT_PREFIXES]
    if user_id is not None:
        try:
            canonical_user_id = str(
                user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise JobStorageManifestError("user_id must be a UUID") from exc
        roots.append(f"{canonical_user_id}/{canonical_job_id}/")
    return tuple(roots)


def _safe_job_prefix(value: object, *, job_id: str | uuid.UUID) -> str:
    if not isinstance(value, str):
        raise JobStorageManifestError("storage prefix must be a string")
    prefix = value.strip().lstrip("/")
    if not prefix.endswith("/") or ".." in prefix.split("/"):
        raise JobStorageManifestError("storage prefix is invalid")
    canonical_job_id = _canonical_job_id(job_id)
    allowlisted = any(prefix.startswith(root) for root in _allowed_prefix_roots(job_id))
    # Historical default jobs used ``{user_uuid}/{job_uuid}/``. The Job row is
    # gone when the outbox runs, so validate this self-contained shape without
    # requiring a user FK on the durable manifest.
    parts = prefix.split("/")
    legacy_owned = False
    if len(parts) >= 3 and parts[1] == canonical_job_id:
        try:
            legacy_owned = str(uuid.UUID(parts[0])) == parts[0]
        except ValueError:
            legacy_owned = False
    if not allowlisted and not legacy_owned:
        raise JobStorageManifestError("storage prefix is outside the Job allowlist")
    return prefix


def parse_job_storage_manifest(
    payload: object,
    *,
    job_id: str | uuid.UUID,
) -> JobStorageManifest:
    """Parse legacy/v2 JSONB without silently discarding malformed debt."""
    if isinstance(payload, list):
        exact_paths = tuple(
            dict.fromkeys(_safe_exact_path(path, job_id=job_id) for path in payload)
        )
        if len(exact_paths) > JOB_STORAGE_EXACT_PATH_CAP:
            raise JobStorageManifestError("exact storage path cap exceeded")
        return JobStorageManifest(exact_paths=exact_paths, legacy=True)
    if not isinstance(payload, dict) or payload.get("version") != JOB_STORAGE_MANIFEST_VERSION:
        raise JobStorageManifestError("unsupported job-storage manifest")

    raw_exact = payload.get("exact_paths")
    raw_prefixes = payload.get("prefixes")
    if not isinstance(raw_exact, list) or not isinstance(raw_prefixes, list):
        raise JobStorageManifestError("v2 manifest fields must be lists")
    exact_paths = tuple(dict.fromkeys(_safe_exact_path(path, job_id=job_id) for path in raw_exact))
    if len(exact_paths) > JOB_STORAGE_EXACT_PATH_CAP:
        raise JobStorageManifestError("exact storage path cap exceeded")

    prefix_by_path: dict[str, datetime] = {}
    for raw_entry in raw_prefixes:
        if not isinstance(raw_entry, dict):
            raise JobStorageManifestError("prefix entry must be an object")
        prefix = _safe_job_prefix(raw_entry.get("prefix"), job_id=job_id)
        not_before = _parse_timestamp(raw_entry.get("not_before"))
        previous = prefix_by_path.get(prefix)
        if previous is None or not_before > previous:
            prefix_by_path[prefix] = not_before
    if len(prefix_by_path) > JOB_STORAGE_PREFIX_CAP:
        raise JobStorageManifestError("storage prefix cap exceeded")
    prefix_by_path = _promote_parent_prefix_deadlines(prefix_by_path)
    prefixes = tuple(
        JobStoragePrefix(prefix=prefix, not_before=not_before)
        for prefix, not_before in sorted(prefix_by_path.items())
    )
    return JobStorageManifest(exact_paths=exact_paths, prefixes=prefixes)


def build_job_storage_manifest(
    *,
    job_id: str | uuid.UUID,
    exact_paths: list[str] | tuple[str, ...],
    prefixes: list[str] | tuple[str, ...],
    not_before: datetime,
) -> JobStorageManifest:
    if not_before.tzinfo is None:
        raise JobStorageManifestError("not_before must be timezone-aware")
    payload = {
        "version": JOB_STORAGE_MANIFEST_VERSION,
        "exact_paths": list(exact_paths),
        "prefixes": [
            {
                "prefix": prefix,
                "not_before": not_before.astimezone(UTC).isoformat(),
            }
            for prefix in prefixes
        ],
    }
    return parse_job_storage_manifest(payload, job_id=job_id)


def conservative_job_prefixes(
    job_id: str | uuid.UUID,
    *,
    user_id: str | uuid.UUID | None = None,
    include_legacy_user_prefix: bool = True,
) -> tuple[str, ...]:
    """Every allowlisted lifecycle-exempt output root for one Job."""
    roots = _allowed_prefix_roots(
        job_id,
        user_id=user_id if include_legacy_user_prefix else None,
    )
    return roots


def build_job_storage_manifest_for_job(
    *,
    job: Any,
    exact_paths: list[str] | tuple[str, ...],
    conservative_not_before: datetime,
    include_legacy_user_prefix: bool = True,
) -> JobStorageManifest:
    """Externalize every private cleanup receipt before deleting a Job row.

    Conservative root prefixes backstop unexpected writers. Attempt-specific
    prefixes retain their own leases for diagnostics and exact ownership. The
    root deadline advances to the latest active receipt lease so a broad sweep
    cannot race a late worker.
    """
    if conservative_not_before.tzinfo is None:
        raise JobStorageManifestError("conservative_not_before must be timezone-aware")
    from app.services.durable_attempt_cleanup import (  # noqa: PLC0415
        ExactKeysCleanupReceipt,
        PrefixCleanupReceipt,
        iter_cleanup_receipts,
    )

    canonical_default = conservative_not_before.astimezone(UTC)
    conservative_prefixes = conservative_job_prefixes(
        job.id,
        user_id=job.user_id,
        include_legacy_user_prefix=include_legacy_user_prefix,
    )
    prefix_deadlines: dict[str, datetime] = {
        prefix: canonical_default for prefix in conservative_prefixes
    }
    merged_exact = list(exact_paths)
    plan = getattr(job, "assembly_plan", None)
    if plan is not None:
        if not isinstance(plan, dict):
            raise JobStorageManifestError("assembly_plan is malformed")
        try:
            receipts = list(iter_cleanup_receipts(plan, job=job))
        except ValueError as exc:
            raise JobStorageManifestError("private cleanup receipt is malformed") from exc
        for receipt in receipts:
            if isinstance(receipt, ExactKeysCleanupReceipt):
                merged_exact.extend(receipt.paths)
                continue
            assert isinstance(receipt, PrefixCleanupReceipt)
            deadline = (
                max(canonical_default, receipt.lease_expires_at)
                if receipt.upload_state == "writing"
                else canonical_default
            )
            previous = prefix_deadlines.get(receipt.prefix)
            if previous is None or deadline > previous:
                prefix_deadlines[receipt.prefix] = deadline
            # A conservative parent prefix must not run before a child upload
            # lease. This includes generative-jobs/{job_id}/.
            for root in conservative_prefixes:
                if receipt.prefix.startswith(root):
                    prefix_deadlines[root] = max(prefix_deadlines[root], deadline)

    payload = {
        "version": JOB_STORAGE_MANIFEST_VERSION,
        "exact_paths": merged_exact,
        "prefixes": [
            {"prefix": prefix, "not_before": deadline.isoformat()}
            for prefix, deadline in sorted(prefix_deadlines.items())
        ],
    }
    return parse_job_storage_manifest(payload, job_id=job.id)


def build_account_erasure_manifest_for_job(
    *,
    job: Any,
    exact_paths: list[str] | tuple[str, ...],
    conservative_not_before: datetime,
) -> JobStorageManifest:
    """Build an erasure outbox even when private receipt JSON has drifted.

    Account deletion must not be held hostage by corrupt internal metadata.
    The conservative roots cover every source/render attempt namespace after
    the worker hard-limit deadline; known exact paths remain included. Narrow
    individual-job deletion should use the strict builder and reject instead.
    """
    try:
        return build_job_storage_manifest_for_job(
            job=job,
            exact_paths=exact_paths,
            conservative_not_before=conservative_not_before,
            include_legacy_user_prefix=True,
        )
    except JobStorageManifestError:
        return build_job_storage_manifest(
            job_id=job.id,
            exact_paths=exact_paths,
            prefixes=list(conservative_job_prefixes(job.id, user_id=job.user_id)),
            not_before=conservative_not_before,
        )


def merge_job_storage_manifests(
    existing_payload: object,
    incoming: JobStorageManifest,
    *,
    job_id: str | uuid.UUID,
) -> JobStorageManifest:
    """Idempotently merge outbox debt, taking the latest prefix deadline."""
    existing = parse_job_storage_manifest(existing_payload, job_id=job_id)
    exact_paths = tuple(dict.fromkeys((*existing.exact_paths, *incoming.exact_paths)))
    if len(exact_paths) > JOB_STORAGE_EXACT_PATH_CAP:
        raise JobStorageManifestError("merged exact storage path cap exceeded")
    prefix_by_path: dict[str, datetime] = {
        entry.prefix: entry.not_before for entry in existing.prefixes
    }
    for entry in incoming.prefixes:
        previous = prefix_by_path.get(entry.prefix)
        if previous is None or entry.not_before > previous:
            prefix_by_path[entry.prefix] = entry.not_before
    if len(prefix_by_path) > JOB_STORAGE_PREFIX_CAP:
        raise JobStorageManifestError("merged storage prefix cap exceeded")
    prefix_by_path = _promote_parent_prefix_deadlines(prefix_by_path)
    return JobStorageManifest(
        exact_paths=exact_paths,
        prefixes=tuple(
            JobStoragePrefix(prefix=prefix, not_before=not_before)
            for prefix, not_before in sorted(prefix_by_path.items())
        ),
    )


def cleanup_job_storage_manifest(
    manifest: JobStorageManifest,
    *,
    now: datetime,
    prefix_timeout_s: float = 3.0,
    prefix_limit: int = JOB_STORAGE_PREFIX_ATTEMPT_LIMIT,
    exact_limit: int = JOB_STORAGE_EXACT_ATTEMPT_LIMIT,
) -> JobStorageManifestCleanupResult:
    """Attempt one manifest; retain every ambiguous or not-yet-due target."""
    if now.tzinfo is None:
        raise JobStorageManifestError("now must be timezone-aware")
    if prefix_limit < 0 or exact_limit < 0:
        raise JobStorageManifestError("cleanup limits must be non-negative")
    from app.storage import (  # noqa: PLC0415
        delete_object_best_effort,
        delete_object_once,
        delete_prefix_verified,
        object_exists_once,
    )

    unavailable = False
    prefixes_verified = 0
    retained_prefixes: list[JobStoragePrefix] = []
    verified_parent_prefixes: list[str] = []
    canonical_now = now.astimezone(UTC)
    prefixes_attempted = 0
    for entry in manifest.prefixes:
        if entry.not_before > canonical_now:
            retained_prefixes.append(entry)
            continue
        if any(entry.prefix.startswith(parent) for parent in verified_parent_prefixes):
            prefixes_verified += 1
            continue
        if prefixes_attempted >= prefix_limit:
            retained_prefixes.append(entry)
            continue
        prefixes_attempted += 1
        result = delete_prefix_verified(entry.prefix, timeout_s=prefix_timeout_s)
        if result.status == "verified_empty":
            prefixes_verified += 1
            verified_parent_prefixes.append(entry.prefix)
        else:
            retained_prefixes.append(entry)
            unavailable = unavailable or result.status == "unavailable"

    exact_deleted = 0
    failed_exact: list[str] = []
    exact_attempted = 0
    for path in manifest.exact_paths:
        # A verified parent-prefix re-list is already a stronger absence proof
        # and avoids two extra storage calls per known key on large jobs.
        if any(path.startswith(parent) for parent in verified_parent_prefixes):
            exact_deleted += 1
            continue
        if exact_attempted >= exact_limit:
            failed_exact.append(path)
            continue
        exact_attempted += 1
        if manifest.legacy:
            if delete_object_best_effort(path):
                exact_deleted += 1
            else:
                failed_exact.append(path)
            continue
        try:
            delete_object_once(path, timeout_s=prefix_timeout_s)
            still_exists = object_exists_once(path, timeout_s=prefix_timeout_s)
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — durable manifest retains the key
            failed_exact.append(path)
            unavailable = True
            continue
        if still_exists:
            failed_exact.append(path)
        else:
            exact_deleted += 1

    remaining = JobStorageManifest(
        exact_paths=tuple(failed_exact),
        prefixes=tuple(retained_prefixes),
        legacy=manifest.legacy and not manifest.prefixes,
    )
    if remaining.empty:
        status: ManifestStatus = "completed"
    elif unavailable:
        status = "unavailable"
    else:
        status = "pending"
    return JobStorageManifestCleanupResult(
        status=status,
        remaining=remaining,
        exact_deleted=exact_deleted,
        exact_failed=len(failed_exact),
        prefixes_verified=prefixes_verified,
        prefixes_retained=len(retained_prefixes),
    )
