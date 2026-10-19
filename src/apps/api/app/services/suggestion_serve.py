"""The read path behind ``GET /creation-threads/{id}/suggestions`` (KRI-756).

Never calls a model. It recomputes the deterministic candidates, looks up the wording the
worker cached for this input digest, joins them, and decides what the creator is served:

* understanding still running           -> ``pending``, ``default_edit`` only, retry in 3 s
* understanding settled-empty           -> ``ok``, ``default_edit`` only, template label
* wording cached (agent or fallback)    -> ``ok``, labels from the cache, FROZEN for the digest
* wording not cached yet                -> ``pending`` (``default_edit`` only at first_prompt,
                                           nothing at ready) for at most 6 s after the digest
                                           was first seen, then template labels, FROZEN
* kill lever (max runs per thread = 0)  -> template labels immediately

"Frozen" means the final set is stored once per digest (``suggestion_store.put_served``) and
every later GET returns it unchanged, so a chip's label never changes under the creator's
finger. A new ``set_id`` appears only when the input digest changes. A Redis outage degrades to
template labels without freezing.

Also owns the enqueue helpers the triggers call (clip understanding done, render ready, lazy
from this GET). They are enqueue-only and never raise.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import settings
from app.services import suggestion_store as store
from app.services.suggestion_chips import (
    PENDING_RETRY_S,
    WORDING_RETRY_S,
    WORDING_WAIT_S,
    CandidatePlan,
    SuggestionContext,
    SuggestionSetOut,
    assemble_set,
    build_candidates,
    load_suggestion_context,
    wording_from_dict,
)

log = structlog.get_logger()

SUGGESTER_QUEUE = "agent-control"


@dataclass
class Served:
    out: SuggestionSetOut
    media_digest: str
    input_digest: str
    # kind -> witness tier (strategy | editor | policy_only): telemetry only, not on the wire.
    witness_tiers: dict[str, str] = field(default_factory=dict)


def agent_enabled() -> bool:
    """False when ``KRIA_SUGGESTER_MAX_RUNS_PER_THREAD=0``: template labels only."""
    return settings.kria_suggester_max_runs_per_thread > 0


def needs_wording(plan: CandidatePlan) -> bool:
    """Only a set with at least one lever has anything for the agent to rank."""
    return bool(plan.levers)


def enqueue_suggester(
    thread_id: str,
    *,
    trigger: str,
    creator_id: object = None,
    expected_digest: str | None = None,
    language: str | None = None,
    protocol: int | None = None,
) -> bool:
    """Fire-and-forget. Re-checks the flag and the kill lever; never raises."""
    try:
        if creator_id is not None and not settings.suggestion_chips_for(creator_id):
            return False
        if not agent_enabled():
            return False
        from app.tasks.kria_suggester import kria_suggester  # noqa: PLC0415

        kria_suggester.apply_async(
            kwargs={
                "thread_id": str(thread_id),
                "trigger": trigger,
                "expected_digest": expected_digest,
                "language": language,
                "protocol": protocol,
            },
            queue=SUGGESTER_QUEUE,
            expires=600,
        )
        return True
    except Exception:  # noqa: BLE001 - chips are an enhancement
        log.warning("suggestion_chips.enqueue_failed", thread_id=str(thread_id), exc_info=True)
        return False


def enqueue_for_item(item_id: object, *, trigger: str) -> int:
    """Enqueue a run for every active v2 thread on a plan item. Never raises."""
    if not (settings.kria_suggestion_chips_enabled or settings.kria_suggestion_chips_user_ids):
        return 0  # flag off for everyone: not even a lookup
    try:
        from sqlalchemy import select  # noqa: PLC0415

        from app.database import sync_session  # noqa: PLC0415
        from app.models import CreationThread  # noqa: PLC0415

        with sync_session() as db:
            rows = db.execute(
                select(CreationThread.id, CreationThread.creator_id).where(
                    CreationThread.active_plan_item_id == item_id,
                    CreationThread.status == "active",
                    CreationThread.runtime_version == 2,
                )
            ).all()
        return sum(
            1
            for thread_id, creator_id in rows
            if enqueue_suggester(str(thread_id), trigger=trigger, creator_id=creator_id)
        )
    except Exception:  # noqa: BLE001
        log.warning("suggestion_chips.enqueue_failed", trigger=trigger, exc_info=True)
        return 0


def enqueue_for_thread(thread_id: object, *, trigger: str, creator_id: object = None) -> bool:
    """Enqueue one run (the render-ready trigger). Never raises."""
    return enqueue_suggester(str(thread_id), trigger=trigger, creator_id=creator_id)


def _final(
    ctx: SuggestionContext, plan: CandidatePlan, result: dict[str, Any] | None
) -> SuggestionSetOut:
    wording = None
    if result and result.get("status") == "ok":
        wording = wording_from_dict(result.get("wording"))
    return assemble_set(ctx, plan, wording)


async def _freeze(thread_id: str, plan: CandidatePlan, out: SuggestionSetOut) -> SuggestionSetOut:
    """Store the final set once per digest; return whichever set won the race."""
    try:
        winner = await asyncio.to_thread(
            store.put_served, thread_id, plan.input_digest, out.model_dump(mode="json")
        )
        return SuggestionSetOut.model_validate(winner)
    except Exception:  # noqa: BLE001 - Redis down or a corrupt entry: serve what we built
        return out


async def serve_suggestion_set(
    db: Any,
    thread: Any,
    user: Any,
    client_protocol: int | None,
    *,
    locale: str | None = None,
) -> Served:
    """Build what to serve for this thread right now. Wait-free; never calls a model."""
    thread_id = str(thread.id)
    hint = await asyncio.to_thread(store.get_hint, thread_id)
    ctx = await load_suggestion_context(
        db,
        thread,
        user,
        client_protocol,
        locale=locale,
        language_hint=hint.get("language") if isinstance(hint, dict) else None,
    )
    plan = build_candidates(ctx)
    served = Served(
        out=SuggestionSetOut(),
        media_digest=ctx.media_digest,
        input_digest=plan.input_digest,
        witness_tiers={
            str(c.kind): c.witness_tier
            for c in ((plan.default,) if plan.default else ()) + plan.levers
        },
    )
    # The worker has no request headers: tell it the language and protocol this client uses.
    await asyncio.to_thread(
        store.put_hint, thread_id, language=ctx.language, protocol=client_protocol
    )

    if plan.state == "none":
        served.out = assemble_set(ctx, plan)
        return served
    if plan.state == "first_prompt" and ctx.understanding == "pending":
        served.out = assemble_set(ctx, plan, None, pending=True, retry_after_s=PENDING_RETRY_S)
        return served
    if not needs_wording(plan) or not agent_enabled():
        served.out = assemble_set(ctx, plan)
        return served

    try:
        frozen = await asyncio.to_thread(store.get_served, thread_id, plan.input_digest)
        if frozen:
            served.out = SuggestionSetOut.model_validate(frozen)
            return served
        result = await asyncio.to_thread(store.get_result, thread_id, plan.input_digest)
    except store.StoreUnavailable:
        served.out = assemble_set(ctx, plan)  # no cache, no wait, no freeze
        return served
    except ValueError:  # a corrupt frozen entry: rebuild from the plan
        served.out = assemble_set(ctx, plan)
        return served

    if result is not None:
        served.out = await _freeze(thread_id, plan, _final(ctx, plan, result))
        return served

    # Cache miss: make sure a run exists, then wait a bounded time for it.
    try:
        if await asyncio.to_thread(store.try_lock, thread_id, plan.input_digest):
            enqueue_suggester(
                thread_id,
                trigger="lazy",
                creator_id=getattr(thread, "creator_id", None),
                expected_digest=plan.input_digest,
                language=ctx.language,
                protocol=client_protocol,
            )
        first_seen = await asyncio.to_thread(store.first_seen, thread_id, plan.input_digest)
    except store.StoreUnavailable:
        served.out = assemble_set(ctx, plan)
        return served
    if time.time() - first_seen <= WORDING_WAIT_S:
        served.out = assemble_set(ctx, plan, None, pending=True, retry_after_s=WORDING_RETRY_S)
        return served
    served.out = await _freeze(thread_id, plan, assemble_set(ctx, plan))
    return served
