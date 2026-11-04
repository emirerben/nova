"""The read path behind ``GET /creation-threads/{id}/suggestions`` (KRI-756).

Never calls a model. It recomputes the deterministic candidates, looks up the picks the worker
cached for this input digest, joins them, and decides what the creator is served:

* understanding still running           -> ``pending``, ``default_edit`` only, retry in 3 s
* understanding settled-empty           -> ``ok``, ``default_edit`` only
* picks cached (agent or fallback)      -> ``ok``, cards from the cache, FROZEN for the digest
* picks not cached yet                  -> ``pending`` (``default_edit`` only at first_prompt,
                                           nothing at ready) for at most 6 s after the digest
                                           was first seen, then the deterministic set, FROZEN
* kill lever (max runs per thread = 0)  -> the deterministic set immediately

"Frozen" means the final set is stored once per digest (``suggestion_store.put_served``) and
every later GET returns it unchanged, so a card never changes under the creator's finger. A new
``set_id`` appears only when the input digest changes. A Redis outage degrades to the
deterministic set without freezing.

Also owns the enqueue helpers the triggers call (clip understanding done, render ready, lazy
from this GET). They are enqueue-only and never raise.

Freshness (``KRIA_SUGGESTION_EDITOR_TRIGGERS_ENABLED``, off = every byte above unchanged):

* the digest also covers the head draft's ``snapshot_hash``, the brief version and the editor
  snapshot, so a draft revision, a native editor Save or a brief change is a new set;
* draft commit, editor Save and signals-computed enqueue a debounced refresh that warms the agent's
  picks (the GET alone is already correct: it recomputes the digest on every read);
* a dismissed or applied card never comes back: its kind and target fingerprint is kept on the
  ring entry and in a bounded list on ``thread.state`` that outlives the 5-entry ring. A served
  set stays frozen in Redis; the filter runs on read.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import settings
from app.services import suggestion_store as store
from app.services.suggestion_chips import (
    _ACTIVE_TURN_STATUSES,
    PENDING_RETRY_S,
    RING_KEY,
    WORDING_RETRY_S,
    WORDING_WAIT_S,
    CandidatePlan,
    SuggestionContext,
    SuggestionSetOut,
    _set_id,
    assemble_set,
    assemble_suggestions,
    build_candidates,
    issued_for,
    load_suggestion_context,
)
from app.services.suggestions.context import snapshot_digest

log = structlog.get_logger()

SUGGESTER_QUEUE = "agent-control"

# Freshness. The triggers that warm a set after the footage or the edit moved.
REFRESH_TRIGGERS = frozenset({"draft_commit", "editor_save", "signals_computed"})
# A refresh that finds a turn or a render running tries again this often, this many times (about
# the longest cloud render), then gives up: the GET lazily warms the set on the creator's next read.
BUSY_RETRY_MIN_S = 15
BUSY_DEFERRALS_MAX = 12
# `thread.state` key: fingerprints of dismissed and applied cards. The ring keeps 5 sets and a
# creator can edit through all of them, so a card's memory must outlive its ring entry.
SUPPRESSED_KEY = "suggestion_suppressed"
SUPPRESSED_MAX = 32
DISMISSED_FIELD = "dismissed"  # on a ring entry: {card id: fingerprint}
CONSUMED_FIELD = "consumed"  # on a ring entry, written by the pinned-tap commit: {card id: turn}
_IN_FLIGHT_RENDER = frozenset({"rendering", "pending"})


@dataclass
class Served:
    out: SuggestionSetOut
    media_digest: str
    input_digest: str
    # kind -> witness tier (strategy | editor | policy_only): telemetry only, not on the wire.
    witness_tiers: dict[str, str] = field(default_factory=dict)
    # Freshness only (all default off): what the ring write needs to know about this serve.
    ctx: Any = field(default=None, compare=False, repr=False)
    plan: Any = field(default=None, compare=False, repr=False)
    unfiltered: SuggestionSetOut | None = field(default=None, compare=False, repr=False)
    needs_fold: bool = False
    ring_has_cards: bool = False


def transcripts_of(item: Any) -> dict[str, str]:
    """``{media_id: transcript}`` of a plan item's clips, for the agent and for the checks that
    keep the agent's wording from echoing them. Untrusted text: it never reaches a message."""
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    out: dict[str, str] = {}
    for assignment in getattr(item, "clip_assignments", None) or []:
        if not isinstance(assignment, dict) or not isinstance(assignment.get("media_id"), str):
            continue
        kind = assignment.get("kind") if assignment.get("kind") in {"video", "image"} else "video"
        text = clip_record(assignment.get("analysis"), kind=kind).speech.transcript
        if text:
            out[assignment["media_id"]] = text
    return out


