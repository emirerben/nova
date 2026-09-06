"""Durable lifecycle and public contract for chat speech-cleanup preflight."""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import ContentPlan, Job, Persona, PlanItem, SpeechCleanupAnalysis
from app.services.active_narration_source import ActiveNarrationResolution, ActiveNarrationSource
from app.services.speech_cleanup_selection import DETECTOR_VERSION

SPEECH_CLEANUP_ENGINE_VERSION = "preflight-v1-2026-09-05"
SPEECH_CLEANUP_PAYLOAD_VERSION = "1"
SPEECH_CLEANUP_TASK_SOFT_LIMIT_S = 840
SPEECH_CLEANUP_TASK_HARD_LIMIT_S = 900
SPEECH_CLEANUP_LEASE_S = 960
SPEECH_CLEANUP_CLOCK_SKEW_S = 30
SPEECH_CLEANUP_RECONCILE_BATCH = 25
SPEECH_CLEANUP_MAX_ATTEMPTS = 3
SPEECH_CLEANUP_MAX_DURATION_S = 300.0
# These are execution-policy inputs, unlike the off/shadow/enforce exposure
# controls. Keep them explicit and shared by fingerprinting and the worker so
# an analysis identity always describes the algorithm that produced it.
SPEECH_CLEANUP_MIXED_GAP_MODE: Literal["apply"] = "apply"
SPEECH_CLEANUP_OVER_BUDGET_POLICY: Literal["clamp"] = "clamp"

if SPEECH_CLEANUP_LEASE_S <= SPEECH_CLEANUP_TASK_HARD_LIMIT_S + SPEECH_CLEANUP_CLOCK_SKEW_S:
    raise RuntimeError("speech cleanup lease must exceed hard limit plus clock skew")

SpeechCleanupDecision = Literal["clean", "keep_original", "create_without_cleanup"]

_FAILURE_RETRYABLE: dict[str, bool] = {
    "source_temporarily_unavailable": True,
    "audio_extraction_timeout": True,
    "transcription_unavailable": True,
    "analysis_timeout": True,
    "unsupported_media": False,
    "no_speech_track": False,
    "snapshot_mismatch": False,
    "internal_error": True,
}


