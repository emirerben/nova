"""Crash-safe cleanup primitives for lifecycle-exempt render attempts.

The renderer persists small cleanup receipts inside the private
``Job.assembly_plan`` namespace before it writes remote objects. This module
owns their canonical shapes, caps, reference proofs, and starvation-resistant
sweep query. Orchestrators own the surrounding row-lock/CAS transitions.

No helper in this module treats a malformed plan or receipt as empty. Cleanup
is destructive and therefore fails closed whenever ownership is uncertain.
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.orm.attributes import flag_modified

from app.database import sync_session
from app.models import Job, JobClip
from app.services.job_storage_paths import owned_job_output_path

SPEECH_CLEANUP_INTERNAL_FIELD = "_speech_cleanup_internal"
SOURCE_COPY_CLEANUP_FIELD = "durable_source_copy_pending"
RENDER_GENERATION_CLEANUP_FIELD = "render_generation_cleanup_pending"
CLEANUP_LIST_FIELDS = (
    SOURCE_COPY_CLEANUP_FIELD,
    RENDER_GENERATION_CLEANUP_FIELD,
)

SOURCE_COPY_RECEIPT_CAP = 32
RENDER_GENERATION_RECEIPT_CAP = 64
EXACT_KEY_PATH_CAP = 32
FAILURE_CLASS_MAX_LENGTH = 64

# Reconciliation currently keeps the Job and JobClip ownership locks while it
# talks to storage. Until every writer participates in a persisted I/O claim,
# releasing those locks would permit a reference-publication/delete race. Keep
# the safe lock-based protocol, but strictly bound its remote work.
CLEANUP_IO_RECEIPT_CAP = 1
CLEANUP_EXACT_PATH_CAP_PER_PASS = 2
CLEANUP_LIVE_PATH_CAP_PER_PASS = 2
CLEANUP_STORAGE_REQUEST_TIMEOUT_S = 3.0
CLEANUP_STORAGE_TOTAL_BUDGET_S = 12.0

UploadState = Literal["writing", "closed"]
CleanupReceiptField = Literal[
    "durable_source_copy_pending",
    "render_generation_cleanup_pending",
]
ReferenceStatus = Literal["proved", "unavailable"]
PrefixDisposition = Literal[
    "delete",
    "wait_for_uploads",
    "referenced",
    "adopted_live",
    "unavailable",
]

_SAFE_ERROR_CLASS = re.compile(r"[^A-Za-z0-9_.-]+")
_PLAN_UNSET = object()


class CleanupReceiptError(ValueError):
    """A private cleanup container or receipt cannot be safely interpreted."""


class CleanupReceiptBackpressure(CleanupReceiptError):
    """The bounded durable receipt list has no safe capacity."""


@dataclass(frozen=True)
class CleanupReceiptLocator:
    field: CleanupReceiptField
    receipt_id: str


@dataclass(frozen=True)
class PrefixCleanupReceipt:
    locator: CleanupReceiptLocator
    prefix: str
    upload_state: UploadState
    lease_expires_at: datetime


@dataclass(frozen=True)
class ExactKeysCleanupReceipt:
    locator: CleanupReceiptLocator
    paths: tuple[str, ...]


ParsedCleanupReceipt = PrefixCleanupReceipt | ExactKeysCleanupReceipt


@dataclass(frozen=True)
class StorageReferenceProof:
    status: ReferenceStatus
    references: frozenset[str] = frozenset()
    reason: str | None = None

    @property
    def proved(self) -> bool:
        return self.status == "proved"


@dataclass(frozen=True)
class PrefixCleanupDecision:
    disposition: PrefixDisposition
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupReconcileResult:
    receipts_seen: int = 0
    deleted: int = 0
    adopted_live: int = 0
    retained: int = 0
    failures: int = 0

    @property
    def ok(self) -> bool:
        return self.failures == 0 and self.retained == 0


def _canonical_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise CleanupReceiptError(f"{label} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise CleanupReceiptError(f"{label} must be a UUID") from exc
    # Existing render_generation_id values use UUID.hex while new source IDs
    # use the hyphenated spelling. Preserve either lowercase persisted token so
    # its database owner and storage prefix remain byte-for-byte correlated.
    if value not in {str(parsed), parsed.hex}:
        raise CleanupReceiptError(f"{label} must be a lowercase UUID")
    return value


def _canonical_job_id(job_id: str | uuid.UUID) -> str:
    try:
        return str(job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CleanupReceiptError("job_id must be a UUID") from exc


def _parse_lease(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise CleanupReceiptError("lease_expires_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CleanupReceiptError("lease_expires_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CleanupReceiptError("lease_expires_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _serialize_lease(value: datetime) -> str:
    if value.tzinfo is None:
        raise CleanupReceiptError("lease_expires_at must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def source_copy_attempt_prefix(
    job_id: str | uuid.UUID,
    copy_attempt_id: str | uuid.UUID,
) -> str:
    canonical_job = _canonical_job_id(job_id)
    canonical_attempt = _canonical_uuid(str(copy_attempt_id), label="copy_attempt_id")
    return f"generative-jobs/{canonical_job}/sources/copy-attempts/{canonical_attempt}/"


def render_generation_prefix(
    job_id: str | uuid.UUID,
    generation: str | uuid.UUID,
) -> str:
    canonical_job = _canonical_job_id(job_id)
    canonical_generation = _canonical_uuid(str(generation), label="generation")
    return f"generative-jobs/{canonical_job}/render-generations/{canonical_generation}/"


def _private_container(plan: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        raise CleanupReceiptError("assembly_plan must be an object")
    if SPEECH_CLEANUP_INTERNAL_FIELD not in plan:
        if not create:
            return None
        plan[SPEECH_CLEANUP_INTERNAL_FIELD] = {}
    internal = plan.get(SPEECH_CLEANUP_INTERNAL_FIELD)
    if not isinstance(internal, dict):
        raise CleanupReceiptError("private speech-cleanup container is malformed")
    return internal


def _receipt_list(
    plan: dict[str, Any],
    field: CleanupReceiptField,
    *,
    create: bool,
) -> list[Any] | None:
    internal = _private_container(plan, create=create)
    if internal is None:
        return None
    if field not in internal:
        if not create:
            return None
        internal[field] = []
    receipts = internal.get(field)
    if not isinstance(receipts, list):
        raise CleanupReceiptError(f"{field} must be a list")
    return receipts


def _remove_empty_cleanup_keys(plan: dict[str, Any]) -> None:
    internal = _private_container(plan, create=False)
    if internal is None:
        return
    for field in CLEANUP_LIST_FIELDS:
        if internal.get(field) == []:
            internal.pop(field, None)
    if not internal:
        plan.pop(SPEECH_CLEANUP_INTERNAL_FIELD, None)


def prune_empty_cleanup_keys(plan: dict[str, Any]) -> bool:
    """Remove empty sparse-index keys and report whether the plan changed."""
    before = copy.deepcopy(plan.get(SPEECH_CLEANUP_INTERNAL_FIELD))
    _remove_empty_cleanup_keys(plan)
    return before != plan.get(SPEECH_CLEANUP_INTERNAL_FIELD)


def new_source_copy_receipt(
    *,
    job_id: str | uuid.UUID,
    copy_attempt_id: str | uuid.UUID,
    lease_expires_at: datetime,
) -> dict[str, Any]:
    canonical_attempt = _canonical_uuid(str(copy_attempt_id), label="copy_attempt_id")
    return {
        "copy_attempt_id": canonical_attempt,
        "prefix": source_copy_attempt_prefix(job_id, canonical_attempt),
        "upload_state": "writing",
        "lease_expires_at": _serialize_lease(lease_expires_at),
    }


def new_render_generation_receipt(
    *,
    job_id: str | uuid.UUID,
    generation: str | uuid.UUID,
    lease_expires_at: datetime,
) -> dict[str, Any]:
    canonical_generation = _canonical_uuid(str(generation), label="generation")
    return {
        "generation": canonical_generation,
        "prefix": render_generation_prefix(job_id, canonical_generation),
        "upload_state": "writing",
        "lease_expires_at": _serialize_lease(lease_expires_at),
    }


def new_exact_keys_receipt(
    *,
    job: Job,
    debt_id: str | uuid.UUID,
    paths: Sequence[str],
) -> dict[str, Any]:
    canonical_debt = _canonical_uuid(str(debt_id), label="debt_id")
    normalized: list[str] = []
    for raw_path in paths:
        path = owned_job_output_path(raw_path, job)
        if path is None:
            raise CleanupReceiptError("exact cleanup path is not owned by the Job")
        if path not in normalized:
            normalized.append(path)
    if not normalized:
        raise CleanupReceiptError("exact cleanup debt must contain at least one path")
    if len(normalized) > EXACT_KEY_PATH_CAP:
        raise CleanupReceiptBackpressure("exact cleanup path cap exceeded")
    return {
        "debt_id": canonical_debt,
        "kind": "exact_keys",
        "paths": normalized,
        "upload_state": "closed",
    }


def _append_receipt(
    plan: dict[str, Any],
    field: CleanupReceiptField,
    receipt: dict[str, Any],
    *,
    cap: int,
    receipt_id: str,
) -> dict[str, Any]:
    receipts = _receipt_list(plan, field, create=True)
    assert receipts is not None
    for existing in receipts:
        existing_id = _raw_receipt_id(field, existing)
        if existing_id == receipt_id:
            if existing == receipt:
                return existing
            raise CleanupReceiptError("receipt id already exists with different state")
    if len(receipts) >= cap:
        raise CleanupReceiptBackpressure(f"{field} receipt cap reached")
    receipts.append(copy.deepcopy(receipt))
    return receipt


def reserve_source_copy_cleanup(
    plan: dict[str, Any],
    *,
    job_id: str | uuid.UUID,
    copy_attempt_id: str | uuid.UUID,
    lease_expires_at: datetime,
) -> dict[str, Any]:
    receipt = new_source_copy_receipt(
        job_id=job_id,
        copy_attempt_id=copy_attempt_id,
        lease_expires_at=lease_expires_at,
    )
    return _append_receipt(
        plan,
        SOURCE_COPY_CLEANUP_FIELD,
        receipt,
        cap=SOURCE_COPY_RECEIPT_CAP,
        receipt_id=receipt["copy_attempt_id"],
    )


def reserve_render_generation_cleanup(
    plan: dict[str, Any],
    *,
    job_id: str | uuid.UUID,
    generation: str | uuid.UUID,
    lease_expires_at: datetime,
) -> dict[str, Any]:
    receipt = new_render_generation_receipt(
        job_id=job_id,
        generation=generation,
        lease_expires_at=lease_expires_at,
    )
    return _append_receipt(
        plan,
        RENDER_GENERATION_CLEANUP_FIELD,
        receipt,
        cap=RENDER_GENERATION_RECEIPT_CAP,
        receipt_id=receipt["generation"],
    )


def append_exact_key_cleanup(
    plan: dict[str, Any],
    *,
    job: Job,
    debt_id: str | uuid.UUID,
    paths: Sequence[str],
) -> dict[str, Any]:
    receipt = new_exact_keys_receipt(job=job, debt_id=debt_id, paths=paths)
    return _append_receipt(
        plan,
        RENDER_GENERATION_CLEANUP_FIELD,
        receipt,
        cap=RENDER_GENERATION_RECEIPT_CAP,
        receipt_id=receipt["debt_id"],
    )


def _raw_receipt_id(field: CleanupReceiptField, raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    if field == SOURCE_COPY_CLEANUP_FIELD:
        value = raw.get("copy_attempt_id")
    elif raw.get("kind") == "exact_keys":
        value = raw.get("debt_id")
    else:
        value = raw.get("generation")
    return value if isinstance(value, str) else None


def parse_cleanup_receipt(
    *,
    job: Job,
    field: CleanupReceiptField,
    raw: object,
) -> ParsedCleanupReceipt:
    if not isinstance(raw, dict):
        raise CleanupReceiptError("cleanup receipt must be an object")
    job_id = _canonical_job_id(job.id)
    if field == SOURCE_COPY_CLEANUP_FIELD:
        attempt = _canonical_uuid(raw.get("copy_attempt_id"), label="copy_attempt_id")
        expected_prefix = source_copy_attempt_prefix(job_id, attempt)
        locator = CleanupReceiptLocator(field=field, receipt_id=attempt)
    elif raw.get("kind") == "exact_keys":
        debt_id = _canonical_uuid(raw.get("debt_id"), label="debt_id")
        if raw.get("upload_state") != "closed":
            raise CleanupReceiptError("exact cleanup debt must be closed")
        raw_paths = raw.get("paths")
        if not isinstance(raw_paths, list):
            raise CleanupReceiptError("exact cleanup paths must be a list")
        paths: list[str] = []
        for raw_path in raw_paths:
            path = owned_job_output_path(raw_path, job)
            if path is None:
                raise CleanupReceiptError("exact cleanup path is not owned by the Job")
            if path not in paths:
                paths.append(path)
        if not paths or len(paths) > EXACT_KEY_PATH_CAP:
            raise CleanupReceiptError("exact cleanup path count is invalid")
        return ExactKeysCleanupReceipt(
            locator=CleanupReceiptLocator(field=field, receipt_id=debt_id),
            paths=tuple(paths),
        )
    else:
        generation = _canonical_uuid(raw.get("generation"), label="generation")
        expected_prefix = render_generation_prefix(job_id, generation)
        locator = CleanupReceiptLocator(field=field, receipt_id=generation)

    if raw.get("prefix") != expected_prefix:
        raise CleanupReceiptError("cleanup prefix does not match its owner token")
    upload_state = raw.get("upload_state")
    if upload_state not in {"writing", "closed"}:
        raise CleanupReceiptError("upload_state is invalid")
    return PrefixCleanupReceipt(
        locator=locator,
        prefix=expected_prefix,
        upload_state=upload_state,
        lease_expires_at=_parse_lease(raw.get("lease_expires_at")),
    )


def iter_cleanup_receipts(plan: dict[str, Any], *, job: Job) -> Iterator[ParsedCleanupReceipt]:
    for field in CLEANUP_LIST_FIELDS:
        receipts = _receipt_list(plan, field, create=False)
        if receipts is None:
            continue
        for raw in receipts:
            yield parse_cleanup_receipt(job=job, field=field, raw=raw)


def cleanup_debt_present(plan: object) -> bool:
    """Fail-closed quiescence predicate for deletion/cancellation guards."""
    if not isinstance(plan, dict):
        return plan is not None
    if SPEECH_CLEANUP_INTERNAL_FIELD not in plan:
        return False
    internal = plan.get(SPEECH_CLEANUP_INTERNAL_FIELD)
    if not isinstance(internal, dict):
        return True
    for field in CLEANUP_LIST_FIELDS:
        if field not in internal:
            continue
        receipts = internal.get(field)
        if not isinstance(receipts, list) or receipts:
            return True
    return False


def job_render_not_quiescent(job: Any) -> bool:
    """Fail-closed deletion guard for public and private render ownership.

    Coarse ``Job.status`` is insufficient: editor rerenders keep a terminal
    job-level status while a variant, private required-speech generation, or
    durable cleanup receipt still owns future storage writes.
    """
    plan = getattr(job, "assembly_plan", None)
    if plan is None:
        return False
    if not isinstance(plan, dict):
        return True
    variants = plan.get("variants")
    if variants is not None:
        if not isinstance(variants, list):
            return True
        for variant in variants:
            if not isinstance(variant, dict):
                return True
            if variant.get("render_status") in {"pending", "rendering"}:
                return True

    control = plan.get("speech_cut_control")
    if control not in (None, {}):
        return True
    internal = plan.get(SPEECH_CLEANUP_INTERNAL_FIELD)
    if internal is not None:
        if not isinstance(internal, dict):
            return True
        ownership_fields = {
            "required_speech_generation_locks",
            "staged_render_results",
            "working_render_variants",
            "terminal_pending",
        }
        for field in ownership_fields:
            if field not in internal:
                continue
            value = internal[field]
            if not isinstance(value, dict) or value:
                return True
        known_fields = set(CLEANUP_LIST_FIELDS) | ownership_fields
        # Unknown private state has no proven quiescence semantics. Refuse a
        # destructive Job delete until a version-aware reconciler owns it.
        if any(field not in known_fields for field in internal):
            return True
    return cleanup_debt_present(plan)


def mark_cleanup_receipt_closed(
    plan: dict[str, Any],
    locator: CleanupReceiptLocator,
) -> bool:
    receipts = _receipt_list(plan, locator.field, create=False)
    if receipts is None:
        return False
    matches = [raw for raw in receipts if _raw_receipt_id(locator.field, raw) == locator.receipt_id]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise CleanupReceiptError("cleanup receipt ownership is missing or ambiguous")
    if matches[0].get("kind") == "exact_keys":
        if matches[0].get("upload_state") != "closed":
            raise CleanupReceiptError("exact cleanup debt cannot be opened")
        return False
    if matches[0].get("upload_state") == "closed":
        return False
    if matches[0].get("upload_state") != "writing":
        raise CleanupReceiptError("cleanup receipt upload_state is malformed")
    matches[0]["upload_state"] = "closed"
    return True


def remove_cleanup_receipt(
    plan: dict[str, Any],
    locator: CleanupReceiptLocator,
) -> bool:
    receipts = _receipt_list(plan, locator.field, create=False)
    if receipts is None:
        return False
    indexes = [
        index
        for index, raw in enumerate(receipts)
        if _raw_receipt_id(locator.field, raw) == locator.receipt_id
    ]
    if not indexes:
        return False
    if len(indexes) != 1:
        raise CleanupReceiptError("cleanup receipt ownership is ambiguous")
    receipts.pop(indexes[0])
    _remove_empty_cleanup_keys(plan)
    return True


def rotate_retained_cleanup_receipt(
    plan: dict[str, Any],
    locator: CleanupReceiptLocator,
    *,
    status: str,
    attempted_at: datetime,
    error_class: str | None = None,
) -> bool:
    """Move one retained receipt to its list tail with bounded diagnostics."""
    receipts = _receipt_list(plan, locator.field, create=False)
    if receipts is None:
        return False
    indexes = [
        index
        for index, raw in enumerate(receipts)
        if _raw_receipt_id(locator.field, raw) == locator.receipt_id
    ]
    if len(indexes) != 1:
        raise CleanupReceiptError("cleanup receipt ownership is missing or ambiguous")
    raw = receipts[indexes[0]]
    if not isinstance(raw, dict):
        raise CleanupReceiptError("cleanup receipt is malformed")
    rotated = copy.deepcopy(raw)
    try:
        failure_count = int(rotated.get("failure_count") or 0)
    except (TypeError, ValueError) as exc:
        raise CleanupReceiptError("cleanup failure_count is malformed") from exc
    rotated["failure_count"] = min(max(failure_count, 0) + 1, 2**31 - 1)
    rotated["last_status"] = str(status)[:FAILURE_CLASS_MAX_LENGTH]
    rotated["last_attempt_at"] = _serialize_lease(attempted_at)
    if error_class:
        bounded_class = _SAFE_ERROR_CLASS.sub("_", error_class)[:FAILURE_CLASS_MAX_LENGTH]
        rotated["last_error_class"] = bounded_class or "unknown"
    else:
        rotated.pop("last_error_class", None)
    receipts.pop(indexes[0])
    receipts.append(rotated)
    return True


def _rotate_raw_receipt_at_index(
    receipts: list[Any],
    index: int,
    *,
    status: str,
    attempted_at: datetime,
) -> None:
    """Rotate even an unaddressable malformed receipt without dropping it."""
    raw = copy.deepcopy(receipts[index])
    if isinstance(raw, dict):
        raw["last_status"] = str(status)[:FAILURE_CLASS_MAX_LENGTH]
        raw["last_attempt_at"] = _serialize_lease(attempted_at)
        value = raw.get("failure_count")
        if isinstance(value, int) and not isinstance(value, bool):
            raw["failure_count"] = min(max(value, 0) + 1, 2**31 - 1)
        elif value is None:
            raw["failure_count"] = 1
        # Preserve a malformed counter verbatim; diagnostics may never repair
        # or erase the debt shape merely to make it parseable.
    receipts.pop(index)
    receipts.append(raw)


def _remove_excluded_receipt(
    plan: dict[str, Any],
    locator: CleanupReceiptLocator,
) -> dict[str, Any]:
    candidate = copy.deepcopy(plan)
    receipts = _receipt_list(candidate, locator.field, create=False)
    if receipts is None:
        raise CleanupReceiptError("excluded cleanup receipt is missing")
    indexes = [
        index
        for index, raw in enumerate(receipts)
        if _raw_receipt_id(locator.field, raw) == locator.receipt_id
    ]
    if len(indexes) != 1:
        raise CleanupReceiptError("excluded cleanup receipt is missing or ambiguous")
    receipts.pop(indexes[0])
    _remove_empty_cleanup_keys(candidate)
    return candidate


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            # JSON keys can resemble paths but are labels, never references.
            if isinstance(key, str):
                yield from _walk_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_strings(nested)


def prove_job_storage_references(
    job: Job,
    *,
    clips: Iterable[JobClip] = (),
    exclude_receipt: CleanupReceiptLocator | None = None,
    assembly_plan_override: object = _PLAN_UNSET,
) -> StorageReferenceProof:
    """Collect every owned persisted path, excluding one canonical debt receipt.

    The generic recursive scan is intentionally conservative: newly added
    media-bearing fields are protected automatically. Only strings resolving
    under this Job's owned output prefixes can become references.
    """
    try:
        plan = (
            job.assembly_plan if assembly_plan_override is _PLAN_UNSET else assembly_plan_override
        )
        if plan is None:
            plan_for_scan: dict[str, Any] = {}
        elif not isinstance(plan, dict):
            return StorageReferenceProof(
                status="unavailable",
                reason="assembly_plan_not_object",
            )
        elif exclude_receipt is not None:
            plan_for_scan = _remove_excluded_receipt(plan, exclude_receipt)
        else:
            plan_for_scan = plan

        values: list[object] = [
            getattr(job, "raw_storage_path", None),
            getattr(job, "all_candidates", None),
            plan_for_scan,
            getattr(job, "probe_metadata", None),
            getattr(job, "transcript", None),
            getattr(job, "scene_cuts", None),
        ]
        for clip in clips:
            values.extend(
                [
                    getattr(clip, "video_path", None),
                    getattr(clip, "thumbnail_path", None),
                ]
            )

        references: set[str] = set()
        for value in values:
            for raw_path in _walk_strings(value):
                path = owned_job_output_path(raw_path, job)
                if path is not None:
                    references.add(path)
        return StorageReferenceProof(status="proved", references=frozenset(references))
    except Exception:  # noqa: BLE001 — destructive proof fails closed
        return StorageReferenceProof(status="unavailable", reason="reference_scan_failed")


def classify_prefix_cleanup(
    receipt: PrefixCleanupReceipt,
    proof: StorageReferenceProof,
    *,
    now: datetime,
    allow_source_adoption: bool = False,
    committed_source_paths: Sequence[str] = (),
) -> PrefixCleanupDecision:
    """Decide whether a fresh, row-locked prefix receipt may reach storage I/O."""
    if now.tzinfo is None:
        raise CleanupReceiptError("now must be timezone-aware")
    if not proof.proved:
        return PrefixCleanupDecision(disposition="unavailable")
    references = tuple(sorted(path for path in proof.references if path.startswith(receipt.prefix)))
    if references:
        uploads_quiescent = (
            receipt.upload_state == "closed" or receipt.lease_expires_at <= now.astimezone(UTC)
        )
        if allow_source_adoption and uploads_quiescent:
            normalized_sources = tuple(
                path
                for raw_path in committed_source_paths
                if (path := owned_job_output_path(raw_path, _JobIdentityProxy(receipt.prefix)))
                is not None
            )
            # The proxy above only accepts this exact attempt prefix. Every
            # committed attempt-owned path must be represented in the proof,
            # and no proof reference under the prefix may be hidden elsewhere.
            if normalized_sources and set(normalized_sources) == set(references):
                return PrefixCleanupDecision(
                    disposition="adopted_live",
                    references=references,
                )
        return PrefixCleanupDecision(disposition="referenced", references=references)
    if receipt.upload_state == "writing" and receipt.lease_expires_at > now.astimezone(UTC):
        return PrefixCleanupDecision(disposition="wait_for_uploads")
    return PrefixCleanupDecision(disposition="delete")


class _JobIdentityProxy:
    """Narrow owned-path adapter used only for source-adoption equality."""

    def __init__(self, prefix: str) -> None:
        parts = prefix.split("/")
        self.id = uuid.UUID(parts[1])
        self.user_id = uuid.UUID(int=0)


def coalesce_exact_key_cleanup(plan: dict[str, Any], *, job: Job) -> bool:
    """Coalesce overlapping exact-key debts without dropping malformed state."""
    receipts = _receipt_list(plan, RENDER_GENERATION_CLEANUP_FIELD, create=False)
    if receipts is None:
        return False

    parsed: list[ParsedCleanupReceipt] = [
        parse_cleanup_receipt(job=job, field=RENDER_GENERATION_CLEANUP_FIELD, raw=raw)
        for raw in receipts
    ]
    exact_indexes = [
        index
        for index, receipt in enumerate(parsed)
        if isinstance(receipt, ExactKeysCleanupReceipt)
    ]
    if len(exact_indexes) < 2:
        return False

    groups: list[dict[str, Any]] = []
    for index in exact_indexes:
        receipt = parsed[index]
        assert isinstance(receipt, ExactKeysCleanupReceipt)
        overlapping = [group for group in groups if group["paths"] & set(receipt.paths)]
        if not overlapping:
            groups.append(
                {
                    "first_index": index,
                    "ids": [receipt.locator.receipt_id],
                    "paths": set(receipt.paths),
                }
            )
            continue
        merged = overlapping[0]
        merged["first_index"] = min(merged["first_index"], index)
        merged["ids"].append(receipt.locator.receipt_id)
        merged["paths"].update(receipt.paths)
        for extra in overlapping[1:]:
            merged["first_index"] = min(merged["first_index"], extra["first_index"])
            merged["ids"].extend(extra["ids"])
            merged["paths"].update(extra["paths"])
            groups.remove(extra)

    if all(len(group["ids"]) == 1 for group in groups):
        return False
    for group in groups:
        if len(group["paths"]) > EXACT_KEY_PATH_CAP:
            raise CleanupReceiptBackpressure("coalesced exact cleanup path cap exceeded")

    group_by_index = {group["first_index"]: group for group in groups}
    grouped_ids = {receipt_id for group in groups for receipt_id in group["ids"]}
    rewritten: list[Any] = []
    for index, (raw, receipt) in enumerate(zip(receipts, parsed, strict=True)):
        if not isinstance(receipt, ExactKeysCleanupReceipt):
            rewritten.append(raw)
            continue
        group = group_by_index.get(index)
        if group is not None:
            rewritten.append(
                {
                    "debt_id": min(group["ids"]),
                    "kind": "exact_keys",
                    "paths": sorted(group["paths"]),
                    "upload_state": "closed",
                }
            )
        elif receipt.locator.receipt_id not in grouped_ids:
            rewritten.append(raw)
    receipts[:] = rewritten
    return True


def jobs_with_storage_attempt_cleanup_receipts(db: Any, *, limit: int) -> list[uuid.UUID]:
    """Return an indexed, bounded page of Jobs with private cleanup debt."""
    if limit < 1:
        return []
    private = Job.assembly_plan.op("->")(literal_column(f"'{SPEECH_CLEANUP_INTERNAL_FIELD}'"))
    return list(
        db.execute(
            select(Job.id)
            .where(
                func.jsonb_typeof(private) == literal_column("'object'"),
                or_(
                    private.op("?")(literal_column(f"'{SOURCE_COPY_CLEANUP_FIELD}'")),
                    private.op("?")(literal_column(f"'{RENDER_GENERATION_CLEANUP_FIELD}'")),
                ),
            )
            .order_by(Job.updated_at.asc(), Job.id.asc())
            .limit(limit)
        ).scalars()
    )


def _locked_job_clips(db: Any, job: Job) -> tuple[JobClip, ...]:
    """Load every JobClip reference in the destructive row-lock scope."""
    # Pure unit-test sessions intentionally omit SQL execution and model jobs
    # without clip rows. A real SQLAlchemy Session always takes this query.
    if not hasattr(db, "execute"):
        return ()
    return tuple(
        db.execute(select(JobClip).where(JobClip.job_id == job.id).with_for_update())
        .scalars()
        .all()
    )


def _remaining_cleanup_storage_timeout(deadline: float) -> float | None:
    """Return one no-retry request timeout inside the lock-hold budget."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(CLEANUP_STORAGE_REQUEST_TIMEOUT_S, remaining)


