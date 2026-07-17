"""Transactional owner of Smart Edit plan and revision state.

Callers own commit/rollback.  This module owns every row lock and pointer
transition so API routes and workers cannot invent subtly different state
machines.

Initial render:

    create plan + revision 0 + outbox row (one transaction)
                            |
                            v
               broker dispatch after commit
                            |
                            v
        verified output -- CAS requested == revision --> ready

Correction:

    lock parent -> idempotency lookup -> compare expected revision
         -> append N+1 + outbox + requested=N+1 (one transaction)

The last ready revision is never cleared while a newer render is pending or
failed.  Redelivered workers are fenced by the immutable generation token.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.models import Job, SmartEditDispatch, SmartEditPlan, SmartEditPlanRevision
from app.smart_edit.schemas import (
    MAX_SMART_WORDS,
    SmartEditCorrectionCommand,
    SmartEditPlanDocument,
    SmartWord,
)

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_GENERATION_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DISPATCH_LEASE = timedelta(minutes=30)


class SmartEditNotFoundError(LookupError):
    """The active plan is absent, retired, or not owned by the caller."""


class SmartEditRevisionConflictError(RuntimeError):
    """The caller based a mutation on a stale requested revision."""

    def __init__(self, *, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"expected revision {expected_revision}, "
            f"current requested revision is {current_revision}"
        )


class SmartEditStateError(RuntimeError):
    """Persisted state cannot safely perform the requested transition."""


class SmartEditIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different correction request."""


@dataclass(frozen=True, slots=True)
class RevisionRequestReceipt:
    plan_id: uuid.UUID
    revision: int
    render_generation_id: str
    ready_revision: int | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class RevisionReadyReceipt:
    plan_id: uuid.UUID
    revision: int
    advanced_ready_pointer: bool
    ready_revision: int | None


def _lock_active_plan(
    db: Session,
    plan_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
) -> SmartEditPlan:
    plan = db.get(SmartEditPlan, plan_id, with_for_update=True)
    if plan is None or plan.retired_at is not None:
        raise SmartEditNotFoundError(str(plan_id))
    if user_id is not None and plan.user_id != user_id:
        # Ownership mismatches intentionally look identical to missing plans.
        raise SmartEditNotFoundError(str(plan_id))
    return plan


def _serialize_document(document: SmartEditPlanDocument) -> dict[str, Any]:
    return document.model_dump(mode="json")


def revision_output_gcs_path(
    *, plan_id: uuid.UUID, revision: int, render_generation_id: str
) -> str:
    """Return the only object key a revision generation may publish."""

    if revision < 0 or not _GENERATION_RE.fullmatch(render_generation_id):
        raise SmartEditStateError("invalid revision output identity")
    return f"smart-edits/{plan_id}/revisions/{revision}/{render_generation_id}/output.mp4"


def _validate_and_serialize_words(
    normalized_words: list[SmartWord], document: SmartEditPlanDocument
) -> list[dict[str, Any]]:
    if not normalized_words or len(normalized_words) > MAX_SMART_WORDS:
        raise SmartEditStateError(
            f"normalized_words must contain between 1 and {MAX_SMART_WORDS} words"
        )
    if any(not isinstance(word, SmartWord) for word in normalized_words):
        raise SmartEditStateError("normalized_words must contain validated SmartWord values")

    word_ids = [word.word_id for word in normalized_words]
    if word_ids != sorted(word_ids) or len(word_ids) != len(set(word_ids)):
        raise SmartEditStateError("normalized_words must have unique ascending word ids")
    known_words = set(word_ids)
    display_alignment_ids = {
        aligned_id for word in normalized_words for aligned_id in word.display_alignment
    }
    caption_word_ids = {word_id for cue in document.baseline_captions for word_id in cue.word_ids}
    if not display_alignment_ids <= known_words or not caption_word_ids <= known_words:
        raise SmartEditStateError("document and display alignment must reference normalized words")
    return [word.model_dump(mode="json") for word in normalized_words]


