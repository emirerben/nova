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
import inspect
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import settings
from app.services import suggestion_store as store
from app.services.suggestion_chips import (
    _ACTIVE_TURN_STATUSES,
    FRESHNESS_KEY,
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
    suggestion_digest,
)
from app.services.suggestions import brief_asks
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
    # The v2 cards for the ring entry, built while the read session was still live (see
    # ``_prepare_cards``). ``cards_built`` tells "built, nothing to store" from "never built".
    cards: list[dict[str, Any]] | None = field(default=None, compare=False, repr=False)
    cards_built: bool = False


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
    """``ctx`` plus what the loader does not carry, read from the thread and the plan item.

    Always: the clips' transcripts under ``kind_inputs["transcripts"]`` (the agent's input and
    the transcript-echo check on the picks the GET joins). With any v2 flag on (``v2_inputs_on``):
    the brief and its headline, every enabled kind's own inputs (``load_kind_inputs``) and the
    freshness key. The worker and the GET both call this, so they always see the same context.

    Best effort: each read is its own failure domain. A miss never drops another kind's inputs,
    and a kind whose input is missing simply offers nothing (every kind fails closed).
    """
    from app.models import PlanItem  # noqa: PLC0415

    try:
        item_id = getattr(thread, "active_plan_item_id", None)
        item = await db.get(PlanItem, item_id) if item_id else None
        if item is None:
            return ctx
        ctx = dataclasses.replace(
            ctx, kind_inputs={**ctx.kind_inputs, "transcripts": transcripts_of(item)}
        )
    except Exception:  # noqa: BLE001 - the wording checks degrade, the set still serves
        log.warning("suggestion_chips.error", stage="transcripts", exc_info=True)
        return _brief_unread(ctx)
    if ctx.state not in ("first_prompt", "ready") or not v2_inputs_on():
        return ctx
    try:
        return await _with_v2_inputs(db, thread, item, ctx)
    except Exception:  # noqa: BLE001 - never worse than the transcripts-only context
        log.warning("suggestion_chips.error", stage="kind_inputs", exc_info=True)
        return _brief_unread(ctx)


def _brief_unread(ctx: SuggestionContext) -> SuggestionContext:
    """A v2 flag is on and the brief was not read: mark it unreadable, not absent (KRI-129).

    An absent ``BRIEF_KEY`` means the loader never ran (flags off), which the five brief-obeying
    kinds read as "the creator said nothing". After a failed read that would offer a captions card
    to someone who asked for none; ``UNREADABLE`` makes them offer nothing instead."""
    if ctx.state not in ("first_prompt", "ready") or not v2_inputs_on():
        return ctx
    return dataclasses.replace(
        ctx, kind_inputs={**ctx.kind_inputs, BRIEF_KEY: brief_asks.UNREADABLE}
    )


# ── The v2 kinds' inputs (each read isolated: one failing never drops the others) ────────

# Kinds whose card reads something the editor snapshot and the clip rows cannot say.
INPUT_KINDS = ("hook", "mute_clip", "filler_words", "duplicates")
# The kinds whose tap re-drafts the thread's plan (a brief update or an item setting). They are
# only the variant's next revision while its head draft IS the plan (``pinned.restate_base``), and
# only while the saved video is one the agent has read (``pinned.save_unread``).
RESTATING_KINDS = frozenset({"mute_clip", "filler_words"})
BRIEF_KEY = "brief"  # kind_inputs: the latest CreativeBrief (None = the thread has none)


def input_kinds_on() -> frozenset[str]:
    from app.services.suggestions import registry  # noqa: PLC0415

    return frozenset(
        k.kind for k in registry.all_kinds() if k.kind in INPUT_KINDS and registry.enabled(k)
    )


def v2_inputs_on() -> bool:
    """Any v2 flag that reads more than the legacy loader gives. All off: nothing here runs.

    Pinned taps alone (cards for the ported editor kinds) need nothing the engine does not
    already read, so they do not count."""
    return bool(input_kinds_on() or settings.kria_suggestion_editor_triggers_enabled)


