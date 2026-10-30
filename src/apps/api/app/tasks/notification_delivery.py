"""Notification task adapters. Configuration disables the entire path by default."""

import asyncio
import uuid
from datetime import UTC, datetime

from app.database import sync_session
from app.services.notification_delivery import (
    NotificationSender,
    discover_deliveries,
    dispatch_delivery,
    due_delivery_ids,
    notification_policy,
)
from app.worker import celery_app


def notification_sender() -> NotificationSender | None:
    from app.services.notification_runtime import configured_notification_sender

    return configured_notification_sender()


@celery_app.task(name="tasks.sweep_notifications", soft_time_limit=20, time_limit=30)
def sweep_notifications() -> dict:
    policy = notification_policy()
    if not policy.available:
        return {"status": "disabled", "dispatched": 0}
    if notification_sender() is None:
        return {"status": "transport_unavailable", "dispatched": 0}
    with sync_session() as db:
        now = datetime.now(UTC)
        discovered = discover_deliveries(db, policy, now)
        ids = due_delivery_ids(db, policy, now)
        db.commit()
    dispatched = 0
    for delivery_id in ids:
        try:
            deliver_notification.apply_async(args=[str(delivery_id)])
            dispatched += 1
        except Exception:
            # The durable pending row remains discoverable after broker failure.
            continue
    return {"status": "queued", "discovered": discovered, "dispatched": dispatched}


@celery_app.task(
    name="tasks.deliver_notification",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=25,
    time_limit=30,
)
def deliver_notification(raw_id: str) -> dict:
    policy = notification_policy()
    if not policy.available:
        return {"status": "disabled"}
    sender = notification_sender()
    if sender is None:
        return {"status": "transport_unavailable"}
    try:
        delivery_id = uuid.UUID(raw_id)
    except (ValueError, TypeError):
        return {"status": "invalid_id"}
    return {"status": asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=policy))}