def _ready_evidence_matches(
    revision_row: SmartEditPlanRevision,
    *,
    output_gcs_path: str,
    output_sha256: str,
    output_gcs_generation: str,
    output_size_bytes: int,
    output_duration_ms: int,
    output_probe_receipt: dict[str, Any],
    stage_artifacts: dict[str, Any] | None,
    render_receipt: dict[str, Any],
) -> bool:
    """Return whether a redelivery describes the already-persisted bytes."""

    return (
        revision_row.output_gcs_path == output_gcs_path
        and revision_row.output_sha256 == output_sha256
        and revision_row.output_gcs_generation == output_gcs_generation
        and revision_row.output_size_bytes == output_size_bytes
        and revision_row.output_duration_ms == output_duration_ms
        and revision_row.output_probe_receipt == output_probe_receipt
        and revision_row.stage_artifacts == stage_artifacts
        and revision_row.render_receipt == render_receipt
    )


def create_initial_plan(
    db: Session,
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    variant_id: str,
    source_base_gcs_path: str,
    source_base_sha256: str,
    transcript_hash: str,
    preset_id: str,
    preset_version: str,
    asset_pack_id: str,
    asset_pack_version: str,
    language: str,
    normalized_words: list[SmartWord],
    face_observations: dict[str, Any] | None,
    document: SmartEditPlanDocument,
    compiled_patch: dict[str, Any],
    planner_versions: dict[str, Any],
    validation_receipt: dict[str, Any],
    render_generation_id: str | None = None,
    expected_active_plan_id: uuid.UUID | None = None,
) -> tuple[SmartEditPlan, RevisionRequestReceipt]:
    """Stage revision zero and its dispatch atomically; caller commits.

    Redelivery replays a matching active plan.  A caller that has already
    verified new base bytes or a new transcript may explicitly retire the old
    identity and create its successor under the same Job lock.
    """

    if (
        not source_base_gcs_path.strip()
        or not _SHA256_RE.fullmatch(source_base_sha256)
        or not _SHA256_RE.fullmatch(transcript_hash)
    ):
        raise SmartEditStateError("source identity evidence is incomplete")
    # The Job row is the creation mutex.  It also binds the plan to the
    # authenticated owner so concurrent redelivery cannot create two active
    # plans or cross tenant boundaries.
    job = db.get(Job, job_id, with_for_update=True)
    if job is None or job.user_id != user_id:
        raise SmartEditNotFoundError(str(job_id))

    supersedes_plan_id: uuid.UUID | None = None
    existing_plan = db.execute(
        select(SmartEditPlan).where(
            SmartEditPlan.job_id == job_id,
            SmartEditPlan.variant_id == variant_id,
            SmartEditPlan.retired_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing_plan is not None:
        same_identity = (
            existing_plan.user_id == job.user_id
            and existing_plan.source_base_sha256 == source_base_sha256
            and existing_plan.transcript_hash == transcript_hash
            and existing_plan.schema_version == document.schema_version
            and existing_plan.preset_id == preset_id
            and existing_plan.preset_version == preset_version
            and existing_plan.asset_pack_id == asset_pack_id
            and existing_plan.asset_pack_version == asset_pack_version
            and existing_plan.language == language
        )
        if not same_identity:
            if expected_active_plan_id is None:
                raise SmartEditStateError(
                    "an active plan already exists for different immutable source identity"
                )
            if existing_plan.id != expected_active_plan_id:
                raise SmartEditStateError("active plan changed before supersession")
            existing_plan.state = "retired"
            existing_plan.retired_at = datetime.now(UTC)
            supersedes_plan_id = existing_plan.id
            db.execute(
                update(SmartEditDispatch)
                .where(
                    SmartEditDispatch.plan_id == existing_plan.id,
                    SmartEditDispatch.state.in_(("pending", "dispatched")),
                )
                .values(state="cancelled", last_error="plan superseded")
            )
            # Make the partial unique index release explicit before inserting
            # the successor.  A later failure still rolls the transaction back.
            db.flush()
        else:
            requested_row = db.execute(
                select(SmartEditPlanRevision).where(
                    SmartEditPlanRevision.plan_id == existing_plan.id,
                    SmartEditPlanRevision.revision == existing_plan.requested_revision,
                )
            ).scalar_one_or_none()
            if requested_row is None or not requested_row.render_generation_id:
                raise SmartEditStateError("active plan is missing its requested revision")
            return existing_plan, RevisionRequestReceipt(
                plan_id=existing_plan.id,
                revision=existing_plan.requested_revision,
                render_generation_id=requested_row.render_generation_id,
                ready_revision=existing_plan.ready_revision,
                replayed=True,
            )

    serialized_words = _validate_and_serialize_words(normalized_words, document)
    plan_id = uuid.uuid4()
    generation = render_generation_id or uuid.uuid4().hex
    if not _GENERATION_RE.fullmatch(generation):
        raise SmartEditStateError("invalid render generation token")
    plan = SmartEditPlan(
        id=plan_id,
        job_id=job_id,
        user_id=user_id,
        variant_id=variant_id,
        source_base_gcs_path=source_base_gcs_path,
        source_base_sha256=source_base_sha256,
        transcript_hash=transcript_hash,
        schema_version=document.schema_version,
        preset_id=preset_id,
        preset_version=preset_version,
        asset_pack_id=asset_pack_id,
        asset_pack_version=asset_pack_version,
        language=language,
        normalized_words=serialized_words,
        face_observations=face_observations,
        requested_revision=0,
        ready_revision=None,
        accepted_revision=None,
        state="rendering",
        supersedes_plan_id=supersedes_plan_id,
    )
    revision_row = SmartEditPlanRevision(
        plan_id=plan_id,
        revision=0,
        parent_revision=None,
        document=_serialize_document(document),
        compiled_patch=dict(compiled_patch),
        planner_versions=dict(planner_versions),
        validation_receipt=dict(validation_receipt),
        render_generation_id=generation,
        status="requested",
    )
    dispatch = SmartEditDispatch(
        plan_id=plan_id,
        revision=0,
        render_generation_id=generation,
        state="pending",
    )
    db.add_all([plan, revision_row, dispatch])
    db.flush()
    return plan, RevisionRequestReceipt(
        plan_id=plan_id,
        revision=0,
        render_generation_id=generation,
        ready_revision=None,
        replayed=False,
    )


def append_revision(
    db: Session,
    *,
    plan_id: uuid.UUID,
    user_id: uuid.UUID,
    command: SmartEditCorrectionCommand,
    document: SmartEditPlanDocument,
    compiled_patch: dict[str, Any],
    planner_versions: dict[str, Any],
    validation_receipt: dict[str, Any],
    render_generation_id: str | None = None,
) -> RevisionRequestReceipt:
    """Compare-and-append one immutable correction revision plus outbox row."""

    expected_revision = command.expected_revision
    idempotency_key = command.idempotency_key
    correction = command.model_dump(mode="json")
    plan = _lock_active_plan(db, plan_id, user_id=user_id)

    if document.schema_version != plan.schema_version:
        raise SmartEditStateError("document schema version does not match the active plan")

    existing = db.execute(
        select(SmartEditPlanRevision).where(
            SmartEditPlanRevision.plan_id == plan_id,
            SmartEditPlanRevision.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.render_generation_id:
            raise SmartEditStateError("idempotent revision is missing its generation token")
        if existing.parent_revision != expected_revision or existing.correction != dict(correction):
            raise SmartEditIdempotencyConflictError(
                "idempotency key was already used for a different correction"
            )
        return RevisionRequestReceipt(
            plan_id=plan_id,
            revision=existing.revision,
            render_generation_id=existing.render_generation_id,
            ready_revision=plan.ready_revision,
            replayed=True,
        )

    if plan.requested_revision != expected_revision:
        raise SmartEditRevisionConflictError(
            expected_revision=expected_revision,
            current_revision=plan.requested_revision,
        )

    parent = db.execute(
        select(SmartEditPlanRevision).where(
            SmartEditPlanRevision.plan_id == plan_id,
            SmartEditPlanRevision.revision == expected_revision,
        )
    ).scalar_one_or_none()
    if parent is None:
        raise SmartEditStateError("requested revision pointer has no matching revision row")

    revision = expected_revision + 1
    generation = render_generation_id or uuid.uuid4().hex
    if not _GENERATION_RE.fullmatch(generation):
        raise SmartEditStateError("invalid render generation token")
    revision_row = SmartEditPlanRevision(
        plan_id=plan_id,
        revision=revision,
        parent_revision=expected_revision,
        document=_serialize_document(document),
        compiled_patch=dict(compiled_patch),
        correction=dict(correction),
        planner_versions=dict(planner_versions),
        validation_receipt=dict(validation_receipt),
        render_generation_id=generation,
        status="requested",
        idempotency_key=idempotency_key,
    )
    dispatch = SmartEditDispatch(
        plan_id=plan_id,
        revision=revision,
        render_generation_id=generation,
        state="pending",
    )
    db.add_all([revision_row, dispatch])
    plan.requested_revision = revision
    plan.state = "rerendering"
    db.flush()
    return RevisionRequestReceipt(
        plan_id=plan_id,
        revision=revision,
        render_generation_id=generation,
        ready_revision=plan.ready_revision,
        replayed=False,
    )


def mark_revision_ready(
    db: Session,
    *,
    plan_id: uuid.UUID,
    revision: int,
    render_generation_id: str,
    output_gcs_path: str,
    output_sha256: str,
    output_gcs_generation: str,
    output_size_bytes: int,
    output_duration_ms: int,
    output_probe_receipt: dict[str, Any],
    stage_artifacts: dict[str, Any] | None,
    render_receipt: dict[str, Any],
) -> RevisionReadyReceipt:
    """Record verified immutable output and CAS-advance the last-good pointer."""

    if output_probe_receipt.get("verified") is not True:
        raise SmartEditStateError("output probe must be verified before ready")
    expected_output_path = revision_output_gcs_path(
        plan_id=plan_id,
        revision=revision,
        render_generation_id=render_generation_id,
    )
    if (
        output_gcs_path != expected_output_path
        or not output_gcs_generation.strip()
        or not _SHA256_RE.fullmatch(output_sha256)
        or output_size_bytes <= 0
        or output_duration_ms <= 0
    ):
        raise SmartEditStateError("output evidence is incomplete")

    plan = _lock_active_plan(db, plan_id)
    revision_row = db.execute(
        select(SmartEditPlanRevision).where(
            SmartEditPlanRevision.plan_id == plan_id,
            SmartEditPlanRevision.revision == revision,
        )
    ).scalar_one_or_none()
    if revision_row is None:
        raise SmartEditStateError("revision row does not exist")
    if revision_row.render_generation_id != render_generation_id:
        raise SmartEditStateError("stale render generation")

    dispatch = db.execute(
        select(SmartEditDispatch).where(
            SmartEditDispatch.plan_id == plan_id,
            SmartEditDispatch.revision == revision,
            SmartEditDispatch.render_generation_id == render_generation_id,
        )
    ).scalar_one_or_none()
    if dispatch is None:
        raise SmartEditStateError("revision is missing its dispatch outbox row")

    normalized_probe_receipt = dict(output_probe_receipt)
    normalized_stage_artifacts = dict(stage_artifacts) if stage_artifacts is not None else None
    normalized_render_receipt = dict(render_receipt)
    if revision_row.status == "ready":
        if not _ready_evidence_matches(
            revision_row,
            output_gcs_path=output_gcs_path,
            output_sha256=output_sha256,
            output_gcs_generation=output_gcs_generation,
            output_size_bytes=output_size_bytes,
            output_duration_ms=output_duration_ms,
            output_probe_receipt=normalized_probe_receipt,
            stage_artifacts=normalized_stage_artifacts,
            render_receipt=normalized_render_receipt,
        ):
            raise SmartEditStateError("ready revision output evidence is immutable")
        # Heal a partially observed outbox state without rewriting immutable
        # output evidence.  This is safe for duplicate worker delivery.
        dispatch.state = "completed"
        dispatch.last_error = None
        db.flush()
        return RevisionReadyReceipt(
            plan_id=plan_id,
            revision=revision,
            advanced_ready_pointer=False,
            ready_revision=plan.ready_revision,
        )

    revision_row.output_gcs_path = output_gcs_path
    revision_row.output_sha256 = output_sha256
    revision_row.output_gcs_generation = output_gcs_generation
    revision_row.output_size_bytes = output_size_bytes
    revision_row.output_duration_ms = output_duration_ms
    revision_row.output_probe_receipt = normalized_probe_receipt
    revision_row.stage_artifacts = normalized_stage_artifacts
    revision_row.render_receipt = normalized_render_receipt
    revision_row.status = "ready"
    revision_row.error_code = None
    revision_row.error_detail = None
    revision_row.render_finished_at = datetime.now(UTC)
    dispatch.state = "completed"
    dispatch.last_error = None

    advanced = plan.requested_revision == revision
    if advanced:
        plan.ready_revision = revision
        plan.state = "ready"
    db.flush()
    return RevisionReadyReceipt(
        plan_id=plan_id,
        revision=revision,
        advanced_ready_pointer=advanced,
        ready_revision=plan.ready_revision,
    )


def mark_revision_failed(
    db: Session,
    *,
    plan_id: uuid.UUID,
    revision: int,
    render_generation_id: str,
    error_code: str,
    error_detail: str,
) -> None:
    """Record failure without clearing the last completed revision."""

    plan = _lock_active_plan(db, plan_id)
    revision_row = db.execute(
        select(SmartEditPlanRevision).where(
            SmartEditPlanRevision.plan_id == plan_id,
            SmartEditPlanRevision.revision == revision,
        )
    ).scalar_one_or_none()
    if revision_row is None or revision_row.render_generation_id != render_generation_id:
        raise SmartEditStateError("missing revision or stale render generation")

    dispatch = db.execute(
        select(SmartEditDispatch).where(
            SmartEditDispatch.plan_id == plan_id,
            SmartEditDispatch.revision == revision,
            SmartEditDispatch.render_generation_id == render_generation_id,
        )
    ).scalar_one_or_none()
    if dispatch is None:
        raise SmartEditStateError("revision is missing its dispatch outbox row")
    if revision_row.status == "ready":
        # Success is terminal.  A late exception from a duplicate delivery must
        # not downgrade verified bytes or the plan's last-good pointer.
        dispatch.state = "completed"
        dispatch.last_error = None
        db.flush()
        return
    if revision_row.status == "failed":
        return

    revision_row.status = "failed"
    revision_row.error_code = error_code[:128]
    revision_row.error_detail = error_detail[:2000]
    revision_row.render_finished_at = datetime.now(UTC)
    dispatch.state = "failed"
    dispatch.last_error = error_detail[:2000]
    if plan.requested_revision == revision:
        plan.state = "failed"
    db.flush()


def accept_ready_revision(
    db: Session,
    *,
    plan_id: uuid.UUID,
    user_id: uuid.UUID,
    revision: int,
) -> SmartEditPlanRevision:
    """Pin the latest verified revision; canonical variant projection happens outside."""

    plan = _lock_active_plan(db, plan_id, user_id=user_id)
    if plan.requested_revision != revision or plan.ready_revision != revision:
        raise SmartEditRevisionConflictError(
            expected_revision=revision,
            current_revision=plan.requested_revision,
        )
    revision_row = db.execute(
        select(SmartEditPlanRevision).where(
            SmartEditPlanRevision.plan_id == plan_id,
            SmartEditPlanRevision.revision == revision,
        )
    ).scalar_one_or_none()
    if (
        revision_row is None
        or revision_row.status != "ready"
        or not revision_row.output_gcs_path
        or not revision_row.output_sha256
        or not isinstance(revision_row.output_probe_receipt, dict)
        or revision_row.output_probe_receipt.get("verified") is not True
    ):
        raise SmartEditStateError("only a verified ready revision can be accepted")
    plan.accepted_revision = revision
    db.flush()
    return revision_row


def lock_pending_dispatches(
    db: Session,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> list[SmartEditDispatch]:
    """Lock due outbox rows for enqueue; caller records result then commits.

    The broker call intentionally happens while these rows are locked.  A
    successful enqueue followed by commit failure may redeliver, which is safe
    because revision workers are fenced by ``render_generation_id``.  The
    inverse failure (DB commit but no broker message) cannot strand a revision.
    """

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    claim_time = now or datetime.now(UTC)
    stmt = (
        select(SmartEditDispatch)
        .join(SmartEditPlan, SmartEditPlan.id == SmartEditDispatch.plan_id)
        .where(
            SmartEditPlan.retired_at.is_(None),
            or_(
                and_(
                    SmartEditDispatch.state == "pending",
                    SmartEditDispatch.available_at <= claim_time,
                ),
                and_(
                    SmartEditDispatch.state == "dispatched",
                    SmartEditDispatch.available_at <= claim_time,
                ),
            ),
        )
        .order_by(SmartEditDispatch.available_at.asc(), SmartEditDispatch.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(db.execute(stmt).scalars().all())


def record_dispatch_succeeded(
    db: Session,
    dispatch: SmartEditDispatch,
    *,
    delivery_lease: timedelta = _DISPATCH_LEASE,
) -> None:
    if dispatch.state not in {"pending", "dispatched"}:
        return
    revision_row = db.execute(
        select(SmartEditPlanRevision).where(
            SmartEditPlanRevision.plan_id == dispatch.plan_id,
            SmartEditPlanRevision.revision == dispatch.revision,
            SmartEditPlanRevision.render_generation_id == dispatch.render_generation_id,
        )
    ).scalar_one_or_none()
    if revision_row is None:
        raise SmartEditStateError("dispatch is missing its revision generation")
    if revision_row.status == "ready":
        dispatch.state = "completed"
        dispatch.last_error = None
        db.flush()
        return
    if revision_row.status == "failed":
        dispatch.state = "failed"
        db.flush()
        return
    if revision_row.status == "requested":
        revision_row.render_started_at = datetime.now(UTC)
    revision_row.status = "rendering"
    dispatch.state = "dispatched"
    dispatch.attempt_count = (dispatch.attempt_count or 0) + 1
    dispatch.last_error = None
    dispatch.available_at = datetime.now(UTC) + delivery_lease
    db.flush()


def record_dispatch_failed(
    db: Session,
    dispatch: SmartEditDispatch,
    *,
    error: str,
    retry_after: timedelta | None = None,
) -> None:
    if dispatch.state not in {"pending", "dispatched"}:
        return
    dispatch.state = "pending"
    dispatch.attempt_count = (dispatch.attempt_count or 0) + 1
    dispatch.last_error = error[:2000]
    if retry_after is None:
        base_seconds = min(15 * (2 ** min(dispatch.attempt_count - 1, 5)), 300)
        # Stable ±10% jitter prevents every outbox row from retrying on the same
        # second after a broker outage while keeping tests deterministic.
        jitter_pct = ((dispatch.plan_id.int + dispatch.revision) % 21 - 10) / 100
        retry_after = timedelta(seconds=base_seconds * (1 + jitter_pct))
    dispatch.available_at = datetime.now(UTC) + retry_after
    db.flush()
