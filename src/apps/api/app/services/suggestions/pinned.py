"""Pinned suggestion taps: how a stored card is read back and what a tap may run.

A tap carries ``{set_id, chip_id}``. The server never re-reads the card's sentence through the
planner. It finds the card the thread was offered (``thread.state["suggestion_sets"]``), builds a
``PlannedKriaTurn`` from the card's stored ``Action`` and hands it to the one existing draft path
(``tasks/kria_runtime._complete_draft_turn``). This module is the pure half: ring reads, the lane
allow-list, digests, typed brief updates and the rebuild of an action whose base moved. The
planner glue is ``kria/planner.plan_pinned_suggestion_turn``.

Ring contract (writer: the serve lane). An entry of ``thread.state["suggestion_sets"]`` that
offers v2 cards carries ``"suggestions": [Suggestion.model_dump(mode="json"), ...]`` next to the
legacy ``chips``; ``ring_suggestions`` is the one serializer. The action is server-side only: it
is never on the wire and never in a chat event.

The seam writes one thing back to an entry: ``"consumed": {<suggestion id>: <turn id>}``, set in
the same transaction that commits the draft the card produced (``mark_consumed``). A card is one
offer. Its stored action is the exact change it was proven for, so it may run again only against
the exact base it was built for; a rebuild derives a new change from the edit as it is now, and
for a relative card (the music level is 60% of whatever it is now) every rebuild would be a new
change the creator was never offered. A rebuild of a consumed card is refused (``is_consumed``).

A v2 card id starts with ``ss_`` (``models.suggestion_id``); a KRI-756 chip id starts with
``sc_`` and is never pinned.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.services.suggestion_chips import RING_KEY
from app.services.suggestions import registry
from app.services.suggestions.context import NO_TARGET, SuggestionContext, Target, snapshot_digest
from app.services.suggestions.models import (
    Action,
    BriefUpdateAction,
    CopyDecisionAction,
    EditorOpsAction,
    ItemSettingAction,
    Suggestion,
    SuggestionSet,
)

log = structlog.get_logger()

V2_ID_PREFIX = "ss_"
# The key a submitted pin is stamped under on the creator's ``user_message`` event.
PIN_KEY = "pinned_suggestion"
# The key on a ring entry that records the cards a tap already applied.
CONSUMED_FIELD = "consumed"
# The key on a ring entry that records the cards a tap was refused for ({card id: turn id}).
REFUSED_FIELD = "refused"

# The editor ops a pinned tap may run in this lane. A stored action with any other op is refused,
# so nothing that carries text (add_text, rewrite_text, set_title ...) or runs server-async
# (apply_speech_cut_candidate) can reach a draft through a tap, whatever the ring holds.
PINNED_OP_ALLOW = frozenset(
    {"add_sfx", "set_transition", "set_mix", "set_caption_meta", "remove_clip"}
)

# The one op a copy-decision (hook) card may run: its own exact title, through the same compiler.
# Text-carrying ops on an editor-ops card stay refused above, so no agent copy reaches the screen
# except as the creator-approved card text.
COPY_OP_ALLOW = frozenset({"set_title"})

# Refusal reason codes (logged and recorded on the plan's diagnostics; never shown as text).
UNRESOLVED = "unresolved"
RING_CHANGED = "ring_changed"
PROMPT_CHANGED = "prompt_changed"
ACTION_UNSUPPORTED = "action_unsupported"
OP_NOT_ALLOWED = "op_not_allowed"
BASE_MOVED = "base_moved"
CONSUMED = "consumed"
OP_REFUSED = "op_refused"
NO_CHANGE = "no_change"
BRIEF_UNAVAILABLE = "brief_unavailable"
TARGET_UNAVAILABLE = "target_unavailable"
NO_STRATEGY = "no_strategy"
UNSAVED_EDITS = "unsaved_edits"
SETTING_REFUSED = "setting_refused"
PROMPT_MISMATCH = "prompt_mismatch"
# A refusal that says nothing about the card itself: the creator can tap it again later.
TRANSIENT_REASONS = frozenset({TARGET_UNAVAILABLE})


def is_pin_id(chip_id: object) -> bool:
    return isinstance(chip_id, str) and chip_id.startswith(V2_ID_PREFIX)


def _norm(text: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def action_digest(action: Action) -> str:
    return hashlib.sha256(_canonical(action.model_dump(mode="json")).encode()).hexdigest()[:32]


def projected_digest(projected: Mapping[str, Any]) -> str:
    """Fingerprint of ``editor_base.projected``, the state an op list compiles against.

    Index-addressed ops (``remove_clip.slot_index``) are only meaningful against the exact slot
    order they were built for, so the commit re-checks this inside the row lock.
    """
    return hashlib.sha256(_canonical(projected).encode()).hexdigest()[:32]


# ── The ring ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredCard:
    set_id: str
    suggestion: Suggestion
    action_digest: str


def ring_suggestions(sset: SuggestionSet) -> list[dict[str, Any]]:
    """The ``suggestions`` value of a ring entry: what ``stored_card`` reads back."""
    return [s.model_dump(mode="json") for s in sset.suggestions]


def stored_card(
    state: Mapping[str, Any] | None, set_id: str, suggestion_id: str
) -> StoredCard | None:
    """The card the thread was offered, parsed back from the ring; None when it is not there.

    Only this thread's own state is read, so another thread's set can never resolve. A stored
    card that no longer parses is treated as missing, never repaired.
    """
    ring = state.get(RING_KEY) if isinstance(state, Mapping) else None
    if not isinstance(ring, list):
        return None
    for entry in reversed(ring):
        if not isinstance(entry, Mapping) or entry.get("set_id") != set_id:
            continue
        for raw in entry.get("suggestions") or []:
            if not isinstance(raw, Mapping) or raw.get("id") != suggestion_id:
                continue
            try:
                suggestion = Suggestion.model_validate(raw)
            except ValueError:
                log.warning("suggestion_pin.stored_card_invalid", set_id=set_id)
                return None
            return StoredCard(set_id, suggestion, action_digest(suggestion.action))
    return None


def _entry_index(ring: Sequence[Any], set_id: str) -> int | None:
    for index in range(len(ring) - 1, -1, -1):
        entry = ring[index]
        if isinstance(entry, Mapping) and entry.get("set_id") == set_id:
            return index
    return None


def is_consumed(state: Mapping[str, Any] | None, set_id: str, suggestion_id: str) -> bool:
    """True once a tap has applied this card from this set."""
    ring = state.get(RING_KEY) if isinstance(state, Mapping) else None
    index = _entry_index(ring, set_id) if isinstance(ring, list) else None
    consumed = ring[index].get(CONSUMED_FIELD) if index is not None else None
    return isinstance(consumed, Mapping) and suggestion_id in consumed


def _mark(
    state: Mapping[str, Any] | None, field: str, set_id: str, suggestion_id: str, turn_id: object
) -> dict[str, Any] | None:
    ring = state.get(RING_KEY) if isinstance(state, Mapping) else None
    index = _entry_index(ring, set_id) if isinstance(ring, list) else None
    if index is None or not isinstance(state, Mapping):
        return None
    entry = ring[index]
    prior = entry.get(field)
    prior = prior if isinstance(prior, Mapping) else {}
    marked = {**entry, field: {**prior, suggestion_id: str(turn_id)}}
    return {**state, RING_KEY: [*ring[:index], marked, *ring[index + 1 :]]}


def mark_consumed(
    state: Mapping[str, Any] | None, set_id: str, suggestion_id: str, turn_id: object
) -> dict[str, Any] | None:
    """``state`` with the card marked as applied by ``turn_id``; None when its entry is gone.

    Returns new containers all the way down to the entry. ``thread.state`` is a plain JSONB
    column, so a nested in-place edit compares equal to the loaded value and is never written.
    """
    return _mark(state, CONSUMED_FIELD, set_id, suggestion_id, turn_id)


def mark_refused(
    state: Mapping[str, Any] | None, set_id: str, suggestion_id: str, turn_id: object
) -> dict[str, Any] | None:
    """``state`` with the card marked as refused for ``turn_id``; None when its entry is gone.

    A refused card is not offered again from that set, and a read that finds a mark mints a new
    set (``with_pin_outcomes``), so the phone's "hidden until the set changes" ledger lets go.
    """
    return _mark(state, REFUSED_FIELD, set_id, suggestion_id, turn_id)


def stamp_pin(
    state: Mapping[str, Any] | None, tap: Mapping[str, Any] | None, message: str
) -> dict[str, Any] | None:
    """What a submitted turn stamps on the creator's event, or None when it is not a pin.

    * not a v2 card id: None (a KRI-756 chip keeps its legacy path);
    * a v2 id this thread was never offered: ``unresolved``, so the worker answers "no longer
      fits" instead of letting the planner read the words;
    * the message is not the card's prompt: None, the creator typed their own request (KRI-129);
    * otherwise the pin plus the digest of the action it resolved to.
    """
    if not tap:
        return None
    set_id, chip_id = str(tap.get("set_id") or ""), str(tap.get("chip_id") or "")
    if not is_pin_id(chip_id):
        return None
    card = stored_card(state, set_id, chip_id)
    if card is None:
        return {"set_id": set_id, "id": chip_id, "unresolved": True}
    if _norm(message) != _norm(card.suggestion.prompt):
        return None
    return {"set_id": set_id, "id": chip_id, "action_digest": card.action_digest}


@dataclass(frozen=True)
class Resolved:
    card: StoredCard | None = None
    reason: str | None = None


def resolve_pin(state: Mapping[str, Any] | None, pin: Mapping[str, Any], message: str) -> Resolved:
    """The worker's read of a stamped pin: the exact card the creator tapped, or a reason."""
    if pin.get("unresolved"):
        return Resolved(reason=UNRESOLVED)
    card = stored_card(state, str(pin.get("set_id") or ""), str(pin.get("id") or ""))
    if card is None:
        return Resolved(reason=UNRESOLVED)
    if _norm(message) != _norm(card.suggestion.prompt):
        return Resolved(reason=PROMPT_CHANGED)
    if card.action_digest != pin.get("action_digest"):
        return Resolved(reason=RING_CHANGED)
    return Resolved(card=card)


