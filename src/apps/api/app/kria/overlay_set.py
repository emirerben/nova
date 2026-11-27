"""``overlay_set`` thread events: generated overlays asked for, answered and made in chat (CA-7).

One set is one ask. The thread state holds it (``state["overlay_sets"][set_id]``, server
bookkeeping, never in the public thread state) and every change appends one ``overlay_set`` event
carrying the whole set, so the newest event of a set is what the creator last saw and a replay in
order is the history. Events are only written for, and only read by, builds at
``overlay_set_min_client_protocol`` while ``KRIA_SUGGESTION_GENERATED_OVERLAY_ENABLED`` is on.

Phases: ``ask`` (the card "Let Kria decide" / "Describe them"), ``compose`` (the creator's answer
is stored, the plan is not yet), ``progress`` (at least one image overlay is ``making``) and
``done`` (none is; the receipt lists the ``ready`` ids, a ``failed`` row keeps its retry).

Only ``image`` rows of the plan are made. Every paid step is idempotent: the plan is claimed on
the set before the paid call, an image's operation id is derived from (set, overlay, attempt), and
only a failed attempt whose reservation was released starts the next attempt.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CreationThread
from app.services.client_protocol import current_client_protocol
from app.services.suggestions.kinds.generated_overlay import QUOTED_COST_USD

EVENT_TYPE = "overlay_set"
STATE_KEY = "overlay_sets"
SCHEMA_VERSION = 1
MAX_SETS = 8
MAX_PROMPTS = 6
MAX_PROMPT_CHARS = 300
PLAN_CLAIM_TTL = timedelta(minutes=5)
ATTEMPT_NAMESPACE = uuid.UUID("0e6a9c52-7d41-5b38-8f2a-3c1d9b6e4a70")

Phase = Literal["ask", "compose", "progress", "done"]
OverlayState = Literal["making", "ready", "failed"]


class OverlayRow(BaseModel):
    id: str
    title: str
    prompt: str
    state: OverlayState
    operation_id: str | None = None
    error_code: str | None = None
    retryable: bool = False


class OverlaySetEvent(BaseModel):
    """The payload of every ``overlay_set`` event and the body of every route's answer."""

    v: int = SCHEMA_VERSION
    set_id: str
    phase: Phase
    mode: Literal["automatic", "prompt"] | None = None
    prompts: list[str] = Field(default_factory=list)
    unit_price_usd: float = QUOTED_COST_USD
    max_overlays: int = MAX_PROMPTS
    overlays: list[OverlayRow] = Field(default_factory=list)
    overlay_ids: list[str] = Field(default_factory=list)
    message: str = ""


class OverlaySetOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_id: uuid.UUID


