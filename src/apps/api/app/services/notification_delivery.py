"""Durable notification admission and outcomes. Transport is always injected."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import SecretStr
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import VerifiedMobilePrincipal
from app.database import sync_session
from app.models import MobileSession, NativeActivity, NotificationDelivery, NotificationInstallation
from app.services.apns import (
    Accepted,
    ActivityNotification,
    APNsTarget,
    Environment,
    Rejected,
    Result,
    TransportUncertain,
)
from app.services.auth_locks import mobile_session_family_lock_statement
from app.services.native_activity import activity_query, is_current
from app.services.token_crypto import TokenCryptoError, decrypt_token, encrypt_token


@dataclass(frozen=True)
class NotificationPolicy:
    enabled: bool = False
    environment: Environment = "sandbox"
    topic: str | None = None
    allowed_owner_ids: frozenset[uuid.UUID] | None = None
    activity_lifetime: timedelta = timedelta(minutes=10)
    registration_lifetime: timedelta = timedelta(days=30)
    lease_lifetime: timedelta = timedelta(seconds=30)
    retry_delay: timedelta = timedelta(seconds=30)
    minimum_interval: timedelta = timedelta(seconds=60)
    max_per_hour: int = 3
    max_attempts: int = 3
    batch_size: int = 50

    def __post_init__(self) -> None:
        if (
            self.environment not in {"sandbox", "production"}
            or (
                self.topic is not None
                and not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", self.topic)
            )
            or (self.topic is not None and len(self.topic) > 255)
            or not 1 <= self.max_attempts <= 5
            or not 1 <= self.batch_size <= 100
            or not 1 <= self.max_per_hour <= 10
            or not timedelta(seconds=1) <= self.minimum_interval <= timedelta(hours=1)
            or not timedelta(0) < self.activity_lifetime <= timedelta(hours=1)
            or not timedelta(0) < self.registration_lifetime <= timedelta(days=30)
            or not timedelta(seconds=20) <= self.lease_lifetime <= timedelta(minutes=5)
            or not timedelta(seconds=1) <= self.retry_delay <= timedelta(minutes=5)
        ):
            raise ValueError("Invalid notification policy")

    @property
    def available(self) -> bool:
        return self.enabled and self.topic is not None

    def allows(self, owner_id: uuid.UUID) -> bool:
        return self.available and (
            self.allowed_owner_ids is None or owner_id in self.allowed_owner_ids
        )


def notification_policy() -> NotificationPolicy:
    from app.services.notification_runtime import notification_configuration

    configuration = notification_configuration()
    if configuration is None:
        return NotificationPolicy()
    return NotificationPolicy(
        enabled=True,
        environment=configuration.environment,
        topic=configuration.topic,
        allowed_owner_ids=configuration.allowed_owner_ids,
    )


class RegistrationConflict(ValueError):
    pass


class NotificationSender(Protocol):
    async def send(self, target: APNsTarget, notification: ActivityNotification) -> Result: ...


def _live_family(owner_id, family_id, now: datetime):
    return exists(
        select(MobileSession.id).where(
            MobileSession.user_id == owner_id,
            MobileSession.family_id == family_id,
            MobileSession.revoked_at.is_(None),
            MobileSession.used_at.is_(None),
            MobileSession.expires_at > now,
        )
    )


def register_installation(
    db: Session,
    principal: VerifiedMobilePrincipal,
    installation_id: uuid.UUID,
    token: SecretStr,
    enabled: bool,
    policy: NotificationPolicy,
    now: datetime,
) -> NotificationInstallation:
    if not policy.allows(principal.user_id):
        raise ValueError("Notifications unavailable")
    db.execute(mobile_session_family_lock_statement(principal.family_id))
    member = db.scalar(
        select(MobileSession)
        .where(
            MobileSession.user_id == principal.user_id,
            MobileSession.family_id == principal.family_id,
            MobileSession.revoked_at.is_(None),
            MobileSession.used_at.is_(None),
            MobileSession.expires_at > now,
        )
        .order_by(MobileSession.id)
        .limit(1)
        .with_for_update()
    )
    if member is None:
        raise RegistrationConflict("Registration unavailable")
    raw = token.get_secret_value().lower()
    fingerprint = hashlib.sha256(bytes.fromhex(raw)).hexdigest()
    row = db.scalar(
        select(NotificationInstallation)
        .where(NotificationInstallation.id == installation_id)
        .with_for_update()
    )
    if row is not None and row.owner_id != principal.user_id:
        raise RegistrationConflict("Registration unavailable")
    conflict = db.scalar(
        select(NotificationInstallation.id).where(
            NotificationInstallation.environment == policy.environment,
            NotificationInstallation.topic == policy.topic,
            NotificationInstallation.token_fingerprint == fingerprint,
            NotificationInstallation.revoked_at.is_(None),
            NotificationInstallation.id != installation_id,
        )
    )
    if conflict is not None:
        raise RegistrationConflict("Registration unavailable")
    changed = row is None or (
        row.family_id != principal.family_id
        or row.environment != policy.environment
        or row.topic != policy.topic
        or row.token_fingerprint != fingerprint
        or row.enabled != enabled
        or row.revoked_at is not None
    )
    if row is not None and changed and now <= row.last_seen_at:
        raise RegistrationConflict("Registration superseded")
    if row is None:
        row = NotificationInstallation(
            id=installation_id, owner_id=principal.user_id, registration_version=0
        )
        db.add(row)
    if changed:
        row.encrypted_token = encrypt_token(raw)
        row.family_id = principal.family_id
        row.environment = policy.environment
        row.topic = policy.topic
        row.token_fingerprint = fingerprint
        row.enabled = enabled
        row.revoked_at = None
        row.registration_version += 1
        row.registered_at = now
    row.last_seen_at = max(row.last_seen_at, now) if row.last_seen_at is not None else now
    db.flush()
    return row


def revoke_installation(
    db: Session, principal: VerifiedMobilePrincipal, installation_id: uuid.UUID, now: datetime
) -> None:
    row = db.scalar(
        select(NotificationInstallation)
        .where(NotificationInstallation.id == installation_id)
        .with_for_update()
    )
    if row is None:
        return
    if row.owner_id != principal.user_id:
        raise RegistrationConflict("Registration unavailable")
    if row.revoked_at is None:
        row.revoked_at = now
        row.enabled = False
        row.encrypted_token = None
        row.registration_version += 1
    db.flush()


def revoke_family_installations_statement(owner_id: uuid.UUID, family_id: uuid.UUID, now: datetime):
    return (
        update(NotificationInstallation)
        .where(
            NotificationInstallation.owner_id == owner_id,
            NotificationInstallation.family_id == family_id,
            NotificationInstallation.revoked_at.is_(None),
        )
        .values(
            revoked_at=now,
            enabled=False,
            encrypted_token=None,
            registration_version=NotificationInstallation.registration_version + 1,
        )
    )


def discover_deliveries(db: Session, policy: NotificationPolicy, now: datetime) -> int:
    if not policy.available:
        return 0
    rows = db.execute(
        select(
            NativeActivity.id,
            NotificationInstallation.id,
            NotificationInstallation.registration_version,
        )
        .join(
            NotificationInstallation, NotificationInstallation.owner_id == NativeActivity.owner_id
        )
        .outerjoin(
            NotificationDelivery,
            and_(
                NotificationDelivery.activity_id == NativeActivity.id,
                NotificationDelivery.installation_id == NotificationInstallation.id,
            ),
        )
        .where(
            NotificationDelivery.id.is_(None),
            NotificationInstallation.owner_id.in_(policy.allowed_owner_ids)
            if policy.allowed_owner_ids is not None
            else True,
            NativeActivity.read_at.is_(None),
            NativeActivity.created_at >= now - policy.activity_lifetime,
            NativeActivity.created_at <= now,
            NativeActivity.created_at >= NotificationInstallation.registered_at,
            NotificationInstallation.last_seen_at >= now - policy.registration_lifetime,
            NotificationInstallation.enabled.is_(True),
            NotificationInstallation.revoked_at.is_(None),
            NotificationInstallation.environment == policy.environment,
            NotificationInstallation.topic == policy.topic,
            _live_family(
                NotificationInstallation.owner_id, NotificationInstallation.family_id, now
            ),
        )
        .order_by(NativeActivity.created_at, NativeActivity.id, NotificationInstallation.id)
        .limit(policy.batch_size)
    ).all()
    inserted = 0
    for activity_id, installation_id, version in rows:
        result = db.execute(
            insert(NotificationDelivery)
            .values(
                id=uuid.uuid4(),
                activity_id=activity_id,
                installation_id=installation_id,
                registration_version=version,
                status="pending",
                attempts=0,
                available_at=now,
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_notification_delivery_pair")
        )
        inserted += result.rowcount
    return inserted


def due_delivery_ids(db: Session, policy: NotificationPolicy, now: datetime) -> list[uuid.UUID]:
    if not policy.available:
        return []
    return list(
        db.scalars(
            select(NotificationDelivery.id)
            .where(
                or_(
                    and_(
                        NotificationDelivery.status == "pending",
                        NotificationDelivery.available_at <= now,
                    ),
                    and_(
                        NotificationDelivery.status == "leased",
                        NotificationDelivery.lease_until <= now,
                    ),
                )
            )
            .order_by(NotificationDelivery.available_at, NotificationDelivery.id)
            .limit(policy.batch_size)
        )
    )


def _finish(row: NotificationDelivery, state: str, code: str, now: datetime) -> None:
    row.status = state
    row.last_result_code = code
    row.lease_id = None
    row.lease_until = None
    row.completed_at = now


@dataclass(frozen=True)
class DeliveryAdmission:
    delivery_id: uuid.UUID
    lease_id: uuid.UUID
    target: APNsTarget
    notification: ActivityNotification
    registration_seen_at: datetime


def _admit(
    db: Session, delivery_id: uuid.UUID, policy: NotificationPolicy, now: datetime
) -> DeliveryAdmission | None:
    row = db.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.id == delivery_id)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    if row.status == "leased" and row.lease_until <= now:
        _finish(row, "uncertain", "lease_expired", now)
        return None
    if row.status != "pending" or row.available_at > now:
        return None
    installation = db.scalar(
        select(NotificationInstallation)
        .where(NotificationInstallation.id == row.installation_id)
        .with_for_update()
    )
    if (
        installation is None
        or not policy.allows(installation.owner_id)
        or installation.revoked_at is not None
        or not installation.enabled
        or installation.registration_version != row.registration_version
        or installation.environment != policy.environment
        or installation.topic != policy.topic
        or installation.last_seen_at < now - policy.registration_lifetime
        or not db.scalar(select(_live_family(installation.owner_id, installation.family_id, now)))
    ):
        _finish(row, "suppressed", "registration_ineligible", now)
        return None
    current = db.execute(
        activity_query(installation.owner_id).where(NativeActivity.id == row.activity_id)
    ).first()
    if (
        current is None
        or current[0].read_at is not None
        or current[0].created_at < now - policy.activity_lifetime
        or current[0].created_at < installation.registered_at
        or current[0].created_at > now
        or not is_current(*current)
    ):
        _finish(row, "suppressed", "activity_ineligible", now)
        return None
    if row.attempts >= policy.max_attempts:
        _finish(row, "exhausted", "attempt_limit", now)
        return None
    reserved_at = func.coalesce(NotificationDelivery.lease_until, NotificationDelivery.completed_at)
    reservations = list(
        db.scalars(
            select(reserved_at)
            .where(
                NotificationDelivery.installation_id == installation.id,
                NotificationDelivery.status.in_(["leased", "accepted", "uncertain"]),
                reserved_at > now - timedelta(hours=1),
            )
            .order_by(reserved_at.desc())
            .limit(policy.max_per_hour)
        )
    )
    if reservations and (
        len(reservations) >= policy.max_per_hour or reservations[0] > now - policy.minimum_interval
    ):
        row.available_at = now + policy.minimum_interval
        row.last_result_code = "installation_rate_limited"
        return None
    try:
        target = APNsTarget(
            installation.environment,
            installation.topic,
            SecretStr(decrypt_token(installation.encrypted_token)),
        )
    except (TokenCryptoError, ValueError):
        _finish(row, "rejected", "token_unavailable", now)
        return None
    row.attempts += 1
    row.status = "leased"
    row.lease_id = uuid.uuid4()
    row.lease_until = now + policy.lease_lifetime
    return DeliveryAdmission(
        row.id,
        row.lease_id,
        target,
        ActivityNotification(row.activity_id, row.id),
        installation.last_seen_at,
    )


def _complete(
    db: Session,
    admission: DeliveryAdmission,
    result: Result,
    policy: NotificationPolicy,
    now: datetime,
) -> str:
    row = db.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.id == admission.delivery_id)
        .with_for_update()
    )
    if row is None or row.status != "leased" or row.lease_id != admission.lease_id:
        return "superseded"
    if isinstance(result, Accepted):
        _finish(row, "accepted", "accepted", now)
    elif isinstance(result, TransportUncertain):
        _finish(row, "uncertain", result.category, now)
    elif isinstance(result, Rejected):
        if result.category == "retryable":
            activity = db.get(NativeActivity, row.activity_id)
            available = now + policy.retry_delay * row.attempts
            if (
                row.attempts < policy.max_attempts
                and activity is not None
                and available < activity.created_at + policy.activity_lifetime
            ):
                row.status = "pending"
                row.last_result_code = result.code
                row.lease_id = None
                row.lease_until = None
                row.available_at = available
            else:
                _finish(row, "exhausted", result.code, now)
        else:
            _finish(row, "rejected", result.code, now)
            if result.category == "token":
                installation = db.scalar(
                    select(NotificationInstallation)
                    .where(NotificationInstallation.id == row.installation_id)
                    .with_for_update()
                )
                if (
                    installation is not None
                    and installation.registration_version == row.registration_version
                    and installation.revoked_at is None
                    and installation.last_seen_at == admission.registration_seen_at
                    and (
                        result.invalidated_at_ms is None
                        or result.invalidated_at_ms
                        >= int(installation.last_seen_at.timestamp() * 1000)
                    )
                ):
                    installation.revoked_at = now
                    installation.enabled = False
                    installation.encrypted_token = None
                    installation.registration_version += 1
    else:
        _finish(row, "uncertain", "invalid_result", now)
    return row.status


async def dispatch_delivery(
    delivery_id: uuid.UUID,
    *,
    transport: NotificationSender,
    policy: NotificationPolicy = NotificationPolicy(),
    session_factory: Callable[[], Session] = sync_session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    if not policy.available:
        return "disabled"
    with session_factory() as db:
        admission = _admit(db, delivery_id, policy, clock())
        db.commit()
    if admission is None:
        return "skipped"
    try:
        async with asyncio.timeout(18):
            result = await transport.send(admission.target, admission.notification)
    except Exception:
        result = TransportUncertain("protocol")
    with session_factory() as db:
        state = _complete(db, admission, result, policy, clock())
        db.commit()
    return state