def reconcile_durable_source_copy_cleanup_locked(
    db: Any,
    job: Job,
    *,
    limit: int = 2,
    now: datetime | None = None,
) -> CleanupReconcileResult:
    """Reconcile bounded source-copy receipts while the Job row lock is held.

    A stale receipt adopted by the exact committed source vector is consumed
    without deleting live bytes. Every abandoned prefix clears only after a
    successful empty re-list. Retained work is rotated to the tail and the Job
    write advances ``updated_at``, preventing one outage from starving later
    indexed rows.
    """
    if limit < 1:
        return CleanupReconcileResult()
    raw_now = now or datetime.now(UTC)
    if raw_now.tzinfo is None:
        raise CleanupReceiptError("now must be timezone-aware")
    attempted_at = raw_now.astimezone(UTC)
    clips = _locked_job_clips(db, job)
    if job.assembly_plan is None:
        return CleanupReconcileResult()
    if not isinstance(job.assembly_plan, dict):
        job.assembly_plan = copy.deepcopy(job.assembly_plan)
        flag_modified(job, "assembly_plan")
        db.commit()
        return CleanupReconcileResult(receipts_seen=1, retained=1, failures=1)
    plan = copy.deepcopy(job.assembly_plan)
    try:
        receipts = _receipt_list(plan, SOURCE_COPY_CLEANUP_FIELD, create=False)
    except CleanupReceiptError:
        job.assembly_plan = plan
        flag_modified(job, "assembly_plan")
        db.commit()
        return CleanupReconcileResult(receipts_seen=1, retained=1, failures=1)
    if receipts is None:
        return CleanupReconcileResult()
    if not receipts:
        changed = prune_empty_cleanup_keys(plan)
        if changed:
            job.assembly_plan = plan
            flag_modified(job, "assembly_plan")
            db.commit()
        return CleanupReconcileResult()

    candidates = getattr(job, "all_candidates", None)
    raw_source_paths = candidates.get("clip_paths") if isinstance(candidates, dict) else None
    source_paths = (
        list(raw_source_paths)
        if isinstance(raw_source_paths, list)
        and raw_source_paths
        and all(isinstance(path, str) and path for path in raw_source_paths)
        else None
    )
    # One receipt per locked transaction keeps the maximum storage wait fixed;
    # later receipts remain durable and are selected by the next sweep.
    initial_count = min(limit, len(receipts), CLEANUP_IO_RECEIPT_CAP)
    deleted = 0
    adopted_live = 0
    failures = 0

    from app.storage import delete_prefix_verified  # noqa: PLC0415

    for _ in range(initial_count):
        raw = receipts[0]
        try:
            parsed = parse_cleanup_receipt(
                job=job,
                field=SOURCE_COPY_CLEANUP_FIELD,
                raw=raw,
            )
        except CleanupReceiptError:
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="malformed",
                attempted_at=attempted_at,
            )
            failures += 1
            continue
        assert isinstance(parsed, PrefixCleanupReceipt)
        if source_paths is None:
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="reference_unavailable",
                attempted_at=attempted_at,
            )
            failures += 1
            continue
        proof = prove_job_storage_references(
            job,
            clips=clips,
            exclude_receipt=parsed.locator,
        )
        decision = classify_prefix_cleanup(
            parsed,
            proof,
            now=attempted_at,
            allow_source_adoption=True,
            committed_source_paths=source_paths,
        )
        if decision.disposition == "adopted_live":
            try:
                remove_cleanup_receipt(plan, parsed.locator)
            except CleanupReceiptError:
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status="ownership_ambiguous",
                    attempted_at=attempted_at,
                )
                failures += 1
            else:
                adopted_live += 1
            continue
        if decision.disposition != "delete":
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status=decision.disposition,
                attempted_at=attempted_at,
            )
            failures += decision.disposition == "unavailable"
            continue

        deletion = delete_prefix_verified(
            parsed.prefix,
            timeout_s=CLEANUP_STORAGE_REQUEST_TIMEOUT_S,
        )
        if deletion.status != "verified_empty":
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status=deletion.status,
                attempted_at=attempted_at,
            )
            failures += 1
            continue
        # Re-prove from the row-locked state immediately before clearing the
        # durable receipt. A later integration may release the lock during I/O;
        # this second proof remains mandatory in either arrangement.
        final_proof = prove_job_storage_references(
            job,
            clips=clips,
            exclude_receipt=parsed.locator,
        )
        final_decision = classify_prefix_cleanup(
            parsed,
            final_proof,
            now=attempted_at,
            allow_source_adoption=True,
            committed_source_paths=source_paths,
        )
        if final_decision.disposition != "delete":
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="reference_changed",
                attempted_at=attempted_at,
            )
            failures += 1
            continue
        try:
            remove_cleanup_receipt(plan, parsed.locator)
        except CleanupReceiptError:
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="ownership_ambiguous",
                attempted_at=attempted_at,
            )
            failures += 1
        else:
            deleted += 1

    prune_empty_cleanup_keys(plan)
    job.assembly_plan = plan
    flag_modified(job, "assembly_plan")
    db.commit()
    remaining = _receipt_list(plan, SOURCE_COPY_CLEANUP_FIELD, create=False) or []
    return CleanupReconcileResult(
        receipts_seen=initial_count,
        deleted=deleted,
        adopted_live=adopted_live,
        retained=len(remaining),
        failures=failures,
    )


