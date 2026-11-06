"""Outbox delivery only for never-claimed original AI requests. GET never calls this."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models import SlidePostAIRequest
from app.services.slide_post_ai_prepared import execute_prepared, supported
from app.services.slide_post_ai_requests import (
    AIExecution,
    AIReceiptConflict,
    SQLAIRequestStore,
    delivery_enabled,
)

log = structlog.get_logger()


async def deliver(db, request_id):
    row = await db.get(SlidePostAIRequest, request_id, populate_existing=True)
    if row is None or row.status != "pending" or row.claim_id is not None:
        return
    if not delivery_enabled(row.kind):
        return
    store = SQLAIRequestStore(db)
    # Legacy pending input lacks the original effective snapshot. Never rebuild it.
    if not row.prepared_input or not row.authority_snapshot:
        await store.reject(
            row.id, "The original AI inputs are unavailable. Send a new instruction."
        )
        return
    if "rejection" in row.prepared_input:
        await store.reject(row.id, row.prepared_input["rejection"])
        return
    if not supported(row.kind, row.prepared_input):
        await store.reject(
            row.id,
            "This original request cannot run on this editor version. Send a new instruction.",
        )
        return
    execution = AIExecution(store, row.id)
    try:
        result = await execution.run(
            row.prepared_input, lambda: execute_prepared(row.kind, row.prepared_input)
        )
        await execution.complete(result)
    except AIReceiptConflict:
        await db.rollback()  # Claim busy remains pending for the next outbox delivery.
    except HTTPException as exc:
        if execution.claim_id is None and 400 <= exc.status_code < 500:
            await store.reject(
                row.id, "The original request's project or source changed. Send a new instruction."
            )
        else:
            await execution.unknown()
    except asyncio.CancelledError:
        await execution.unknown()
        raise
    except Exception:
        await execution.unknown()
        if execution.claim_id is None:
            raise  # Operational errors remain pending, observed by a later sweep.


async def reserve_publications(db, *, request_id=None, now=None):
    """Commit pacing BEFORE publishing, including the very first publication.

    No finite cap: a missed broker publication is recoverable. Permanent claims,
    not broker acknowledgements, prevent paid replay.
    """
    if not delivery_enabled("propose"):
        return []
    now = now or datetime.now(UTC)
    statement = select(SlidePostAIRequest).where(
        SlidePostAIRequest.status == "pending",
        SlidePostAIRequest.claim_id.is_(None),
        or_(
            SlidePostAIRequest.next_delivery_at.is_(None),
            SlidePostAIRequest.next_delivery_at <= now,
        ),
    )
    if not delivery_enabled("chat_edit"):
        statement = statement.where(SlidePostAIRequest.kind != "chat_edit")
    if request_id is not None:
        statement = statement.where(SlidePostAIRequest.id == request_id)
    rows = (
        await db.scalars(
            statement.order_by(
                SlidePostAIRequest.next_delivery_at.asc().nullsfirst(),
                SlidePostAIRequest.created_at,
                SlidePostAIRequest.id,
            )
            .limit(50)
            .with_for_update(skip_locked=True)
        )
    ).all()
    ids = []
    for row in rows:
        attempt = int(row.delivery_attempts or 0) + 1
        row.delivery_attempts = attempt
        row.next_delivery_at = now + timedelta(seconds=min(300, 30 * 2 ** min(attempt - 1, 4)))
        ids.append(row.id)
    await db.commit()
    return ids


async def publish_due(db, *, request_id=None, enqueue=None):
    if enqueue is None:
        from app.tasks.slide_post_ai_delivery import execute_slide_post_ai_request

        def enqueue(key):
            return execute_slide_post_ai_request.delay(str(key))

    ids = await reserve_publications(db, request_id=request_id)
    for key in ids:
        try:
            await asyncio.to_thread(enqueue, key)
        except Exception:
            # Reserved deadline remains durable; one poison/broker failure cannot
            # prevent attempting the rest of the bounded batch.
            log.warning("slide_post_ai_publish_failed", request_id=str(key))
    return len(ids)
