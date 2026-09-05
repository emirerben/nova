"""Stable source identity for speech-cleanup analysis and clip-list mutations.

``clip_paths`` indexes are editor-facing positions, not durable identities.  A
generated clip can be removed and a different clip appended at the same index,
so rollout assignment and cached metadata must bind to a never-reused UUID that
moves atomically with its path.

The helpers in this module are deliberately copy-on-write and database-agnostic.
Callers remain responsible for holding the Job row lock while they persist the
returned ``all_candidates`` value.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

CLIP_PATHS_FIELD = "clip_paths"
CLIP_SOURCE_INSTANCE_IDS_FIELD = "clip_source_instance_ids"
CLIP_METADATA_IDENTITY_INDEX_KEY = "clip_metadata_identity_index_v2"
CLIP_METADATA_IDENTITY_INDEX_VERSION = 2

SourceIdentityStatus: TypeAlias = Literal[
    "assigned",
    "missing_source_instance",
    "cardinality_mismatch",
    "invalid_source_instance",
    "duplicate_source_instance",
]
SpeechCleanupAssignmentStatus: TypeAlias = Literal[
    "assigned",
    "missing_source_instance",
    "cardinality_mismatch",
    "invalid_source_instance",
    "duplicate_source_instance",
    "unmapped_clip_id",
    "ambiguous_clip_id",
    "identity_cache_unavailable",
]

_IdentityFactory: TypeAlias = Callable[[], uuid.UUID | str]


@dataclass(frozen=True)
class ClipSourceIdentity:
    """Validation result for one ordered path/instance vector."""

    status: SourceIdentityStatus
    clip_paths: tuple[str, ...]
    source_instance_ids: tuple[str, ...]
    provisioned: bool = False

    @property
    def valid(self) -> bool:
        return self.status == "assigned"

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        if not self.valid:
            return ()
        return tuple(zip(self.clip_paths, self.source_instance_ids, strict=True))


@dataclass(frozen=True)
class SpeechCleanupAssignment:
    """Identity-derived treatment assignment carried beside a rendered clip."""

    source_slot: int | None
    rollout_fingerprint: str | None
    status: SpeechCleanupAssignmentStatus


class ClipSourceIdentityError(ValueError):
    """A clip-list mutation could not preserve the persisted identity vector."""

    def __init__(self, status: SourceIdentityStatus) -> None:
        self.status = status
        super().__init__(f"clip_source_identity_unavailable:{status}")


def _canonical_uuid(value: object) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        return None
    return str(parsed)


def _clip_paths(candidates: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw_paths = candidates.get(CLIP_PATHS_FIELD, [])
    if not isinstance(raw_paths, list) or any(
        not isinstance(path, str) or not path.strip() for path in raw_paths
    ):
        return None
    return tuple(raw_paths)


def validate_clip_source_identity(candidates: Mapping[str, Any]) -> ClipSourceIdentity:
    """Validate a persisted ordered path/UUID vector without mutating it.

    Only a wholly absent identity field is considered provisionable.  A present
    malformed field remains an explicit failure so retries cannot silently move
    a source into a different rollout bucket.
    """

    paths = _clip_paths(candidates)
    if paths is None:
        return ClipSourceIdentity("cardinality_mismatch", (), ())
    if CLIP_SOURCE_INSTANCE_IDS_FIELD not in candidates:
        return ClipSourceIdentity("missing_source_instance", paths, ())

    raw_ids = candidates.get(CLIP_SOURCE_INSTANCE_IDS_FIELD)
    if not isinstance(raw_ids, list):
        return ClipSourceIdentity("invalid_source_instance", paths, ())
    if len(raw_ids) != len(paths):
        return ClipSourceIdentity("cardinality_mismatch", paths, ())

    canonical_ids: list[str] = []
    for value in raw_ids:
        canonical = _canonical_uuid(value)
        if canonical is None:
            return ClipSourceIdentity("invalid_source_instance", paths, ())
        canonical_ids.append(canonical)
    if len(set(canonical_ids)) != len(canonical_ids):
        return ClipSourceIdentity("duplicate_source_instance", paths, tuple(canonical_ids))
    return ClipSourceIdentity("assigned", paths, tuple(canonical_ids))


def _fresh_source_instance_id(
    existing: set[str],
    identity_factory: _IdentityFactory,
) -> str:
    # UUID4 collisions are fantastically unlikely.  The bounded retry also
    # makes a broken or deterministic test factory fail closed instead of
    # violating the never-reused-within-vector invariant.
    for _ in range(8):
        candidate = _canonical_uuid(identity_factory())
        if candidate is None:
            raise ValueError("source identity factory returned an invalid UUID")
        if candidate not in existing:
            return candidate
    raise ValueError("source identity factory repeatedly returned an existing UUID")


def provision_clip_source_instance_ids(
    candidates: Mapping[str, Any],
    *,
    identity_factory: _IdentityFactory = uuid.uuid4,
) -> tuple[dict[str, Any], ClipSourceIdentity]:
    """Copy candidates and backfill IDs only when the field is wholly absent."""

    updated = copy.deepcopy(dict(candidates))
    validation = validate_clip_source_identity(updated)
    if validation.status != "missing_source_instance":
        return updated, validation

    existing: set[str] = set()
    source_ids: list[str] = []
    for _path in validation.clip_paths:
        source_id = _fresh_source_instance_id(existing, identity_factory)
        source_ids.append(source_id)
        existing.add(source_id)
    updated[CLIP_SOURCE_INSTANCE_IDS_FIELD] = source_ids
    provisioned = validate_clip_source_identity(updated)
    return updated, ClipSourceIdentity(
        provisioned.status,
        provisioned.clip_paths,
        provisioned.source_instance_ids,
        provisioned=True,
    )


def _require_valid_identity(
    candidates: Mapping[str, Any],
    *,
    identity_factory: _IdentityFactory,
) -> tuple[dict[str, Any], ClipSourceIdentity]:
    updated, identity = provision_clip_source_instance_ids(
        candidates,
        identity_factory=identity_factory,
    )
    if not identity.valid:
        raise ClipSourceIdentityError(identity.status)
    return updated, identity


def append_clip_source(
    candidates: Mapping[str, Any],
    path: str,
    *,
    identity_factory: _IdentityFactory = uuid.uuid4,
) -> tuple[dict[str, Any], int, str]:
    """Append one path with a fresh source UUID and return its new slot."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("clip path must be a non-empty string")
    updated, identity = _require_valid_identity(
        candidates,
        identity_factory=identity_factory,
    )
    source_id = _fresh_source_instance_id(set(identity.source_instance_ids), identity_factory)
    paths = list(identity.clip_paths)
    source_ids = list(identity.source_instance_ids)
    paths.append(path)
    source_ids.append(source_id)
    updated[CLIP_PATHS_FIELD] = paths
    updated[CLIP_SOURCE_INSTANCE_IDS_FIELD] = source_ids
    return updated, len(paths) - 1, source_id