def reconcile_durable_source_copy_cleanup(
    job_id: str | uuid.UUID,
    *,
    limit: int = 2,
) -> CleanupReconcileResult:
    job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    with sync_session() as db:
        job = db.get(Job, job_uuid, with_for_update=True)
        if job is None:
            return CleanupReconcileResult()
        return reconcile_durable_source_copy_cleanup_locked(db, job, limit=limit)


def _replace_exact_receipt_paths(
    plan: dict[str, Any],
    locator: CleanupReceiptLocator,
    paths: Sequence[str],
) -> None:
    receipts = _receipt_list(plan, locator.field, create=False)
    if receipts is None:
        raise CleanupReceiptError("cleanup receipt ownership is missing")
    indexes = [
        index
        for index, raw in enumerate(receipts)
        if _raw_receipt_id(locator.field, raw) == locator.receipt_id
    ]
    if len(indexes) != 1 or not isinstance(receipts[indexes[0]], dict):
        raise CleanupReceiptError("cleanup receipt ownership is missing or ambiguous")
    raw = receipts[indexes[0]]
    if raw.get("kind") != "exact_keys":
        raise CleanupReceiptError("cleanup receipt is not exact-key debt")
    raw["paths"] = list(paths)


def _value_names_render_generation(
    value: object,
    *,
    generation: str,
    prefix: str,
) -> bool:
    if isinstance(value, str):
        return value == generation or value.startswith(prefix)
    if isinstance(value, dict):
        return any(
            _value_names_render_generation(
                nested,
                generation=generation,
                prefix=prefix,
            )
            for nested in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _value_names_render_generation(
                nested,
                generation=generation,
                prefix=prefix,
            )
            for nested in value
        )
    return False


