"""Single fail-closed policy for edit-feedback training eligibility.

This module is intentionally small and dependency-light so capture, admin
read/playback, and export code can all call the same policy.  Eligibility is
time-sensitive: the latest consent event (or active internal grant) is checked
on every boundary.  A later re-grant does not revive an artifact bound to an
older consent event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import (
    EDIT_ARTIFACT_KINDS,
    EditArtifact,
    InternalAccountGrant,
    TrainingArtifactRetentionEvent,
    TrainingConsentEvent,
)

TRAINING_PURPOSE = "edit_feedback_training"
_INTERNAL_ACCOUNT_STATUS = "active"
_GRANT_ACTION = "grant"


@dataclass(frozen=True)
class TrainingEligibilityDecision:
    eligible: bool
    creator_id: uuid.UUID
    basis: str | None = None
    consent_event_id: uuid.UUID | None = None
    internal_grant_id: uuid.UUID | None = None
    reason: str | None = None


# Stable public name for capture/admin/export integrations.
TrainingEligibility = TrainingEligibilityDecision


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    return current if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _deny(creator_id: uuid.UUID, reason: str) -> TrainingEligibilityDecision:
    return TrainingEligibilityDecision(eligible=False, creator_id=creator_id, reason=reason)


def _decision(
    creator_id: uuid.UUID,
    *,
    internal_grant: InternalAccountGrant | None,
    consent_event: TrainingConsentEvent | None,
    now: datetime,
) -> TrainingEligibilityDecision:
    # Explicit internal grants take precedence, but only while active.  The
    # synthetic user is not special-cased here: it is eligible only if an
    # operator deliberately grants it, matching the trust-boundary contract.
    if (
        internal_grant is not None
        and internal_grant.status == _INTERNAL_ACCOUNT_STATUS
        and internal_grant.effective_at <= now
    ):
        return TrainingEligibilityDecision(
            eligible=True,
            creator_id=creator_id,
            basis="internal_grant",
            internal_grant_id=internal_grant.id,
        )

    if (
        consent_event is not None
        and consent_event.action == _GRANT_ACTION
        and consent_event.effective_at <= now
    ):
        return TrainingEligibilityDecision(
            eligible=True,
            creator_id=creator_id,
            basis="training_consent",
            consent_event_id=consent_event.id,
        )

    return _deny(creator_id, "no_active_training_grant")


def _latest_sync(db: Session, creator_id: uuid.UUID, now: datetime):
    grant = db.execute(
        select(InternalAccountGrant)
        .where(
            InternalAccountGrant.creator_id == creator_id,
            InternalAccountGrant.status == _INTERNAL_ACCOUNT_STATUS,
            InternalAccountGrant.effective_at <= now,
        )
        .order_by(InternalAccountGrant.effective_at.desc(), InternalAccountGrant.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    consent = db.execute(
        select(TrainingConsentEvent)
        .where(
            TrainingConsentEvent.creator_id == creator_id,
            TrainingConsentEvent.purpose == TRAINING_PURPOSE,
            TrainingConsentEvent.effective_at <= now,
        )
        .order_by(
            TrainingConsentEvent.effective_at.desc(),
            TrainingConsentEvent.created_at.desc(),
            TrainingConsentEvent.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()
    return grant, consent


async def _latest_async(db: AsyncSession, creator_id: uuid.UUID, now: datetime):
    grant = (
        await db.execute(
            select(InternalAccountGrant)
            .where(
                InternalAccountGrant.creator_id == creator_id,
                InternalAccountGrant.status == _INTERNAL_ACCOUNT_STATUS,
                InternalAccountGrant.effective_at <= now,
            )
            .order_by(
                InternalAccountGrant.effective_at.desc(),
                InternalAccountGrant.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    consent = (
        await db.execute(
            select(TrainingConsentEvent)
            .where(
                TrainingConsentEvent.creator_id == creator_id,
                TrainingConsentEvent.purpose == TRAINING_PURPOSE,
                TrainingConsentEvent.effective_at <= now,
            )
            .order_by(
                TrainingConsentEvent.effective_at.desc(),
                TrainingConsentEvent.created_at.desc(),
                TrainingConsentEvent.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return grant, consent


def evaluate_training_eligibility(
    db: Session, creator_id: uuid.UUID, *, now: datetime | None = None
) -> TrainingEligibilityDecision:
    """Evaluate current creator eligibility using a synchronous session."""
    if not isinstance(creator_id, uuid.UUID):
        return _deny(creator_id, "invalid_creator_id")  # type: ignore[arg-type]
    current = _now(now)
    grant, consent = _latest_sync(db, creator_id, current)
    return _decision(creator_id, internal_grant=grant, consent_event=consent, now=current)


def resolve_training_eligibility(
    db: Session, creator_id: uuid.UUID, *, at: datetime | None = None
) -> TrainingEligibility:
    """Resolve current eligibility using the integration-facing API name."""
    return evaluate_training_eligibility(db, creator_id, now=at)


async def evaluate_training_eligibility_async(
    db: AsyncSession, creator_id: uuid.UUID, *, now: datetime | None = None
) -> TrainingEligibilityDecision:
    """Evaluate current creator eligibility using an async session."""
    if not isinstance(creator_id, uuid.UUID):
        return _deny(creator_id, "invalid_creator_id")  # type: ignore[arg-type]
    current = _now(now)
    grant, consent = await _latest_async(db, creator_id, current)
    return _decision(creator_id, internal_grant=grant, consent_event=consent, now=current)


def evaluate_artifact_eligibility(
    db: Session,
    artifact: EditArtifact,
    *,
    now: datetime | None = None,
) -> TrainingEligibilityDecision:
    """Re-check one artifact's current eligibility at a read/export boundary."""
    if artifact.artifact_kind not in EDIT_ARTIFACT_KINDS:
        return _deny(artifact.creator_id, "artifact_kind_not_retained")
    if (
        not artifact.storage_path
        or not artifact.storage_generation
        or not artifact.storage_content_hash
    ):
        return _deny(artifact.creator_id, "artifact_storage_identity_incomplete")
    if artifact.storage_path.lower().startswith(("http://", "https://", "gs://", "s3://")):
        return _deny(artifact.creator_id, "artifact_storage_path_not_an_object_key")

    decision = evaluate_training_eligibility(db, artifact.creator_id, now=now)
    if not decision.eligible:
        return decision
    if decision.basis == "internal_grant":
        if artifact.internal_grant_id != decision.internal_grant_id:
            return _deny(artifact.creator_id, "artifact_grant_mismatch")
    elif artifact.consent_event_id != decision.consent_event_id:
        return _deny(artifact.creator_id, "artifact_consent_mismatch")
    return decision