def remove_clip_source_at(
    candidates: Mapping[str, Any],
    source_slot: int,
    *,
    expected_path: str | None = None,
    identity_factory: _IdentityFactory = uuid.uuid4,
) -> tuple[dict[str, Any], tuple[str, str]]:
    """Remove one path/UUID pair, optionally fencing on its current path."""

    updated, identity = _require_valid_identity(
        candidates,
        identity_factory=identity_factory,
    )
    if isinstance(source_slot, bool) or not 0 <= source_slot < len(identity.clip_paths):
        raise IndexError("clip source slot is out of range")
    if expected_path is not None and identity.clip_paths[source_slot] != expected_path:
        raise ValueError("clip source path changed")
    paths = list(identity.clip_paths)
    source_ids = list(identity.source_instance_ids)
    removed = (paths.pop(source_slot), source_ids.pop(source_slot))
    updated[CLIP_PATHS_FIELD] = paths
    updated[CLIP_SOURCE_INSTANCE_IDS_FIELD] = source_ids
    return updated, removed


def replace_clip_source_at(
    candidates: Mapping[str, Any],
    source_slot: int,
    path: str,
    *,
    expected_path: str | None = None,
    identity_factory: _IdentityFactory = uuid.uuid4,
) -> tuple[dict[str, Any], tuple[str, str], str]:
    """Replace media in one slot and mint a new, unrelated source identity."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("clip path must be a non-empty string")
    updated, identity = _require_valid_identity(
        candidates,
        identity_factory=identity_factory,
    )
    if isinstance(source_slot, bool) or not 0 <= source_slot < len(identity.clip_paths):
        raise IndexError("clip source slot is out of range")
    if expected_path is not None and identity.clip_paths[source_slot] != expected_path:
        raise ValueError("clip source path changed")
    paths = list(identity.clip_paths)
    source_ids = list(identity.source_instance_ids)
    replaced = (paths[source_slot], source_ids[source_slot])
    new_source_id = _fresh_source_instance_id(set(source_ids), identity_factory)
    paths[source_slot] = path
    source_ids[source_slot] = new_source_id
    updated[CLIP_PATHS_FIELD] = paths
    updated[CLIP_SOURCE_INSTANCE_IDS_FIELD] = source_ids
    return updated, replaced, new_source_id


def reorder_clip_sources(
    candidates: Mapping[str, Any],
    source_slots: Sequence[int],
    *,
    identity_factory: _IdentityFactory = uuid.uuid4,
) -> dict[str, Any]:
    """Apply one exact permutation to paths and UUIDs together."""

    updated, identity = _require_valid_identity(
        candidates,
        identity_factory=identity_factory,
    )
    slots = list(source_slots)
    if any(isinstance(slot, bool) or not isinstance(slot, int) for slot in slots) or sorted(
        slots
    ) != list(range(len(identity.clip_paths))):
        raise ValueError("clip source order must be an exact permutation")
    updated[CLIP_PATHS_FIELD] = [identity.clip_paths[slot] for slot in slots]
    updated[CLIP_SOURCE_INSTANCE_IDS_FIELD] = [identity.source_instance_ids[slot] for slot in slots]
    return updated


def speech_cleanup_rollout_fingerprint(job_id: str, source_instance_id: str) -> str:
    """Return the stable, non-path-based rollout identity specified by V2."""

    canonical_id = _canonical_uuid(source_instance_id)
    if canonical_id is None:
        raise ValueError("invalid source instance UUID")
    return hashlib.sha256(f"speech-cleanup-source-v1:{job_id}:{canonical_id}".encode()).hexdigest()


def speech_cleanup_source_tag(rollout_fingerprint: str) -> str:
    """Return the bounded, non-reversible tag allowed in diagnostic receipts.

    The rollout fingerprint remains process-local input to assignment.  Receipts
    persist only this independently domain-separated 64-bit tag so operators can
    correlate attempts without exposing the stable treatment identity itself.
    """

    if not isinstance(rollout_fingerprint, str) or len(rollout_fingerprint) != 64:
        raise ValueError("invalid rollout fingerprint")
    try:
        bytes.fromhex(rollout_fingerprint)
    except ValueError as exc:
        raise ValueError("invalid rollout fingerprint") from exc
    return hashlib.sha256(
        f"speech-cleanup-source-tag-v1:{rollout_fingerprint}".encode()
    ).hexdigest()[:16]


def source_identity_vector_fingerprint(source_instance_ids: Sequence[str]) -> str:
    """Fingerprint one ordered source vector for indexed metadata-cache reads."""

    canonical_ids: list[str] = []
    for source_id in source_instance_ids:
        canonical = _canonical_uuid(source_id)
        if canonical is None:
            raise ValueError("invalid source instance UUID")
        canonical_ids.append(canonical)
    encoded = json.dumps(canonical_ids, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"clip-metadata-identity-v2:{encoded}".encode()).hexdigest()


@dataclass(frozen=True)
class ClipMetadataIdentityIndex:
    """Validated indexed cache data, never bound by completion/list order."""

    available: bool
    records_by_source_slot: dict[int, dict[str, Any]]
    failed_source_slots: tuple[int, ...]
    reason: str | None = None


def build_clip_metadata_identity_index(
    *,
    records: Iterable[tuple[int, str, Mapping[str, Any]]],
    failed_source_slots: Iterable[int],
    source_instance_ids: Sequence[str],
    analyzed_source_slots: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Build and self-validate the JSON envelope written beside the v1 cache."""

    source_ids = tuple(source_instance_ids)
    analyzed = list(
        range(len(source_ids)) if analyzed_source_slots is None else analyzed_source_slots
    )
    envelope_records: list[dict[str, Any]] = []
    for source_slot, clip_id, meta in records:
        if isinstance(source_slot, bool) or not 0 <= source_slot < len(source_ids):
            raise ValueError("metadata source slot is out of range")
        envelope_records.append(
            {
                "source_slot": source_slot,
                "source_instance_id": source_ids[source_slot],
                "clip_id": clip_id,
                "meta": copy.deepcopy(dict(meta)),
            }
        )
    envelope = {
        "version": CLIP_METADATA_IDENTITY_INDEX_VERSION,
        "source_identity_fingerprint": source_identity_vector_fingerprint(source_ids),
        "analyzed_source_slots": list(analyzed),
        "records": envelope_records,
        "failed_source_slots": list(failed_source_slots),
    }
    result = validate_clip_metadata_identity_index(
        envelope,
        source_instance_ids=source_ids,
        expected_source_slots=analyzed,
    )
    if not result.available:
        raise ValueError(f"invalid metadata identity index:{result.reason}")
    return envelope


