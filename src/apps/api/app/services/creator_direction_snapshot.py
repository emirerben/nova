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
    Persona,
    ProjectDirectionOverride,
    User,
)
from app.services.creator_direction import (
    COMPATIBILITY_INPUT_VERSION,
    MAX_ACTIVE_ITEMS,
    MAX_TOTAL_INSTRUCTION_CHARS,
    CreatorDirectionResolver,
    CreatorDirectionSnapshot,
)
from app.services.creator_direction_capabilities import capability_status
from app.services.tiktok_style_observations import persona_style_expires_at

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


def _without_compatibility(snapshot: CreatorDirectionSnapshot) -> CreatorDirectionSnapshot:
    return CreatorDirectionSnapshot(
        enabled=snapshot.enabled,
        revision=snapshot.revision,
        items=snapshot.items,
        overrides=snapshot.overrides,
        compatibility_items=(),
        compatibility_input_version=snapshot.compatibility_input_version,
        compatibility_expires_at=None,
    )


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


def _snapshot_rows(snapshot: CreatorDirectionSnapshot) -> list[dict[str, Any]]:
    return [
        row
        for row in (
            *snapshot.compatibility_items,
            *snapshot.items,
            *snapshot.overrides,
        )
        if isinstance(row, dict)
    ]


def _capability_results(snapshot: CreatorDirectionSnapshot) -> list[dict[str, Any]]:
    """Persist capability receipts without copying instruction text."""

    results: list[dict[str, Any]] = []
    scoped_rows = (
        [(row, "compatibility") for row in snapshot.compatibility_items]
        + [(row, "account") for row in snapshot.items]
        + [(row, "project") for row in snapshot.overrides]
    )
    for row, scope in scoped_rows:
        item_id = row.get("id")
        if not item_id:
            continue
        normalized_key = row.get("normalized_key")
        conflict = row.get("conflicted") is True or bool(row.get("conflict_id"))
        results.append(
            {
                "id": str(item_id),
                "normalized_key": str(normalized_key) if normalized_key else None,
                "scope": scope,
                "status": capability_status(
                    normalized_key,
                    enforcement=str(row.get("enforcement") or "advisory"),
                    conflicted=conflict,
                ),
                "enforcement": str(row.get("enforcement") or "advisory"),
            }
        )
    return results


def _conflict_ids(snapshot: CreatorDirectionSnapshot) -> list[str]:
    ids: list[str] = []
    for row in _snapshot_rows(snapshot):
        conflict_id = row.get("conflict_id")
        if conflict_id:
            ids.append(str(conflict_id))
        for value in row.get("conflict_ids") or ():
            if value:
                ids.append(str(value))
    return list(dict.fromkeys(ids))


def _redacted_context_sections(snapshot: CreatorDirectionSnapshot) -> dict[str, dict[str, Any]]:
    sections: dict[str, dict[str, Any]] = {}
    for section, instructions in snapshot.context_sections.items():
        ids = [
            str(row.get("id"))
            for row in _snapshot_rows(snapshot)
            if row.get("id") and row.get("instruction") in instructions
        ]
        sections[section] = {"item_ids": ids, "count": len(instructions)}
    return sections


def _project_override_values(snapshot: CreatorDirectionSnapshot) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for row in snapshot.overrides:
        key = row.get("normalized_key")
        structured = row.get("structured_value")
        if key and isinstance(structured, dict):
            value = structured.get(str(key)) if len(structured) == 1 else structured
            values[str(key)] = _json_safe(value)
    return values


