# ruff: noqa: E501
"""Canonical creator-direction reads and writes.

This module deliberately contains no model/provider calls.  It is the small,
deterministic boundary that later extraction and pipeline integrations consume.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CreationThread,
    CreatorMemoryItem,
    CreatorMemoryOperation,
    ProjectDirectionOverride,
    User,
)
from app.services.creator_direction_capabilities import (
    ENFORCEABLE_FONT_FAMILIES,
    SUPPORTED_STRUCTURED_KEYS,
    validate_capability_values,
)
from app.services.private_text import normalize_private_text

MAX_ACTIVE_ITEMS = 100
MAX_TOTAL_INSTRUCTION_CHARS = 4_000
MAX_INSTRUCTION_CHARS = 500
UNDO_TTL = timedelta(minutes=10)
_WS = re.compile(r"\s+")
_FONT_PATTERNS = (
    re.compile(r"\balways use (?:the )?([a-z0-9][a-z0-9 ._-]{0,59}?) font\b", re.IGNORECASE),
    re.compile(
        r"\buse (?:the )?([a-z0-9][a-z0-9 ._-]{0,59}?) font "
        r"(?:every time|for every video|from now on)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:the )?font (?:should|must) always be "
        r"([a-z0-9][a-z0-9 ._-]{0,59}?)(?:[.!]|$)",
        re.IGNORECASE,
    ),
)


class DirectionError(ValueError):
    code = "invalid_direction"


class DirectionConflict(DirectionError):
    code = "memory_conflict"


class IdempotencyMismatch(DirectionError):
    code = "idempotency_mismatch"


class StaleRevision(DirectionError):
    code = "stale_revision"


class LimitReached(DirectionError):
    code = "memory_limit_reached"


class MemoryItemNotFound(DirectionError):
    code = "memory_item_not_found"


class ProjectDirectionNotFound(DirectionError):
    code = "project_direction_not_found"


class UndoExpired(DirectionConflict):
    code = "undo_expired"


class UndoNotApplicable(DirectionConflict):
    code = "undo_no_longer_applicable"


class UnsupportedKey(DirectionError):
    code = "unsupported_key"


class UnsupportedValue(DirectionError):
    code = "unsupported_value"


class InstructionTooLong(DirectionError):
    code = "instruction_too_long"


def normalize_instruction(value: str) -> str:
    if not isinstance(value, str):
        raise DirectionError("instruction must be text")
    value = normalize_private_text(value)
    value = _WS.sub(" ", value).strip()
    if not value:
        raise DirectionError("instruction is required")
    if len(value) > MAX_INSTRUCTION_CHARS:
        raise InstructionTooLong("instruction is too long")
    return value


def normalize_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = normalize_instruction(value).casefold().replace(" ", "_")
    if not re.fullmatch(r"[a-z0-9_:-]{1,120}", key):
        raise UnsupportedKey("invalid normalized key")
    return key


def _hash(value: str) -> str:
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()


def request_fingerprint(operation_kind: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation_kind, **payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_structured(
    value: dict[str, Any] | None, *, normalized_key: str | None = None
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - SUPPORTED_STRUCTURED_KEYS:
        raise UnsupportedKey("unsupported structured key")
    if not validate_capability_values(value):
        raise UnsupportedValue("unsupported structured value")
    if normalized_key is not None and set(value) != {normalized_key}:
        raise UnsupportedValue("structured value must match normalized key")
    return dict(value)


def infer_typed_direction(
    instruction: str,
    normalized_key: str | None,
    structured_value: dict[str, Any] | None,
    enforcement: str,
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Map narrow, unambiguous durable instructions to renderer-safe keys."""
    instruction = normalize_instruction(instruction)
    lowered = instruction.casefold()
    if normalized_key == "font_family" and structured_value is None:
        mentioned_fonts = [
            font
            for font in ENFORCEABLE_FONT_FAMILIES
            if re.search(rf"(?<!\w){re.escape(font)}(?!\w)", instruction, re.IGNORECASE)
        ]
        if len(mentioned_fonts) == 1:
            font_family = mentioned_fonts[0]
            return normalized_key, {normalized_key: font_family}, enforcement
        # An edited sentence that no longer names one enforceable font must not
        # retain a renderer key with an empty (or stale) value.
        return None, None, enforcement
    if normalized_key == "shadow_enabled" and structured_value is None:
        if any(token in lowered for token in ("no shadow", "never add shadow", "without shadows")):
            return normalized_key, {normalized_key: False}, "constraint"
        if any(token in lowered for token in ("use shadow", "add shadow", "with shadows")):
            return normalized_key, {normalized_key: True}, enforcement
        return None, None, enforcement
    if normalized_key is None and any(
        token in lowered for token in ("no shadow", "never add shadow", "without shadows")
    ):
        return "shadow_enabled", {"shadow_enabled": False}, "constraint"
    if normalized_key is None:
        for pattern in _FONT_PATTERNS:
            match = pattern.search(instruction)
            if match:
                font_family = match.group(1).strip(" ._-")
                if font_family and validate_capability_values({"font_family": font_family}):
                    return "font_family", {"font_family": font_family}, "constraint"
    return normalized_key, structured_value, enforcement