def brief_headline(brief: Any) -> str | None:
    """The creator's live requests in one line, for the agent (KRI-129: their request wins)."""
    live = getattr(brief, "live", None)
    lines = [
        f"{req.kind}: {req.text()}" for req in (live()[:6] if callable(live) else []) if req.text()
    ]
    return "; ".join(lines) or None


def needed_media(brief: Any, media_ids: Sequence[str]) -> frozenset[str]:
    """Media the creator's own request depends on: a clip a requirement names, or every clip
    when a requirement orders, selects or labels clips in a way this cannot resolve (KRI-129).
    Over-reporting only hides a duplicates card; it never lets one remove a requested clip."""
    from app.kria.asset_holds import resolve_clip_scope  # noqa: PLC0415

    live = getattr(brief, "live", None)
    if not callable(live):
        return frozenset()
    everything = frozenset(media_ids)
    needed: set[str] = set()
    for req in live():
        if req.scope.startswith("clip:"):
            resolved = resolve_clip_scope(req.scope.split(":", 1)[1], media_ids)
            if resolved is None:
                return everything
            needed.add(resolved)
        elif req.kind in {"order", "select", "text_sequence"} or req.scope == "per_clip":
            return everything
    return frozenset(needed)


def scheduler_will_redo(row: Any, item: Any, source: Any) -> bool:
    """Would the preflight scheduler reset this analysis row the next time it is asked?

    The read-only twin of ``speech_cleanup_preflight._redo_for_misheard_language`` (plus the
    engine-version supersede that precedes it): a card offered on such a row would be
    refused at the tap as ``analysis_redone``. A parity test pins the two together.
    """
    from app.pipeline.caption_language import crosscheck_detected_language  # noqa: PLC0415
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        SPEECH_CLEANUP_ENGINE_VERSION,
        reference_transcript_for_source,
    )

    if getattr(row, "engine_version", None) != SPEECH_CLEANUP_ENGINE_VERSION:
        return True
    if row.status not in {"ready", "no_findings"} or row.decision is not None:
        return False
    payload = row.analysis_payload
    if not isinstance(payload, dict):
        return False
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, dict) and "language_crosscheck" in diagnostics:
        return False
    reference = reference_transcript_for_source(
        item, storage_path=source.storage_path, generation=source.generation
    )
    language = str(payload.get("language") or "")
    return crosscheck_detected_language(language, reference_text=reference) is not None


async def filler_evidence(db: Any, item: Any) -> Any:
    """``filler_words.load_evidence`` that offers nothing on a row the scheduler will redo."""
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        resolve_item_narration,
    )
    from app.services.speech_cleanup_preflight import current_analysis_async  # noqa: PLC0415
    from app.services.suggestions.kinds import filler_words  # noqa: PLC0415

    try:
        analysis = await current_analysis_async(db, item.id)
        source = resolve_item_narration(item, detector_policy=current_detector_policy()).source
        fingerprint = source.source_policy_fingerprint if source is not None else None
        if analysis is not None and source is not None:
            if scheduler_will_redo(analysis, item, source):
                analysis = None
        return filler_words.evidence_for_item(item, analysis, current_fingerprint=fingerprint)
    except Exception:  # noqa: BLE001 - a card that cannot be proven is not offered
        log.warning("suggestion_filler.load_failed", exc_info=True)
        return filler_words.FillerEvidence()