def _slot_list(value: object, *, source_count: int) -> list[int] | None:
    if not isinstance(value, list):
        return None
    slots: list[int] = []
    for slot in value:
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < source_count:
            return None
        slots.append(slot)
    return slots if len(set(slots)) == len(slots) else None


def validate_clip_metadata_identity_index(
    envelope: object,
    *,
    source_instance_ids: Sequence[str],
    expected_source_slots: Iterable[int] | None = None,
) -> ClipMetadataIdentityIndex:
    """Validate a v2 cache envelope against the current persisted source IDs."""

    def unavailable(reason: str) -> ClipMetadataIdentityIndex:
        return ClipMetadataIdentityIndex(False, {}, (), reason)

    try:
        source_ids = tuple(
            canonical
            for source_id in source_instance_ids
            if (canonical := _canonical_uuid(source_id)) is not None
        )
    except TypeError:
        return unavailable("invalid_source_vector")
    if len(source_ids) != len(source_instance_ids):
        return unavailable("invalid_source_vector")
    if len(set(source_ids)) != len(source_ids):
        return unavailable("duplicate_source_vector")
    if not isinstance(envelope, Mapping):
        return unavailable("missing_envelope")
    if envelope.get("version") != CLIP_METADATA_IDENTITY_INDEX_VERSION:
        return unavailable("version_mismatch")
    if envelope.get("source_identity_fingerprint") != source_identity_vector_fingerprint(
        source_ids
    ):
        return unavailable("fingerprint_mismatch")

    analyzed = _slot_list(envelope.get("analyzed_source_slots"), source_count=len(source_ids))
    if analyzed is None:
        return unavailable("invalid_analyzed_slots")
    expected = list(
        range(len(source_ids)) if expected_source_slots is None else expected_source_slots
    )
    if (
        any(isinstance(slot, bool) or not isinstance(slot, int) for slot in expected)
        or len(set(expected)) != len(expected)
        or sorted(analyzed) != sorted(expected)
    ):
        return unavailable("analyzed_slots_mismatch")
    analyzed_set = set(analyzed)

    raw_records = envelope.get("records")
    if not isinstance(raw_records, list):
        return unavailable("invalid_records")
    records_by_slot: dict[int, dict[str, Any]] = {}
    seen_clip_ids: set[str] = set()
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            return unavailable("invalid_record")
        source_slot = raw.get("source_slot")
        if (
            isinstance(source_slot, bool)
            or not isinstance(source_slot, int)
            or source_slot not in analyzed_set
            or source_slot in records_by_slot
        ):
            return unavailable("invalid_record_slot")
        source_id = _canonical_uuid(raw.get("source_instance_id"))
        if source_id != source_ids[source_slot]:
            return unavailable("record_identity_mismatch")
        clip_id = raw.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id.strip():
            return unavailable("invalid_clip_id")
        if clip_id in seen_clip_ids:
            return unavailable("duplicate_clip_id")
        meta = raw.get("meta")
        if not isinstance(meta, Mapping):
            return unavailable("invalid_meta")
        seen_clip_ids.add(clip_id)
        records_by_slot[source_slot] = copy.deepcopy(dict(raw))

    failed = _slot_list(envelope.get("failed_source_slots"), source_count=len(source_ids))
    if failed is None or any(slot not in analyzed_set for slot in failed):
        return unavailable("invalid_failed_slots")
    successful_slots = set(records_by_slot)
    failed_slots = set(failed)
    if successful_slots & failed_slots:
        return unavailable("overlapping_slot_outcomes")
    if successful_slots | failed_slots != analyzed_set:
        return unavailable("incomplete_slot_partition")
    return ClipMetadataIdentityIndex(
        True,
        records_by_source_slot=records_by_slot,
        failed_source_slots=tuple(sorted(failed_slots)),
    )
