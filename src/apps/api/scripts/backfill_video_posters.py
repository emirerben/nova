"""Progressively backfill JPEG posters for retained browser-visible videos.

Run from ``src/apps/api``::

    python -m scripts.backfill_video_posters --dry-run
    python -m scripts.backfill_video_posters --batch-size 25 --limit 100
    python -m scripts.backfill_video_posters --exclude-synthetic --strict

The script is deliberately idempotent and safe to run more than once. It
discovers all output shapes used by the library (generative variants,
single-output jobs, and ready ``JobClip`` rows), downloads one source at a
time, and writes a source-scoped immutable poster object. It commits the UUID
poster reference before uploading bytes, then re-locks/rechecks that exact
reservation while publishing. A process kill can therefore leave a referenced
missing object (repaired on the next strict run), never an unreferenced durable
object. Missing objects and malformed legacy URLs are counted per candidate.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm.attributes import flag_modified

from app.auth import SYNTHETIC_USER_ID
from app.database import sync_session
from app.models import Job, JobClip
from app.services.job_status import PLAN_ITEM_JOB_READY
from app.services.job_storage_paths import owned_job_output_path
from app.services.template_poster import (
    PosterExtractionError,
    extract_poster_bytes,
    poster_object_path,
)
from app.services.video_poster_cleanup import (
    VIDEO_POSTER_BACKFILL_CLEANUP_FIELD,
    VIDEO_POSTER_BACKFILL_MARKER,
    append_video_poster_cleanup_receipt,
    backfill_poster_source,
    reconcile_video_poster_cleanup_receipts,
    reconcile_video_poster_cleanup_receipts_locked,
)
from app.storage import (
    download_generation_to_file,
    download_to_file,
    object_exists,
    object_metadata,
    object_metadata_once,
    upload_bytes_public_read,
)


@dataclass(frozen=True)
class PosterCandidate:
    job_id: uuid.UUID
    kind: str
    source_key: str | None
    poster_field: str
    variant_id: str | None = None
    variant_index: int | None = None
    render_identity: object = None
    clip_id: uuid.UUID | None = None
    already_present: bool = False
    observed_source_value: object = None
    observed_poster_value: object = None
    existing_poster_key: str | None = None
    blocked_outcome: str | None = None


OUTCOMES = (
    "already_present",
    "would_generate",
    "generated",
    "expired_source",
    "unresolvable_legacy_url",
    "stale_race",
    "orphan_cleanup_failed",
    "failed",
    "skipped_not_owned",
)

_BACKFILL_POSTER_MARKER = VIDEO_POSTER_BACKFILL_MARKER
_CLEANUP_RECEIPTS_FIELD = VIDEO_POSTER_BACKFILL_CLEANUP_FIELD
_MAX_EXISTING_POSTER_BYTES = 10 * 1024 * 1024
_MAX_EXISTING_POSTER_DIMENSION = 8192
_SOURCE_METADATA_TIMEOUT_S = 3.0
_JPEG_CONTENT_TYPES = frozenset({"image/jpeg", "image/jpg"})


def _backfill_poster_object_path(source_key: str) -> str:
    """Return a source-bound key that no concurrent renderer can overwrite."""
    return f"{source_key}{_BACKFILL_POSTER_MARKER}{uuid.uuid4()}.jpg"


def _is_backfill_poster_path(poster_key: str, source_key: str) -> bool:
    return _backfill_poster_source(poster_key) == source_key


def _backfill_poster_source(poster_key: str) -> str | None:
    return backfill_poster_source(poster_key)


def _poster_matches_source(poster_key: str, source_key: str) -> bool:
    """Accept live deterministic posters and UUID-scoped backfill posters."""
    return poster_key == poster_object_path(source_key) or _is_backfill_poster_path(
        poster_key, source_key
    )


def _safe_owned_poster_path(raw_path: object, job: Job) -> str | None:
    path = owned_job_output_path(raw_path, job)
    if path is None or not path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return None
    return path


def _append_cleanup_receipt(plan: dict[str, Any], *, old_path: str, new_path: str) -> None:
    append_video_poster_cleanup_receipt(
        plan,
        old_path=old_path,
        replacement_path=new_path,
    )


def _reconcile_cleanup_receipts_locked(db: Any, job: Job) -> bool:
    return reconcile_video_poster_cleanup_receipts_locked(db, job).ok


def _reconcile_cleanup_receipts(job_id: uuid.UUID) -> bool:
    try:
        return reconcile_video_poster_cleanup_receipts(job_id).ok
    except Exception:  # noqa: BLE001 — strict rollout must fail closed
        return False


def _job_has_cleanup_receipts_current(job_id: uuid.UUID) -> bool | None:
    """Read the current Job-level cleanup debt under a row lock.

    Strict dry-runs must not trust the paged discovery snapshot here: a
    renderer can add or clear cleanup receipts while storage checks for the
    page are in flight. ``None`` is deliberately distinct from no receipts so
    an inconclusive DB read fails closed.
    """
    try:
        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None:
                return False
            if job.assembly_plan is None:
                return False
            if not isinstance(job.assembly_plan, dict):
                return None
            return _CLEANUP_RECEIPTS_FIELD in job.assembly_plan
    except Exception:  # noqa: BLE001 — strict rollout must fail closed
        return None


def _existing_poster_is_usable(poster_key: str) -> bool | None:
    """Validate one exact stored generation as a bounded, decodable JPEG.

    ``False`` means the object is missing or definitively invalid and may be
    repaired. ``None`` means storage was inconclusive, so strict mode fails
    closed instead of overwriting a potentially healthy poster.
    """
    try:
        metadata = object_metadata(poster_key)
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 — strict verification must fail closed
        return None
    content_type = metadata.content_type.split(";", 1)[0].strip().lower()
    if (
        metadata.size <= 0
        or metadata.size > _MAX_EXISTING_POSTER_BYTES
        or content_type not in _JPEG_CONTENT_TYPES
    ):
        return False

    with tempfile.TemporaryDirectory(prefix="nova-poster-verify-") as temp_dir:
        local_path = os.path.join(temp_dir, "poster.jpg")
        try:
            download_generation_to_file(
                poster_key,
                local_path,
                generation=metadata.generation,
            )
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001 — strict verification must fail closed
            return None
        try:
            if os.path.getsize(local_path) != metadata.size:
                return False
            with Image.open(local_path) as image:
                width, height = image.size
                if (
                    image.format != "JPEG"
                    or width <= 0
                    or height <= 0
                    or width > _MAX_EXISTING_POSTER_DIMENSION
                    or height > _MAX_EXISTING_POSTER_DIMENSION
                ):
                    return False
                image.verify()
            # ``verify`` checks container integrity without decoding pixels.
            # Re-open the bounded image and force a full decode so truncated
            # payloads cannot pass a strict production audit.
            with Image.open(local_path) as image:
                image.load()
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
            return False
    return True


def _candidate_has_usable_poster(
    candidate: PosterCandidate, *, verify_storage: bool
) -> bool | None:
    """Return True/False for usable/missing, or None when HEAD is inconclusive."""
    if not candidate.already_present:
        return False
    if not verify_storage:
        return True
    if candidate.existing_poster_key is None:
        return False
    return _existing_poster_is_usable(candidate.existing_poster_key)


def _candidate_has_usable_source(candidate: PosterCandidate) -> bool | None:
    """Strictly prove that the current preview source exists and is non-empty."""
    if candidate.source_key is None:
        return False
    try:
        metadata = object_metadata_once(
            candidate.source_key,
            timeout_s=_SOURCE_METADATA_TIMEOUT_S,
        )
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 — strict verification must fail closed
        return None
    return metadata.size > 0


def _is_missing_object(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "notfound",
            "not found",
            "no such object",
            "status code: 404",
            "404",
        )
    )


def _source_candidate(job: Job, raw_source: object) -> tuple[str | None, str | None]:
    """Return an owned key or the disjoint outcome for an untrusted source."""
    source_key = owned_job_output_path(raw_source, job)
    if source_key:
        return source_key, None
    if isinstance(raw_source, str) and "://" in raw_source:
        return None, "unresolvable_legacy_url"
    return None, "skipped_not_owned"


def _make_candidate(
    job: Job,
    *,
    kind: str,
    raw_source: object,
    poster_value: object,
    poster_field: str,
    variant_id: str | None = None,
    variant_index: int | None = None,
    render_identity: object = None,
    clip_id: uuid.UUID | None = None,
    verify_existing: bool = False,
    required_source: bool = False,
) -> PosterCandidate | None:
    if not isinstance(raw_source, str) or not raw_source.strip():
        if required_source:
            return PosterCandidate(
                job_id=job.id,
                kind=kind,
                source_key=None,
                poster_field=poster_field,
                variant_id=variant_id,
                variant_index=variant_index,
                render_identity=render_identity,
                clip_id=clip_id,
                observed_source_value=raw_source,
                observed_poster_value=poster_value,
                blocked_outcome="failed",
            )
        return None
    source_key, blocked_outcome = _source_candidate(job, raw_source)
    if blocked_outcome:
        return PosterCandidate(
            job_id=job.id,
            kind=kind,
            source_key=None,
            poster_field=poster_field,
            variant_id=variant_id,
            variant_index=variant_index,
            render_identity=render_identity,
            clip_id=clip_id,
            observed_source_value=raw_source,
            blocked_outcome=blocked_outcome,
        )
    owned_poster = owned_job_output_path(poster_value, job) if poster_value else None
    legacy_clip_thumbnail = bool(
        kind == "job_clip"
        and owned_poster
        and owned_poster.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    )
    poster_matches_source = bool(
        owned_poster and (_poster_matches_source(owned_poster, source_key) or legacy_clip_thumbnail)
    )
    already_present = poster_matches_source if verify_existing else bool(poster_value)

    return PosterCandidate(
        job_id=job.id,
        kind=kind,
        source_key=source_key,
        poster_field=poster_field,
        variant_id=variant_id,
        variant_index=variant_index,
        render_identity=render_identity,
        clip_id=clip_id,
        already_present=already_present,
        observed_source_value=raw_source,
        observed_poster_value=poster_value,
        existing_poster_key=owned_poster if poster_matches_source else None,
    )


def _missing_preview_signature(job: Job, clips: list[JobClip]) -> tuple[object, ...]:
    """Snapshot every primary-preview field for a ready job with no candidate."""
    return (
        job.status,
        job.job_type,
        repr(copy.deepcopy(job.assembly_plan)),
        tuple(
            (
                str(clip.id),
                clip.render_status,
                repr(clip.video_path),
                repr(clip.thumbnail_path),
            )
            for clip in clips
        ),
    )


def _candidates_for_job(
    job: Job,
    clips: list[JobClip],
    *,
    include_preview_bases: bool,
    verify_existing: bool = False,
) -> Iterator[PosterCandidate]:
    # Admin-only lyric preview jobs are not part of /me/jobs. Their historical
    # objects live under the one-day ``music-lyrics-previews/`` lifecycle
    # prefix and cannot be recovered after expiry, so including them only
    # pollutes rollout metrics with irrelevant ``unresolvable_legacy_url``
    # counts (62/62 in the production audit).
    if job.status not in PLAN_ITEM_JOB_READY or job.job_type == "lyrics_preview":
        return
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    primary_candidates = 0
    variants = plan.get("variants")
    if isinstance(variants, list):
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                continue
            if variant.get("render_status") != "ready":
                continue
            variant_id = str(variant.get("variant_id") or "") or None
            video_source = variant.get("video_path") or variant.get("output_url")
            candidate = _make_candidate(
                job,
                kind="variant",
                raw_source=video_source,
                poster_value=variant.get("poster_path"),
                poster_field="poster_path",
                variant_id=variant_id,
                variant_index=index,
                render_identity=(
                    variant.get("render_generation_id") or variant.get("render_finished_at")
                ),
                verify_existing=verify_existing,
                required_source=True,
            )
            if candidate:
                primary_candidates += 1
                yield candidate
            if not include_preview_bases:
                continue
            for video_field, poster_field, kind in (
                ("base_video_path", "base_poster_path", "variant_base"),
                (
                    "pre_media_overlay_video_path",
                    "pre_overlay_poster_path",
                    "variant_pre_overlay",
                ),
            ):
                candidate = _make_candidate(
                    job,
                    kind=kind,
                    raw_source=variant.get(video_field),
                    poster_value=variant.get(poster_field),
                    poster_field=poster_field,
                    variant_id=variant_id,
                    variant_index=index,
                    render_identity=(
                        variant.get("render_generation_id") or variant.get("render_finished_at")
                    ),
                    verify_existing=verify_existing,
                )
                if candidate:
                    yield candidate

    output_source = plan.get("output_path") or plan.get("video_path") or plan.get("output_url")
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=output_source,
        poster_value=plan.get("poster_path"),
        poster_field="poster_path",
        verify_existing=verify_existing,
    )
    if candidate:
        primary_candidates += 1
        yield candidate

    for clip in clips:
        if clip.render_status != "ready":
            continue
        candidate = _make_candidate(
            job,
            kind="job_clip",
            raw_source=clip.video_path,
            poster_value=clip.thumbnail_path,
            poster_field="thumbnail_path",
            clip_id=clip.id,
            verify_existing=verify_existing,
            required_source=True,
        )
        if candidate:
            primary_candidates += 1
            yield candidate

    if primary_candidates == 0:
        # A ready real-user Job with no primary preview entry is exactly the
        # blank "Ready to post" state this rollout must not certify as fixed.
        # Store a signature of every relevant field so a concurrent renderer
        # can supersede this blocked snapshot and trigger the bounded rescan.
        yield PosterCandidate(
            job_id=job.id,
            kind="job_preview_missing",
            source_key=None,
            poster_field="poster_path",
            render_identity=_missing_preview_signature(job, clips),
            blocked_outcome="failed",
        )


def _job_batches(
    db: Any,
    *,
    batch_size: int,
    job_id: uuid.UUID | None,
    mode: str | None,
    cursor: tuple[datetime, uuid.UUID] | None,
    exclude_synthetic: bool = False,
) -> Iterator[list[Job]]:
    if job_id is not None:
        job = db.get(Job, job_id)
        if job is not None and (not exclude_synthetic or job.user_id != SYNTHETIC_USER_ID):
            yield [job]
        return

    current_cursor = cursor
    while True:
        conditions = []
        if mode:
            conditions.append(or_(Job.mode == mode, Job.job_type == mode))
        if exclude_synthetic:
            conditions.append(Job.user_id != SYNTHETIC_USER_ID)
        if current_cursor is not None:
            created_at, last_id = current_cursor
            conditions.append(
                or_(
                    Job.created_at > created_at,
                    and_(Job.created_at == created_at, Job.id > last_id),
                )
            )
        statement = select(Job).order_by(Job.created_at.asc(), Job.id.asc()).limit(batch_size)
        if conditions:
            statement = statement.where(*conditions)
        rows = list(db.execute(statement).scalars().all())
        if not rows:
            return
        last = rows[-1]
        next_cursor = (last.created_at, last.id)
        yield rows
        current_cursor = next_cursor


def _load_ready_clips(db: Any, jobs: list[Job]) -> dict[uuid.UUID, list[JobClip]]:
    if not jobs:
        return {}
    rows = list(
        db.execute(
            select(JobClip)
            .where(
                JobClip.job_id.in_([job.id for job in jobs]),
                JobClip.render_status == "ready",
            )
            .order_by(JobClip.job_id.asc(), JobClip.rank.asc(), JobClip.created_at.asc())
        )
        .scalars()
        .all()
    )
    by_job: dict[uuid.UUID, list[JobClip]] = {}
    for clip in rows:
        by_job.setdefault(clip.job_id, []).append(clip)
    return by_job


def _reload_candidates_for_job(
    job_id: uuid.UUID,
    *,
    include_preview_bases: bool,
    verify_existing: bool,
) -> list[PosterCandidate] | None:
    """Take one bounded fresh snapshot after a candidate loses a race."""
    try:
        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None:
                return []
            clips_by_job = _load_ready_clips(db, [job])
            return list(
                _candidates_for_job(
                    job,
                    clips_by_job.get(job.id, []),
                    include_preview_bases=include_preview_bases,
                    verify_existing=verify_existing,
                )
            )
    except Exception:  # noqa: BLE001 — strict reconciliation must fail closed
        return None


def _candidate_snapshot_key(candidate: PosterCandidate) -> tuple[object, ...]:
    """Identify one immutable discovery snapshot across a bounded rescan."""
    return (
        candidate.kind,
        candidate.variant_id,
        candidate.variant_index,
        candidate.clip_id,
        candidate.poster_field,
        candidate.source_key,
        repr(candidate.observed_source_value),
        repr(candidate.observed_poster_value),
        repr(candidate.render_identity),
        candidate.blocked_outcome,
    )


def _extract_candidate_poster(source_key: str) -> tuple[bytes | None, str]:
    """Download and extract poster bytes without creating a storage object."""
    with tempfile.TemporaryDirectory(prefix="nova_poster_backfill_") as tmpdir:
        local_path = os.path.join(tmpdir, "source.mp4")
        try:
            download_to_file(source_key, local_path)
        except Exception as exc:  # noqa: BLE001 — one missing object must not stop the batch
            return None, "expired_source" if _is_missing_object(exc) else "failed"
        try:
            poster_bytes = extract_poster_bytes(local_path)
        except PosterExtractionError:
            return None, "failed"
        except Exception as exc:  # noqa: BLE001 — continue after GCS/FFmpeg failures
            return None, "expired_source" if _is_missing_object(exc) else "failed"
    return poster_bytes, "generated"


def _variant_for_candidate(
    plan: dict[str, Any], candidate: PosterCandidate
) -> dict[str, Any] | None:
    variants = plan.get("variants")
    if not isinstance(variants, list):
        return None
    if candidate.variant_id:
        matches = [
            (index, variant)
            for index, variant in enumerate(variants)
            if isinstance(variant, dict)
            and str(variant.get("variant_id") or "") == candidate.variant_id
        ]
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1 and candidate.variant_index is not None:
            # Historical JSONB rows do not enforce unique variant IDs. The
            # discovery index makes duplicates individually addressable; if
            # concurrent reordering invalidates it, source/identity checks
            # classify the snapshot stale rather than mutating the first ID.
            for index, variant in matches:
                if index == candidate.variant_index:
                    return variant
            return None
        return None
    if candidate.variant_index is not None and 0 <= candidate.variant_index < len(variants):
        variant = variants[candidate.variant_index]
        return variant if isinstance(variant, dict) else None
    return None


def _candidate_matches_loaded_state(
    job: Job,
    candidate: PosterCandidate,
    *,
    expected_poster: object,
    clip: JobClip | None = None,
    require_ready_job: bool,
) -> bool:
    """Compare one immutable candidate snapshot with freshly locked state."""
    if job.status == "cancelled":
        return False
    if require_ready_job and job.status not in PLAN_ITEM_JOB_READY:
        return False
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    if candidate.kind == "job_clip":
        if clip is None or clip.job_id != candidate.job_id or clip.render_status != "ready":
            return False
        current_raw_source = clip.video_path
        current_poster = clip.thumbnail_path
        current_identity = None
    elif candidate.kind == "job_output":
        current_raw_source = (
            plan.get("output_path") or plan.get("video_path") or plan.get("output_url")
        )
        current_poster = plan.get("poster_path")
        current_identity = None
    else:
        variant = _variant_for_candidate(plan, candidate)
        if variant is None or variant.get("render_status") != "ready":
            return False
        source_field = {
            "variant": "video_path",
            "variant_base": "base_video_path",
            "variant_pre_overlay": "pre_media_overlay_video_path",
        }.get(candidate.kind)
        if source_field is None:
            return False
        current_raw_source = (
            variant.get("video_path") or variant.get("output_url")
            if candidate.kind == "variant"
            else variant.get(source_field)
        )
        current_poster = variant.get(candidate.poster_field)
        current_identity = variant.get("render_generation_id") or variant.get("render_finished_at")

    if candidate.blocked_outcome:
        return (
            current_raw_source == candidate.observed_source_value
            and current_identity == candidate.render_identity
        )
    if candidate.source_key is None:
        return False
    return (
        owned_job_output_path(current_raw_source, job) == candidate.source_key
        and current_poster == expected_poster
        and current_identity == candidate.render_identity
    )


def _candidate_is_still_current(candidate: PosterCandidate) -> bool | None:
    """Recheck a failed storage operation against locked current DB state.

    False is positive stale-race proof. None means the DB check itself was
    inconclusive, so callers preserve the original storage failure and fail
    closed instead of incorrectly forgiving it as stale.
    """
    try:
        with sync_session() as db:
            job = db.get(Job, candidate.job_id, with_for_update=True)
            if job is None:
                return False
            if candidate.kind == "job_preview_missing":
                clips_by_job = _load_ready_clips(db, [job])
                return (
                    job.status in PLAN_ITEM_JOB_READY
                    and job.job_type != "lyrics_preview"
                    and _missing_preview_signature(
                        job,
                        clips_by_job.get(job.id, []),
                    )
                    == candidate.render_identity
                )
            clip = None
            if candidate.kind == "job_clip":
                if candidate.clip_id is None:
                    return False
                clip = db.get(JobClip, candidate.clip_id, with_for_update=True)
            return _candidate_matches_loaded_state(
                job,
                candidate,
                expected_poster=candidate.observed_poster_value,
                clip=clip,
                require_ready_job=True,
            )
    except Exception:  # noqa: BLE001 — preserve the original failure outcome
        return None


def _post_commit_candidate_outcome(
    job: Job,
    candidate: PosterCandidate,
    poster_key: str,
    *,
    clip: JobClip | None = None,
) -> str:
    """Distinguish a persisted reservation from no-op vs superseding writes."""
    if _candidate_matches_loaded_state(
        job,
        candidate,
        expected_poster=poster_key,
        clip=clip,
        require_ready_job=True,
    ):
        return "generated"
    if _candidate_matches_loaded_state(
        job,
        candidate,
        expected_poster=candidate.observed_poster_value,
        clip=clip,
        require_ready_job=True,
    ):
        # The exact pre-write state survived commit: this is the JSONB no-op
        # regression the verification exists to catch, not a concurrent race.
        return "failed"
    return "stale_race"


def _persist_poster(candidate: PosterCandidate, poster_key: str) -> str:
    """Reserve a UUID key only if the source is unchanged under a row lock."""
    if candidate.source_key is None or not _poster_matches_source(poster_key, candidate.source_key):
        return "failed"
    with sync_session() as db:
        job = db.get(Job, candidate.job_id, with_for_update=True)
        if job is None or job.status not in PLAN_ITEM_JOB_READY:
            return "stale_race"
        # A non-object JSONB value is forensic/corrupt state.  In particular,
        # JobClip candidates do not otherwise need the plan, but replacing an
        # existing UUID poster may need to append a durable cleanup receipt.
        # Coercing the corrupt value to ``{}`` would silently destroy it.  Fail
        # before locking/mutating the clip so strict rollout reports the debt
        # and preserves the exact database value for diagnosis.
        if job.assembly_plan is not None and not isinstance(job.assembly_plan, dict):
            return "failed"
        # JSONB is not mutation-tracked recursively. A shallow ``dict(...)``
        # copy still shares the nested variants list/dicts with the ORM-loaded
        # value, so changing ``variant["poster_path"]`` mutates both the old and
        # new values. SQLAlchemy then sees no change and commits no UPDATE while
        # the script falsely reports success. Deep-copy before any nested write
        # and explicitly flag the JSONB column as a second line of defence.
        plan = copy.deepcopy(job.assembly_plan or {})
        persisted_kind = candidate.kind
        replaced_poster_path = _safe_owned_poster_path(candidate.observed_poster_value, job)

        if candidate.kind == "job_clip":
            clip = db.get(JobClip, candidate.clip_id, with_for_update=True)
            if clip is None or clip.job_id != candidate.job_id or clip.render_status != "ready":
                return "stale_race"
            current_source = owned_job_output_path(clip.video_path, job)
            if (
                current_source != candidate.source_key
                or clip.thumbnail_path != candidate.observed_poster_value
            ):
                return "stale_race"
            # Preserve the signed URL stored by the legacy writer. The admin
            # job-debug API exposes this field verbatim as a browser playback
            # URL; /me can normalize it for ownership and re-sign separately.
            clip.thumbnail_path = poster_key
        elif candidate.kind == "job_output":
            current_source = owned_job_output_path(
                plan.get("output_path") or plan.get("video_path") or plan.get("output_url"),
                job,
            )
            if (
                current_source != candidate.source_key
                or plan.get("poster_path") != candidate.observed_poster_value
            ):
                return "stale_race"
            if not plan.get("output_path") and not plan.get("video_path"):
                plan["output_path"] = candidate.source_key
            plan["poster_path"] = poster_key
            job.assembly_plan = plan
            flag_modified(job, "assembly_plan")
        else:
            variant = _variant_for_candidate(plan, candidate)
            if variant is None or variant.get("render_status") != "ready":
                return "stale_race"
            source_field = {
                "variant": "video_path",
                "variant_base": "base_video_path",
                "variant_pre_overlay": "pre_media_overlay_video_path",
            }.get(candidate.kind)
            if source_field is None:
                return "stale_race"
            current_raw_source = (
                variant.get("video_path") or variant.get("output_url")
                if candidate.kind == "variant"
                else variant.get(source_field)
            )
            current_source = owned_job_output_path(current_raw_source, job)
            current_identity = variant.get("render_generation_id") or variant.get(
                "render_finished_at"
            )
            if (
                current_source != candidate.source_key
                or variant.get(candidate.poster_field) != candidate.observed_poster_value
                or current_identity != candidate.render_identity
            ):
                return "stale_race"
            if candidate.kind == "variant" and not variant.get("video_path"):
                variant["video_path"] = candidate.source_key
            variant[candidate.poster_field] = poster_key
            job.assembly_plan = plan
            flag_modified(job, "assembly_plan")

        if replaced_poster_path and replaced_poster_path != poster_key:
            _append_cleanup_receipt(
                plan,
                old_path=replaced_poster_path,
                new_path=poster_key,
            )
            job.assembly_plan = plan
            flag_modified(job, "assembly_plan")

        db.commit()

        # ``commit()`` returning is not enough evidence for JSONB persistence:
        # the production bug was exactly a successful no-op commit. Expire the
        # identity map and force a database round-trip before reporting
        # ``generated``. Query instead of ``refresh`` because the user may
        # delete the row after the lock is released by commit; that race is a
        # normal stale outcome, not grounds to abort the entire backfill.
        db.expire_all()
        if persisted_kind == "job_clip":
            persisted_clip = db.execute(
                select(JobClip).where(JobClip.id == candidate.clip_id)
            ).scalar_one_or_none()
            persisted_job = db.execute(
                select(Job).where(Job.id == candidate.job_id)
            ).scalar_one_or_none()
            if persisted_clip is None or persisted_job is None:
                return "stale_race"
            return _post_commit_candidate_outcome(
                persisted_job,
                candidate,
                poster_key,
                clip=persisted_clip,
            )

        persisted_job = db.execute(
            select(Job).where(Job.id == candidate.job_id)
        ).scalar_one_or_none()
        if persisted_job is None:
            return "stale_race"
        return _post_commit_candidate_outcome(persisted_job, candidate, poster_key)


def _publish_reserved_poster(
    candidate: PosterCandidate,
    poster_key: str,
    poster_bytes: bytes,
) -> str:
    """Upload bytes only while the committed UUID reservation still wins.

    ``_persist_poster`` commits the reference first. Holding the Job row lock
    across this short upload prevents a renderer/backfill from replacing the
    reservation between the final comparison and object creation. If this
    process is killed at any point, the UUID key remains DB-referenced; a later
    strict run either observes the completed object or replaces the missing
    reservation. No lifecycle-exempt orphan is created.
    """
    if candidate.source_key is None or not _poster_matches_source(poster_key, candidate.source_key):
        return "failed"
    with sync_session() as db:
        job = db.get(Job, candidate.job_id, with_for_update=True)
        if job is None or job.status not in PLAN_ITEM_JOB_READY:
            return "stale_race"
        plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}

        if candidate.kind == "job_clip":
            clip = db.get(JobClip, candidate.clip_id, with_for_update=True)
            if clip is None or clip.job_id != candidate.job_id or clip.render_status != "ready":
                return "stale_race"
            current_source = owned_job_output_path(clip.video_path, job)
            reservation_is_current = (
                current_source == candidate.source_key and clip.thumbnail_path == poster_key
            )
        elif candidate.kind == "job_output":
            current_source = owned_job_output_path(
                plan.get("output_path") or plan.get("video_path") or plan.get("output_url"),
                job,
            )
            reservation_is_current = (
                current_source == candidate.source_key and plan.get("poster_path") == poster_key
            )
        else:
            variant = _variant_for_candidate(plan, candidate)
            source_field = {
                "variant": "video_path",
                "variant_base": "base_video_path",
                "variant_pre_overlay": "pre_media_overlay_video_path",
            }.get(candidate.kind)
            if variant is None or source_field is None or variant.get("render_status") != "ready":
                return "stale_race"
            current_raw_source = (
                variant.get("video_path") or variant.get("output_url")
                if candidate.kind == "variant"
                else variant.get(source_field)
            )
            current_source = owned_job_output_path(current_raw_source, job)
            current_identity = variant.get("render_generation_id") or variant.get(
                "render_finished_at"
            )
            reservation_is_current = (
                current_source == candidate.source_key
                and variant.get(candidate.poster_field) == poster_key
                and current_identity == candidate.render_identity
            )

        if not reservation_is_current:
            return "stale_race"
        try:
            upload_bytes_public_read(poster_bytes, poster_key, content_type="image/jpeg")
            if not object_exists(poster_key):
                return "failed"
        except Exception:  # noqa: BLE001 — reservation remains recoverable on retry
            return "failed"
        if not _reconcile_cleanup_receipts_locked(db, job):
            return "orphan_cleanup_failed"
    return "generated"


def _parse_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not value:
        return None
    try:
        created_at_raw, job_id_raw = value.split("|", 1)
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at, uuid.UUID(job_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be '<ISO timestamp>|<job UUID>'") from exc


def _cursor_for(job: Job) -> str:
    created_at = job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return f"{created_at.isoformat()}|{job.id}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Discover only; do not touch GCS or DB."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum missing posters to process (0 = no cap).",
    )
    parser.add_argument("--batch-size", type=int, default=25, help="Jobs fetched per keyset page.")
    parser.add_argument("--job-id", type=uuid.UUID, help="Process one job only.")
    parser.add_argument(
        "--mode", help="Filter by Job.mode or Job.job_type (for example: generative)."
    )
    parser.add_argument(
        "--include-preview-bases",
        action="store_true",
        help="Also backfill generative base and pre-overlay posters.",
    )
    parser.add_argument(
        "--exclude-synthetic",
        action="store_true",
        help="Skip anonymous/admin jobs owned by the synthetic development user.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero on blocked, expired, unowned, or failed candidates. "
            "With --dry-run, also fail if any poster is still missing."
        ),
    )
    parser.add_argument(
        "--cursor",
        help=(
            "Resume after '<ISO timestamp>|<job UUID>'; the final cursor is printed for operators."
        ),
    )
    args = parser.parse_args(argv)
    if args.limit < 0 or args.batch_size < 1:
        parser.error("--limit must be >= 0 and --batch-size must be >= 1")
    try:
        cursor = _parse_cursor(args.cursor)
    except ValueError as exc:
        parser.error(str(exc))

    counts: Counter[str] = Counter()
    processed = 0
    last_cursor: str | None = None
    stop = False

    with sync_session() as db:
        for jobs in _job_batches(
            db,
            batch_size=args.batch_size,
            job_id=args.job_id,
            mode=args.mode,
            cursor=cursor,
            exclude_synthetic=args.exclude_synthetic,
        ):
            clips_by_job = _load_ready_clips(db, jobs)
            candidate_groups = [
                (
                    job.id,
                    _cursor_for(job),
                    list(
                        _candidates_for_job(
                            job,
                            clips_by_job.get(job.id, []),
                            include_preview_bases=args.include_preview_bases,
                            verify_existing=args.strict,
                        )
                    ),
                )
                for job in jobs
            ]
            # Candidates are immutable primitive snapshots. End the read
            # transaction before any GCS HEAD/download or FFmpeg work so a
            # slow page cannot hold an MVCC snapshot and delay vacuum.
            db.rollback()
            for current_job_id, job_cursor, candidates in candidate_groups:
                # Receipt cleanup is Job-level maintenance, not a property of
                # any one current preview candidate. A renderer may remove the
                # last candidate after journaling a displaced UUID poster, so
                # strict reconciliation must still run for an empty group.
                provisional_cleanup_outcomes = 0
                handled_snapshots: set[tuple[object, ...]] = set()
                candidate_round = candidates
                rescan_round = 0
                while True:
                    rescan_needed = False
                    deferred_stale_count = 0
                    for candidate in candidate_round:
                        snapshot_key = _candidate_snapshot_key(candidate)
                        if snapshot_key in handled_snapshots:
                            continue
                        if candidate.blocked_outcome:
                            current = _candidate_is_still_current(candidate)
                            if current is False:
                                if args.strict and rescan_round == 0:
                                    rescan_needed = True
                                    deferred_stale_count += 1
                                else:
                                    counts["failed" if args.strict else "stale_race"] += 1
                                    handled_snapshots.add(snapshot_key)
                            else:
                                counts[candidate.blocked_outcome] += 1
                                handled_snapshots.add(snapshot_key)
                            continue
                        if args.strict:
                            source_is_usable = _candidate_has_usable_source(candidate)
                            if source_is_usable is not True:
                                current = _candidate_is_still_current(candidate)
                                if current is False:
                                    if rescan_round == 0:
                                        rescan_needed = True
                                        deferred_stale_count += 1
                                    else:
                                        counts["failed"] += 1
                                        handled_snapshots.add(snapshot_key)
                                else:
                                    counts[
                                        "expired_source" if source_is_usable is False else "failed"
                                    ] += 1
                                    handled_snapshots.add(snapshot_key)
                                continue
                        poster_is_usable = _candidate_has_usable_poster(
                            candidate, verify_storage=args.strict
                        )
                        if poster_is_usable is None:
                            current = _candidate_is_still_current(candidate)
                            if current is False:
                                if args.strict and rescan_round == 0:
                                    rescan_needed = True
                                    deferred_stale_count += 1
                                else:
                                    counts["failed" if args.strict else "stale_race"] += 1
                                    handled_snapshots.add(snapshot_key)
                            else:
                                counts["failed"] += 1
                                handled_snapshots.add(snapshot_key)
                            continue
                        if poster_is_usable:
                            if args.strict:
                                # HEAD only proves that the snapshotted object
                                # exists. Prove under a row lock that the same
                                # ready candidate still owns that source/poster
                                # before accepting it as rollout-complete.
                                current = _candidate_is_still_current(candidate)
                                if current is not True:
                                    if current is False and rescan_round == 0:
                                        rescan_needed = True
                                        deferred_stale_count += 1
                                    else:
                                        counts["failed"] += 1
                                        handled_snapshots.add(snapshot_key)
                                    continue
                            counts["already_present"] += 1
                            handled_snapshots.add(snapshot_key)
                            continue
                        if args.strict and candidate.already_present:
                            # HEAD proved the snapshotted poster missing, but
                            # the row may have been deleted or superseded while
                            # HEAD was in flight. A DB error is inconclusive and
                            # therefore fails closed without stale download.
                            current = _candidate_is_still_current(candidate)
                            if current is not True:
                                if current is False and rescan_round == 0:
                                    rescan_needed = True
                                    deferred_stale_count += 1
                                else:
                                    counts["failed"] += 1
                                    handled_snapshots.add(snapshot_key)
                                continue
                        if args.dry_run and args.strict:
                            # A dry-run has no later persistence fence. Recheck
                            # immediately before recording rollout debt so a
                            # concurrent renderer cannot make strict mode fail
                            # on a stale missing-poster snapshot.
                            current = _candidate_is_still_current(candidate)
                            if current is not True:
                                if current is False and rescan_round == 0:
                                    rescan_needed = True
                                    deferred_stale_count += 1
                                else:
                                    counts["failed"] += 1
                                    handled_snapshots.add(snapshot_key)
                                continue
                        if args.limit and processed >= args.limit:
                            stop = True
                            break
                        processed += 1
                        assert candidate.source_key is not None
                        if args.dry_run:
                            counts["would_generate"] += 1
                            handled_snapshots.add(snapshot_key)
                            print(
                                f"[dry-run] {candidate.job_id} {candidate.kind} "
                                f"{candidate.source_key} -> "
                                f"{candidate.source_key}{_BACKFILL_POSTER_MARKER}<uuid>.jpg",
                                flush=True,
                            )
                            continue
                        poster_bytes, outcome = _extract_candidate_poster(candidate.source_key)
                        if poster_bytes is None:
                            current = _candidate_is_still_current(candidate)
                            if current is False:
                                if args.strict and rescan_round == 0:
                                    rescan_needed = True
                                    deferred_stale_count += 1
                                else:
                                    counts["failed" if args.strict else "stale_race"] += 1
                                    handled_snapshots.add(snapshot_key)
                            else:
                                counts[outcome] += 1
                                handled_snapshots.add(snapshot_key)
                            continue
                        poster_key = _backfill_poster_object_path(candidate.source_key)
                        persisted = _persist_poster(candidate, poster_key)
                        if persisted == "generated":
                            persisted = _publish_reserved_poster(
                                candidate, poster_key, poster_bytes
                            )
                        if persisted == "stale_race":
                            if args.strict and rescan_round == 0:
                                rescan_needed = True
                                deferred_stale_count += 1
                            else:
                                counts["failed" if args.strict else "stale_race"] += 1
                                handled_snapshots.add(snapshot_key)
                            continue
                        if persisted == "orphan_cleanup_failed":
                            # Publishing succeeded and left a durable receipt,
                            # but another candidate in this Job (or the
                            # mandatory end-of-Job pass below) may make
                            # deletion safe. Defer the candidate outcome until
                            # that final state check.
                            provisional_cleanup_outcomes += 1
                        else:
                            counts[persisted] += 1
                        handled_snapshots.add(snapshot_key)
                        print(
                            f"[{persisted}] {candidate.job_id} {candidate.kind} "
                            f"{candidate.source_key}",
                            flush=True,
                        )
                    if stop or not rescan_needed:
                        break
                    # At most one fresh Job snapshot is processed. This closes
                    # A→B races without looping forever under an active
                    # renderer. A second supersession is an inconclusive
                    # rollout check and therefore a strict failure.
                    fresh_candidates = _reload_candidates_for_job(
                        current_job_id,
                        include_preview_bases=args.include_preview_bases,
                        verify_existing=args.strict,
                    )
                    if fresh_candidates is None:
                        counts["failed"] += 1
                        break
                    candidate_round = [
                        fresh
                        for fresh in fresh_candidates
                        if _candidate_snapshot_key(fresh) not in handled_snapshots
                    ]
                    if not candidate_round:
                        counts["stale_race"] += max(1, deferred_stale_count)
                        break
                    rescan_round += 1
                # Final-state check: a crash-after-reservation receipt can be
                # pending at job start and become safe only after the missing
                # current candidate is repaired above.
                if args.dry_run and args.strict:
                    has_cleanup_receipts = _job_has_cleanup_receipts_current(current_job_id)
                    if has_cleanup_receipts is True:
                        counts["orphan_cleanup_failed"] += 1
                    elif has_cleanup_receipts is None:
                        counts["failed"] += 1
                elif not args.dry_run:
                    cleanup_ok = _reconcile_cleanup_receipts(current_job_id)
                    if cleanup_ok:
                        counts["generated"] += provisional_cleanup_outcomes
                    else:
                        counts["orphan_cleanup_failed"] += max(1, provisional_cleanup_outcomes)
                if stop:
                    break
                # Only advance the resume cursor after the entire job has been
                # scanned. If --limit stops in the middle of a job, replaying
                # that job is safe; skipping it could strand another candidate.
                last_cursor = job_cursor
            if stop or args.job_id is not None:
                break

    print("Backfill complete:", " ".join(f"{name}={counts[name]}" for name in OUTCOMES))
    if last_cursor and args.job_id is None:
        print(f"resume_cursor={last_cursor}")
    strict_failures = sum(
        counts[name]
        for name in (
            "expired_source",
            "unresolvable_legacy_url",
            "orphan_cleanup_failed",
            "failed",
            "skipped_not_owned",
        )
    )
    if args.strict and (strict_failures > 0 or (args.dry_run and counts["would_generate"] > 0)):
        return 1
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