def duplicates_input(item: Any, brief: Any) -> dict[str, Any]:
    from app.kria.duplicate_uploads import _capture_time  # noqa: PLC0415

    assignments = [
        a
        for a in getattr(item, "clip_assignments", None) or []
        if isinstance(a, dict) and isinstance(a.get("media_id"), str)
    ]
    ids = [a["media_id"] for a in assignments]
    times: dict[str, str] = {}
    exposure: dict[str, Any] = {}
    for a in assignments:
        when = _capture_time(a)
        if when is not None:
            times[a["media_id"]] = when.isoformat()
        analysis = a.get("analysis")
        found = analysis.get("exposure") if isinstance(analysis, Mapping) else None
        if isinstance(found, Mapping):
            exposure[a["media_id"]] = dict(found)
    return {
        "needed": sorted(needed_media(brief, ids)),
        "capture_times": times,
        "exposure": exposure,
    }


async def _hook_input(db: Any, thread: Any, item: Any, ctx: SuggestionContext, brief: Any) -> Any:
    from app.kria import planner  # noqa: PLC0415
    from app.services.clip_understanding import clip_record  # noqa: PLC0415
    from app.services.creative_copy_decisions import fold_creative_copy  # noqa: PLC0415
    from app.services.suggestions.kinds import hook  # noqa: PLC0415

    events = await planner._load_thread_events(db, thread.id, release=False)
    states = fold_creative_copy(
        events, dependency_digest=ctx.media_digest, include_suggestions=True
    )
    records = [
        clip_record(a.get("analysis"), kind="image" if a.get("kind") == "image" else "video")
        for a in getattr(item, "clip_assignments", None) or []
        if isinstance(a, dict) and isinstance(a.get("media_id"), str)
    ]
    return hook.hook_inputs(brief=brief, copy_states=states, records=records)


