"""Durable cleanup for immutable historical video-poster objects.

The historical poster backfill writes UUID-scoped objects so a concurrent
renderer can never overwrite the bytes it reserved.  When a later render
replaces one of those posters, the old object lives under a lifecycle-exempt
generative prefix or can outlive the database reference until a bounded
``jobs``/``music-jobs`` lifecycle runs.  Deletion therefore needs a durable
receipt in ``Job.assembly_plan`` instead of a one-shot best-effort call.

Only UUID backfill poster keys are deletion targets here.  Deterministic
``<video>.poster.jpg`` keys are shared writer destinations and are deliberately
never deleted by this service.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, literal_column, select
from sqlalchemy.orm.attributes import flag_modified

from app.database import sync_session
from app.models import Job, JobClip
from app.services.job_storage_paths import owned_job_output_path

VIDEO_POSTER_BACKFILL_CLEANUP_FIELD = "_poster_backfill_cleanup_receipts"
VIDEO_POSTER_BACKFILL_MARKER = ".poster.backfill-"
# Account lifecycle selects at most two Jobs per 60s sweep. Two receipts with
# three single-attempt calls each keep worst-case storage time to 18s per Job.
VIDEO_POSTER_CLEANUP_RECEIPTS_PER_PASS = 2
VIDEO_POSTER_CLEANUP_STORAGE_TIMEOUT_S = 3.0

_PLAN_ASSET_FIELDS = (
    "output_path",
    "video_path",
    "output_url",
    "base_output_path",
    "base_output_url",
    "poster_path",
    "base_video_path",
    "base_poster_path",
)
_VARIANT_ASSET_FIELDS = (
    "video_path",
    "output_url",
    "poster_path",
    "base_video_path",
    "base_poster_path",
    "pre_media_overlay_video_path",
    "pre_overlay_poster_path",
    "pre_sfx_video_path",
    "subject_matte_path",
    "visual_blocks_base_path",
    "motion_base_path",
)
_VARIANT_POSTER_REPLACEMENTS = (
    ("poster_path", ("video_path", "output_url")),
    ("base_poster_path", ("base_video_path",)),
    ("pre_overlay_poster_path", ("pre_media_overlay_video_path",)),
)
_PLAN_POSTER_REPLACEMENTS = (
    ("poster_path", ("output_path", "video_path", "output_url")),
    (
        "base_poster_path",
        ("base_output_path", "base_video_path", "base_output_url"),
    ),
)


@dataclass(frozen=True)
class VideoPosterCleanupResult:
    receipts_seen: int = 0
    deleted: int = 0
    retained: int = 0
    failures: int = 0
    ignored: int = 0

    @property
    def ok(self) -> bool:
        return self.failures == 0 and self.retained == 0


def backfill_poster_source(poster_key: str) -> str | None:
    """Return the source key encoded by one canonical UUID poster key."""
    source_key, marker, suffix = poster_key.rpartition(VIDEO_POSTER_BACKFILL_MARKER)
    if not marker or not source_key or not suffix.endswith(".jpg"):
        return None
    token = suffix[: -len(".jpg")]
    try:
        return source_key if str(uuid.UUID(token)) == token else None
    except ValueError:
        return None


def is_uuid_backfill_poster(path: object) -> bool:
    return isinstance(path, str) and backfill_poster_source(path) is not None


def append_video_poster_cleanup_receipt(
    plan: dict[str, Any],
    *,
    old_path: str,
    replacement_path: str,
) -> bool:
    """Retarget cleanup chains and journal a displaced UUID poster in-place."""
    if not isinstance(old_path, str) or not isinstance(replacement_path, str):
        return False
    if not old_path or not replacement_path or replacement_path == old_path:
        return False

    if VIDEO_POSTER_BACKFILL_CLEANUP_FIELD in plan:
        raw_receipts = plan.get(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD)
        receipts: list[Any] = (
            copy.deepcopy(raw_receipts)
            if isinstance(raw_receipts, list)
            else [copy.deepcopy(raw_receipts)]
        )
    else:
        receipts = []
    changed = False
    for receipt in receipts:
        if (
            isinstance(receipt, dict)
            and isinstance(receipt.get("replacement_path"), str)
            and receipt["replacement_path"] == old_path
        ):
            receipt["replacement_path"] = replacement_path
            changed = True
    if is_uuid_backfill_poster(old_path) and not any(
        isinstance(receipt, dict)
        and receipt.get("old_path") == old_path
        and receipt.get("replacement_path") == replacement_path
        for receipt in receipts
    ):
        receipts.append({"old_path": old_path, "replacement_path": replacement_path})
        changed = True
    if not changed:
        return False
    deduplicated: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for receipt in receipts:
        if not (
            isinstance(receipt, dict)
            and isinstance(receipt.get("old_path"), str)
            and isinstance(receipt.get("replacement_path"), str)
        ):
            # A renderer must never make prior cleanup debt disappear merely
            # because a historical row drifted from the canonical shape.
            deduplicated.append(receipt)
            continue
        key = (receipt["old_path"], receipt["replacement_path"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(receipt)
    plan[VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] = deduplicated
    return True


def append_retired_variant_poster_receipts(
    plan: dict[str, Any],
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    """Journal UUID posters and retarget receipt proofs after a variant write.

    A poster extractor is fail-open, so a successful video swap can have a null
    new poster.  In that case the current owned video is the replacement proof:
    reconciliation verifies that asset is still referenced and exists before it
    removes the old UUID poster.
    """
    journaled: list[str] = []
    for poster_field, video_fields in _VARIANT_POSTER_REPLACEMENTS:
        replacement = current.get(poster_field)
        if not isinstance(replacement, str) or not replacement:
            replacement = next(
                (
                    value
                    for field in video_fields
                    if isinstance((value := current.get(field)), str) and value
                ),
                None,
            )
        if not isinstance(replacement, str):
            continue
        previous_proofs = list(
            dict.fromkeys(
                value
                for field in (poster_field, *video_fields)
                if isinstance((value := previous.get(field)), str) and value
            )
        )
        for old_path in previous_proofs:
            if old_path != replacement and append_video_poster_cleanup_receipt(
                plan,
                old_path=old_path,
                replacement_path=replacement,
            ):
                journaled.append(old_path)
    return journaled


def append_retired_top_level_poster_receipts(
    plan: dict[str, Any],
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    """Journal/retarget receipts for an accepted top-level output write.

    Template and music orchestrators store their primary render directly on
    ``assembly_plan`` instead of in ``variants``.  Their poster extraction is
    fail-open too, so the replacement proof falls back to the newly committed
    video object when no poster was produced.

    ``previous`` is the row-locked committed plan.  Its receipt list is the
    authoritative chain and is copied into ``plan`` before any retargeting;
    this matters for finalizers that intentionally rebuild the rest of the
    plan from scratch.
    """
    if VIDEO_POSTER_BACKFILL_CLEANUP_FIELD in previous:
        plan[VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] = copy.deepcopy(
            previous.get(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD)
        )
    else:
        plan.pop(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD, None)

    journaled: list[str] = []
    for poster_field, video_fields in _PLAN_POSTER_REPLACEMENTS:
        replacement = current.get(poster_field)
        if not isinstance(replacement, str) or not replacement:
            replacement = next(
                (
                    value
                    for field in video_fields
                    if isinstance((value := current.get(field)), str) and value
                ),
                None,
            )
        if not isinstance(replacement, str):
            continue
        previous_proofs = list(
            dict.fromkeys(
                value
                for field in (poster_field, *video_fields)
                if isinstance((value := previous.get(field)), str) and value
            )
        )
        for old_path in previous_proofs:
            if old_path != replacement and append_video_poster_cleanup_receipt(
                plan,
                old_path=old_path,
                replacement_path=replacement,
            ):
                journaled.append(old_path)
    return journaled


def _safe_owned_asset_path(raw_path: object, job: Job) -> str | None:
    return owned_job_output_path(raw_path, job)


def _safe_owned_uuid_poster(raw_path: object, job: Job) -> str | None:
    path = _safe_owned_asset_path(raw_path, job)
    if path is None or not is_uuid_backfill_poster(path):
        return None
    return path


def _asset_references(
    plan: dict[str, Any],
    clips: list[JobClip],
    job: Job,
) -> tuple[set[str], set[str]]:
    """Return active and rollback-only owned asset references."""
    references: set[str] = set()
    rollback_references: set[str] = set()

    def add(raw_path: object, *, rollback: bool = False) -> None:
        path = _safe_owned_asset_path(raw_path, job)
        if path is not None:
            (rollback_references if rollback else references).add(path)

    def add_variant(variant: object, *, rollback: bool = False) -> None:
        if not isinstance(variant, dict):
            return
        for field in _VARIANT_ASSET_FIELDS:
            add(variant.get(field), rollback=rollback)

    for field in _PLAN_ASSET_FIELDS:
        add(plan.get(field))
    variants = plan.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            add_variant(variant)
    for clip in clips:
        add(clip.video_path)
        add(clip.thumbnail_path)

    # Speech-cut rerenders keep exact rollback variants until publication. If a
    # render fails, those snapshots are restored into the active variants list;
    # deleting their old poster first would turn the rollback into a blank tile.
    add_variant(plan.get("speech_cut_previous_variant"), rollback=True)
    previous_variants = plan.get("speech_cut_previous_variants")
    if isinstance(previous_variants, list):
        for variant in previous_variants:
            add_variant(variant, rollback=True)
    return references, rollback_references


def reconcile_video_poster_cleanup_receipts_locked(
    db: Any,
    job: Job,
) -> VideoPosterCleanupResult:
    """Reconcile one Job's receipts while its row lock is held.

    The old UUID key must be unreferenced.  Its replacement can be either a
    poster or the current video, but must be owned by this Job, referenced by
    committed state, and present in storage.  Failed deletes remain journaled
    for the five-minute maintenance sweep.
    """
    if job.assembly_plan is None:
        return VideoPosterCleanupResult()
    if not isinstance(job.assembly_plan, dict):
        # Do not coerce a corrupt JSONB value to an empty plan: strict repair
        # must stay red instead of claiming that cleanup debt is absent.
        return VideoPosterCleanupResult(receipts_seen=1, retained=1, failures=1)
    plan = copy.deepcopy(job.assembly_plan)
    if VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in plan:
        return VideoPosterCleanupResult()
    raw_receipts = plan.get(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD)
    if isinstance(raw_receipts, list) and not raw_receipts:
        # The bounded sweep queries JSON key existence. Leaving an empty list
        # in place would select the same oldest rows forever.
        plan.pop(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD, None)
        job.assembly_plan = plan
        flag_modified(job, "assembly_plan")
        db.commit()
        return VideoPosterCleanupResult()
    receipts: list[Any] = (
        copy.deepcopy(raw_receipts)
        if isinstance(raw_receipts, list)
        else [copy.deepcopy(raw_receipts)]
    )

    clips = list(
        db.execute(select(JobClip).where(JobClip.job_id == job.id).with_for_update())
        .scalars()
        .all()
    )
    references, rollback_references = _asset_references(plan, clips, job)
    all_references = references | rollback_references
    processing_receipts = receipts[:VIDEO_POSTER_CLEANUP_RECEIPTS_PER_PASS]
    untouched_receipts = receipts[VIDEO_POSTER_CLEANUP_RECEIPTS_PER_PASS:]
    processed_retained: list[Any] = []
    deleted = 0
    failures = 0
    ignored = 0

    from app.storage import delete_object_once, object_exists_once  # noqa: PLC0415

    for raw_receipt in processing_receipts:
        if not isinstance(raw_receipt, dict):
            processed_retained.append(copy.deepcopy(raw_receipt))
            failures += 1
            continue
        raw_old_path = raw_receipt.get("old_path")
        old_path = _safe_owned_uuid_poster(raw_old_path, job)
        replacement_path = _safe_owned_asset_path(raw_receipt.get("replacement_path"), job)
        if old_path is None:
            owned_old_path = _safe_owned_asset_path(raw_old_path, job)
            if owned_old_path is not None and not is_uuid_backfill_poster(owned_old_path):
                # Deterministic poster keys are shared destinations and are not
                # cleanup debt for this immutable-object service.
                ignored += 1
            else:
                processed_retained.append(copy.deepcopy(raw_receipt))
                failures += 1
            continue
        if replacement_path is None:
            processed_retained.append(copy.deepcopy(raw_receipt))
            failures += 1
            continue
        receipt = {
            "old_path": old_path,
            "replacement_path": replacement_path,
        }
        if old_path in references:
            # It is still a live shared asset, not an orphan. Drop this
            # receipt; the accepted write that eventually displaces the
            # remaining reference will journal it again atomically.
            ignored += 1
            continue
        if old_path in rollback_references:
            processed_retained.append(receipt)
            continue
        try:
            old_exists = object_exists_once(
                old_path,
                timeout_s=VIDEO_POSTER_CLEANUP_STORAGE_TIMEOUT_S,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — durable receipt is the retry guarantee
            processed_retained.append(receipt)
            failures += 1
            continue
        if not old_exists:
            # GCS deletes are idempotent.  A lifecycle rule or an earlier
            # worker may already have completed this receipt even if its
            # replacement proof was subsequently superseded.
            ignored += 1
            continue
        if replacement_path not in all_references:
            # There is no committed replacement proof, so this is an
            # unresolved orphan rather than a successful no-op. Strict
            # backfills must fail closed and the maintenance sweep must retry.
            processed_retained.append(receipt)
            failures += 1
            continue
        try:
            replacement_exists = object_exists_once(
                replacement_path,
                timeout_s=VIDEO_POSTER_CLEANUP_STORAGE_TIMEOUT_S,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — durable receipt is the retry guarantee
            replacement_exists = False
        if not replacement_exists:
            processed_retained.append(receipt)
            failures += 1
            continue
        try:
            delete_object_once(
                old_path,
                timeout_s=VIDEO_POSTER_CLEANUP_STORAGE_TIMEOUT_S,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — durable receipt is the retry guarantee
            processed_retained.append(receipt)
            failures += 1
            continue
        deleted += 1

    # Rotate failed/rollback receipts behind the untouched tail. A permanently
    # malformed entry at the head must stay durable, but it must not consume
    # every bounded pass and starve later, deletable orphan receipts forever.
    retained = untouched_receipts + processed_retained
    if retained:
        plan[VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] = retained
    else:
        plan.pop(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD, None)
    job.assembly_plan = plan
    flag_modified(job, "assembly_plan")
    # Commit while the Job + JobClip locks still protect the reference proof.
    # Retained receipts intentionally update Job.updated_at, rotating bounded
    # sweep pages so one persistent failure cannot starve newer Jobs.
    db.commit()
    return VideoPosterCleanupResult(
        receipts_seen=len(processing_receipts),
        deleted=deleted,
        retained=len(retained),
        failures=failures,
        ignored=ignored,
    )


def reconcile_video_poster_cleanup_receipts(
    job_id: str | uuid.UUID,
) -> VideoPosterCleanupResult:
    job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    with sync_session() as db:
        job = db.get(Job, job_uuid, with_for_update=True)
        if job is None:
            return VideoPosterCleanupResult()
        return reconcile_video_poster_cleanup_receipts_locked(db, job)


def jobs_with_video_poster_cleanup_receipts(db: Any, *, limit: int) -> list[uuid.UUID]:
    """Return a bounded, starvation-resistant page of Jobs with receipts."""
    if limit < 1:
        return []
    return list(
        db.execute(
            select(Job.id)
            # Keep the JSONB key literal so PostgreSQL can prove this query
            # implies the matching partial-index predicate. A bound parameter
            # can force a generic plan that cannot use the sparse index.
            .where(
                func.jsonb_typeof(Job.assembly_plan) == literal_column("'object'"),
                Job.assembly_plan.op("?")(
                    literal_column(f"'{VIDEO_POSTER_BACKFILL_CLEANUP_FIELD}'")
                ),
            )
            .order_by(Job.updated_at.asc(), Job.id.asc())
            .limit(limit)
        ).scalars()
    )