async def with_transcripts(db: Any, thread: Any, ctx: SuggestionContext) -> SuggestionContext:
    """``ctx`` plus the clips' transcripts under ``kind_inputs["transcripts"]``.

    Both the worker (the agent's input) and the GET (the transcript-echo check on the picks it
    joins) need them, and the loader does not carry them. Best effort: a miss changes nothing,
    and nothing here feeds the input digest, so no set is re-keyed.
    """
    from app.models import PlanItem  # noqa: PLC0415

    try:
        item_id = getattr(thread, "active_plan_item_id", None)
        item = await db.get(PlanItem, item_id) if item_id else None
        if item is None:
            return ctx
        return dataclasses.replace(
            ctx, kind_inputs={**ctx.kind_inputs, "transcripts": transcripts_of(item)}
        )
    except Exception:  # noqa: BLE001 - the wording checks degrade, the set still serves
        log.warning("suggestion_chips.error", stage="transcripts", exc_info=True)
        return ctx


def agent_enabled() -> bool:
    """False when ``KRIA_SUGGESTER_MAX_RUNS_PER_THREAD=0``: template labels only."""
    return settings.kria_suggester_max_runs_per_thread > 0


def needs_wording(ctx: SuggestionContext, plan: CandidatePlan) -> bool:
    """Only a set with at least one issued kind has anything for the agent to rank."""
    return bool(issued_for(ctx, plan))


def enqueue_suggester(
    thread_id: str,
    *,
    trigger: str,
    creator_id: object = None,
    expected_digest: str | None = None,
    language: str | None = None,
    protocol: int | None = None,
    countdown: int | None = None,
    attempt: int = 0,
) -> bool:
    """Fire-and-forget. Re-checks the flag and the kill lever; never raises.

    ``countdown`` and ``attempt`` are the freshness triggers' only additions: left at their
    defaults the broker message is exactly what it was.
    """
    try:
        if creator_id is not None and not settings.suggestion_chips_for(creator_id):
            return False
        if not agent_enabled():
            return False
        from app.tasks.kria_suggester import kria_suggester  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "thread_id": str(thread_id),
            "trigger": trigger,
            "expected_digest": expected_digest,
            "language": language,
            "protocol": protocol,
        }
        if attempt:
            kwargs["attempt"] = attempt
        options: dict[str, Any] = {"queue": SUGGESTER_QUEUE, "expires": 600 + (countdown or 0)}
        if countdown:
            options["countdown"] = countdown
        kria_suggester.apply_async(kwargs=kwargs, **options)
        return True
    except Exception:  # noqa: BLE001 - chips are an enhancement
        log.warning("suggestion_chips.enqueue_failed", thread_id=str(thread_id), exc_info=True)
        return False


def _active_threads(item_id: object) -> list[tuple[object, object]]:
    """``(thread_id, creator_id)`` of every active v2 thread on a plan item."""
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
    return [(thread_id, creator_id) for thread_id, creator_id in rows]


