"""Immutable creator-direction snapshots and renderer adapters.

The direction tables are mutable.  Jobs, plans, and chat receipts are not: once
one of those enters a generation attempt it must keep the same resolved
direction even when the creator edits Memory in another tab.  This module is
the deliberately small boundary between those two lifecycles.

Only bounded identifiers and typed values are exposed in public projections. The
natural-language memory text is used by private worker-side prompts, but is never
copied into a public ``Job.assembly_plan`` projection or creation-thread receipt.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import uuid
from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CreatorMemoryItem,
    ProjectDirectionOverride,
    User,
)
from app.services.creator_direction import (
    MAX_ACTIVE_ITEMS,
    MAX_TOTAL_INSTRUCTION_CHARS,
    CreatorDirectionResolver,
    CreatorDirectionSnapshot,
)

SNAPSHOT_KEY = "_creator_direction_snapshot_v1"
SNAPSHOT_SCHEMA = "CreatorDirectionSnapshotV1"
SNAPSHOT_VERSION = 1
RESOLVER_VERSION = "creator-direction-resolver-v1"
_CURRENT_TYPED_OVERRIDES: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "creator_direction_typed_overrides", default={}
)
_RENDERER_POLICY_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "creator_direction_renderer_policy_scope", default=False
)


@dataclass(frozen=True)
class CreatorDirectionSnapshotV1:
    """Named immutable envelope used by jobs, plans, and chat receipts."""

    value: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.value)

    @property
    def typed_overrides(self) -> dict[str, Any]:
        raw = self.value.get("typed_overrides")
        return dict(raw) if isinstance(raw, dict) else {}


def _json_safe(value: Any) -> Any:
    """Return a bounded JSON-compatible copy of typed direction data."""

    try:
        return json.loads(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return None


def _prompt_hash(snapshot: CreatorDirectionSnapshot) -> str | None:
    if not snapshot.prompt_block:
        return None
    return hashlib.sha256(snapshot.prompt_block.encode("utf-8")).hexdigest()


def serialize_snapshot(
    snapshot: CreatorDirectionSnapshot,
    *,
    source: str,
    generation_id: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Serialize a resolved snapshot without persisting raw memory text."""

    typed = _json_safe(snapshot.typed_overrides) or {}
    rows = [row for row in snapshot.items if isinstance(row, dict)]
    overrides = [row for row in snapshot.overrides if isinstance(row, dict)]
    return CreatorDirectionSnapshotV1(
        {
            "schema": SNAPSHOT_SCHEMA,
            "version": SNAPSHOT_VERSION,
            "snapshot_id": snapshot_id or uuid.uuid4().hex,
            "resolver_version": RESOLVER_VERSION,
            "source": str(source)[:80],
            "created_at": datetime.now(UTC).isoformat(),
            "memory_revision": int(snapshot.revision),
            "enabled": bool(snapshot.enabled),
            "applied_item_ids": [str(row.get("id")) for row in rows if row.get("id")],
            "applied_override_ids": [str(row.get("id")) for row in overrides if row.get("id")],
            "typed_overrides": typed,
            "prompt_hash": _prompt_hash(snapshot),
            "generation_id": str(generation_id) if generation_id else None,
        }
    ).to_dict()


def serialize_private_snapshot(
    snapshot: CreatorDirectionSnapshot,
    *,
    source: str,
    generation_id: str | None = None,
) -> dict[str, Any]:
    """Serialize the full prompt-bearing snapshot for private DB columns."""

    result = serialize_snapshot(snapshot, source=source, generation_id=generation_id)
    result["prompt_block"] = snapshot.prompt_block
    return result


def is_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema") == SNAPSHOT_SCHEMA
        and value.get("version") == SNAPSHOT_VERSION
        and isinstance(value.get("snapshot_id"), str)
        and isinstance(value.get("memory_revision"), int)
        and isinstance(value.get("typed_overrides"), dict)
    )


def snapshot_from_container(container: Any) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    candidate = container.get(SNAPSHOT_KEY)
    return copy.deepcopy(candidate) if is_snapshot(candidate) else None


def private_snapshot_is_valid(value: Any) -> bool:
    return is_snapshot(value) and isinstance(value.get("prompt_block"), str)


def private_snapshot_from(value: Any) -> dict[str, Any] | None:
    return copy.deepcopy(value) if private_snapshot_is_valid(value) else None


@contextmanager
def renderer_policy_scope():
    """Bind renderer policy for one task and always restore the prior context.

    Celery workers execute many jobs in one process.  A bare ``ContextVar.set``
    therefore leaks a prior user's typed direction into the next job (and into
    non-job helper calls).  Task entry points use this scope; all policy reads
    outside it intentionally behave as the empty legacy policy.
    """

    scope_token = _RENDERER_POLICY_SCOPE.set(True)
    policy_token = _CURRENT_TYPED_OVERRIDES.set({})
    try:
        yield
    finally:
        _CURRENT_TYPED_OVERRIDES.reset(policy_token)
        _RENDERER_POLICY_SCOPE.reset(scope_token)