def understanding_marks(item: Any, clips: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``clips`` with ``_empty`` / ``_incomplete`` (what the mute kind reads to know a clip's
    record is complete). The loader strips every underscore key from its clip rows, so without
    this a clip never reads as understood and the kind can never offer."""
    from app.services.clip_understanding import (  # noqa: PLC0415
        clip_record,
        understanding_incomplete,
    )

    analyses = {
        a["media_id"]: (a.get("analysis"), "image" if a.get("kind") == "image" else "video")
        for a in getattr(item, "clip_assignments", None) or []
        if isinstance(a, dict) and isinstance(a.get("media_id"), str)
    }
    out: list[dict[str, Any]] = []
    for clip in clips:
        row = dict(clip)
        found = analyses.get(str(row.get("media_id")))
        if found is not None:
            analysis, kind = found
            row["_empty"] = clip_record(analysis, kind=kind).is_empty()
            row["_incomplete"] = understanding_incomplete(analysis, kind=kind)
        out.append(row)
    return out


async def _head_snapshot_hash(db: Any, thread: Any) -> str | None:
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import CreatorEditDraft  # noqa: PLC0415

    return (
        await db.execute(
            select(CreatorEditDraft.snapshot_hash)
            .where(CreatorEditDraft.thread_id == thread.id, CreatorEditDraft.is_head.is_(True))
            .order_by(CreatorEditDraft.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _plan_is_head(db: Any, thread: Any, ctx: SuggestionContext) -> bool:
    """False when a restating tap would be refused ``base_moved``: the variant's head draft is
    newer work (an editor draft) than the plan the tap re-drafts. The tap's own check, asked at
    offer time, so a card is not shown that its tap would refuse (and hide for good)."""
    from app.services.suggestions import pinned  # noqa: PLC0415

    try:
        base = await pinned.restate_base(db, thread_id=thread.id, ctx=ctx)
    except Exception:  # noqa: BLE001 - a card that cannot be proven is not offered
        log.warning("suggestion_chips.error", stage="restate_base", exc_info=True)
        return False
    return base.reason != pinned.BASE_MOVED


async def _video_is_read(db: Any, thread: Any, ctx: SuggestionContext) -> bool:
    """False when a restating tap would be refused ``base_moved`` for a Save: the saved video is
    not the one the agent session last read. The tap's own second witness (``pinned.save_unread``),
    asked at offer time, because the plan the tap re-drafts holds none of the Save's edits and its
    approval would render from that plan. The card comes back when the pointer catches up (a Kria
    render completes, an editor tap re-syncs it)."""
    from app.models import CreatorAgentSession  # noqa: PLC0415
    from app.services.suggestions import pinned  # noqa: PLC0415

    try:
        session_id = getattr(thread, "active_creator_agent_session_id", None)
        session = await db.get(CreatorAgentSession, session_id) if session_id else None
    except Exception:  # noqa: BLE001 - a card that cannot be proven is not offered
        log.warning("suggestion_chips.error", stage="session_pointer", exc_info=True)
        return False
    seen = session.target_generation_id if session is not None else None
    return not pinned.save_unread(ctx.render_generation_id, seen)


async def _with_v2_inputs(
    db: Any, thread: Any, item: Any, ctx: SuggestionContext
) -> SuggestionContext:
    from app.kria.brief import load_latest_brief  # noqa: PLC0415
    from app.services.suggestions.kinds import mute_clip  # noqa: PLC0415

    kinds = input_kinds_on()
    if (
        ctx.state == "ready"
        and kinds & RESTATING_KINDS
        and not (await _plan_is_head(db, thread, ctx) and await _video_is_read(db, thread, ctx))
    ):
        # No input, no card: each kind fails closed without its own read.
        kinds = kinds - RESTATING_KINDS
    inputs: dict[str, Any] = dict(ctx.kind_inputs)
    brief: Any = None
    brief_ok = False
    try:
        brief = await load_latest_brief(db, thread.id)
        brief_ok = True
        inputs[BRIEF_KEY] = brief
    except Exception:  # noqa: BLE001 - every brief-reading kind then offers nothing
        log.warning("suggestion_chips.error", stage="brief", exc_info=True)
        inputs[BRIEF_KEY] = brief_asks.UNREADABLE

    if editor_triggers_on() and brief_ok:
        try:
            inputs[FRESHNESS_KEY] = {
                "snapshot_hash": await _head_snapshot_hash(db, thread),
                "brief_version": brief.version if brief is not None else None,
            }
        except Exception:  # noqa: BLE001 - plan_for falls back to reading the key itself
            log.warning("suggestion_chips.error", stage="freshness_key", exc_info=True)

    if ctx.state == "ready":
        if "mute_clip" in kinds:
            try:
                ctx = dataclasses.replace(ctx, clips=understanding_marks(item, ctx.clips))
            except Exception:  # noqa: BLE001 - the kind then sees no clip as understood
                log.warning("suggestion_chips.error", stage="understanding_marks", exc_info=True)
        # Each kind's read is its own failure domain: one that fails offers nothing, the rest serve.
        reads: dict[str, Callable[[], Any]] = {
            "filler_words": lambda: filler_evidence(db, item),
        }
        if brief_ok:
            reads["mute_clip"] = lambda: mute_clip.loader_input(brief)
            reads["duplicates"] = lambda: duplicates_input(item, brief)
            reads["hook"] = lambda: _hook_input(db, thread, item, ctx, brief)
        for name, read in reads.items():
            if name not in kinds:
                continue
            try:
                found = read()
                inputs[name] = await found if inspect.isawaitable(found) else found
            except Exception:  # noqa: BLE001
                log.warning("suggestion_chips.error", stage=name, exc_info=True)

    headline = brief_headline(brief) if brief_ok and ctx.brief_headline is None else None
    return dataclasses.replace(
        ctx, kind_inputs=inputs, brief_headline=headline or ctx.brief_headline
    )


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
    """Build what to serve for this thread right now. Wait-free; never calls a model.

    Everything the ring write needs is built here, inside the caller's live read session: the
    editor witness closes over the ORM ``Job``, and the route rolls the session back before it
    writes, which expires the Job and makes every witness fail.
    """
    served = await _serve_core(db, thread, user, client_protocol, locale=locale)
    if not settings.kria_suggestion_pinned_taps_enabled:
        # A set frozen while the flag was on still carries its cards: switching the flag off must
        # stop them now, not when the 24 h freeze ends. The freeze itself is not edited.
        served.out = _legacy_only(served.out)
    else:
        await _strip_dead_cards(db, thread, served)
    if editor_triggers_on():
        served = _without_suppressed(served, thread)
    else:
        served.ring_has_cards = _ring_has_cards(
            served.out.set_id, getattr(thread, "state", None), _shown_ids(served)
        )
    await _prepare_cards(served)
    return served


def _legacy_only(out: SuggestionSetOut) -> SuggestionSetOut:
    """``out`` as the server sent it before v2 cards existed (byte-identical on the wire)."""
    if not out.suggestions and out.digest is None:
        return out
    return out.model_copy(update={"suggestions": [], "digest": None})


# ── The cards whose tap would be refused right now ─────────────────────────────────────
#
# A served set can be hours old (it is frozen for 24 h). Between the freeze and this GET a switch
# can be pulled, the creator can settle the on-screen title, the plan can be replaced by newer
# work. The tap checks all of that again and refuses, and a refusal is a dead tap. So the GET asks
# the tap's own questions and leaves out the cards it would refuse. The stored set is not edited:
# the ring keeps the cards the set was built with, so a card that comes back (a switch turned on
# again) needs no new run, and the cards that stay keep the ids the phone already holds.


def _kind_off(kind: str) -> bool:
    """The tap's own first check (``pinned.kind_enabled``): unregistered or switched off."""
    from app.services.suggestions import registry  # noqa: PLC0415

    spec = registry.get(kind)
    return spec is None or not registry.enabled(spec)


async def _copy_gate_dead(
    db: Any, thread: Any, ctx: SuggestionContext, item: Any, kinds: frozenset[str]
) -> dict[str, str]:
    """Cards the creative-copy consent gate refuses at the draft commit (KRI-506).

    The commit asks ``creative_copy_problem`` with the plan the draft carries. An editor-ops card
    (and the hook card, which is one editor op) is committed with no plan, so ANY decision about
    an on-screen title refuses it; a restating card carries the head plan, so only a plan that
    disagrees with the decision refuses it. The same function answers here, over the same events
    and the same media, so the two cannot drift.
    """
    from app.kria import planner  # noqa: PLC0415
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
    from app.services.creative_copy_gate import creative_copy_problem  # noqa: PLC0415
    from app.services.suggestions import pinned  # noqa: PLC0415

    events = await planner._load_thread_events(db, thread.id, release=False)
    media = snapshot_media(item)
    dead: dict[str, str] = {}
    editor_ops = kinds - RESTATING_KINDS
    if editor_ops and creative_copy_problem(events, media, strategy=None) is not None:
        dead.update(dict.fromkeys(editor_ops, "copy_gate"))
    restating = kinds & RESTATING_KINDS
    if restating:
        base = await pinned.restate_base(db, thread_id=thread.id, ctx=ctx)
        if base.strategy is None:
            dead.update(dict.fromkeys(restating, base.reason or "no_plan"))
        elif creative_copy_problem(events, media, strategy=base.strategy) is not None:
            dead.update(dict.fromkeys(restating, "copy_gate"))
    return dead


async def _restate_held(
    db: Any, thread: Any, ctx: SuggestionContext, item: Any, kind: str
) -> str | None:
    """Why a restating tap would be held at approval readiness, or None when it would not.

    The tap re-drafts the head plan with one more requirement and holds the draft back when a
    requirement the card did not write is still unmet ("Do not replace a creator's current draft
    with a known partial edit"), answering ``requirements_not_ready``. This asks the draft check's
    own functions (``plan_facts_from_strategy`` -> ``build_receipts`` -> ``needs_creator_choice``)
    about the same plan and brief. It reads only the plan and the brief: facts that need the
    phone's montage preflight or the research step are left unknown, which never turns into a
    verdict (an unknown fact is an unchecked receipt), so what this holds back is what the plan
    itself falls short of.

    An account on approval readiness (KRI-721) is held back whole: the pinned restate never
    resolves the readiness manifest (``planned.readiness_manifest`` stays None), so
    ``assess_readiness`` answers "I haven't established which renderer can make this edit yet"
    for every requirement and every restating tap is refused.
    """
    from app.kria.brief import load_latest_brief  # noqa: PLC0415
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        needs_creator_choice,
        plan_facts_from_strategy,
        requirements_to_check_at_draft,
    )
    from app.models import ContentPlan, Persona  # noqa: PLC0415
    from app.services.choice_questions import answered_brief  # noqa: PLC0415
    from app.services.creator_sessions import resolve_item_creator_context  # noqa: PLC0415
    from app.services.suggestions import pinned  # noqa: PLC0415

    creator_id = getattr(thread, "creator_id", None)
    if settings.approval_readiness_for(creator_id):
        return "readiness_manifest_unresolved"
    base = await pinned.restate_base(db, thread_id=thread.id, ctx=ctx)
    if base.strategy is None:
        return base.reason or "no_plan"
    brief = await load_latest_brief(db, thread.id)
    if brief is None or not brief.live():
        return None
    if settings.kria_choice_questions_enabled:
        brief = answered_brief(brief, base.strategy)
    plan = await db.get(ContentPlan, item.content_plan_id)
    persona = await db.get(Persona, plan.persona_id) if plan is not None else None
    if persona is None:
        return "no_persona"
    manifest, _media = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )
    facts = plan_facts_from_strategy(
        base.strategy,
        clip_ids=[str(media.media_id) for media in manifest.media],
        manifest=manifest,
        # The tap flips the setting before it judges the draft; mute leaves it alone.
        speech_cleanup_enabled=True
        if kind == "filler_words"
        else bool(getattr(item, "speech_cleanup_enabled", False)),
    )
    checked = requirements_to_check_at_draft(
        brief.live(),
        creator_id=creator_id,
        strategy=base.strategy,
        item_edit_format=item.edit_format,
        clip_paths=item.clip_gcs_paths or (),
        client_protocol=ctx.client_protocol,
    )
    bound = settings.brief_binding_for(creator_id)
    receipts = build_receipts(checked, facts, include_unchecked=bound, strict_order=bound)
    unmet = [r.requirement_id for r in receipts if needs_creator_choice(r)]
    return "requirements_not_ready" if unmet else None


