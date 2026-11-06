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


# Required serialized fields, shared by the in-memory and pre-LIMIT SQL checks.
_AUTHORITY_FIELDS = {
    "plan_id": ("string",),
    "owner_id": ("string",),
    "ownership_epoch": ("integer",),
    "item_id": ("string",),
    "project_id": ("string",),
    "thread_status": ("string",),
    "thread_state": ("object",),
    "active_item_id": ("string",),
    "draft": ("object", "null"),
    "format": ("string", "null"),
    "theme": ("string", "null"),
    "idea": ("string", "null"),
    "notes": ("string", "null"),
    "assets": ("array",),
}
_CONTEXT_FIELDS = {
    "creator_id": ("string",),
    "request_id": ("string",),
    "request_id_authoritative": ("boolean",),
    "usage_purpose": ("string", "null"),
    "test_run_id": ("string", "null"),
    "release_canary_id": ("string", "null"),
    "timeout_override_s": ("number", "null"),
}
_DRAFT_FIELDS = {
    "schema_version": ("integer",),
    "version": ("integer",),
    "platform_profile": ("string",),
    "slides": ("array",),
    "cover_index": ("integer",),
    "caption": ("string",),
    "rendered_version": ("integer", "null"),
    "user_edited": ("boolean",),
    "brief_binding": ("object", "null"),
}


def _matches_fields(value, fields):
    def matches(item, name):
        return {
            "object": isinstance(item, dict),
            "array": isinstance(item, list),
            "string": isinstance(item, str),
            "integer": type(item) is int and item >= 0,
            "number": type(item) in (int, float),
            "boolean": type(item) is bool,
            "null": item is None,
        }[name]

    return isinstance(value, dict) and all(
        key in value and any(matches(value[key], t) for t in types) for key, types in fields.items()
    )


def _complete_snapshot(row, snapshot):
    if not _matches_fields(snapshot, {"agent_input": ("object",), "context": ("object",)}):
        return False
    context = snapshot["context"]
    if not (
        _matches_fields(context, _CONTEXT_FIELDS)
        and context["creator_id"] == str(row.owner_id)
        and context["request_id"] == f"slide-post-ai:{row.id}"
        and context["request_id_authoritative"] is True
    ):
        return False
    try:
        from app.schemas.slide_post import SlidePostDraft

        if row.kind == "chat_edit":
            from app.agents.edit_copilot import EditCopilotInput
            from app.kria.brief_binding import BriefBinding

            if not (
                _matches_fields(
                    snapshot,
                    {
                        "baseline": ("object",),
                        "server_version": ("integer",),
                        "binding": ("object", "null"),
                    },
                )
                and _matches_fields(snapshot["baseline"], _DRAFT_FIELDS)
                and _matches_fields(
                    snapshot["agent_input"],
                    {
                        "utterance": ("string",),
                        "prior_turns": ("array",),
                        "variant_snapshot": ("object",),
                    },
                )
            ):
                return False
            SlidePostDraft.model_validate(snapshot["baseline"])
            EditCopilotInput.model_validate(snapshot["agent_input"])
            if snapshot["binding"] is not None:
                BriefBinding.model_validate(snapshot["binding"])
        elif row.kind == "propose":
            from app.agents.slide_post_composer import SlidePostComposerInput

            if not (
                _matches_fields(
                    snapshot,
                    {
                        "baseline": ("object", "null"),
                        "fallback": ("object",),
                        "previous_version": ("integer",),
                    },
                )
                and _matches_fields(snapshot["fallback"], _DRAFT_FIELDS)
                and _matches_fields(
                    snapshot["agent_input"],
                    {
                        "media": ("array",),
                        "theme": ("string",),
                        "idea": ("string",),
                        "notes": ("string",),
                        "platform_profile": ("string",),
                    },
                )
            ):
                return False
            SlidePostDraft.model_validate(snapshot["fallback"])
            if snapshot["baseline"] is not None:
                SlidePostDraft.model_validate(snapshot["baseline"])
            SlidePostComposerInput.model_validate(snapshot["agent_input"])
        else:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return True


def has_original_envelope(row):
    prepared = getattr(row, "prepared_input", None)
    authority = getattr(row, "authority_snapshot", None)
    if not (
        isinstance(prepared, dict)
        and type(prepared.get("version")) is int
        and prepared["version"] == 1
        and _matches_fields(authority, _AUTHORITY_FIELDS)
    ):
        return False
    manifest = prepared.get("manifest")
    snapshot = prepared.get("snapshot")
    captured = (
        _complete_snapshot(row, snapshot)
        and isinstance(manifest, dict)
        and type(manifest.get("executor_version")) is int
        and manifest["executor_version"] == 1
        and isinstance(manifest.get("code_digest"), str)
        and bool(manifest["code_digest"])
        and isinstance(manifest.get("prompt_digest"), str)
        and bool(manifest["prompt_digest"])
        and isinstance(manifest.get("spec"), dict)
        and bool(manifest["spec"])
        and isinstance(manifest.get("flags"), dict)
    )
    result = (
        isinstance(prepared.get("result"), dict)
        and bool(prepared["result"])
        and isinstance(prepared.get("code_digest"), str)
        and bool(prepared["code_digest"])
    )
    rejection = isinstance(prepared.get("rejection"), str) and bool(prepared["rejection"])
    return bool(captured or result or rejection)