# ── What a tap may run ───────────────────────────────────────────────────────────────


def disallowed_op(
    ops: Sequence[Mapping[str, Any]], allow: frozenset[str] = PINNED_OP_ALLOW
) -> str | None:
    """The first op outside ``allow``, or None when every op is allowed."""
    for op in ops:
        name = str(op.get("op") or "")
        if name not in allow:
            return name or "?"
    return None


def parse_ops(ops: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any]) -> list[dict] | None:
    """Each op through the copilot's own parser against the CURRENT snapshot, no model.

    The parser coerces and validates exactly as it does for a copilot answer, so the draft path
    receives the same op shapes it always has. One refused op voids the whole list.
    """
    from app.agents.edit_copilot import (  # noqa: PLC0415
        EditorOperationParseState,
        parse_editor_operation,
    )

    state = EditorOperationParseState(1.0)
    parsed: list[dict] = []
    for op in ops:
        try:
            out = parse_editor_operation(dict(op), dict(snapshot), state)
        except Exception:  # noqa: BLE001 - a parser crash proves nothing, so refuse
            log.warning("suggestion_pin.parse_failed", op=str(op.get("op")), exc_info=True)
            return None
        if not out:
            return None
        parsed.append(out)
    return parsed


# What the creator asked for, as a typed requirement. Receipts (KRI-558) bind it to the diff by
# the words of its description, so the description is the card's own prompt: exactly the message
# the creator sees in the chat.
_CLIP_ID = re.compile(r"[A-Za-z0-9._:-]{1,100}")  # the brief's `clip:<id>` scope grammar
_REQUIREMENT_KIND = {
    "add_sfx": "audio",
    "set_mix": "audio",
    "set_transition": "style",
    "set_caption_meta": "style",
    "remove_clip": "select",
    "set_title": "text",
}