async def dead_card_kinds(
    db: Any, thread: Any, ctx: SuggestionContext, kinds: frozenset[str]
) -> dict[str, str]:
    """``{kind: why}`` for the card kinds in ``kinds`` whose tap would be refused right now.

    Fails closed: a check that cannot be made leaves its cards out (a card the server cannot
    prove is not offered), never the other way round.
    """
    dead = {kind: "kind_off" for kind in kinds if _kind_off(kind)}
    left = kinds - dead.keys()
    if not left:
        return dead
    item_id = getattr(thread, "active_plan_item_id", None)
    if not item_id:
        return dead  # the loader serves no card without an item: there is nothing to ask
    try:
        from app.models import PlanItem  # noqa: PLC0415

        item = await db.get(PlanItem, item_id)
        if item is None:
            return {**dead, **dict.fromkeys(left, "no_item")}
        dead.update(await _copy_gate_dead(db, thread, ctx, item, left))
    except Exception:  # noqa: BLE001 - what cannot be proven is not offered
        log.warning("suggestion_chips.error", stage="dead_cards", exc_info=True)
        return {**dead, **dict.fromkeys(left - dead.keys(), "check_failed")}
    # One restating kind failing to prove itself costs that card, not the others.
    for kind in sorted((left - dead.keys()) & RESTATING_KINDS):
        try:
            reason = await _restate_held(db, thread, ctx, item, kind)
        except Exception:  # noqa: BLE001
            log.warning("suggestion_chips.error", stage="restate_held", kind=kind, exc_info=True)
            reason = "check_failed"
        if reason is not None:
            dead[kind] = reason
    return dead