def _exact_live_render_generation_paths(
    job: Job,
    plan: dict[str, Any],
    receipt: PrefixCleanupReceipt,
    proof: StorageReferenceProof,
) -> tuple[str, ...] | None:
    """Return exact published paths when a closed receipt is healthy live state.

    This is deliberately stricter than finding any reference under the prefix.
    A hard-kill can leave the same generation mentioned by a public placeholder,
    private stage, lock, or terminal capsule.  Beat may consume the receipt only
    when one successful public variant is the *only* owner of every prefix path.
    """

    if receipt.upload_state != "closed" or not proof.proved:
        return None
    variants = plan.get("variants")
    if not isinstance(variants, list):
        return None
    generation = receipt.locator.receipt_id
    matches = [
        variant
        for variant in variants
        if isinstance(variant, dict) and variant.get("render_generation_id") == generation
    ]
    if len(matches) != 1:
        return None
    published = matches[0]
    if published.get("render_status") != "ready" or published.get("ok") is not True:
        return None

    if _value_names_render_generation(
        plan.get("speech_cut_control"),
        generation=generation,
        prefix=receipt.prefix,
    ):
        return None
    internal = plan.get(SPEECH_CLEANUP_INTERNAL_FIELD)
    if not isinstance(internal, dict):
        return None
    for field in (
        "required_speech_generation_locks",
        "staged_render_results",
        "working_render_variants",
        "terminal_pending",
    ):
        if _value_names_render_generation(
            internal.get(field),
            generation=generation,
            prefix=receipt.prefix,
        ):
            return None

    published_paths = {
        path
        for raw_path in _walk_strings(published)
        if (path := owned_job_output_path(raw_path, job)) is not None
        and path.startswith(receipt.prefix)
    }
    referenced_paths = {path for path in proof.references if path.startswith(receipt.prefix)}
    if not published_paths or published_paths != referenced_paths:
        return None
    return tuple(sorted(published_paths))