def artifact_is_eligible(
    db: Session,
    artifact: EditArtifact,
    *,
    at: datetime | None = None,
) -> bool:
    """Return only the safe boolean used by admin list/export boundaries."""
    return evaluate_artifact_eligibility(db, artifact, now=at).eligible


def retained_copy_is_eligible(
    db: Session,
    artifact: EditArtifact,
    *,
    at: datetime | None = None,
) -> bool:
    """Require the latest successful generation-pinned training copy.

    Product source identity lives on ``EditArtifact``. Playback/export callers
    must additionally require this separate retained copy, whose path is owned
    by ``TrainingArtifactRetentionEvent`` and is covered by account deletion.
    """
    if not artifact_is_eligible(db, artifact, at=at):
        return False
    expected_prefix = f"users/{artifact.creator_id}/edit-feedback/{artifact.id}/"
    latest = db.execute(
        select(TrainingArtifactRetentionEvent)
        .where(TrainingArtifactRetentionEvent.artifact_id == artifact.id)
        .order_by(
            TrainingArtifactRetentionEvent.created_at.desc(),
            TrainingArtifactRetentionEvent.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()
    return bool(
        latest
        and latest.event_type in {"copy", "ready"}
        and latest.status == "succeeded"
        and latest.storage_path.startswith(expected_prefix)
        and latest.storage_generation
        and latest.content_hash
    )


def eligible_artifact_ids(
    db: Session,
    artifact_ids: set[uuid.UUID] | list[uuid.UUID] | tuple[uuid.UUID, ...],
    *,
    at: datetime | None = None,
) -> set[uuid.UUID]:
    """Batch-check artifacts and return only currently eligible IDs."""
    ids = set(artifact_ids)
    if not ids:
        return set()
    rows = db.execute(select(EditArtifact).where(EditArtifact.id.in_(ids))).scalars().all()
    return {row.id for row in rows if artifact_is_eligible(db, row, at=at)}


def eligible_artifact_statement(*, creator_id: uuid.UUID | None = None):
    """Return a conservative SQL predicate for candidate artifact reads.

    Callers must still pass each row through :func:`artifact_is_eligible` before
    playback/export; the predicate is only a bounded pre-filter and intentionally
    does not attempt to encode latest-event ordering in SQL.
    """
    conditions = [
        EditArtifact.artifact_kind.in_(EDIT_ARTIFACT_KINDS),
        EditArtifact.storage_path.is_not(None),
        EditArtifact.storage_generation.is_not(None),
        EditArtifact.storage_content_hash.is_not(None),
        or_(
            EditArtifact.eligibility_basis == "internal_grant",
            EditArtifact.eligibility_basis == "training_consent",
        ),
    ]
    if creator_id is not None:
        conditions.append(EditArtifact.creator_id == creator_id)
    return select(EditArtifact).where(and_(*conditions))


def require_training_eligibility(
    db: Session, creator_id: uuid.UUID, *, now: datetime | None = None
) -> TrainingEligibilityDecision:
    """Raise a stable error for capture paths that must fail closed."""
    decision = evaluate_training_eligibility(db, creator_id, now=now)
    if not decision.eligible:
        raise PermissionError(decision.reason or "training_ineligible")
    return decision