def enqueue_for_item(item_id: object, *, trigger: str) -> int:
    """Enqueue a run for every active v2 thread on a plan item. Never raises."""
    if not (settings.kria_suggestion_chips_enabled or settings.kria_suggestion_chips_user_ids):
        return 0  # flag off for everyone: not even a lookup
    try:
        return sum(
            1
            for thread_id, creator_id in _active_threads(item_id)
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
    return assemble_set(ctx, plan, store.picks_from_result(result))


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
    served = await _serve_core(db, thread, user, client_protocol, locale=locale)
    if editor_triggers_on():
        served = _without_suppressed(served, thread)
    return served


async def _serve_core(
    db: Any,
    thread: Any,
    user: Any,
    client_protocol: int | None,
    *,
    locale: str | None = None,
) -> Served:
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
    ctx = await with_transcripts(db, thread, ctx)
    plan = await plan_for(db, thread, ctx)
    served = Served(
        out=SuggestionSetOut(),
        media_digest=ctx.media_digest,
        input_digest=plan.input_digest,
        witness_tiers={
            str(c.kind): c.witness_tier
            for c in ((plan.default,) if plan.default else ()) + plan.levers
        },
        ctx=ctx,
        plan=plan,
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
    if not needs_wording(ctx, plan) or not agent_enabled():
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


# ── Freshness: the digest, the triggers, the dismiss ring ────────────────────────────


def editor_triggers_on() -> bool:
    return bool(settings.kria_suggestion_editor_triggers_enabled)


async def plan_for(db: Any, thread: Any, ctx: SuggestionContext) -> CandidatePlan:
    """``build_candidates`` plus, behind the editor-triggers flag, the freshness key.

    The GET and the worker both call this, so they always agree on the digest (the worker has no
    request headers; everything here comes from the database). The key adds what a creator can
    change without touching the footage: the head draft's ``snapshot_hash``, the brief version and
    the editor snapshot (a native Save changes the Job and writes no draft). Flag off, or a read
    that fails: the plan is returned untouched, byte-identical to the legacy digest.
    """
    plan = build_candidates(ctx)
    if not editor_triggers_on() or plan.state == "none":
        return plan
    try:
        from sqlalchemy import func, select  # noqa: PLC0415

        from app.models import CreativeBriefVersion, CreatorEditDraft  # noqa: PLC0415

        head = (
            await db.execute(
                select(CreatorEditDraft.snapshot_hash)
                .where(CreatorEditDraft.thread_id == thread.id, CreatorEditDraft.is_head.is_(True))
                .order_by(CreatorEditDraft.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        brief = (
            await db.execute(
                select(func.max(CreativeBriefVersion.version)).where(
                    CreativeBriefVersion.thread_id == thread.id
                )
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - freshness is an enhancement; the legacy key still serves
        log.warning("suggestion_chips.error", stage="freshness_key", exc_info=True)
        return plan
    digest = hashlib.sha256(
        json.dumps(
            {
                "input": plan.input_digest,
                "head": head,
                "brief": brief,
                "editor": snapshot_digest(ctx.snapshot),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]
    return dataclasses.replace(plan, input_digest=digest, set_id=_set_id(ctx.thread_id, digest))


async def thread_busy(db: Any, thread: Any) -> bool:
    """A creator turn is running, or the item's current render has not settled."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import CreatorAgentTurn, Job, PlanItem  # noqa: PLC0415

    open_turn = (
        await db.execute(
            select(CreatorAgentTurn.id)
            .where(
                CreatorAgentTurn.thread_id == thread.id,
                CreatorAgentTurn.status.in_(_ACTIVE_TURN_STATUSES),
            )
            .limit(1)
        )
    ).first()
    if open_turn is not None:
        return True
    item_id = getattr(thread, "active_plan_item_id", None)
    item = await db.get(PlanItem, item_id) if item_id else None
    job_id = getattr(item, "current_job_id", None)
    job = await db.get(Job, job_id) if job_id else None
    if job is None:
        return False
    return any(
        isinstance(row, dict) and row.get("render_status") in _IN_FLIGHT_RENDER
        for row in (job.assembly_plan or {}).get("variants") or []
    )


def refresh_for_thread(
    thread_id: object, *, trigger: str, creator_id: object = None, attempt: int = 0
) -> bool:
    """Debounced freshness refresh for one thread. Flag off, never raises, never blocks a save.

    One refresh per ``KRIA_SUGGESTION_REFRESH_DEBOUNCE_S`` window: the queued run reads the thread
    when it starts, so a trigger inside the window is already covered. A Redis outage enqueues
    without coalescing (the worker is the same Redis, so a storm there ends at the run cap).
    """
    if not editor_triggers_on():
        return False
    try:
        window = int(settings.kria_suggestion_refresh_debounce_s)
        if attempt == 0 and window > 0:
            try:
                if not store.claim_refresh_slot(str(thread_id), window):
                    return False
            except store.StoreUnavailable:
                log.warning("suggestion_chips.error", stage="debounce", thread_id=str(thread_id))
        return enqueue_suggester(
            str(thread_id),
            trigger=trigger,
            creator_id=creator_id,
            countdown=window or None,
            attempt=attempt,
        )
    except Exception:  # noqa: BLE001
        log.warning("suggestion_chips.enqueue_failed", trigger=trigger, exc_info=True)
        return False


def refresh_for_item(item_id: object, *, trigger: str) -> int:
    """``refresh_for_thread`` for every active v2 thread on a plan item. Never raises."""
    if not editor_triggers_on():
        return 0
    try:
        identifier = item_id if isinstance(item_id, uuid.UUID) else uuid.UUID(str(item_id))
        return sum(
            1
            for thread_id, creator_id in _active_threads(identifier)
            if refresh_for_thread(thread_id, trigger=trigger, creator_id=creator_id)
        )
    except Exception:  # noqa: BLE001
        log.warning("suggestion_chips.enqueue_failed", trigger=trigger, exc_info=True)
        return 0


async def arefresh_for_item(item_id: object, *, trigger: str) -> int:
    """For async routes: call it AFTER the commit, never under a row lock."""
    if not editor_triggers_on():
        return 0
    try:
        return await asyncio.to_thread(refresh_for_item, item_id, trigger=trigger)
    except Exception:  # noqa: BLE001
        return 0


def on_signals_computed(item_id: object) -> int:
    """The hook for whatever measures a clip's signals once it has stored them."""
    return refresh_for_item(item_id, trigger="signals_computed")


# The ring half. Pure functions over ``thread.state`` first, then the one writer.


def fingerprint(kind: object, anchor: Mapping[str, Any] | None) -> str:
    """What a card is about, stable across sets (card ids are not: they follow the edit).

    A clip is named by its media id when it has one; the ordinal moves when a clip is removed.
    """
    a = anchor if isinstance(anchor, Mapping) else {}
    media_id = a.get("media_id") or None
    moment = a.get("moment") if isinstance(a.get("moment"), Mapping) else None
    parts = [
        str(kind or ""),
        media_id,
        None if media_id else a.get("clip_ordinal"),
        a.get("text_bar_index"),
        [moment.get("start_s"), moment.get("end_s")] if moment else None,
    ]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:16]


def _entries(state: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    ring = state.get(RING_KEY) if isinstance(state, Mapping) else None
    return [e for e in ring if isinstance(e, Mapping)] if isinstance(ring, list) else []


def _card(entry: Mapping[str, Any], card_id: str) -> Mapping[str, Any] | None:
    for raw in entry.get("suggestions") or []:
        if isinstance(raw, Mapping) and raw.get("id") == card_id:
            return raw
    return None


def _card_fp(raw: Mapping[str, Any]) -> str:
    return fingerprint(raw.get("kind"), raw.get("anchor"))


def _marked_fingerprints(state: Mapping[str, Any] | None) -> list[str]:
    """Fingerprints of the cards the ring says were dismissed or applied, oldest entry first."""
    found: list[str] = []
    for entry in _entries(state):
        dismissed = entry.get(DISMISSED_FIELD)
        if isinstance(dismissed, Mapping):
            found.extend(str(v) for v in dismissed.values() if v)
        consumed = entry.get(CONSUMED_FIELD)
        if isinstance(consumed, Mapping):
            for card_id in consumed:
                raw = _card(entry, str(card_id))
                if raw is not None:
                    found.append(_card_fp(raw))
    return found


def _kept_list(state: Mapping[str, Any] | None) -> list[str]:
    kept = state.get(SUPPRESSED_KEY) if isinstance(state, Mapping) else None
    return [str(v) for v in kept] if isinstance(kept, list) else []


def suppressed_set(state: Mapping[str, Any] | None) -> frozenset[str]:
    return frozenset(_kept_list(state)) | frozenset(_marked_fingerprints(state))


def _with_suppressed(state: Mapping[str, Any], new: list[str]) -> dict[str, Any] | None:
    """``state`` with ``new`` appended to the bounded list; None when nothing was added."""
    kept = _kept_list(state)
    added = [fp for fp in dict.fromkeys(new) if fp not in kept]
    if not added:
        return None
    return {**state, SUPPRESSED_KEY: [*kept, *added][-SUPPRESSED_MAX:]}


def fold_suppressed(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Copy the ring's marks into the list that outlives the ring. None when it is up to date."""
    return _with_suppressed(state, _marked_fingerprints(state))


def apply_dismiss(
    state: Mapping[str, Any], set_id: str, card_id: str
) -> tuple[dict[str, Any] | None, str]:
    """``(new state, outcome)``. A None state means there is nothing to write.

    The card is read from the creator's own thread state only. An id the ring never offered
    (or one that already rolled out of it) is ``unknown``: no write, nothing to forget. The set
    id is only a hint: the set may have been replaced since the GET, and the id of a card that
    is still offered is stable across sets.
    """
    ring = _entries(state)
    hit: tuple[int, Mapping[str, Any]] | None = None
    for index in range(len(ring) - 1, -1, -1):
        if ring[index].get("set_id") == set_id and _card(ring[index], card_id) is not None:
            hit = (index, ring[index])
            break
    if hit is None:
        for index in range(len(ring) - 1, -1, -1):
            if _card(ring[index], card_id) is not None:
                hit = (index, ring[index])
                break
    if hit is None:
        return None, "unknown"
    index, entry = hit
    raw = _card(entry, card_id)
    assert raw is not None
    prior = entry.get(DISMISSED_FIELD)
    prior = prior if isinstance(prior, Mapping) else {}
    if card_id in prior:
        return None, "already_dismissed"
    fp = _card_fp(raw)
    marked = {**entry, DISMISSED_FIELD: {**prior, card_id: fp}}
    ring_out = [*ring[:index], marked, *ring[index + 1 :]]
    updated = {**state, RING_KEY: ring_out}
    return _with_suppressed(updated, [fp]) or updated, "dismissed"


def attach_cards(
    state: Mapping[str, Any], set_id: str, cards: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Put the v2 cards on the entry of ``set_id``; None when it is gone or already has them."""
    ring = _entries(state)
    for index in range(len(ring) - 1, -1, -1):
        if ring[index].get("set_id") != set_id:
            continue
        if ring[index].get("suggestions"):
            return None
        entry = {**ring[index], "suggestions": cards}
        return {**state, RING_KEY: [*ring[:index], entry, *ring[index + 1 :]]}
    return None


def _without_suppressed(served: Served, thread: Any) -> Served:
    """Hide what the creator dismissed or applied, from the cards and the legacy chips alike.

    The client falls back to ``chips`` once the cards run out, so a chip of a handled kind would
    bring the card back. The frozen set itself is never edited.
    """
    state = getattr(thread, "state", None)
    suppressed = suppressed_set(state)
    served.unfiltered = served.out
    served.needs_fold = fold_suppressed(state or {}) is not None if suppressed else False
    served.ring_has_cards = any(
        e.get("set_id") == served.out.set_id and e.get("suggestions") for e in _entries(state)
    )
    out = served.out
    if not suppressed:
        return served
    update: dict[str, Any] = {}
    for name in ("suggestions", "chips"):
        items = getattr(out, name)
        kept = [
            item
            for item in items
            if fingerprint(item.kind, item.anchor.model_dump(mode="json")) not in suppressed
        ]
        if len(kept) != len(items):
            update[name] = [item.model_copy(update={"rank": i + 1}) for i, item in enumerate(kept)]
    if update:
        served.out = out.model_copy(update=update)
    return served


async def _mutate_state(
    db: Any,
    thread_id: Any,
    change: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str]],
    *,
    delays: tuple[float | None, ...] = (0.1, 0.25, None),
) -> str:
    """Read-modify-write ``thread.state`` under ``FOR UPDATE SKIP LOCKED``; return the outcome.

    Never waits behind a turn that holds the row: a locked row is retried on ``delays`` and then
    reported as ``busy`` (nothing written). Revision-neutral: no event, no revision bump, and
    ``updated_at`` is pinned to its own value. structlog only: ``record_pipeline_event`` here
    would self-deadlock on the row lock.
    """
    from sqlalchemy import select, update  # noqa: PLC0415

    from app.models import CreationThread  # noqa: PLC0415

    row = None
    for delay in delays:
        row = (
            await db.execute(
                select(CreationThread.state)
                .where(CreationThread.id == thread_id)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if row is not None:
            break
        await db.rollback()
        if delay is not None:
            await asyncio.sleep(delay)
    if row is None:
        return "busy"
    new_state, outcome = change(dict(row[0] or {}))
    if new_state is None:
        await db.rollback()
        return outcome
    await db.execute(
        update(CreationThread)
        .where(CreationThread.id == thread_id)
        .values(state=new_state, updated_at=CreationThread.updated_at)
    )
    await db.commit()
    return outcome


async def dismiss_suggestion(db: Any, thread_id: Any, set_id: str, card_id: str) -> str:
    """``dismissed | already_dismissed | unknown | busy``. The caller owns the ownership check."""
    outcome = await _mutate_state(
        db,
        thread_id,
        lambda state: apply_dismiss(state, set_id, card_id),
        delays=(0.1, 0.25, 0.5, None),
    )
    log.info("suggestion_chips.dismiss", thread_id=str(thread_id), set_id=set_id, outcome=outcome)
    return outcome


def _outcome(new_state: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    return new_state, "written" if new_state is not None else "current"


def _folded(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    return _outcome(fold_suppressed(state))


async def _server_cards(served: Served) -> list[dict[str, Any]] | None:
    """The v2 cards of the set that was served, actions included, for the ring entry.

    Rebuilt from the same context, plan and cached picks the wire cards came from. If the rebuilt
    ids differ from the served ids (the picks expired from Redis in between) nothing is stored:
    a tap on such a card answers "no longer fits" instead of running a different change.
    """
    from app.services.suggestions.pinned import ring_suggestions  # noqa: PLC0415

    shown = served.unfiltered or served.out
    if not shown.suggestions or served.ctx is None or served.plan is None:
        return None
    picks = None
    try:
        result = await asyncio.to_thread(
            store.get_result, served.ctx.thread_id, served.input_digest
        )
        picks = store.picks_from_result(result)
    except Exception:  # noqa: BLE001 - no cache: the deterministic order is the fallback
        picks = None
    built, _drops = assemble_suggestions(served.ctx, served.plan, picks)
    if built is None or [s.id for s in built.suggestions] != [s.id for s in shown.suggestions]:
        log.info("suggestion_chips.cards_not_stored", set_id=shown.set_id)
        return None
    return ring_suggestions(built)


async def persist_served_set(
    db: Any, thread_id: Any, served: Served, *, protocol: int | None
) -> bool:
    """The ring write for the freshness path: the legacy entry, plus what a dismiss needs.

    1. fold the ring's dismissed / applied marks into the list that survives eviction, BEFORE the
       append can push an entry out;
    2. the legacy ``persist_suggestion_set`` (chips, idempotent, revision-neutral);
    3. store the v2 cards on the set's entry, so a tap and a dismiss can find them.
    Each step is its own short ``SKIP LOCKED`` write; a skipped one is retried by the next read.
    """
    from app.services.suggestion_chips import persist_suggestion_set  # noqa: PLC0415

    if served.needs_fold:
        await _mutate_state(db, thread_id, _folded)
    wrote = await persist_suggestion_set(
        db,
        thread_id,
        served.out,
        media_digest=served.media_digest,
        protocol=protocol,
        input_digest=served.input_digest,
        witness_tiers=served.witness_tiers,
    )
    shown = served.unfiltered or served.out
    if shown.suggestions and shown.status == "ok" and not served.ring_has_cards:
        cards = await _server_cards(served)
        if cards:
            await _mutate_state(
                db, thread_id, lambda state: _outcome(attach_cards(state, shown.set_id, cards))
            )
    return wrote