def reconcile_render_generation_cleanup_locked(
    db: Any,
    job: Job,
    *,
    limit: int = 2,
    now: datetime | None = None,
) -> CleanupReconcileResult:
    """Reconcile retired render prefixes and legacy exact-key cleanup debt.

    A closed receipt left behind after a partial terminal transaction may be
    consumed without deleting bytes only when the exact ready public variant
    is the sole owner of every prefix reference and every referenced object
    still exists. Abandoned prefixes clear only after list-delete-relist proves
    them empty. Exact keys clear only after delete, HEAD absence, and a second
    self-excluding reference proof.
    """
    if limit < 1:
        return CleanupReconcileResult()
    raw_now = now or datetime.now(UTC)
    if raw_now.tzinfo is None:
        raise CleanupReceiptError("now must be timezone-aware")
    attempted_at = raw_now.astimezone(UTC)
    clips = _locked_job_clips(db, job)
    if job.assembly_plan is None:
        return CleanupReconcileResult()
    if not isinstance(job.assembly_plan, dict):
        job.assembly_plan = copy.deepcopy(job.assembly_plan)
        flag_modified(job, "assembly_plan")
        db.commit()
        return CleanupReconcileResult(receipts_seen=1, retained=1, failures=1)

    plan = copy.deepcopy(job.assembly_plan)
    try:
        receipts = _receipt_list(plan, RENDER_GENERATION_CLEANUP_FIELD, create=False)
    except CleanupReceiptError:
        job.assembly_plan = plan
        flag_modified(job, "assembly_plan")
        db.commit()
        return CleanupReconcileResult(receipts_seen=1, retained=1, failures=1)
    if receipts is None:
        return CleanupReconcileResult()
    if not receipts:
        if prune_empty_cleanup_keys(plan):
            job.assembly_plan = plan
            flag_modified(job, "assembly_plan")
            db.commit()
        return CleanupReconcileResult()

    # Coalescing must precede exact-key I/O. Otherwise one duplicate receipt
    # would self-exclude while the other still proves the key is referenced.
    try:
        coalesce_exact_key_cleanup(plan, job=job)
    except CleanupReceiptError:
        _rotate_raw_receipt_at_index(
            receipts,
            0,
            status="malformed",
            attempted_at=attempted_at,
        )
        job.assembly_plan = plan
        flag_modified(job, "assembly_plan")
        db.commit()
        return CleanupReconcileResult(
            receipts_seen=1,
            retained=len(receipts),
            failures=1,
        )

    receipts = _receipt_list(plan, RENDER_GENERATION_CLEANUP_FIELD, create=False)
    assert receipts is not None
    # One receipt per locked transaction keeps the maximum storage wait fixed;
    # later receipts remain durable and are selected by the next sweep.
    initial_count = min(limit, len(receipts), CLEANUP_IO_RECEIPT_CAP)
    deleted = 0
    adopted_live = 0
    failures = 0
    storage_deadline = time.monotonic() + CLEANUP_STORAGE_TOTAL_BUDGET_S

    from app.storage import (  # noqa: PLC0415
        delete_object_once,
        delete_prefix_verified,
        object_exists_once,
    )

    for _ in range(initial_count):
        raw = receipts[0]
        try:
            parsed = parse_cleanup_receipt(
                job=job,
                field=RENDER_GENERATION_CLEANUP_FIELD,
                raw=raw,
            )
        except CleanupReceiptError:
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="malformed",
                attempted_at=attempted_at,
            )
            failures += 1
            continue

        proof = prove_job_storage_references(
            job,
            clips=clips,
            exclude_receipt=parsed.locator,
            assembly_plan_override=plan,
        )
        if not proof.proved:
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="reference_unavailable",
                attempted_at=attempted_at,
            )
            failures += 1
            continue

        if isinstance(parsed, PrefixCleanupReceipt):
            decision = classify_prefix_cleanup(parsed, proof, now=attempted_at)
            if decision.disposition == "referenced":
                live_paths = _exact_live_render_generation_paths(
                    job,
                    plan,
                    parsed,
                    proof,
                )
                if live_paths is not None:
                    all_exist = len(live_paths) <= CLEANUP_LIVE_PATH_CAP_PER_PASS
                    if all_exist:
                        try:
                            for path in live_paths:
                                request_timeout = _remaining_cleanup_storage_timeout(
                                    storage_deadline
                                )
                                if request_timeout is None or not object_exists_once(
                                    path,
                                    timeout_s=request_timeout,
                                ):
                                    all_exist = False
                                    break
                        except SoftTimeLimitExceeded:
                            raise
                        except Exception:  # noqa: BLE001 — live proof fails closed
                            all_exist = False
                    final_proof = prove_job_storage_references(
                        job,
                        clips=clips,
                        exclude_receipt=parsed.locator,
                        assembly_plan_override=plan,
                    )
                    final_live_paths = _exact_live_render_generation_paths(
                        job,
                        plan,
                        parsed,
                        final_proof,
                    )
                    if all_exist and final_live_paths == live_paths:
                        try:
                            remove_cleanup_receipt(plan, parsed.locator)
                        except CleanupReceiptError:
                            _rotate_raw_receipt_at_index(
                                receipts,
                                0,
                                status="ownership_ambiguous",
                                attempted_at=attempted_at,
                            )
                            failures += 1
                        else:
                            adopted_live += 1
                        continue
                    _rotate_raw_receipt_at_index(
                        receipts,
                        0,
                        status="live_object_unavailable",
                        attempted_at=attempted_at,
                    )
                    failures += 1
                    continue
            if decision.disposition != "delete":
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status=decision.disposition,
                    attempted_at=attempted_at,
                )
                failures += decision.disposition == "unavailable"
                continue
            request_timeout = _remaining_cleanup_storage_timeout(storage_deadline)
            if request_timeout is None:
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status="budget_exhausted",
                    attempted_at=attempted_at,
                )
                failures += 1
                continue
            deletion = delete_prefix_verified(
                parsed.prefix,
                timeout_s=request_timeout,
            )
            if deletion.status != "verified_empty":
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status=deletion.status,
                    attempted_at=attempted_at,
                )
                failures += 1
                continue
            final_proof = prove_job_storage_references(
                job,
                clips=clips,
                exclude_receipt=parsed.locator,
                assembly_plan_override=plan,
            )
            final_decision = classify_prefix_cleanup(
                parsed,
                final_proof,
                now=attempted_at,
            )
            if final_decision.disposition != "delete":
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status="reference_changed",
                    attempted_at=attempted_at,
                )
                failures += 1
                continue
            try:
                remove_cleanup_receipt(plan, parsed.locator)
            except CleanupReceiptError:
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status="ownership_ambiguous",
                    attempted_at=attempted_at,
                )
                failures += 1
            else:
                deleted += 1
            continue

        if set(parsed.paths) & set(proof.references):
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="referenced",
                attempted_at=attempted_at,
            )
            continue

        bounded_paths = parsed.paths[:CLEANUP_EXACT_PATH_CAP_PER_PASS]
        # Unprocessed paths remain debt without touching storage. The path cap
        # plus total deadline bounds this branch to at most four remote calls.
        failed_paths: list[str] = list(parsed.paths[CLEANUP_EXACT_PATH_CAP_PER_PASS:])
        for index, path in enumerate(bounded_paths):
            try:
                request_timeout = _remaining_cleanup_storage_timeout(storage_deadline)
                if request_timeout is None:
                    failed_paths.extend(bounded_paths[index:])
                    break
                delete_object_once(path, timeout_s=request_timeout)
                request_timeout = _remaining_cleanup_storage_timeout(storage_deadline)
                if request_timeout is None:
                    # The delete may have succeeded, but absence was not proved.
                    failed_paths.extend(bounded_paths[index:])
                    break
                if object_exists_once(path, timeout_s=request_timeout):
                    failed_paths.append(path)
            except SoftTimeLimitExceeded:
                raise
            except Exception:  # noqa: BLE001 — durable debt retains target
                failed_paths.append(path)

        final_proof = prove_job_storage_references(
            job,
            clips=clips,
            exclude_receipt=parsed.locator,
            assembly_plan_override=plan,
        )
        if not final_proof.proved or set(parsed.paths) & set(final_proof.references):
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="reference_changed",
                attempted_at=attempted_at,
            )
            failures += 1
            continue
        if failed_paths:
            try:
                _replace_exact_receipt_paths(
                    plan,
                    parsed.locator,
                    sorted(set(failed_paths)),
                )
                rotate_retained_cleanup_receipt(
                    plan,
                    parsed.locator,
                    status="partial",
                    attempted_at=attempted_at,
                )
            except CleanupReceiptError:
                _rotate_raw_receipt_at_index(
                    receipts,
                    0,
                    status="ownership_ambiguous",
                    attempted_at=attempted_at,
                )
            failures += 1
            continue
        try:
            remove_cleanup_receipt(plan, parsed.locator)
        except CleanupReceiptError:
            _rotate_raw_receipt_at_index(
                receipts,
                0,
                status="ownership_ambiguous",
                attempted_at=attempted_at,
            )
            failures += 1
        else:
            deleted += 1

    prune_empty_cleanup_keys(plan)
    job.assembly_plan = plan
    flag_modified(job, "assembly_plan")
    db.commit()
    remaining = _receipt_list(plan, RENDER_GENERATION_CLEANUP_FIELD, create=False) or []
    return CleanupReconcileResult(
        receipts_seen=initial_count,
        deleted=deleted,
        adopted_live=adopted_live,
        retained=len(remaining),
        failures=failures,
    )


def reconcile_render_generation_cleanup(
    job_id: str | uuid.UUID,
    *,
    limit: int = 2,
) -> CleanupReconcileResult:
    job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    with sync_session() as db:
        job = db.get(Job, job_uuid, with_for_update=True)
        if job is None:
            return CleanupReconcileResult()
        return reconcile_render_generation_cleanup_locked(db, job, limit=limit)


def reconcile_storage_attempt_cleanup(
    job_id: str | uuid.UUID,
    *,
    source_limit: int = 2,
    render_limit: int = 2,
) -> CleanupReconcileResult:
    """Run both bounded receipt families for one fleet-sweep candidate."""
    source = reconcile_durable_source_copy_cleanup(job_id, limit=source_limit)
    render = reconcile_render_generation_cleanup(job_id, limit=render_limit)
    return CleanupReconcileResult(
        receipts_seen=source.receipts_seen + render.receipts_seen,
        deleted=source.deleted + render.deleted,
        adopted_live=source.adopted_live + render.adopted_live,
        retained=source.retained + render.retained,
        failures=source.failures + render.failures,
    )