def bind_typed_overrides(typed_overrides: Any) -> None:
    """Bind a task's immutable typed policy when inside ``renderer_policy_scope``."""

    if not _RENDERER_POLICY_SCOPE.get():
        return
    policy = typed_overrides if isinstance(typed_overrides, dict) else {}
    _CURRENT_TYPED_OVERRIDES.set(copy.deepcopy(policy))


def attach_private_snapshot(
    existing: Any,
    snapshot: CreatorDirectionSnapshot,
    *,
    source: str,
    generation_id: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    prior = private_snapshot_from(existing)
    if prior is None and isinstance(existing, dict):
        prior = private_snapshot_from(existing.get(SNAPSHOT_KEY))
    if prior is not None and not replace:
        return prior
    return serialize_private_snapshot(snapshot, source=source, generation_id=generation_id)


def attach_snapshot(
    container: MutableMapping[str, Any] | None,
    snapshot: CreatorDirectionSnapshot,
    *,
    source: str,
    generation_id: str | None = None,
) -> dict[str, Any]:
    """Attach once; retries/reburns reuse the exact immutable object."""

    result = dict(container or {})
    existing = snapshot_from_container(result)
    if existing is not None:
        result[SNAPSHOT_KEY] = existing
        return result
    result[SNAPSHOT_KEY] = serialize_snapshot(snapshot, source=source, generation_id=generation_id)
    return result


def _sync_snapshot(
    db: Any, user_id: uuid.UUID, *, thread_id: uuid.UUID | None = None
) -> CreatorDirectionSnapshot:
    """Sync counterpart used by Celery tasks and render dispatch."""

    user = db.get(User, user_id)
    enabled = bool(user.creator_memory_enabled) if user is not None else False
    revision = int(user.creator_memory_revision) if user is not None else 0
    rows = list(
        db.execute(
            select(CreatorMemoryItem)
            .where(CreatorMemoryItem.user_id == user_id, CreatorMemoryItem.state == "active")
            .order_by(CreatorMemoryItem.updated_at.desc())
            .limit(MAX_ACTIVE_ITEMS)
        ).scalars()
    )
    total = 0
    items: list[dict[str, Any]] = []
    for row in rows:
        total += len(row.instruction)
        if total > MAX_TOTAL_INSTRUCTION_CHARS:
            break
        items.append(CreatorDirectionResolver._item(row))
    overrides: list[dict[str, Any]] = []
    if thread_id is not None:
        overrides = [
            CreatorDirectionResolver._override(row)
            for row in db.execute(
                select(ProjectDirectionOverride).where(
                    ProjectDirectionOverride.user_id == user_id,
                    ProjectDirectionOverride.thread_id == thread_id,
                )
            ).scalars()
        ]
    return CreatorDirectionSnapshot(
        enabled,
        revision,
        tuple(items if enabled else ()),
        tuple(overrides if enabled else ()),
    )


def _safe_sync_snapshot(
    db: Any, user_id: uuid.UUID | None, *, thread_id: uuid.UUID | None = None
) -> CreatorDirectionSnapshot:
    if user_id is None:
        return CreatorDirectionSnapshot(enabled=False, revision=0, items=(), overrides=())
    try:
        return _sync_snapshot(db, user_id, thread_id=thread_id)
    except Exception:
        # Legacy jobs and mixed-version workers must keep rendering even when
        # personalization storage is absent or temporarily unavailable.
        return CreatorDirectionSnapshot(enabled=False, revision=0, items=(), overrides=())


def resolve_snapshot_sync(
    db: Any, user_id: uuid.UUID, *, thread_id: uuid.UUID | None = None
) -> CreatorDirectionSnapshot:
    return _sync_snapshot(db, user_id, thread_id=thread_id)


async def resolve_snapshot(
    db: Any, user_id: uuid.UUID, *, thread_id: uuid.UUID | None = None
) -> CreatorDirectionSnapshot:
    try:
        return await CreatorDirectionResolver().snapshot(db, user_id, thread_id=thread_id)
    except Exception:
        # Personalization is additive. A DB/read-replica outage must not block
        # creating a project; the worker will use the empty pinned snapshot.
        return CreatorDirectionSnapshot(enabled=False, revision=0, items=(), overrides=())


async def resolve_snapshot_for_dispatch(
    db: Any, user_id: uuid.UUID, *, thread_id: uuid.UUID | None = None
) -> CreatorDirectionSnapshot:
    if not isinstance(db, AsyncSession):
        return CreatorDirectionSnapshot(enabled=False, revision=0, items=(), overrides=())
    return await resolve_snapshot(db, user_id, thread_id=thread_id)


def ensure_job_snapshot(
    db: Any,
    job: Any,
    *,
    source: str,
    thread_id: uuid.UUID | None = None,
    generation_id: str | None = None,
    inherited_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stamp a Job's private assembly plan, preserving an existing snapshot."""

    current = dict(getattr(job, "assembly_plan", None) or {})
    existing = snapshot_from_container(current)
    if existing is None:
        inherited = private_snapshot_from(inherited_snapshot)
        if inherited is not None:
            current[SNAPSHOT_KEY] = inherited
        else:
            current = attach_snapshot(
                current,
                _safe_sync_snapshot(db, getattr(job, "user_id", None), thread_id=thread_id),
                source=source,
                generation_id=generation_id,
            )
        job.assembly_plan = current
        existing = current[SNAPSHOT_KEY]
    bind_typed_overrides(existing.get("typed_overrides"))
    return existing


async def ensure_job_snapshot_async(
    db: Any,
    job: Any,
    *,
    source: str,
    thread_id: uuid.UUID | None = None,
    generation_id: str | None = None,
    inherited_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Async dispatch counterpart used by FastAPI job creators."""

    current = dict(getattr(job, "assembly_plan", None) or {})
    existing = snapshot_from_container(current)
    if existing is None:
        inherited = private_snapshot_from(inherited_snapshot)
        if inherited is not None:
            current[SNAPSHOT_KEY] = inherited
        else:
            resolved = (
                await resolve_snapshot(db, user_id, thread_id=thread_id)
                if (user_id := getattr(job, "user_id", None)) is not None
                else CreatorDirectionSnapshot(enabled=False, revision=0, items=(), overrides=())
            )
            # Direct API-created jobs need the prompt-bearing snapshot for their
            # private worker-side agent inputs. Public job projections redact the
            # entire snapshot, so this does not expose creator instructions.
            current[SNAPSHOT_KEY] = attach_private_snapshot(
                current,
                resolved,
                source=source,
                generation_id=generation_id,
            )
        job.assembly_plan = current
        existing = current[SNAPSHOT_KEY]
    bind_typed_overrides(existing.get("typed_overrides"))
    return existing


def typed_overrides_from_container(container: Any) -> dict[str, Any]:
    snap = snapshot_from_container(container)
    typed = snap.get("typed_overrides") if snap else {}
    return dict(typed) if isinstance(typed, dict) else {}


def current_typed_overrides() -> dict[str, Any]:
    """Return the task-local immutable policy for renderer adapters."""

    if not _RENDERER_POLICY_SCOPE.get():
        return {}
    return dict(_CURRENT_TYPED_OVERRIDES.get() or {})


def apply_direction_overrides(
    overlays: Any,
    *,
    typed_overrides: dict[str, Any] | None = None,
) -> Any:
    """Apply typed render policy to either renderer's overlay payload.

    The copy is intentional: callers may reuse persisted overlay JSON for a
    later edit, while this adapter enforces an immutable generation policy.
    """

    policy = typed_overrides if typed_overrides is not None else current_typed_overrides()
    if not isinstance(overlays, list):
        return overlays
    shadow = policy.get("shadow_enabled")
    font_family = policy.get("font_family")
    if shadow is not False and not isinstance(font_family, str):
        return overlays
    result: list[Any] = []
    for overlay in overlays:
        if isinstance(overlay, dict):
            row = dict(overlay)
            if shadow is False:
                row["shadow_enabled"] = False
            if isinstance(font_family, str) and font_family.strip():
                row["font_family"] = font_family.strip()
                row["font_cycling"] = False
                row["cycle_fonts"] = []
            result.append(row)
        else:
            result.append(overlay)
    return result


__all__ = [
    "SNAPSHOT_KEY",
    "SNAPSHOT_SCHEMA",
    "CreatorDirectionSnapshot",
    "CreatorDirectionSnapshotV1",
    "apply_direction_overrides",
    "attach_snapshot",
    "attach_private_snapshot",
    "bind_typed_overrides",
    "ensure_job_snapshot",
    "ensure_job_snapshot_async",
    "is_snapshot",
    "private_snapshot_from",
    "private_snapshot_is_valid",
    "resolve_snapshot",
    "resolve_snapshot_for_dispatch",
    "resolve_snapshot_sync",
    "serialize_snapshot",
    "serialize_private_snapshot",
    "snapshot_from_container",
    "typed_overrides_from_container",
    "current_typed_overrides",
    "renderer_policy_scope",
]