class OverlayComposeBody(BaseModel):
    """ "Let Kria decide" is ``automatic``, no rows. "Describe them" is ``prompt``, 1 to 6 rows."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["automatic", "prompt"]
    prompts: list[str] = Field(default_factory=list)

    @field_validator("prompts")
    @classmethod
    def _clean(cls, rows: list[str]) -> list[str]:
        return [" ".join(row.split())[:MAX_PROMPT_CHARS] for row in rows if row.strip()]

    @model_validator(mode="after")
    def _rows_match_mode(self) -> OverlayComposeBody:
        if self.mode == "prompt" and not 1 <= len(self.prompts) <= MAX_PROMPTS:
            raise ValueError(f"Describe 1 to {MAX_PROMPTS} overlays")
        if self.mode == "automatic":
            self.prompts = []
        return self


def build_supported(protocol: int | None) -> bool:
    floor = settings.overlay_set_min_client_protocol
    return bool(
        settings.kria_suggestion_generated_overlay_enabled
        and protocol is not None
        and floor < 999
        and protocol >= floor
    )


def capability(user_id: Any, native_client: bool, protocol: int | None) -> bool:
    from app.routes.generated_media import generated_capabilities  # noqa: PLC0415

    return bool(
        native_client
        and build_supported(protocol)
        and generated_capabilities(user_id, native_client, protocol)["generated_images"]
    )


def visible_events(rows: list[Any]) -> list[Any]:
    """The events the calling build may decode: an older build gets none of this type."""
    if build_supported(current_client_protocol()):
        return rows
    return [row for row in rows if row.event_type != EVENT_TYPE]


def admit(user_id: Any, native_client: bool, protocol: int | None, *, paid: bool = False) -> None:
    if not (
        native_client
        and settings.kria_suggestion_generated_overlay_enabled
        and settings.overlay_set_min_client_protocol < 999
    ):
        raise HTTPException(404, "Overlay generation is unavailable")
    if protocol is None or protocol < settings.overlay_set_min_client_protocol:
        raise HTTPException(426, "Update Kria to continue")
    if paid and not capability(user_id, native_client, protocol):
        raise HTTPException(404, "Generated media is unavailable")


def _phase(rec: dict[str, Any]) -> Phase:
    if rec["compose"] is None:
        return "ask"
    if rec["plan"] is None:
        return "compose"
    return "progress" if any(o["state"] == "making" for o in rec["overlays"]) else "done"


def event_payload(set_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    return OverlaySetEvent(
        set_id=set_id,
        phase=_phase(rec),
        mode=(rec["compose"] or {}).get("mode"),
        prompts=(rec["compose"] or {}).get("prompts", []),
        overlays=[
            OverlayRow(**{k: o[k] for k in OverlayRow.model_fields}) for o in rec["overlays"]
        ],
        overlay_ids=[o["id"] for o in rec["overlays"] if o["state"] == "ready"],
        message=rec["message"],
    ).model_dump(mode="json")


def attempt_event_id(set_id: str, overlay_id: str, attempt: int) -> uuid.UUID:
    return uuid.uuid5(ATTEMPT_NAMESPACE, f"{set_id}:{overlay_id}:{attempt}")


async def _locked(
    db: AsyncSession, thread_id: str, owner_id: uuid.UUID
) -> tuple[CreationThread, dict[str, Any]]:
    try:
        identifier = uuid.UUID(thread_id)
    except ValueError:
        raise HTTPException(404, "Creation thread not found") from None
    thread = (
        await db.execute(
            select(CreationThread)
            .where(CreationThread.id == identifier, CreationThread.creator_id == owner_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(404, "Creation thread not found")
    if thread.status != "active" or thread.active_plan_item_id is None:
        raise HTTPException(409, "This video is not open for overlays")
    return thread, copy.deepcopy((thread.state or {}).get(STATE_KEY) or {})


def _rec(sets: dict[str, Any], set_id: uuid.UUID) -> dict[str, Any]:
    rec = sets.get(str(set_id))
    if rec is None:
        raise HTTPException(404, "Overlay set not found")
    return rec


def _overlay(rec: dict[str, Any], overlay_id: uuid.UUID) -> dict[str, Any]:
    row = next((o for o in rec["overlays"] if o["id"] == str(overlay_id)), None)
    if row is None:
        raise HTTPException(404, "Overlay not found")
    return row


async def _save(
    db: AsyncSession,
    thread: CreationThread,
    sets: dict[str, Any],
    set_id: str,
    *,
    role: Literal["assistant", "user"] = "assistant",
) -> OverlaySetEvent:
    from app.kria.runtime import _append_event  # noqa: PLC0415

    rec = sets[set_id]
    rec["n"] += 1
    payload = event_payload(set_id, rec)
    await _append_event(
        db,
        thread,
        role=role,
        event_type=EVENT_TYPE,
        content=None,
        payload=payload,
        client_event_id=f"{EVENT_TYPE}:{set_id}:{rec['n']}",
    )
    while len(sets) > MAX_SETS:
        del sets[min(sets, key=lambda key: sets[key]["opened_at"])]
    thread.state = {**(thread.state or {}), STATE_KEY: sets}
    await db.commit()
    return OverlaySetEvent.model_validate(payload)


def _view(set_id: str, rec: dict[str, Any]) -> OverlaySetEvent:
    return OverlaySetEvent.model_validate(event_payload(set_id, rec))


async def open_set(
    db: AsyncSession, thread_id: str, owner_id: uuid.UUID, set_id: uuid.UUID
) -> OverlaySetEvent:
    thread, sets = await _locked(db, thread_id, owner_id)
    key = str(set_id)
    if key in sets:
        return _view(key, sets[key])
    sets[key] = {
        "opened_at": datetime.now(UTC).isoformat(),
        "n": 0,
        "compose": None,
        "plan": None,
        "claim": None,
        "message": "",
        "overlays": [],
    }
    return await _save(db, thread, sets, key)


async def answer_compose(
    db: AsyncSession,
    thread_id: str,
    owner_id: uuid.UUID,
    set_id: uuid.UUID,
    body: OverlayComposeBody,
) -> OverlaySetEvent:
    thread, sets = await _locked(db, thread_id, owner_id)
    rec = _rec(sets, set_id)
    answer = {"mode": body.mode, "prompts": body.prompts}
    if rec["compose"] is not None:
        if rec["compose"] != answer:
            raise HTTPException(409, "This set was already answered")
        return _view(str(set_id), rec)
    rec["compose"] = answer
    return await _save(db, thread, sets, str(set_id), role="user")


async def claim_plan(
    db: AsyncSession, thread_id: str, owner_id: uuid.UUID, set_id: uuid.UUID
) -> tuple[dict[str, Any] | None, dict[str, Any], uuid.UUID]:
    """The stored plan (a duplicate pays nothing), or the compose answer to plan from.

    The claim is committed before the paid call and never held across it. A set that has a
    claim younger than ``PLAN_CLAIM_TTL`` is being planned by another request.
    """
    thread, sets = await _locked(db, thread_id, owner_id)
    rec = _rec(sets, set_id)
    if rec["compose"] is None:
        raise HTTPException(409, "Answer the question first")
    if rec["plan"] is not None:
        return rec["plan"], rec["compose"], thread.active_plan_item_id
    claimed = rec["claim"]
    if claimed and datetime.now(UTC) - datetime.fromisoformat(claimed) < PLAN_CLAIM_TTL:
        raise HTTPException(409, "Overlays are already being planned")
    rec["claim"] = datetime.now(UTC).isoformat()
    thread.state = {**(thread.state or {}), STATE_KEY: sets}
    item_id = thread.active_plan_item_id
    await db.commit()
    return None, rec["compose"], item_id


async def release_plan_claim(
    db: AsyncSession, thread_id: str, owner_id: uuid.UUID, set_id: uuid.UUID
) -> None:
    thread, sets = await _locked(db, thread_id, owner_id)
    _rec(sets, set_id)["claim"] = None
    thread.state = {**(thread.state or {}), STATE_KEY: sets}
    await db.commit()


async def record_plan(
    db: AsyncSession,
    thread_id: str,
    owner_id: uuid.UUID,
    set_id: uuid.UUID,
    plan: dict[str, Any],
) -> OverlaySetEvent:
    thread, sets = await _locked(db, thread_id, owner_id)
    rec = _rec(sets, set_id)
    rec["claim"] = None
    rec["plan"] = plan
    rec["message"] = plan.get("message", "")
    rec["overlays"] = [
        {
            "id": row["id"],
            "title": row["title"],
            "prompt": row["prompt"],
            "state": "making",
            "attempt": 0,
            "operation_id": None,
            "error_code": None,
            "retryable": False,
        }
        for row in plan["overlays"]
        if row["kind"] == "image"
    ]
    return await _save(db, thread, sets, str(set_id))


async def begin_generation(
    db: AsyncSession,
    thread_id: str,
    owner_id: uuid.UUID,
    set_id: uuid.UUID,
    overlay_id: uuid.UUID,
) -> tuple[OverlaySetEvent | None, str, int, uuid.UUID]:
    """``(receipt, prompt, attempt, item_id)``: a ready overlay returns its set and nothing to make.

    A ``failed`` overlay whose attempt released its reservation starts the next attempt and shows
    ``making``. An overlay already ``making`` repeats its own attempt, which finds its operation.
    """
    thread, sets = await _locked(db, thread_id, owner_id)
    rec = _rec(sets, set_id)
    row = _overlay(rec, overlay_id)
    item_id = thread.active_plan_item_id
    if row["state"] == "ready":
        return _view(str(set_id), rec), row["prompt"], row["attempt"], item_id
    if row["state"] == "failed":
        if not row["retryable"]:
            raise HTTPException(409, "This overlay cannot be made again")
        row.update(state="making", attempt=row["attempt"] + 1, error_code=None, retryable=False)
        await _save(db, thread, sets, str(set_id))
    return None, row["prompt"], row["attempt"], item_id


async def finish_generation(
    db: AsyncSession,
    thread_id: str,
    owner_id: uuid.UUID,
    set_id: uuid.UUID,
    overlay_id: uuid.UUID,
    attempt: int,
    *,
    state: str,
    operation_id: uuid.UUID | None,
    failure_code: str | None,
) -> OverlaySetEvent:
    """Settle one attempt. ``failed`` is retryable (the reservation was released), ``unknown`` is
    not (the provider may have been paid), and an operation still in flight stays ``making``."""
    thread, sets = await _locked(db, thread_id, owner_id)
    rec = _rec(sets, set_id)
    row = _overlay(rec, overlay_id)
    settled = {"ready": "ready", "failed": "failed", "unknown": "failed"}.get(state)
    if row["attempt"] != attempt or row["state"] != "making" or settled is None:
        return _view(str(set_id), rec)
    row.update(
        state=settled,
        operation_id=str(operation_id) if operation_id else None,
        error_code=None if settled == "ready" else failure_code or "generation_failed",
        retryable=state == "failed",
    )
    return await _save(db, thread, sets, str(set_id))