class SpeechCleanupOperationalError(RuntimeError):
    """Expected, sanitized preflight failure that may become public state."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in _FAILURE_RETRYABLE:
            raise ValueError(f"unsupported speech cleanup failure code: {code}")
        self.code = code
        self.retryable = _FAILURE_RETRYABLE[code]
        self.private_detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class ClaimedSpeechCleanupAnalysis:
    analysis_id: uuid.UUID
    attempt_token: str
    source_policy_fingerprint: str


@dataclass(frozen=True)
class SpeechCleanupSchedulingIntent:
    analysis_id: uuid.UUID
    created: bool


@dataclass(frozen=True)
class SnapshotMismatchReanalysis:
    """Locked render row plus optional post-commit preflight publication.

    The render terminalizer still owns the Job mutation and commit.  This
    result only lets it share the canonical Plan -> Persona -> PlanItem -> Job
    lock acquisition while atomically resetting the exact accepted analysis.
    """

    job: Job | None
    analysis_id: uuid.UUID | None = None


def stable_preflight_cohort(fingerprint: str) -> int:
    """Return a stable 0..99 source cohort without exposing the fingerprint."""

    return int(hashlib.sha256(f"speech-preflight:{fingerprint}".encode()).hexdigest()[:8], 16) % 100


def preflight_enabled_for_source(
    fingerprint: str,
    *,
    mode: str,
    rollout_percent: int,
) -> bool:
    if mode == "off" or rollout_percent <= 0:
        return False
    if mode not in {"shadow", "enforce"}:
        raise ValueError("invalid speech cleanup preflight mode")
    return rollout_percent >= 100 or stable_preflight_cohort(fingerprint) < rollout_percent


def _new_analysis(
    plan_item_id: uuid.UUID,
    source: ActiveNarrationSource,
    *,
    now: datetime | None = None,
) -> SpeechCleanupAnalysis:
    current_time = now or datetime.now(UTC)
    return SpeechCleanupAnalysis(
        plan_item_id=plan_item_id,
        source_kind=source.source_kind,
        source_media_identity=source.media_id,
        source_storage_path=source.storage_path,
        source_generation=source.generation,
        window_start_s=source.window_start_s,
        window_end_s=source.window_end_s,
        source_policy_fingerprint=source.source_policy_fingerprint,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        analysis_payload_version=SPEECH_CLEANUP_PAYLOAD_VERSION,
        status="queued",
        next_dispatch_at=current_time,
    )


def _exact_historical_analysis_statement(
    plan_item_id: uuid.UUID,
    source_policy_fingerprint: str,
):
    """Select the one prior row protected by the durable identity key."""

    return (
        select(SpeechCleanupAnalysis)
        .where(
            SpeechCleanupAnalysis.plan_item_id == plan_item_id,
            SpeechCleanupAnalysis.source_policy_fingerprint == source_policy_fingerprint,
            SpeechCleanupAnalysis.engine_version == SPEECH_CLEANUP_ENGINE_VERSION,
            SpeechCleanupAnalysis.superseded_at.is_not(None),
        )
        .with_for_update()
        .limit(1)
    )


async def _exact_historical_analysis_async(
    db: AsyncSession,
    plan_item_id: uuid.UUID,
    source_policy_fingerprint: str,
) -> SpeechCleanupAnalysis | None:
    stmt = _exact_historical_analysis_statement(plan_item_id, source_policy_fingerprint)
    return (await db.execute(stmt)).scalar_one_or_none()


def _exact_historical_analysis_sync(
    db: Session,
    plan_item_id: uuid.UUID,
    source_policy_fingerprint: str,
) -> SpeechCleanupAnalysis | None:
    stmt = _exact_historical_analysis_statement(plan_item_id, source_policy_fingerprint)
    return db.execute(stmt).scalar_one_or_none()


def _source_snapshot_matches(
    row: SpeechCleanupAnalysis,
    source: ActiveNarrationSource,
) -> bool:
    try:
        return bool(
            row.source_kind == source.source_kind
            and row.source_media_identity == source.media_id
            and row.source_storage_path == source.storage_path
            and row.source_generation == source.generation
            and math.isclose(
                float(row.window_start_s),
                float(source.window_start_s),
                rel_tol=0,
                abs_tol=1e-6,
            )
            and row.window_end_s is not None
            and math.isclose(
                float(row.window_end_s),
                float(source.window_end_s),
                rel_tol=0,
                abs_tol=1e-6,
            )
            and row.source_policy_fingerprint == source.source_policy_fingerprint
            and row.engine_version == SPEECH_CLEANUP_ENGINE_VERSION
            and row.detector_version == DETECTOR_VERSION
            and row.analysis_payload_version == SPEECH_CLEANUP_PAYLOAD_VERSION
        )
    except (TypeError, ValueError):
        return False


def _terminal_result_is_reusable(
    row: SpeechCleanupAnalysis,
    source: ActiveNarrationSource,
) -> bool:
    """Validate settled evidence before making a historical row current again."""

    if not _source_snapshot_matches(row, source):
        return False
    if (
        row.completed_at is None
        or row.attempt_token is not None
        or row.lease_expires_at is not None
    ):
        return False
    if row.status == "failed":
        return bool(
            row.failure_code in _FAILURE_RETRYABLE
            and row.failure_retryable is _FAILURE_RETRYABLE[row.failure_code]
            and row.analysis_payload is None
            and row.candidate_count is None
            and row.category_counts is None
            and row.estimated_removed_ms is None
        )
    if row.status not in {"ready", "no_findings"} or not isinstance(row.analysis_payload, dict):
        return False

    # A historical payload is immutable evidence only if its private and public
    # halves still agree. Re-validating here is intentionally rare (only an
    # A -> B -> A source transition) and prevents corrupt old JSON from being
    # promoted back into the decision card.
    try:
        from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisResult

        result = SpeechCleanupAnalysisResult.from_payload(row.analysis_payload)
    except (ImportError, TypeError, ValueError):
        return False
    receipt = result.public_receipt
    expected_status = "ready" if receipt.candidate_count > 0 else "no_findings"
    return bool(
        row.status == expected_status
        and row.failure_code is None
        and row.failure_retryable is None
        and result.source_fingerprint == source.source_policy_fingerprint
        and result.detector_version == DETECTOR_VERSION
        and math.isclose(
            result.source_window_start_s,
            source.window_start_s,
            rel_tol=0,
            abs_tol=1e-6,
        )
        and math.isclose(
            result.source_window_end_s,
            source.window_end_s,
            rel_tol=0,
            abs_tol=1e-6,
        )
        and row.candidate_count == receipt.candidate_count
        and row.category_counts == receipt.category_counts.model_dump()
        and row.estimated_removed_ms == receipt.estimated_removed_ms
    )


def _clear_item_cleanup_choice(item: PlanItem) -> None:
    # False is only the compatibility mirror. A source reactivation is a new
    # consent epoch even when its exact analysis evidence can be reused.
    item.speech_cleanup_enabled = False
    item.speech_cleanup_notice = None


def _reset_analysis_for_dispatch(
    row: SpeechCleanupAnalysis,
    source: ActiveNarrationSource,
    *,
    now: datetime,
) -> None:
    """Fence prior attempts and reset a historical identity for fresh work."""

    row.source_kind = source.source_kind
    row.source_media_identity = source.media_id
    row.source_storage_path = source.storage_path
    row.source_generation = source.generation
    row.window_start_s = source.window_start_s
    row.window_end_s = source.window_end_s
    row.source_policy_fingerprint = source.source_policy_fingerprint
    row.engine_version = SPEECH_CLEANUP_ENGINE_VERSION
    row.detector_version = DETECTOR_VERSION
    row.analysis_payload_version = SPEECH_CLEANUP_PAYLOAD_VERSION
    row.status = "queued"
    row.attempt_token = None
    row.attempt_count = 0
    row.dispatched_at = None
    row.next_dispatch_at = now
    row.started_at = None
    row.lease_expires_at = None
    row.completed_at = None
    row.analysis_payload = None
    row.candidate_count = None
    row.category_counts = None
    row.estimated_removed_ms = None
    row.diagnostic_receipt = None
    row.failure_code = None
    row.failure_retryable = None


def _reactivate_historical_analysis(
    row: SpeechCleanupAnalysis,
    item: PlanItem,
    source: ActiveNarrationSource,
    *,
    now: datetime,
) -> SpeechCleanupSchedulingIntent:
    """Make an exact historical identity current without violating either unique key."""

    reusable = _terminal_result_is_reusable(row, source)
    row.decision = None
    row.decision_at = None
    if not reusable:
        # queued/running attempts from the row's previous activation must not be
        # allowed to complete after it becomes current again. Clearing the token
        # makes their CAS terminal write fail; the fresh dispatch gets a new one.
        _reset_analysis_for_dispatch(row, source, now=now)
    row.superseded_at = None
    _clear_item_cleanup_choice(item)
    return SpeechCleanupSchedulingIntent(row.id, not reusable)


async def current_analysis_async(
    db: AsyncSession,
    plan_item_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SpeechCleanupAnalysis | None:
    stmt = select(SpeechCleanupAnalysis).where(
        SpeechCleanupAnalysis.plan_item_id == plan_item_id,
        SpeechCleanupAnalysis.superseded_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


def current_analysis_sync(
    db: Session,
    plan_item_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SpeechCleanupAnalysis | None:
    stmt = select(SpeechCleanupAnalysis).where(
        SpeechCleanupAnalysis.plan_item_id == plan_item_id,
        SpeechCleanupAnalysis.superseded_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.execute(stmt.limit(1)).scalar_one_or_none()


def preflight_rollout_active() -> bool:
    """Whether media writers should pay the current-row/scheduling cost."""

    from app.config import settings

    return bool(
        settings.speech_cleanup_preflight_mode != "off"
        and settings.speech_cleanup_preflight_rollout_percent > 0
    )


async def mutation_current_analysis_async(
    db: AsyncSession,
    plan_item_id: uuid.UUID,
    *,
    for_update: bool = True,
) -> SpeechCleanupAnalysis | None:
    """Load invalidation state only while preflight rollout is active."""

    if not preflight_rollout_active():
        return None
    return await current_analysis_async(db, plan_item_id, for_update=for_update)


def mutation_current_analysis_sync(
    db: Session,
    plan_item_id: uuid.UUID,
    *,
    for_update: bool = True,
) -> SpeechCleanupAnalysis | None:
    """Synchronous mutation helper with zero dark-launch query overhead."""

    if not preflight_rollout_active():
        return None
    return current_analysis_sync(db, plan_item_id, for_update=for_update)


async def ensure_current_analysis_async(
    db: AsyncSession,
    item: PlanItem,
    resolution: ActiveNarrationResolution,
) -> SpeechCleanupSchedulingIntent | None:
    """Create/reuse one current analysis while the caller holds the item lock."""

    source = resolution.source
    if source is None:
        return None
    current_time = datetime.now(UTC)
    current = await current_analysis_async(db, item.id, for_update=True)
    if current is not None:
        if (
            current.source_policy_fingerprint == source.source_policy_fingerprint
            and current.engine_version == SPEECH_CLEANUP_ENGINE_VERSION
        ):
            return SpeechCleanupSchedulingIntent(current.id, False)
        current.superseded_at = current_time
        _clear_item_cleanup_choice(item)
        # Release the partial current-row unique key before reactivating a
        # historical identity. SQLAlchemy is otherwise free to order the two
        # UPDATEs such that superseded_at=NULL is written first.
        await db.flush()
    historical = await _exact_historical_analysis_async(
        db,
        item.id,
        source.source_policy_fingerprint,
    )
    if historical is not None:
        intent = _reactivate_historical_analysis(
            historical,
            item,
            source,
            now=current_time,
        )
        await db.flush()
        return intent
    analysis = _new_analysis(item.id, source, now=current_time)
    _clear_item_cleanup_choice(item)
    db.add(analysis)
    await db.flush()
    return SpeechCleanupSchedulingIntent(analysis.id, True)


async def schedule_item_preflight_async(
    db: AsyncSession,
    item: PlanItem,
) -> uuid.UUID | None:
    """Resolve rollout + active source and persist new work in one transaction."""

    from app.config import settings
    from app.services.plan_item_media import current_detector_policy, resolve_item_narration

    if (
        settings.speech_cleanup_preflight_mode == "off"
        or settings.speech_cleanup_preflight_rollout_percent <= 0
    ):
        return None
    resolution = resolve_item_narration(item, detector_policy=current_detector_policy())
    if resolution.source is None or not preflight_enabled_for_source(
        resolution.source.source_policy_fingerprint,
        mode=settings.speech_cleanup_preflight_mode,
        rollout_percent=settings.speech_cleanup_preflight_rollout_percent,
    ):
        return None
    intent = await ensure_current_analysis_async(db, item, resolution)
    return intent.analysis_id if intent.created else None


def schedule_item_preflight_sync(
    db: Session,
    item: PlanItem,
) -> uuid.UUID | None:
    """Synchronous twin used by Celery-owned PlanItem media mutations."""

    from app.config import settings
    from app.services.plan_item_media import current_detector_policy, resolve_item_narration

    if (
        settings.speech_cleanup_preflight_mode == "off"
        or settings.speech_cleanup_preflight_rollout_percent <= 0
    ):
        return None
    resolution = resolve_item_narration(item, detector_policy=current_detector_policy())
    if resolution.source is None or not preflight_enabled_for_source(
        resolution.source.source_policy_fingerprint,
        mode=settings.speech_cleanup_preflight_mode,
        rollout_percent=settings.speech_cleanup_preflight_rollout_percent,
    ):
        return None
    intent = ensure_current_analysis_sync(db, item, resolution)
    return intent.analysis_id if intent.created else None


def ensure_current_analysis_sync(
    db: Session,
    item: PlanItem,
    resolution: ActiveNarrationResolution,
) -> SpeechCleanupSchedulingIntent | None:
    source = resolution.source
    if source is None:
        return None
    current_time = datetime.now(UTC)
    current = current_analysis_sync(db, item.id, for_update=True)
    if current is not None:
        if (
            current.source_policy_fingerprint == source.source_policy_fingerprint
            and current.engine_version == SPEECH_CLEANUP_ENGINE_VERSION
        ):
            return SpeechCleanupSchedulingIntent(current.id, False)
        current.superseded_at = current_time
        _clear_item_cleanup_choice(item)
        # See the async twin: the current-key release must reach PostgreSQL
        # before a historical row is promoted to current.
        db.flush()
    historical = _exact_historical_analysis_sync(
        db,
        item.id,
        source.source_policy_fingerprint,
    )
    if historical is not None:
        intent = _reactivate_historical_analysis(
            historical,
            item,
            source,
            now=current_time,
        )
        db.flush()
        return intent
    analysis = _new_analysis(item.id, source, now=current_time)
    _clear_item_cleanup_choice(item)
    db.add(analysis)
    db.flush()
    return SpeechCleanupSchedulingIntent(analysis.id, True)


def claim_analysis(
    db: Session,
    analysis_id: str | uuid.UUID,
    *,
    now: datetime | None = None,
) -> ClaimedSpeechCleanupAnalysis | None:
    """Claim queued/expired work with a fresh fenced attempt token."""

    current_time = now or datetime.now(UTC)
    try:
        identifier = uuid.UUID(str(analysis_id))
    except (TypeError, ValueError):
        return None
    row = db.get(SpeechCleanupAnalysis, identifier, with_for_update=True)
    if row is None or row.superseded_at is not None or row.status not in {"queued", "running"}:
        return None
    if row.status == "running" and row.lease_expires_at and row.lease_expires_at > current_time:
        return None
    if row.attempt_count >= SPEECH_CLEANUP_MAX_ATTEMPTS:
        row.status = "failed"
        row.failure_code = "analysis_timeout"
        row.failure_retryable = True
        row.completed_at = current_time
        row.lease_expires_at = None
        row.attempt_token = None
        return None
    token = uuid.uuid4().hex
    row.status = "running"
    row.attempt_token = token
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.started_at = current_time
    row.lease_expires_at = current_time + timedelta(seconds=SPEECH_CLEANUP_LEASE_S)
    row.dispatched_at = row.dispatched_at or current_time
    row.next_dispatch_at = None
    db.flush()
    return ClaimedSpeechCleanupAnalysis(
        analysis_id=row.id,
        attempt_token=token,
        source_policy_fingerprint=row.source_policy_fingerprint,
    )


def _terminal_predicate(claim: ClaimedSpeechCleanupAnalysis):
    return and_(
        SpeechCleanupAnalysis.id == claim.analysis_id,
        SpeechCleanupAnalysis.status == "running",
        SpeechCleanupAnalysis.attempt_token == claim.attempt_token,
        SpeechCleanupAnalysis.source_policy_fingerprint == claim.source_policy_fingerprint,
        SpeechCleanupAnalysis.superseded_at.is_(None),
    )


def finalize_analysis_success(
    db: Session,
    claim: ClaimedSpeechCleanupAnalysis,
    *,
    analysis_payload: dict[str, Any],
    candidate_count: int,
    category_counts: dict[str, int],
    estimated_removed_ms: int,
    diagnostic_receipt: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(UTC)
    values = {
        "status": "ready" if candidate_count > 0 else "no_findings",
        "analysis_payload": analysis_payload,
        "candidate_count": max(0, int(candidate_count)),
        "category_counts": {str(key): max(0, int(value)) for key, value in category_counts.items()},
        "estimated_removed_ms": max(0, int(estimated_removed_ms)),
        "diagnostic_receipt": diagnostic_receipt or {},
        "failure_code": None,
        "failure_retryable": None,
        "completed_at": current_time,
        "lease_expires_at": None,
        "attempt_token": None,
    }
    result = db.execute(
        update(SpeechCleanupAnalysis).where(_terminal_predicate(claim)).values(**values)
    )
    return result.rowcount == 1


def finalize_analysis_failure(
    db: Session,
    claim: ClaimedSpeechCleanupAnalysis,
    error: SpeechCleanupOperationalError,
    *,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(UTC)
    result = db.execute(
        update(SpeechCleanupAnalysis)
        .where(_terminal_predicate(claim))
        .values(
            status="failed",
            failure_code=error.code,
            failure_retryable=error.retryable,
            completed_at=current_time,
            lease_expires_at=None,
            attempt_token=None,
        )
    )
    return result.rowcount == 1


def retry_failed_analysis(row: SpeechCleanupAnalysis, *, now: datetime | None = None) -> bool:
    if (
        row.superseded_at is not None
        or row.status != "failed"
        or not _FAILURE_RETRYABLE.get(str(row.failure_code), False)
    ):
        return False
    row.status = "queued"
    # A user-initiated retry starts a fresh bounded attempt budget. Without
    # this reset a row terminalized at SPEECH_CLEANUP_MAX_ATTEMPTS can never be
    # claimed again, despite exposing retryable=true to the client.
    row.attempt_count = 0
    row.failure_code = None
    row.failure_retryable = None
    row.completed_at = None
    row.attempt_token = None
    row.lease_expires_at = None
    row.dispatched_at = None
    row.next_dispatch_at = now or datetime.now(UTC)
    row.started_at = None
    row.analysis_payload = None
    row.candidate_count = None
    row.category_counts = None
    row.estimated_removed_ms = None
    row.diagnostic_receipt = None
    row.decision = None
    row.decision_at = None
    return True


def prepare_snapshot_mismatch_reanalysis(
    db: Session,
    job_id: str | uuid.UUID,
    *,
    now: datetime | None = None,
) -> SnapshotMismatchReanalysis:
    """Lock a required render graph and reset its exact analysis for dispatch.

    A render can discover that the immutable preflight snapshot no longer
    describes the bytes or narration spine it was asked to edit.  That is not
    an application retry: the accepted evidence itself must be invalidated and
    the current, generation-pinned source analyzed again.  The identity key is
    ``(item, fingerprint, engine)``, so an unchanged registered fingerprint is
    reset in place instead of attempting an impossible duplicate insert.

    This helper intentionally performs neither commit nor broker publication.
    The caller must persist its Job terminalization in the same transaction,
    then publish ``analysis_id`` only after that commit.  It pre-reads foreign
    keys without locks and then reacquires the mutation graph in the project
    order Plan -> Persona -> PlanItem -> Job; callers must not lock Job first.
    """

    try:
        identifier = uuid.UUID(str(job_id))
    except (TypeError, ValueError):
        return SnapshotMismatchReanalysis(None)

    job_ref = db.get(Job, identifier)
    item_id = getattr(job_ref, "content_plan_item_id", None) if job_ref is not None else None
    if item_id is None:
        return SnapshotMismatchReanalysis(None)
    item_ref = db.get(PlanItem, item_id)
    plan_id = getattr(item_ref, "content_plan_id", None) if item_ref is not None else None
    if plan_id is None:
        return SnapshotMismatchReanalysis(None)

    plan = db.get(ContentPlan, plan_id, with_for_update=True, populate_existing=True)
    if plan is None:
        return SnapshotMismatchReanalysis(None)
    persona = db.get(Persona, plan.persona_id, with_for_update=True, populate_existing=True)
    item = db.get(PlanItem, item_id, with_for_update=True, populate_existing=True)
    job = db.get(Job, identifier, with_for_update=True, populate_existing=True)
    result = SnapshotMismatchReanalysis(job)
    if (
        persona is None
        or item is None
        or job is None
        or getattr(plan, "ownership_quarantined_at", None) is not None
        or persona.id != plan.persona_id
        or persona.user_id != plan.user_id
        or item.content_plan_id != plan.id
        or item.current_job_id != job.id
        or job.user_id != plan.user_id
        or job.content_plan_item_id != item.id
        or job.mode != "content_plan"
        or int(job.content_plan_ownership_epoch or 0) != int(plan.ownership_epoch or 0)
    ):
        return result

    job_plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    if (
        job_plan.get("speech_cleanup_contract") != "required_v1"
        or job_plan.get("speech_cleanup_preflight_contract") != "snapshot_v1"
    ):
        return result
    private = job_plan.get("_speech_cleanup_internal")
    raw_snapshot = private.get("preflight_snapshot") if isinstance(private, dict) else None
    hydrated = None
    analysis_id: uuid.UUID | None = None
    try:
        from app.pipeline.speech_cleanup_apply import (  # noqa: PLC0415
            SpeechCleanupSnapshotError,
            hydrate_speech_cleanup_snapshot,
        )

        hydrated = hydrate_speech_cleanup_snapshot(raw_snapshot)
        analysis_id = uuid.UUID(hydrated.analysis_id)
    except (SpeechCleanupSnapshotError, TypeError, ValueError):
        # The snapshot_v1 marker is server-authored, but an absent/corrupt
        # private payload cannot be trusted for identity. Fall back only to the
        # exact current analysis selected through the authoritative
        # item.current_job graph below; never read an ID from malformed JSON.
        hydrated = None

    row = current_analysis_sync(db, item.id, for_update=True)
    if (
        row is None
        or row.superseded_at is not None
        or (analysis_id is not None and row.id != analysis_id)
    ):
        # A source mutation may already have installed a newer current row.
        # Never let the old render reset or republish that newer analysis.
        return result

    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        resolve_item_narration,
    )

    resolution = resolve_item_narration(item, detector_policy=current_detector_policy())
    source = resolution.source
    source_matches_snapshot = bool(
        source is not None
        and hydrated is not None
        and hydrated.source_kind == source.source_kind
        and hydrated.media_identity == source.media_id
        and hydrated.storage_path == source.storage_path
        and hydrated.generation == source.generation
        and hydrated.source_policy_fingerprint == source.source_policy_fingerprint
    )
    source_matches_current_row = bool(source is not None and _source_snapshot_matches(row, source))
    if not source_matches_snapshot and not (hydrated is None and source_matches_current_row):
        return result

    variants = job_plan.get("variants")
    stored_failure = job_plan.get("speech_cleanup_failure_reason")
    if not isinstance(stored_failure, str) or not stored_failure:
        stored_failure = next(
            (
                value.get("speech_cleanup_failure_reason")
                for value in (variants if isinstance(variants, list) else [])
                if isinstance(value, dict) and value.get("speech_cleanup_failure_reason")
            ),
            None,
        )
    already_terminalized = bool(
        job.status in {"processing_failed", "variants_failed", "variants_ready_partial"}
        and job.failure_reason == "speech_cleanup_failed"
        and stored_failure == "snapshot_mismatch"
    )
    if already_terminalized:
        # A redelivery after the first atomic terminal commit may safely
        # republish queued work, but must not erase a running or newly-settled
        # replacement analysis and start the loop over again.
        if row.status == "queued" and row.decision is None:
            return SnapshotMismatchReanalysis(job, row.id)
        return result

    if row.status != "ready" or row.decision != "clean":
        return result
    if hydrated is not None:
        try:
            if analysis_snapshot(row) != raw_snapshot:
                return result
        except ValueError:
            return result

    current_time = now or datetime.now(UTC)
    _reset_analysis_for_dispatch(row, source, now=current_time)
    row.superseded_at = None
    row.decision = None
    row.decision_at = None
    _clear_item_cleanup_choice(item)
    return SnapshotMismatchReanalysis(job, row.id)


def claim_reconciliation_batch(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = SPEECH_CLEANUP_RECONCILE_BATCH,
) -> list[uuid.UUID]:
    """Claim a bounded SKIP LOCKED page for post-commit publication/recovery."""

    current_time = now or datetime.now(UTC)
    bounded = min(max(1, int(limit)), SPEECH_CLEANUP_RECONCILE_BATCH)
    rows = (
        db.execute(
            select(SpeechCleanupAnalysis)
            .where(
                SpeechCleanupAnalysis.superseded_at.is_(None),
                or_(
                    and_(
                        SpeechCleanupAnalysis.status == "queued",
                        SpeechCleanupAnalysis.dispatched_at.is_(None),
                        SpeechCleanupAnalysis.next_dispatch_at <= current_time,
                    ),
                    and_(
                        SpeechCleanupAnalysis.status == "running",
                        SpeechCleanupAnalysis.lease_expires_at <= current_time,
                    ),
                ),
            )
            .order_by(SpeechCleanupAnalysis.next_dispatch_at, SpeechCleanupAnalysis.created_at)
            .with_for_update(skip_locked=True)
            .limit(bounded)
        )
        .scalars()
        .all()
    )
    identifiers: list[uuid.UUID] = []
    for row in rows:
        if row.status == "running":
            row.status = "queued"
            row.attempt_token = None
            row.lease_expires_at = None
        # This is a renewable dispatch lease, not a terminal "published"
        # marker. If the process dies after this transaction commits and before
        # apply_async, the row becomes due again instead of disappearing from
        # the outbox forever. The worker claim stamps dispatched_at.
        row.dispatched_at = None
        row.next_dispatch_at = current_time + timedelta(seconds=30)
        identifiers.append(row.id)
    db.flush()
    return identifiers


def mark_dispatch_failed(
    db: Session,
    analysis_id: uuid.UUID,
    *,
    retry_after_s: int = 30,
) -> None:
    row = db.get(SpeechCleanupAnalysis, analysis_id, with_for_update=True)
    if row is None or row.superseded_at is not None or row.status != "queued":
        return
    row.dispatched_at = None
    row.next_dispatch_at = datetime.now(UTC) + timedelta(seconds=max(1, retry_after_s))


def analysis_snapshot(row: SpeechCleanupAnalysis) -> dict[str, Any]:
    """Build the exact private Job snapshot; never return this from an API."""

    if row.status not in {"ready", "no_findings"} or not isinstance(row.analysis_payload, dict):
        raise ValueError("speech cleanup analysis is not snapshot-ready")
    return {
        "schema_version": 1,
        "analysis_id": str(row.id),
        "engine_version": row.engine_version,
        "detector_version": row.detector_version,
        "source": {
            "kind": row.source_kind,
            "media_identity": row.source_media_identity,
            "storage_path": row.source_storage_path,
            "generation": row.source_generation,
            "window_start_s": row.window_start_s,
            "window_end_s": row.window_end_s,
            "source_policy_fingerprint": row.source_policy_fingerprint,
        },
        "analysis": dict(row.analysis_payload),
    }


def _public_error(code: object) -> dict[str, Any] | None:
    """Project only stable error taxonomy; provider/debug text is never public."""

    if not isinstance(code, str) or not code:
        return None
    if code not in _FAILURE_RETRYABLE:
        return {"code": "internal_error", "retryable": True}
    # Retryability is taxonomy-owned rather than trusting mutable JSON/columns.
    return {"code": code, "retryable": _FAILURE_RETRYABLE[code]}


def _public_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _outcome_matches_analysis(job: Job, row: SpeechCleanupAnalysis | None) -> bool:
    """Fence an active-Job receipt to the analysis currently shown beside it."""

    if (
        row is None
        or getattr(row, "superseded_at", None) is not None
        or not isinstance(job.assembly_plan, dict)
    ):
        return False
    private = job.assembly_plan.get("_speech_cleanup_internal")
    if not isinstance(private, dict):
        return False
    raw_snapshot = private.get("preflight_snapshot")
    if isinstance(raw_snapshot, dict):
        # A snapshot-mismatch terminalizer resets the same unique analysis row
        # in place.  Source identity alone would keep projecting the old failed
        # render receipt while the replacement analysis is queued, masking the
        # new check behind an application-failure card.
        if row.status not in {"ready", "no_findings"}:
            return False
        try:
            if analysis_snapshot(row) != raw_snapshot:
                return False
        except ValueError:
            return False
        outcome = job.assembly_plan.get("speech_cleanup_outcome")
        outcome_status = outcome.get("status") if isinstance(outcome, dict) else None
        contract = job.assembly_plan.get("speech_cleanup_contract")
        if contract == "required_v1" and row.decision != "clean":
            return False
        if outcome_status == "declined" and row.decision != "keep_original":
            return False
        source = raw_snapshot.get("source")
        return bool(
            isinstance(source, dict)
            and str(raw_snapshot.get("analysis_id") or "") == str(row.id)
            and source.get("kind") == row.source_kind
            and source.get("media_identity") == row.source_media_identity
            and source.get("storage_path") == row.source_storage_path
            and source.get("generation") == row.source_generation
            and source.get("source_policy_fingerprint") == row.source_policy_fingerprint
        )
    # An explicit unchecked bypass may occur before analysis completes, so it
    # has no valid immutable CutPlan snapshot. Its mint path stores this UUID
    # only in the recursively redacted private namespace.
    return str(private.get("outcome_analysis_id") or "") == str(row.id)


def _public_outcome(
    job: Job | None,
    row: SpeechCleanupAnalysis | None,
) -> dict[str, Any] | None:
    if job is None or not isinstance(job.assembly_plan, dict):
        return None
    value = job.assembly_plan.get("speech_cleanup_outcome")
    if not isinstance(value, dict):
        return None
    # The caller supplies the fully owner/item/epoch-fenced active Job, but a
    # receipt from an older render can survive in mutable JSON after a retry.
    # Only the receipt for this exact Job generation is creator-visible.
    if str(value.get("job_id") or "") != str(job.id):
        return None
    active_generation = str(job.assembly_plan.get("creator_generation_id") or "")
    if not active_generation or str(value.get("render_generation_id") or "") != active_generation:
        return None
    # Source replacement can create a new current analysis before the thread's
    # old active Job is replaced. Never combine that new decision card with an
    # older Job's failure/success receipt: recovery payloads would otherwise
    # bind the new analysis UUID to the old immutable snapshot and 409 forever.
    if not _outcome_matches_analysis(job, row):
        return None
    allowed_statuses = {
        "applied",
        "checked_no_change",
        "declined",
        "bypassed_unchecked",
        "failed",
    }
    if value.get("status") not in allowed_statuses:
        return None
    projected = {
        "job_id": str(job.id),
        "render_generation_id": active_generation,
        "status": value["status"],
        "removal_count": _public_count(value.get("removal_count")),
        "removed_ms": _public_count(value.get("removed_ms")),
    }
    error = value.get("error")
    if isinstance(error, dict):
        projected["error"] = _public_error(error.get("code"))
    return projected


def public_projection(
    row: SpeechCleanupAnalysis | None,
    *,
    applicable: bool,
    unavailable_reason: str | None,
    video_present: bool,
    active_job: Job | None = None,
) -> dict[str, Any] | None:
    """Return the content-minimized active-thread detail projection."""

    if not applicable and row is None:
        return None
    analysis = None
    decision = None
    requires_choice = False
    if row is not None:
        error = _public_error(row.failure_code)
        raw_counts = row.category_counts if isinstance(row.category_counts, dict) else {}
        category_counts = {
            key: _public_count(raw_counts.get(key)) for key in ("filler_sounds", "long_pauses")
        }
        analysis = {
            "id": str(row.id),
            "status": row.status,
            "detector_version": row.detector_version,
            "has_findings": row.status == "ready" and int(row.candidate_count or 0) > 0,
            "candidate_count": row.candidate_count,
            "category_counts": category_counts,
            "estimated_removed_ms": row.estimated_removed_ms,
            "error": error,
        }
        decision = row.decision
        requires_choice = bool(
            video_present
            and row.status == "ready"
            and int(row.candidate_count or 0) > 0
            and row.decision is None
        )
    return {
        "applicable": applicable,
        "unavailable_reason": unavailable_reason,
        "analysis": analysis,
        "decision": decision,
        "requires_choice": requires_choice,
        "render_blocker": None if video_present else "video_required",
        "outcome": _public_outcome(active_job, row),
    }