async def _strip_dead_cards(db: Any, thread: Any, served: Served) -> None:
    """Take the v2 cards whose tap would be refused off the wire. The legacy chips stay."""
    out = served.out
    if not out.suggestions or served.ctx is None:
        return
    dead = await dead_card_kinds(db, thread, served.ctx, frozenset(c.kind for c in out.suggestions))
    if not dead:
        return
    kept = [c for c in out.suggestions if c.kind not in dead]
    log.info(
        "suggestion_chips.stripped",
        thread_id=str(thread.id),
        set_id=out.set_id,
        dropped=dead,
        kept=[c.kind for c in kept],
    )
    served.unfiltered = served.unfiltered or out
    served.out = out.model_copy(
        update={
            "suggestions": [c.model_copy(update={"rank": i + 1}) for i, c in enumerate(kept)],
            # A set with no card left reads exactly like a legacy one (see ``_legacy_only``).
            **({} if kept else {"digest": None}),
        }
    )


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
    the editor snapshot (a native Save changes the Job and writes no draft). Both reads come with
    the context (``with_transcripts``); a context without them reads them here. Flag off, or a
    read that fails: the plan is returned untouched, byte-identical to the legacy digest.
    """
    plan = build_candidates(ctx)
    if not editor_triggers_on() or plan.state == "none":
        return plan
    fresh = ctx.kind_inputs.get(FRESHNESS_KEY) if isinstance(ctx.kind_inputs, Mapping) else None
    if not isinstance(fresh, Mapping):
        try:
            from sqlalchemy import func, select  # noqa: PLC0415

            from app.models import CreativeBriefVersion  # noqa: PLC0415

            brief = (
                await db.execute(
                    select(func.max(CreativeBriefVersion.version)).where(
                        CreativeBriefVersion.thread_id == thread.id
                    )
                )
            ).scalar_one_or_none()
            fresh = {"snapshot_hash": await _head_snapshot_hash(db, thread), "brief_version": brief}
        except Exception:  # noqa: BLE001 - freshness is an enhancement; the legacy key serves
            log.warning("suggestion_chips.error", stage="freshness_key", exc_info=True)
            return plan
        ctx = dataclasses.replace(ctx, kind_inputs={**ctx.kind_inputs, FRESHNESS_KEY: fresh})
    digest = hashlib.sha256(
        json.dumps(
            {
                "input": plan.input_digest,
                "digest": suggestion_digest(ctx, plan.state).value(),
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
    if creator_id is not None and not settings.suggestion_chips_for(creator_id):
        return False  # before the debounce slot: a creator without chips must cost nothing
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
    if not editor_triggers_on() or not (
        settings.kria_suggestion_chips_enabled or settings.kria_suggestion_chips_user_ids
    ):
        return 0  # chips off for everyone: not even a lookup
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


def _card_ids_of(entry: Mapping[str, Any]) -> set[str]:
    return {
        str(raw.get("id"))
        for raw in entry.get("suggestions") or []
        if isinstance(raw, Mapping) and raw.get("id")
    }


def attach_cards(
    state: Mapping[str, Any], set_id: str, cards: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Put the v2 cards on the entry of ``set_id``; None when it is gone or holds them already.

    A card the entry lacks is added next to the ones it holds, never in place of them: the phone
    may hold any card of an earlier read of this set, and a served card the entry cannot resolve is
    a dead tap. A card is named by its id, which follows its kind, targets and base.
    """
    ring = _entries(state)
    for index in range(len(ring) - 1, -1, -1):
        if ring[index].get("set_id") != set_id:
            continue
        held_ids = _card_ids_of(ring[index])
        fresh = [c for c in cards if str(c.get("id")) not in held_ids]
        if not fresh:
            return None
        held = [c for c in ring[index].get("suggestions") or [] if isinstance(c, Mapping)]
        entry = {**ring[index], "suggestions": [*held, *fresh]}
        return {**state, RING_KEY: [*ring[:index], entry, *ring[index + 1 :]]}
    return None