@dataclass(frozen=True)
class CreatorDirectionSnapshot:
    enabled: bool
    revision: int
    items: tuple[dict[str, Any], ...]
    overrides: tuple[dict[str, Any], ...] = ()

    @property
    def prompt_block(self) -> str:
        if not self.enabled:
            return ""
        override_keys = {
            row.get("normalized_key") for row in self.overrides if row.get("normalized_key")
        }
        account_lines = [
            f"- {row['instruction']}"
            for row in self.items
            if not row.get("normalized_key") or row.get("normalized_key") not in override_keys
        ]
        project_lines = [
            f"- {row['instruction']} (this project only)"
            for row in self.overrides
            if row.get("instruction")
        ]
        return "\n".join([*account_lines, *project_lines])

    @property
    def typed_overrides(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in self.items:
            if row.get("structured_value"):
                result.update(row["structured_value"])
        for row in self.overrides:
            if row.get("normalized_key"):
                result.pop(str(row["normalized_key"]), None)
            if row.get("structured_value"):
                result.update(row["structured_value"])
        return result


@dataclass(frozen=True)
class ProjectDirectionMutation:
    """Stable response envelope for idempotent project-override writes."""

    id: uuid.UUID
    thread_id: uuid.UUID
    normalized_key: str
    instruction: str
    structured_value: dict[str, Any] | None
    revision: int


class CreatorDirectionResolver:
    """Pure projection over bounded, owner-scoped direction rows."""

    async def snapshot(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        thread_id: uuid.UUID | None = None,
    ) -> CreatorDirectionSnapshot:
        user = await db.get(User, user_id)
        enabled = (
            bool(getattr(user, "creator_memory_enabled", False)) if user is not None else False
        )
        revision = int(getattr(user, "creator_memory_revision", 0)) if user is not None else 0
        rows = (
            (
                await db.execute(
                    select(CreatorMemoryItem)
                    .where(
                        CreatorMemoryItem.user_id == user_id, CreatorMemoryItem.state == "active"
                    )
                    .order_by(CreatorMemoryItem.updated_at.desc())
                    .limit(MAX_ACTIVE_ITEMS)
                )
            )
            .scalars()
            .all()
        )
        total = 0
        items: list[dict[str, Any]] = []
        for row in rows:
            total += len(row.instruction)
            if total > MAX_TOTAL_INSTRUCTION_CHARS:
                break
            items.append(self._item(row))
        overrides: list[dict[str, Any]] = []
        if thread_id is not None:
            override_rows = (
                (
                    await db.execute(
                        select(ProjectDirectionOverride).where(
                            ProjectDirectionOverride.user_id == user_id,
                            ProjectDirectionOverride.thread_id == thread_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            overrides = [self._override(row) for row in override_rows]
        return CreatorDirectionSnapshot(
            enabled, revision, tuple(items if enabled else ()), tuple(overrides if enabled else ())
        )

    @staticmethod
    def _item(row: CreatorMemoryItem) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "category": row.category,
            "normalized_key": row.normalized_key,
            "instruction": row.instruction,
            "enforcement": row.enforcement,
            "structured_value": row.structured_value,
            "source_kind": row.source_kind,
            "source_thread_id": str(row.source_thread_id) if row.source_thread_id else None,
            "state": row.state,
            "user_locked": row.user_locked,
            "confidence": row.confidence,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _override(row: ProjectDirectionOverride) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "normalized_key": row.normalized_key,
            "instruction": row.instruction,
            "structured_value": row.structured_value,
        }


class CreatorDirectionService:
    """Sole mutation boundary for account memory and project overrides."""

    @staticmethod
    def set_compatibility_persona_style(persona: Any, style: dict[str, Any] | None) -> None:
        """Write the legacy Persona.style compatibility projection.

        Persona style predates creator memory and remains a lower-precedence
        compatibility input. Keeping this tiny synchronous adapter on the
        canonical mutation service lets async routes and sync Celery workers
        share one write boundary without changing their transaction behavior.
        It intentionally does not create or revise account memory: inferred
        persona state must never be promoted to creator-authored direction.
        """

        persona.style = style

    async def _user(self, db: AsyncSession, user_id: uuid.UUID, *, lock: bool = False) -> User:
        stmt = select(User).where(User.id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise DirectionError("user not found")
        return row

    async def _op(
        self, db: AsyncSession, user_id: uuid.UUID, key: str, fingerprint: str
    ) -> CreatorMemoryOperation | None:
        row = (
            await db.execute(
                select(CreatorMemoryOperation).where(
                    CreatorMemoryOperation.user_id == user_id,
                    CreatorMemoryOperation.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.request_fingerprint != fingerprint:
            raise IdempotencyMismatch("idempotency key reused with different input")
        return row

    @staticmethod
    async def _duplicate_op(
        db: AsyncSession,
        *,
        user: User,
        item: CreatorMemoryItem,
        idempotency_key: str,
        fingerprint: str,
        source_kind: str,
        source_event_id: uuid.UUID | None,
    ) -> CreatorMemoryOperation:
        operation = CreatorMemoryOperation(
            user_id=user.id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="duplicate",
            item_id=item.id,
            prior_state=None,
            resulting_revision=user.creator_memory_revision,
            actor_kind="system" if source_kind == "creation_thread" else "user",
            source_event_id=source_event_id,
        )
        db.add(operation)
        await db.flush()
        return operation

    async def set_enabled(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        enabled: bool,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "set_enabled", {"enabled": enabled, "expected_revision": expected_revision}
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing:
            return existing
        self._check_revision(user, expected_revision)
        before = {"enabled": user.creator_memory_enabled}
        user.creator_memory_enabled = enabled
        user.creator_memory_revision += 1
        op = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="set_enabled",
            prior_state=before,
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
            undo_expires_at=datetime.now(UTC) + UNDO_TTL,
        )
        db.add(op)
        await db.flush()
        return op

    async def create_item(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        instruction: str,
        category: str,
        enforcement: str,
        normalized_key: str | None,
        structured_value: dict[str, Any] | None,
        expected_revision: int | None,
        idempotency_key: str,
        source_kind: str = "profile",
        source_thread_id: uuid.UUID | None = None,
        source_event_id: uuid.UUID | None = None,
        user_locked: bool = True,
        initial_state: str = "active",
    ) -> CreatorMemoryOperation:
        instruction = normalize_instruction(instruction)
        normalized_key = normalize_key(normalized_key)
        structured_value = validate_structured(structured_value, normalized_key=normalized_key)
        normalized_key, structured_value, enforcement = infer_typed_direction(
            instruction, normalized_key, structured_value, enforcement
        )
        structured_value = validate_structured(structured_value, normalized_key=normalized_key)
        if enforcement not in {"constraint", "default", "advisory"}:
            raise DirectionError("unsupported enforcement")
        if initial_state not in {"active", "suggested"}:
            raise DirectionError("unsupported initial state")
        if category == "other" and normalized_key in {"font_family", "shadow_enabled"}:
            category = "video_style"
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "create_item",
            {
                "instruction": instruction,
                "category": category,
                "enforcement": enforcement,
                "normalized_key": normalized_key,
                "structured_value": structured_value,
                "initial_state": initial_state,
            },
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing:
            return existing
        if expected_revision is not None:
            self._check_revision(user, expected_revision)
        if normalized_key and initial_state == "active":
            conflict = (
                await db.execute(
                    select(CreatorMemoryItem).where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.scope_kind == "account",
                        CreatorMemoryItem.normalized_key == normalized_key,
                        CreatorMemoryItem.state == "active",
                    )
                )
            ).scalar_one_or_none()
            if conflict:
                if (
                    conflict.instruction == instruction
                    and conflict.structured_value == structured_value
                ):
                    return await self._duplicate_op(
                        db,
                        user=user,
                        item=conflict,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        source_kind=source_kind,
                        source_event_id=source_event_id,
                    )
                # A contradictory durable rule must never silently replace the
                # active account rule. Keep it inert for an explicit review.
                initial_state = "suggested"
        content_hash = _hash(instruction) if not normalized_key else None
        if content_hash:
            duplicate = (
                await db.execute(
                    select(CreatorMemoryItem).where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.scope_kind == "account",
                        CreatorMemoryItem.content_hash == content_hash,
                        CreatorMemoryItem.state.in_(("active", "suggested")),
                    )
                )
            ).scalar_one_or_none()
            if duplicate:
                return await self._duplicate_op(
                    db,
                    user=user,
                    item=duplicate,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    source_kind=source_kind,
                    source_event_id=source_event_id,
                )
        active_count = await db.scalar(
            select(func.count())
            .select_from(CreatorMemoryItem)
            .where(CreatorMemoryItem.user_id == user_id, CreatorMemoryItem.state == "active")
        )
        if initial_state == "active" and int(active_count or 0) >= MAX_ACTIVE_ITEMS:
            raise LimitReached("too many active memory items")
        if initial_state == "suggested":
            suggestion_count = await db.scalar(
                select(func.count())
                .select_from(CreatorMemoryItem)
                .where(
                    CreatorMemoryItem.user_id == user_id,
                    CreatorMemoryItem.state == "suggested",
                )
            )
            if int(suggestion_count or 0) >= 20:
                raise LimitReached("too many memory suggestions")
        item = CreatorMemoryItem(
            user_id=user_id,
            category=category,
            normalized_key=normalized_key,
            content_hash=content_hash,
            instruction=instruction,
            enforcement=enforcement,
            structured_value=structured_value,
            source_kind=source_kind,
            source_thread_id=source_thread_id,
            source_event_id=source_event_id,
            state=initial_state,
            user_locked=user_locked,
        )
        db.add(item)
        user.creator_memory_revision += 1
        await db.flush()
        operation_kind = "create_item" if initial_state == "active" else "create_suggestion"
        op = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind=operation_kind,
            item_id=item.id,
            prior_state={"created": True},
            resulting_revision=user.creator_memory_revision,
            actor_kind="system" if source_kind == "creation_thread" else "user",
            source_event_id=source_event_id,
            undo_expires_at=datetime.now(UTC) + UNDO_TTL if initial_state == "active" else None,
        )
        db.add(op)
        await db.flush()
        return op

    async def forget(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "forget", {"item_id": item_id, "expected_revision": expected_revision}
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing:
            return existing
        self._check_revision(user, expected_revision)
        item = (
            await db.execute(
                select(CreatorMemoryItem)
                .where(CreatorMemoryItem.id == item_id, CreatorMemoryItem.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            raise MemoryItemNotFound("memory item not found")
        before = {"state": item.state, "user_locked": item.user_locked}
        item.state = "forgotten"
        user.creator_memory_revision += 1
        op = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="forget",
            item_id=item.id,
            prior_state=before,
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
            undo_expires_at=datetime.now(UTC) + UNDO_TTL,
        )
        db.add(op)
        await db.flush()
        return op

    async def clear_preferences(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        """Clear current account preferences in one revision-guarded mutation."""

        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "clear_preferences", {"expected_revision": expected_revision}
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing is not None:
            return existing
        self._check_revision(user, expected_revision)
        rows = list(
            (
                await db.execute(
                    select(CreatorMemoryItem)
                    .where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.state.in_(("active", "suggested")),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        prior_state = {
            "items": [{"id": str(row.id), "state": row.state} for row in rows],
        }
        for row in rows:
            row.state = "forgotten" if row.state == "active" else "dismissed"
        user.creator_memory_revision += 1
        operation = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="clear_preferences",
            prior_state=prior_state,
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
            undo_expires_at=datetime.now(UTC) + UNDO_TTL,
        )
        db.add(operation)
        await db.flush()
        return operation

    async def set_item_state(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        state: str,
        required_state: str | None = None,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        if state not in {"active", "dismissed", "forgotten"}:
            raise DirectionError("unsupported item state")
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            f"set_{state}",
            {
                "item_id": item_id,
                "state": state,
                "required_state": required_state,
                "expected_revision": expected_revision,
            },
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing:
            return existing
        self._check_revision(user, expected_revision)
        item = (
            await db.execute(
                select(CreatorMemoryItem)
                .where(CreatorMemoryItem.id == item_id, CreatorMemoryItem.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            raise MemoryItemNotFound("memory item not found")
        if required_state is not None and item.state != required_state:
            raise UndoNotApplicable(f"memory item must be {required_state}")
        prior = {"state": item.state, "user_locked": item.user_locked}
        if state == "active" and item.normalized_key:
            conflict = (
                await db.execute(
                    select(CreatorMemoryItem).where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.normalized_key == item.normalized_key,
                        CreatorMemoryItem.state == "active",
                        CreatorMemoryItem.id != item.id,
                    )
                )
            ).scalar_one_or_none()
            if conflict:
                prior["_superseded_item_id"] = str(conflict.id)
                prior["_superseded_state"] = conflict.state
                conflict.state = "superseded"
                # The partial unique index permits one active row per typed
                # key. Persist the old row's exit before activating its
                # replacement; one executemany batch can apply these updates
                # in the opposite order and fail transiently.
                await db.flush()
        item.state = state
        if state == "active":
            item.user_locked = True
        user.creator_memory_revision += 1
        op = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind=f"set_{state}",
            item_id=item.id,
            prior_state=prior,
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
            undo_expires_at=datetime.now(UTC) + UNDO_TTL,
        )
        db.add(op)
        await db.flush()
        return op

    async def update_item(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        instruction: str,
        category: str,
        enforcement: str,
        normalized_key: str | None,
        structured_value: dict[str, Any] | None,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        instruction = normalize_instruction(instruction)
        normalized_key = normalize_key(normalized_key)
        structured_value = validate_structured(structured_value, normalized_key=normalized_key)
        normalized_key, structured_value, enforcement = infer_typed_direction(
            instruction, normalized_key, structured_value, enforcement
        )
        structured_value = validate_structured(structured_value, normalized_key=normalized_key)
        if enforcement not in {"constraint", "default", "advisory"}:
            raise DirectionError("unsupported enforcement")
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "update_item",
            {
                "item_id": item_id,
                "instruction": instruction,
                "category": category,
                "enforcement": enforcement,
                "normalized_key": normalized_key,
                "structured_value": structured_value,
                "expected_revision": expected_revision,
            },
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing:
            return existing
        self._check_revision(user, expected_revision)
        item = (
            await db.execute(
                select(CreatorMemoryItem)
                .where(CreatorMemoryItem.id == item_id, CreatorMemoryItem.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            raise MemoryItemNotFound("memory item not found")
        if normalized_key is not None:
            conflict = (
                await db.execute(
                    select(CreatorMemoryItem)
                    .where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.normalized_key == normalized_key,
                        CreatorMemoryItem.state == "active",
                        CreatorMemoryItem.id != item.id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if conflict is not None:
                raise DirectionConflict("another active memory item already uses this key")
        prior = {
            "instruction": item.instruction,
            "category": item.category,
            "enforcement": item.enforcement,
            "normalized_key": item.normalized_key,
            "structured_value": item.structured_value,
            "state": item.state,
            "user_locked": item.user_locked,
        }
        (
            item.instruction,
            item.category,
            item.enforcement,
            item.normalized_key,
            item.structured_value,
        ) = instruction, category, enforcement, normalized_key, structured_value
        item.content_hash = _hash(instruction) if not normalized_key else None
        item.user_locked = True
        item.state = "active"
        user.creator_memory_revision += 1
        op = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="update_item",
            item_id=item.id,
            prior_state=prior,
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
            undo_expires_at=datetime.now(UTC) + UNDO_TTL,
        )
        db.add(op)
        await db.flush()
        return op

    async def apply_extracted_transition(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        operation_kind: str,
        target_item_id: uuid.UUID,
        instruction: str | None,
        category: str | None,
        enforcement: str | None,
        normalized_key: str | None,
        structured_value: dict[str, Any] | None,
        expected_revision: int,
        idempotency_key: str,
        source_thread_id: uuid.UUID | None,
        source_event_id: uuid.UUID | None,
    ) -> CreatorMemoryOperation:
        """Apply an extractor-proposed revoke or unlocked-item replacement."""

        if operation_kind not in {"forget", "supersede"}:
            raise DirectionError("unsupported extracted transition")
        normalized_key = normalize_key(normalized_key)
        structured_value = validate_structured(structured_value, normalized_key=normalized_key)
        if operation_kind == "supersede":
            if instruction is None or category is None or enforcement is None:
                raise DirectionError("supersede requires a complete direction")
            instruction = normalize_instruction(instruction)
            normalized_key, structured_value, enforcement = infer_typed_direction(
                instruction,
                normalized_key,
                structured_value,
                enforcement,
            )
            structured_value = validate_structured(structured_value, normalized_key=normalized_key)
            if enforcement not in {"constraint", "default"}:
                raise DirectionError("supersede must be explicit")
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            f"extracted_{operation_kind}",
            {
                "target_item_id": target_item_id,
                "instruction": instruction,
                "category": category,
                "enforcement": enforcement,
                "normalized_key": normalized_key,
                "structured_value": structured_value,
                "source_event_id": source_event_id,
            },
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing is not None:
            return existing
        self._check_revision(user, expected_revision)
        target = (
            await db.execute(
                select(CreatorMemoryItem)
                .where(
                    CreatorMemoryItem.id == target_item_id,
                    CreatorMemoryItem.user_id == user_id,
                    CreatorMemoryItem.state == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if target is None:
            raise DirectionConflict("extraction target is no longer active")

        if operation_kind == "forget":
            prior_state = {"state": target.state, "user_locked": target.user_locked}
            target.state = "forgotten"
            item = target
        else:
            if target.user_locked:
                raise DirectionConflict("locked memory requires creator review")
            if normalized_key is None or target.normalized_key != normalized_key:
                raise DirectionConflict("replacement key does not match active memory")
            target.state = "superseded"
            # PostgreSQL may check the partial unique index before applying a
            # same-flush UPDATE batch. Retire the old key first.
            await db.flush()
            item = CreatorMemoryItem(
                user_id=user_id,
                category=category,
                normalized_key=normalized_key,
                content_hash=None,
                instruction=instruction,
                enforcement=enforcement,
                structured_value=structured_value,
                source_kind="creation_thread",
                source_thread_id=source_thread_id,
                source_event_id=source_event_id,
                state="active",
                user_locked=False,
            )
            db.add(item)
            prior_state = {
                "state": "forgotten",
                "_superseded_item_id": str(target.id),
                "_superseded_state": "active",
            }

        user.creator_memory_revision += 1
        await db.flush()
        operation = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind=f"extracted_{operation_kind}",
            item_id=item.id,
            prior_state=prior_state,
            resulting_revision=user.creator_memory_revision,
            actor_kind="system",
            source_event_id=source_event_id,
            undo_expires_at=datetime.now(UTC) + UNDO_TTL,
        )
        db.add(operation)
        await db.flush()
        return operation

    async def set_override(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        thread_id: uuid.UUID,
        *,
        instruction: str,
        normalized_key: str,
        structured_value: dict[str, Any] | None,
        expected_revision: int,
        idempotency_key: str,
    ) -> ProjectDirectionMutation:
        instruction = normalize_instruction(instruction)
        normalized_key = normalize_key(normalized_key)
        if normalized_key is None:
            raise DirectionError("override key is required")
        structured_value = validate_structured(structured_value, normalized_key=normalized_key)
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "set_override",
            {
                "thread_id": thread_id,
                "instruction": instruction,
                "normalized_key": normalized_key,
                "structured_value": structured_value,
                "expected_revision": expected_revision,
            },
        )
        prior_operation = await self._op(db, user_id, idempotency_key, fingerprint)
        if prior_operation is not None:
            return self._override_mutation(prior_operation)
        self._check_revision(user, expected_revision)
        thread = (
            await db.execute(
                select(CreationThread).where(
                    CreationThread.id == thread_id, CreationThread.creator_id == user_id
                )
            )
        ).scalar_one_or_none()
        if thread is None:
            raise ProjectDirectionNotFound("creation thread not found")
        existing = (
            await db.execute(
                select(ProjectDirectionOverride).where(
                    ProjectDirectionOverride.user_id == user_id,
                    ProjectDirectionOverride.thread_id == thread_id,
                    ProjectDirectionOverride.normalized_key == normalized_key,
                )
            )
        ).scalar_one_or_none()
        if existing:
            prior_state: dict[str, Any] = {
                "instruction": existing.instruction,
                "structured_value": existing.structured_value,
            }
            existing.instruction, existing.structured_value = instruction, structured_value
            row = existing
        else:
            prior_state = {"created": True}
            row = ProjectDirectionOverride(
                user_id=user_id,
                thread_id=thread_id,
                normalized_key=normalized_key,
                instruction=instruction,
                structured_value=structured_value,
            )
            db.add(row)
        await db.flush()
        user.creator_memory_revision += 1
        result = ProjectDirectionMutation(
            id=row.id,
            thread_id=row.thread_id,
            normalized_key=row.normalized_key,
            instruction=row.instruction,
            structured_value=row.structured_value,
            revision=user.creator_memory_revision,
        )
        prior_state["_result"] = {
            "id": str(result.id),
            "thread_id": str(result.thread_id),
            "normalized_key": result.normalized_key,
            "instruction": result.instruction,
            "structured_value": result.structured_value,
            "revision": result.revision,
        }
        db.add(
            CreatorMemoryOperation(
                user_id=user_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                operation_kind="set_override",
                prior_state=prior_state,
                resulting_revision=result.revision,
                actor_kind="user",
            )
        )
        await db.flush()
        return result

    async def delete_override(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        thread_id: uuid.UUID,
        normalized_key: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        normalized_key = normalize_key(normalized_key)
        if normalized_key is None:
            raise DirectionError("override key is required")
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "delete_override",
            {
                "thread_id": thread_id,
                "normalized_key": normalized_key,
                "expected_revision": expected_revision,
            },
        )
        prior_operation = await self._op(db, user_id, idempotency_key, fingerprint)
        if prior_operation is not None:
            return prior_operation
        self._check_revision(user, expected_revision)
        row = (
            await db.execute(
                select(ProjectDirectionOverride)
                .where(
                    ProjectDirectionOverride.user_id == user_id,
                    ProjectDirectionOverride.thread_id == thread_id,
                    ProjectDirectionOverride.normalized_key == normalized_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ProjectDirectionNotFound("project direction override not found")
        prior_state = {
            "id": str(row.id),
            "thread_id": str(row.thread_id),
            "normalized_key": row.normalized_key,
            "instruction": row.instruction,
            "structured_value": row.structured_value,
        }
        await db.delete(row)
        user.creator_memory_revision += 1
        operation = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="delete_override",
            prior_state=prior_state,
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
        )
        db.add(operation)
        await db.flush()
        return operation

    @staticmethod
    def _override_mutation(operation: CreatorMemoryOperation) -> ProjectDirectionMutation:
        result = (operation.prior_state or {}).get("_result")
        if not isinstance(result, dict):
            raise DirectionConflict("override operation cannot be replayed")
        try:
            return ProjectDirectionMutation(
                id=uuid.UUID(str(result["id"])),
                thread_id=uuid.UUID(str(result["thread_id"])),
                normalized_key=str(result["normalized_key"]),
                instruction=str(result["instruction"]),
                structured_value=(
                    dict(result["structured_value"])
                    if isinstance(result.get("structured_value"), dict)
                    else None
                ),
                revision=int(result["revision"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DirectionConflict("override operation cannot be replayed") from exc

    async def undo(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        operation_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> CreatorMemoryOperation:
        user = await self._user(db, user_id, lock=True)
        fingerprint = request_fingerprint(
            "undo", {"operation_id": operation_id, "expected_revision": expected_revision}
        )
        existing = await self._op(db, user_id, idempotency_key, fingerprint)
        if existing:
            return existing
        self._check_revision(user, expected_revision)
        op = (
            await db.execute(
                select(CreatorMemoryOperation)
                .where(
                    CreatorMemoryOperation.id == operation_id,
                    CreatorMemoryOperation.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if op is None or op.undone_at is not None or op.undo_expires_at is None:
            raise UndoNotApplicable("operation cannot be undone")
        if op.undo_expires_at < datetime.now(UTC):
            raise UndoExpired("operation cannot be undone because the undo window has expired")
        if op.resulting_revision != expected_revision:
            raise StaleRevision("memory changed after operation")
        if op.operation_kind == "set_enabled":
            if op.prior_state and "enabled" in op.prior_state:
                user.creator_memory_enabled = bool(op.prior_state["enabled"])
        elif op.operation_kind == "clear_preferences":
            # Bulk clear is one canonical operation. Restore only the
            # owner-scoped rows recorded by that operation, preserving the
            # same revision/expiry/single-use guarantees as item Undo.
            prior_items = (op.prior_state or {}).get("items", [])
            if not isinstance(prior_items, list):
                raise UndoNotApplicable("clear operation has invalid prior state")
            for prior_item in prior_items:
                if not isinstance(prior_item, dict):
                    continue
                try:
                    item_id = uuid.UUID(str(prior_item["id"]))
                    prior_state = str(prior_item["state"])
                except (KeyError, TypeError, ValueError):
                    raise UndoNotApplicable("clear operation has invalid prior state") from None
                if prior_state not in {"active", "suggested"}:
                    raise UndoNotApplicable("clear operation has invalid prior state")
                item = (
                    await db.execute(
                        select(CreatorMemoryItem)
                        .where(
                            CreatorMemoryItem.id == item_id,
                            CreatorMemoryItem.user_id == user_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if item is not None:
                    item.state = prior_state
        elif op.item_id:
            item = await db.get(CreatorMemoryItem, op.item_id, with_for_update=True)
            if item is not None:
                if op.operation_kind == "create_item":
                    item.state = "forgotten"
                elif op.prior_state:
                    for key, value in op.prior_state.items():
                        if not key.startswith("_"):
                            setattr(item, key, value)
                    superseded_id = op.prior_state.get("_superseded_item_id")
                    if superseded_id:
                        superseded = await db.get(
                            CreatorMemoryItem, uuid.UUID(superseded_id), with_for_update=True
                        )
                        if superseded is not None:
                            superseded.state = op.prior_state.get("_superseded_state", "active")
        op.undone_at = datetime.now(UTC)
        user.creator_memory_revision += 1
        result = CreatorMemoryOperation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation_kind="undo",
            item_id=op.item_id,
            prior_state={"operation_id": str(operation_id)},
            resulting_revision=user.creator_memory_revision,
            actor_kind="user",
        )
        db.add(result)
        await db.flush()
        return result

    @staticmethod
    def _check_revision(user: User, expected: int) -> None:
        if int(user.creator_memory_revision) != int(expected):
            raise StaleRevision("memory revision is stale")