def serialize_snapshot(
    snapshot: CreatorDirectionSnapshot,
    *,
    source: str,
    generation_id: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Serialize a resolved snapshot without persisting raw memory text."""

    without_compatibility = _without_compatibility(snapshot)
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
            "compatibility_item_ids": [
                str(row.get("id"))
                for row in snapshot.compatibility_items
                if isinstance(row, dict) and row.get("id")
            ],
            "typed_overrides": typed,
            "context_sections": _redacted_context_sections(snapshot),
            "capability_results": _capability_results(snapshot),
            "conflict_ids": _conflict_ids(snapshot),
            "project_override_values": _project_override_values(snapshot),
            "compatibility_input_version": snapshot.compatibility_input_version,
            # Compatibility style derived from retained observations is the
            # only part of an otherwise immutable direction snapshot that has
            # a lifecycle deadline. The no-compatibility projections let every
            # later consumer expire it without consulting mutable Persona rows
            # or discarding creator-authored memory/project overrides.
            "compatibility_expires_at": _isoformat(snapshot.compatibility_expires_at),
            "typed_overrides_without_compatibility": (
                _json_safe(without_compatibility.typed_overrides) or {}
            ),
            "context_sections_without_compatibility": _redacted_context_sections(
                without_compatibility
            ),
            "capability_results_without_compatibility": _capability_results(without_compatibility),
            "prompt_hash_without_compatibility": _prompt_hash(without_compatibility),
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
    without_compatibility = _without_compatibility(snapshot)
    result["prompt_block"] = snapshot.prompt_block
    result["context_sections"] = copy.deepcopy(snapshot.context_sections)
    result["prompt_block_without_compatibility"] = without_compatibility.prompt_block
    result["context_sections_without_compatibility"] = copy.deepcopy(
        without_compatibility.context_sections
    )
    return result


def _effective_snapshot(value: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Expire observation-derived compatibility while preserving pinned intent."""

    result = copy.deepcopy(value)
    original_creator_context = (
        (result.get("context_sections") or {}).get("creator_context")
        if isinstance(result.get("context_sections"), dict)
        else None
    )
    legacy_without_deadline = "compatibility_expires_at" not in result
    has_compatibility = bool(result.get("compatibility_item_ids")) or any(
        isinstance(row, dict) and row.get("scope") == "compatibility"
        for row in result.get("capability_results") or ()
    )
    # V1 snapshots were deployed before observation provenance/deadlines were
    # serialized. There is no defensible way to distinguish an edited Persona
    # style from an observation-derived one in those immutable rows, so fail
    # closed only for their compatibility projection. Account memory and
    # project overrides remain pinned below.
    if legacy_without_deadline and not has_compatibility:
        return result
    raw_expires_at = result.get("compatibility_expires_at")
    # Explicit null is a new snapshot whose compatibility input is edited or
    # did not consume observations, and therefore has no deadline.
    if not legacy_without_deadline and raw_expires_at is None:
        return result
    if legacy_without_deadline:
        expires_at = datetime.min.replace(tzinfo=UTC)
    else:
        try:
            expires_at = datetime.fromisoformat(str(raw_expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            # A marked-but-malformed deadline is not authority to retain
            # derived observation data indefinitely. Treat it as expired.
            expires_at = datetime.min.replace(tzinfo=UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    if reference < expires_at:
        return result

    typed_without = result.get("typed_overrides_without_compatibility")
    if isinstance(typed_without, dict):
        result["typed_overrides"] = copy.deepcopy(typed_without)
    else:
        compatibility_keys = {
            str(row.get("normalized_key"))
            for row in result.get("capability_results") or ()
            if isinstance(row, dict)
            and row.get("scope") == "compatibility"
            and row.get("normalized_key")
        }
        compatibility_keys.update(
            suffix
            for item_id in result.get("compatibility_item_ids") or ()
            if isinstance(item_id, str)
            and item_id.startswith("compatibility-style-")
            and (suffix := item_id.removeprefix("compatibility-style-")) != "set"
        )
        safe_typed = {
            key: item
            for key, item in (result.get("typed_overrides") or {}).items()
            if key not in compatibility_keys
        }
        # Legacy receipts do persist exact project override values, so those
        # can safely win the same key again. Account capability receipts do
        # not say whether an item carried a structured value; merely sharing a
        # normalized key therefore cannot prove that the combined typed value
        # did not originate from expired compatibility.
        project_values = result.get("project_override_values")
        if isinstance(project_values, dict):
            for key in compatibility_keys & project_values.keys():
                safe_typed[key] = copy.deepcopy(project_values[key])
        result["typed_overrides"] = safe_typed

    capability_without = result.get("capability_results_without_compatibility")
    if isinstance(capability_without, list):
        result["capability_results"] = copy.deepcopy(capability_without)
    else:
        result["capability_results"] = [
            copy.deepcopy(row)
            for row in result.get("capability_results") or ()
            if isinstance(row, dict) and row.get("scope") != "compatibility"
        ]
    result["compatibility_item_ids"] = []

    sections_without = result.get("context_sections_without_compatibility")
    if isinstance(sections_without, dict):
        result["context_sections"] = copy.deepcopy(sections_without)
    elif isinstance(result.get("context_sections"), dict):
        sections = copy.deepcopy(result["context_sections"])
        creator_context = sections.get("creator_context")
        sections["creator_context"] = (
            {"item_ids": [], "count": 0} if isinstance(creator_context, dict) else []
        )
        result["context_sections"] = sections

    prompt_without = result.get("prompt_block_without_compatibility")
    if isinstance(prompt_without, str):
        result["prompt_block"] = prompt_without
    elif "prompt_block" in result:
        # Legacy private snapshots preserve the compatibility instructions in
        # the dedicated creator_context section. Remove those exact bullet
        # lines and retain account/project instructions. A malformed producer
        # without that separation fails closed for the whole prompt.
        if isinstance(original_creator_context, list):
            compatibility_lines = [
                f"- {instruction.strip()}"
                for instruction in original_creator_context
                if isinstance(instruction, str) and instruction.strip()
            ]
            prompt_lines = str(result.get("prompt_block") or "").splitlines()
            for compatibility_line in compatibility_lines:
                if compatibility_line in prompt_lines:
                    prompt_lines.remove(compatibility_line)
            result["prompt_block"] = "\n".join(prompt_lines)
        else:
            result["prompt_block"] = ""
    prompt_without_hash = result.get("prompt_hash_without_compatibility")
    if prompt_without_hash is not None:
        result["prompt_hash"] = prompt_without_hash
    elif "prompt_block" in result:
        safe_prompt = str(result.get("prompt_block") or "")
        result["prompt_hash"] = (
            hashlib.sha256(safe_prompt.encode("utf-8")).hexdigest() if safe_prompt else None
        )
    else:
        # Legacy public snapshots contain no prompt text from which a safe
        # non-compatibility fingerprint can be reconstructed. Keeping the old
        # hash would still expose compatibility-derived state after expiry.
        result["prompt_hash"] = None
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


def snapshot_from_container(
    container: Any, *, now: datetime | None = None
) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    candidate = container.get(SNAPSHOT_KEY)
    return _effective_snapshot(candidate, now=now) if is_snapshot(candidate) else None


def private_snapshot_is_valid(value: Any) -> bool:
    return is_snapshot(value) and isinstance(value.get("prompt_block"), str)


def private_snapshot_from(value: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    return _effective_snapshot(value, now=now) if private_snapshot_is_valid(value) else None


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
    persona = db.execute(select(Persona).where(Persona.user_id == user_id)).scalar_one_or_none()
    compatibility_items = CreatorDirectionResolver._compatibility_items(persona)
    compatibility_expires_at = (
        persona_style_expires_at(
            getattr(persona, "style", None),
            profile=getattr(persona, "tiktok_profile", None),
        )
        if compatibility_items
        else None
    )
    return CreatorDirectionSnapshot(
        enabled,
        revision,
        tuple(items if enabled else ()),
        tuple(overrides if enabled else ()),
        tuple(compatibility_items if enabled else ()),
        COMPATIBILITY_INPUT_VERSION,
        compatibility_expires_at if enabled else None,
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
    else:
        # Persist the lifecycle-safe projection when a previously pinned
        # snapshot crosses its compatibility deadline before retry/reburn.
        current[SNAPSHOT_KEY] = existing
        job.assembly_plan = current
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
    else:
        current[SNAPSHOT_KEY] = existing
        job.assembly_plan = current
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