def brief_updates_for(suggestion: Suggestion, ops: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
    """One typed ``BriefUpdate`` for the tap, so the receipt names what changed and a later
    replan reads the change as a requirement instead of undoing it. No model, no extraction."""
    from app.kria.brief import BriefUpdate  # noqa: PLC0415

    first = str(ops[0].get("op") or "") if ops else ""
    kind = _REQUIREMENT_KIND.get(first)
    if kind is None:
        return ()
    scope = "global"
    media_id = suggestion.anchor.media_id
    if first == "remove_clip" and media_id and _CLIP_ID.fullmatch(media_id):
        scope = f"clip:{media_id}"
    elif first == "set_title":
        scope = "title"
    if first == "set_title":
        # The creator approved these exact words by tapping the card that showed them. The brief
        # holds them the way an approved wording always is (KRI-945): the literal plus the
        # server's marker, so the receipt can prove the title, and a replan keeps it. The
        # requirement's description stays the closed card sentence.
        from app.services.creative_copy_decisions import APPROVED_COPY_FACT  # noqa: PLC0415

        text = str(ops[0].get("title") or "")
        return (
            BriefUpdate(
                operation="add",
                kind=kind,
                scope=scope,
                literal=text,
                description=suggestion.prompt[:300],
                facts={APPROVED_COPY_FACT: {"target": "opening_title", "text": text}},
            ),
        )
    return (
        BriefUpdate(operation="add", kind=kind, scope=scope, description=suggestion.prompt[:300]),
    )


# ── Rebuild when the base moved ──────────────────────────────────────────────────────


def _group_targets(
    kind: registry.SuggestionKind,
) -> Callable[[SuggestionContext, str], list[Target]] | None:
    """How a registered kind rebuilds the several clips its card names, from the anchor alone.

    A card stores only its anchor (the first clip it removes); the duplicates card names the pair
    it came from too, so its targets are read back from the measurements as they are now. The
    match is by the kind object, not its name: a test double named "duplicates" is not it.
    """
    from app.services.suggestions.kinds import duplicates  # noqa: PLC0415

    return duplicates.rebuild_targets if kind is duplicates.KIND else None


def _targets(kind: registry.SuggestionKind, ctx: SuggestionContext, suggestion: Suggestion):
    anchor = suggestion.anchor
    moment = (anchor.moment.start_s, anchor.moment.end_s) if anchor.moment else None
    pinned_to = {
        "media_id": anchor.media_id,
        "clip_ordinal": anchor.clip_ordinal,
        "moment": moment,
        "text_bar_index": anchor.text_bar_index,
    }
    named = {key: value for key, value in pinned_to.items() if value is not None}
    group = _group_targets(kind)
    if group is not None:
        return group(ctx, anchor.media_id) if anchor.media_id else []
    if kind.legacy is not None:
        grounded = kind.legacy.ground(ctx)
        if not named:
            return grounded[:1]
        return [t for t in grounded if all(getattr(t, k) == v for k, v in named.items())][:1]
    if not named:
        return [NO_TARGET]
    return [Target(key=str(anchor.media_id or "-"), **named)]


async def ready_context(
    db: Any,
    *,
    thread: Any,
    item: Any,
    manifest: Any,
    target: Any,
    protocol: int | None,
) -> SuggestionContext:
    """The ``ready`` context for the editor target a tap runs on.

    ``load_suggestion_context`` cannot be reused inside the tap's own turn: an open turn makes it
    answer ``none``. This mirrors its ready branch for the facts a kind reads, from the target
    the seam already loaded. The kinds' own inputs (the hook's clip texts, the brief, the filler
    evidence, the duplicates' ``needed``) come from ``suggestion_serve.with_transcripts``: the one
    loader the GET and the worker also call, so a rebuild sees exactly what the serve saw.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
    from app.kria.reply_language import thread_reply_language  # noqa: PLC0415
    from app.models import Job  # noqa: PLC0415
    from app.services import suggestion_chips as chips  # noqa: PLC0415
    from app.services import suggestion_serve  # noqa: PLC0415
    from app.services.creative_copy_decisions import media_digest  # noqa: PLC0415

    rows = chips._clip_rows(item)
    job = await db.get(Job, target.job_id)
    variant = target.variant
    snapshot = target.snapshot
    common: dict[str, Any] = {}
    if settings.kria_suggestion_signals_enabled:
        from app.services import clip_signals  # noqa: PLC0415

        common["signals"] = {r["media_id"]: clip_signals.read_signals(r["_analysis"]) for r in rows}
        common["signals_version"] = clip_signals.signals_version()
    ctx = SuggestionContext(
        thread_id=str(thread.id),
        state="ready",
        language=chips.language_of(thread_reply_language(thread) or "en"),
        client_protocol=protocol,
        runtime_version=int(getattr(thread, "runtime_version", 1) or 1),
        media_digest=media_digest(snapshot_media(item)),
        manifest_hash=manifest.manifest_hash,
        render_generation_id=str(snapshot.get("base_generation") or "") or None,
        variant_id=str(variant.get("variant_id") or "") or None,
        resolved_archetype=str(variant.get("resolved_archetype") or "") or None,
        understanding="n/a",
        # Both completeness marks stay: the mute kind refuses a clip not marked understood.
        clips=[
            {k: v for k, v in r.items() if not k.startswith("_") or k in _KEPT_ROW_MARKS}
            for r in rows
        ],
        manifest=chips._manifest_view(manifest),
        snapshot=snapshot,
        edit_format=manifest.edit_format,
        job_id=str(target.job_id),
        creator_id=str(getattr(thread, "creator_id", "") or "") or None,
        editor_witness=chips.make_editor_witness(job, variant, snapshot),
        **common,
    )
    return await suggestion_serve.with_transcripts(db, thread, ctx)


_KEPT_ROW_MARKS = frozenset({"_empty", "_incomplete"})


async def rebuild_action(
    db: Any,
    *,
    suggestion: Suggestion,
    thread: Any,
    item: Any,
    manifest: Any,
    target: Any,
    protocol: int | None,
    ctx: SuggestionContext | None = None,
) -> Action | None:
    """Build the card's action again from the registry and the facts as they are now.

    None unless the kind is still switched on, still available, not already in the edit, and its
    own witness proves the rebuilt action against the current editor snapshot. Never raises.
    """
    kind = registry.get(suggestion.kind)
    if kind is None or "ready" not in kind.states or not registry.enabled(kind):
        return None
    if kind.min_protocol is not None and protocol is not None and protocol < kind.min_protocol:
        return None
    try:
        if ctx is None:
            ctx = await ready_context(
                db, thread=thread, item=item, manifest=manifest, target=target, protocol=protocol
            )
        if not kind.availability(ctx) or kind.already_present(ctx):
            return None
        targets = _targets(kind, ctx, suggestion)
        if not targets:
            return None
        action = kind.build_action(ctx, targets, suggestion.copy_text)
        if action is None or not kind.witness(action, ctx).ok:
            return None
        if not _says_what_was_offered(kind, suggestion, action, ctx, targets):
            return None
    except Exception:  # noqa: BLE001 - a kind that cannot rebuild is a card that no longer fits
        log.warning("suggestion_pin.rebuild_failed", kind=suggestion.kind, exc_info=True)
        return None
    return action


def _says_what_was_offered(
    kind: registry.SuggestionKind,
    suggestion: Suggestion,
    action: Action,
    ctx: SuggestionContext,
    targets: Sequence[Target],
) -> bool:
    """A rebuilt action must be the change the creator tapped, not merely a valid one.

    A hook's text is the card's own text; a closed-sentence card (clips named in its prompt) must
    render the same sentence from the rebuilt targets. Anything else drifted since it was served.
    """
    if isinstance(action, CopyDecisionAction):
        return action.text == suggestion.copy_text
    from app.services.suggestions.kinds import duplicates, mute_clip  # noqa: PLC0415

    if kind is duplicates.KIND or kind is mute_clip.KIND:
        from app.services.suggestion_chip_copy import render_suggestion_copy  # noqa: PLC0415

        ordinals = [t.clip_ordinal for t in targets if t.clip_ordinal is not None]
        rendered = render_suggestion_copy(suggestion.kind, ctx.language, ordinals)
        return rendered is not None and _norm(rendered[1]) == _norm(suggestion.prompt)
    return True


def proves(suggestion: Suggestion, action: Action, ctx: SuggestionContext) -> bool:
    """The kind's own witness accepts ``action`` on ``ctx`` (never raises)."""
    kind = registry.get(suggestion.kind)
    if kind is None:
        return False
    try:
        return bool(kind.witness(action, ctx).ok)
    except Exception:  # noqa: BLE001 - a witness that crashes proves nothing
        log.warning("suggestion_pin.witness_failed", kind=suggestion.kind, exc_info=True)
        return False


def kind_enabled(suggestion: Suggestion) -> bool:
    """False once the card's kind is unregistered or its own flag was switched off (a kill switch
    must stop a card that was served before it was pulled)."""
    kind = registry.get(suggestion.kind)
    return kind is not None and registry.enabled(kind)


def editor_ops(action: Action) -> list[dict[str, Any]] | None:
    """The op list of an editor-ops action; None for any other action type."""
    return list(action.ops) if isinstance(action, EditorOpsAction) else None


def copy_ops(action: Action) -> list[dict[str, Any]] | None:
    """The one op a hook card runs (its own exact title); None for any other action type.

    The title comes from the action's text and nowhere else: the card prompt never carries it.
    """
    if not isinstance(action, CopyDecisionAction) or action.role != "hook":
        return None
    from app.services.suggestions.kinds import hook  # noqa: PLC0415

    return [hook.set_title_op(action)]


def restate_updates(suggestion: Suggestion, action: Action) -> tuple[Any, ...]:
    """The typed ``BriefUpdate`` rows a restating tap writes: the card's own, never a model's.

    A brief-update card carries them (and is bound to a brief version); an item setting is one
    plain-language requirement, so the receipt names it and a later replan reads it as asked.
    """
    from app.kria.brief import BriefUpdate  # noqa: PLC0415

    if isinstance(action, BriefUpdateAction):
        return tuple(BriefUpdate.model_validate(u) for u in action.updates)
    if isinstance(action, ItemSettingAction):
        return (
            BriefUpdate(
                operation="add",
                kind="timing",
                scope="global",
                description=suggestion.prompt[:300],
                facts={"speech_cleanup": action.value},
            ),
        )
    return ()


async def restated_strategy(db: Any, *, thread_id: Any) -> dict[str, Any] | None:
    """The plan this thread's current video was made from: its newest strategy draft.

    None when there is none (a video made without a chat plan) or it no longer validates: a
    restate that cannot name the plan it restates is refused, never invented.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.models import CreatorEditDraft  # noqa: PLC0415

    rows = (
        await db.execute(
            select(CreatorEditDraft.snapshot_json)
            .where(CreatorEditDraft.thread_id == thread_id)
            .order_by(CreatorEditDraft.draft_revision.desc())
            .limit(25)
        )
    ).scalars()
    for snapshot in rows:
        if isinstance(snapshot, Mapping) and snapshot.get("kind") == "strategy":
            raw = snapshot.get("strategy")
            if not isinstance(raw, Mapping):
                return None
            try:
                return CreativeStrategy.model_validate(raw).model_dump(
                    mode="json", exclude_none=True
                )
            except ValueError:
                return None
    return None


async def head_hash(db: Any, *, thread_id: Any, ctx: SuggestionContext | None) -> str | None:
    """``snapshot_hash`` of the head draft of the variant a tap runs on (None: no draft yet).

    The commit compares it again inside the row lock, so a restate never replaces newer work.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import CreatorEditDraft  # noqa: PLC0415

    variant_key = str((ctx.variant_id if ctx is not None else None) or "initial")
    return (
        await db.execute(
            select(CreatorEditDraft.snapshot_hash)
            .where(
                CreatorEditDraft.thread_id == thread_id,
                CreatorEditDraft.variant_key == variant_key,
                CreatorEditDraft.is_head.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def restates_strategy(action: Action) -> bool:
    """A typed brief update or an item setting: no editor op can express it, so the tap restates
    the thread's current plan as a draft that the normal approval then renders."""
    return isinstance(action, (BriefUpdateAction, ItemSettingAction))


__all__ = [
    "COPY_OP_ALLOW",
    "PIN_KEY",
    "PINNED_OP_ALLOW",
    "StoredCard",
    "action_digest",
    "brief_updates_for",
    "copy_ops",
    "disallowed_op",
    "editor_ops",
    "is_consumed",
    "is_pin_id",
    "kind_enabled",
    "mark_consumed",
    "mark_refused",
    "parse_ops",
    "projected_digest",
    "proves",
    "rebuild_action",
    "restate_updates",
    "restated_strategy",
    "restates_strategy",
    "head_hash",
    "resolve_pin",
    "ring_suggestions",
    "snapshot_digest",
    "stamp_pin",
    "stored_card",
]