def unclaimed_pending(row):
    return (
        row.status == "pending"
        and has_original_envelope(row)
        and row.claim_id is None
        and row.claimed_at is None
        and row.resolved_input is None
    )


def unclaimed_pending_predicates():
    """Same admission evidence as unclaimed_pending, before LIMIT or mutation.

    SQL NULL, JSON null and empty objects do not establish original-input evidence.
    """
    from sqlalchemy import JSON, String, and_, func, or_

    from app.models import SlidePostAIRequest as Row

    def nonempty_object(value):
        return and_(func.jsonb_typeof(value) == "object", value != {})

    def nonempty_string(value):
        return and_(func.jsonb_typeof(value) == "string", value.astext != "")

    def version_one(value):
        return and_(func.jsonb_typeof(value) == "number", value.astext == "1")

    def fields_present(value, fields):
        def typed(field, name):
            if name == "integer":
                return and_(
                    func.jsonb_typeof(field) == "number", field.astext.op("~")(r"^(0|[1-9][0-9]*)$")
                )
            return func.jsonb_typeof(field) == name

        return and_(
            func.jsonb_typeof(value) == "object",
            *(or_(*(typed(value[key], name) for name in types)) for key, types in fields.items()),
        )

    prepared = Row.prepared_input
    manifest = prepared["manifest"]
    snapshot = prepared["snapshot"]
    context = snapshot["context"]
    complete_snapshot = and_(
        fields_present(context, _CONTEXT_FIELDS),
        context["creator_id"].astext == Row.owner_id.cast(String),
        context["request_id"].astext == "slide-post-ai:" + Row.id.cast(String),
        context["request_id_authoritative"].astext == "true",
        or_(
            and_(
                Row.kind == "chat_edit",
                fields_present(
                    snapshot,
                    {
                        "baseline": ("object",),
                        "server_version": ("integer",),
                        "binding": ("object", "null"),
                    },
                ),
                fields_present(snapshot["baseline"], _DRAFT_FIELDS),
                fields_present(
                    snapshot["agent_input"],
                    {
                        "utterance": ("string",),
                        "prior_turns": ("array",),
                        "variant_snapshot": ("object",),
                    },
                ),
            ),
            and_(
                Row.kind == "propose",
                fields_present(
                    snapshot,
                    {
                        "baseline": ("object", "null"),
                        "fallback": ("object",),
                        "previous_version": ("integer",),
                    },
                ),
                fields_present(snapshot["fallback"], _DRAFT_FIELDS),
                fields_present(
                    snapshot["agent_input"],
                    {
                        "media": ("array",),
                        "theme": ("string",),
                        "idea": ("string",),
                        "notes": ("string",),
                        "platform_profile": ("string",),
                    },
                ),
            ),
        ),
    )
    return (
        Row.status == "pending",
        Row.claim_id.is_(None),
        Row.claimed_at.is_(None),
        or_(Row.resolved_input.is_(None), Row.resolved_input == JSON.NULL),
        nonempty_object(prepared),
        version_one(prepared["version"]),
        fields_present(Row.authority_snapshot, _AUTHORITY_FIELDS),
        or_(
            and_(
                complete_snapshot,
                nonempty_object(manifest),
                version_one(manifest["executor_version"]),
                nonempty_string(manifest["code_digest"]),
                nonempty_string(manifest["prompt_digest"]),
                nonempty_object(manifest["spec"]),
                func.jsonb_typeof(manifest["flags"]) == "object",
            ),
            and_(nonempty_object(prepared["result"]), nonempty_string(prepared["code_digest"])),
            nonempty_string(prepared["rejection"]),
        ),
    )


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
        if not unclaimed_pending(row):
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
                        *unclaimed_pending_predicates(),
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

        if claim_id is None:
            return False
        changed = await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                SlidePostAIRequest.claim_id == claim_id,
                SlidePostAIRequest.claim_id.is_not(None),
                SlidePostAIRequest.status.in_(["pending", "running", "unknown"]),
            )
            .values(status="completed", result=result, completed_at=datetime.now(UTC))
        )
        await self.db.commit()
        return changed.rowcount == 1

    async def reject(self, request_id, message):
        from sqlalchemy import update

        from app.models import SlidePostAIRequest

        row = await self.db.get(SlidePostAIRequest, request_id, populate_existing=True)
        if row is None or not unclaimed_pending(row):
            return
        await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                *unclaimed_pending_predicates(),
            )
            .values(status="rejected", result={"message": message})
        )
        await self.db.commit()

    async def cancel(self, request_id):
        from sqlalchemy import update

        from app.models import SlidePostAIRequest

        row = await self.db.get(SlidePostAIRequest, request_id, populate_existing=True)
        if row is None or not unclaimed_pending(row):
            return False
        changed = await self.db.execute(
            update(SlidePostAIRequest)
            .where(
                SlidePostAIRequest.id == request_id,
                *unclaimed_pending_predicates(),
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
    ambiguous_pending = status == "pending" and not unclaimed_pending(row)
    if ambiguous_pending:
        status = "unknown"
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
        "delivery_version": 1
        if not ambiguous_pending and getattr(row, "prepared_input", None)
        else 0,
        "result": row.result if status in {"completed", "rejected"} else None,
    }