def _ring_has_cards(
    set_id: str, state: Mapping[str, Any] | None, card_ids: Sequence[str] = ()
) -> bool:
    """Does the ring entry of ``set_id`` hold every card in ``card_ids`` (any card, when none)?"""
    wanted = {str(i) for i in card_ids}
    for entry in _entries(state):
        if entry.get("set_id") != set_id:
            continue
        held = _card_ids_of(entry)
        if (wanted <= held) if wanted else bool(held):
            return True
    return False


def _shown_ids(served: Served) -> list[str]:
    """The cards the ring must hold for this read: every one the set was built with, including
    the ones the strip took off this read's wire (they come back without a new run)."""
    return [c.id for c in (served.unfiltered or served.out).suggestions]


def _without_suppressed(served: Served, thread: Any) -> Served:
    """Hide what the creator dismissed or applied, from the cards and the legacy chips alike.

    The client falls back to ``chips`` once the cards run out, so a chip of a handled kind would
    bring the card back. The frozen set itself is never edited.
    """
    state = getattr(thread, "state", None)
    suppressed = suppressed_set(state)
    served.unfiltered = served.unfiltered or served.out
    served.needs_fold = fold_suppressed(state or {}) is not None if suppressed else False
    served.ring_has_cards = _ring_has_cards(served.out.set_id, state, _shown_ids(served))
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


