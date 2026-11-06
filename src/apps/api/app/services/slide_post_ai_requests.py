"""Permanent paid-execution claims for recoverable SlidePost AI requests.

A claim is never leased or stolen. A lost worker may have reached the provider.
Only the original claim may publish a late, verified result.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any


class AIReceiptConflict(Exception):
    pass


def delivery_enabled(kind):
    from app.config import settings

    return bool(
        settings.slide_post_ai_recovery_enabled
        and settings.slide_posts_enabled
        and (
            kind != "chat_edit"
            or (settings.slide_post_chat_edit_enabled and settings.slide_post_rich_text_enabled)
        )
    )


def input_digest(kind: str, body: dict) -> str:
    value = json.dumps(
        {"kind": kind, "input": body}, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(value.encode()).hexdigest()


class AIExecution:
    def __init__(self, store, request_id: uuid.UUID):
        self.store = store
        self.request_id = request_id
        self.claim_id = None

    async def run(self, snapshot: dict, operation):
        self.claim_id = await self.store.claim(self.request_id, snapshot)
        if self.claim_id is None:
            raise AIReceiptConflict("request_already_claimed")
        try:
            return await operation()
        except BaseException:
            await self.store.unknown(self.request_id, self.claim_id)
            raise

    async def complete(self, result: dict):
        # A preflight-only answer (e.g. unsupported edit) also has a single owner.
        if self.claim_id is None:
            self.claim_id = await self.store.claim(self.request_id, {"no_paid_call": True})
        if self.claim_id is None or not await self.store.complete(
            self.request_id, self.claim_id, result
        ):
            raise AIReceiptConflict("request_claim_changed")

    async def unknown(self):
        if self.claim_id is not None:
            await self.store.unknown(self.request_id, self.claim_id)


class SQLAIRequestStore:
    def __init__(self, db):
        self.db = db

    async def reserve(
        self,
        *,
        request_id,
        owner_id,
        item_id,
        project_id,
        kind,
        body,
        prepared_input=None,
        authority_snapshot=None,
    ):
        from sqlalchemy.dialects.postgresql import insert

        from app.models import SlidePostAIRequest

        digest = input_digest(kind, body)
        await self.db.execute(
            insert(SlidePostAIRequest)
            .values(
                id=request_id,
                owner_id=owner_id,
                item_id=item_id,
                project_id=project_id,
                kind=kind,
                input_digest=digest,
                request_input=body,
                status="pending",
                prepared_input=prepared_input,
                authority_snapshot=authority_snapshot,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await self.db.commit()
        row = await self.db.get(SlidePostAIRequest, request_id, populate_existing=True)
        if (
            row is None
            or row.owner_id != owner_id
            or row.item_id != item_id
            or row.project_id != project_id
            or row.kind != kind
            or row.input_digest != digest
        ):
            raise AIReceiptConflict("request_identity_mismatch")
        return row

    async def claim(self, request_id, snapshot):
        from fastapi import HTTPException
        from sqlalchemy import select, update
        from sqlalchemy.exc import DBAPIError

        from app.models import CreationThread, SlidePostAIRequest
        from app.routes.plan_items import _load_owned_item_context
        from app.services.slide_post_ai_authority import authority_snapshot
        from app.services.slide_post_ai_prepared import supported

        row = await self.db.get(SlidePostAIRequest, request_id, populate_existing=True)
        if row is None:
            raise AIReceiptConflict("request_missing")
        if row.status != "pending":
            return None
        if not delivery_enabled(row.kind):
            raise AIReceiptConflict("request_delivery_paused")
        token = uuid.uuid4()
        try:
            # Legacy mutation paths use both Thread→Item and Item→Thread order.
            # Never wait for Thread while holding canonical owner/item locks.
            # Rolling back this savepoint releases every claim-acquired lock.
            async with self.db.begin_nested():
                item, plan, _ = await _load_owned_item_context(
                    str(row.item_id), row.owner_id, self.db, for_update=True
                )
                thread = await self.db.scalar(
                    select(CreationThread)
                    .where(
                        CreationThread.id == row.project_id,
                        CreationThread.creator_id == row.owner_id,
                        CreationThread.active_plan_item_id == row.item_id,
                        CreationThread.status == "active",
                    )
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if thread is None:
                    raise HTTPException(409, "The project changed before the AI request started")
                current_version = (item.slide_post or {}).get("version", 0)
                if row.request_input.get("expected_version", 0) != current_version:
                    raise HTTPException(
                        409, "The saved draft changed before the AI request started"
                    )
                if (
                    not row.prepared_input
                    or not row.authority_snapshot
                    or not supported(row.kind, row.prepared_input)
                    or snapshot != row.prepared_input
                    or await authority_snapshot(self.db, item, plan, thread, lock_sources=True)
                    != row.authority_snapshot
                ):
                    raise HTTPException(409, "The original AI inputs are unavailable or changed")
                if not delivery_enabled(row.kind):
                    raise AIReceiptConflict("request_delivery_paused")
                result = await self.db.execute(
                    update(SlidePostAIRequest)
                    .where(
                        SlidePostAIRequest.id == request_id,
                        SlidePostAIRequest.status == "pending",
                    )
                    .values(
                        status="running",
                        claim_id=token,
                        resolved_input=snapshot,
                        claimed_at=datetime.now(UTC),
                    )
                )
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code == "55P03":
                raise AIReceiptConflict("request_claim_busy") from exc
            raise
        await self.db.commit()
        return token if result.rowcount == 1 else None

    async def complete(self, request_id, claim_id, result):
        from sqlalchemy import update

        from app.models import SlidePostAIRequest

        changed = await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                SlidePostAIRequest.claim_id == claim_id,
                SlidePostAIRequest.status.in_(["running", "unknown"]),
            )
            .values(status="completed", result=result, completed_at=datetime.now(UTC))
        )
        await self.db.commit()
        return changed.rowcount == 1

    async def reject(self, request_id, message):
        from sqlalchemy import update

        from app.models import SlidePostAIRequest

        await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                SlidePostAIRequest.status == "pending",
            )
            .values(status="rejected", result={"message": message})
        )
        await self.db.commit()

    async def cancel(self, request_id):
        from sqlalchemy import update

        from app.models import SlidePostAIRequest

        changed = await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                SlidePostAIRequest.status == "pending",
                SlidePostAIRequest.claim_id.is_(None),
            )
            .values(
                status="rejected",
                result={"message": "This original AI request was cancelled.", "code": "cancelled"},
            )
        )
        await self.db.commit()
        return changed.rowcount == 1

    async def unknown(self, request_id, claim_id):
        from sqlalchemy import update

        from app.models import SlidePostAIRequest

        await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                SlidePostAIRequest.claim_id == claim_id,
                SlidePostAIRequest.status == "running",
            )
            .values(status="unknown")
        )
        await self.db.commit()


def receipt_output(row) -> dict[str, Any]:
    status = row.status
    # Observation does not grant a new execution claim. A late original result
    # can still heal this state using its immutable token.
    if (
        status == "running"
        and row.claimed_at
        and (datetime.now(UTC) - row.claimed_at).total_seconds() > 120
    ):
        status = "unknown"
    return {
        "client_request_id": str(row.id),
        "item_id": str(row.item_id),
        "project_id": str(row.project_id),
        "kind": row.kind,
        "input_digest": row.input_digest,
        "input": row.request_input,
        "status": status,
        "delivery_version": 1 if getattr(row, "prepared_input", None) else 0,
        "result": row.result if status in {"completed", "rejected"} else None,
    }