async def _prepare_cards(served: Served) -> None:
    """Build the cards the ring entry will hold, unless the entry already has them.

    Never raises: cards are an enhancement of a set that serves without them, and a set whose
    cards cannot be built is stored without them (a tap then answers "no longer fits").
    """
    shown = served.unfiltered or served.out
    if not shown.suggestions or shown.status != "ok" or served.ring_has_cards:
        return
    try:
        served.cards = await _server_cards(served)
    except Exception:  # noqa: BLE001
        log.warning("suggestion_chips.error", stage="cards", exc_info=True)
        served.cards = None
    served.cards_built = True


async def persist_served_set(
    db: Any, thread_id: Any, served: Served, *, protocol: int | None
) -> bool:
    """The ring write behind the GET: the legacy entry, plus what a tap and a dismiss need.

    1. fold the ring's dismissed / applied marks into the list that survives eviction, BEFORE the
       append can push an entry out (freshness path only: ``needs_fold`` is set by it);
    2. the legacy ``persist_suggestion_set`` (chips, idempotent, revision-neutral);
    3. store the v2 cards on the set's entry, so a tap and a dismiss can find them. They come from
       ``serve_suggestion_set`` (built in the live read session); a ``Served`` that was not built
       there builds them here, which only works while the caller's session is still live.
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
        cards = served.cards if served.cards_built else await _server_cards(served)
        if not await _store_cards(db, thread_id, shown.set_id, cards or [], _shown_ids(served)):
            # The cards are an enhancement of a set that serves without them. A card the ring does
            # not hold answers "no longer fits" however good it was, so the phone gets the chips
            # now (same set id) and the next read builds and stores the cards again.
            log.info("suggestion_chips.cards_not_stored", set_id=shown.set_id)
            served.out = _legacy_only(served.out)
    return wrote


async def _store_cards(
    db: Any, thread_id: Any, set_id: str, cards: list[dict[str, Any]], card_ids: Sequence[str]
) -> bool:
    """Write ``cards`` on the entry of ``set_id``; True only when, once written, the entry holds
    every one of ``card_ids``. A skipped write (the row stayed locked), a missing entry or cards
    that were not built all answer False."""
    held = False

    def attach(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        nonlocal held
        new = attach_cards(state, set_id, cards)
        held = _ring_has_cards(set_id, new if new is not None else state, card_ids)
        return _outcome(new)

    outcome = await _mutate_state(db, thread_id, attach)
    return outcome != "busy" and held
