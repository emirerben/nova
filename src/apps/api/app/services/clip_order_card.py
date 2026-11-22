"""The clip-order card: the draft message's numbered clip strip and the drag that edits it
(``KRIA_CLIP_ORDER_LEDGER_ENABLED`` plus a client-protocol capability, `clip_order_card`).

The card is a PROJECTION of the ledger (`clip_order_ledger`): nothing here stores state of its own.
It rides the assistant event payload beside ``clip_order_entries`` and is read back from the
thread's own events (the newest card says what the creator last saw, so ``moved`` and
``confirmed`` are derived, never stored elsewhere). A drag is one more ledger entry
(`creator_drag`) written by `apply_order`, the same function chat and the editor use, so the three
can never disagree about what the order is.

Pure functions over plain data: no I/O, no model. The only settings read is the gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Literal

from app.kria.reply_language import say
from app.services import clip_order_ledger as ledger
from app.services.clip_order_ledger import CREATOR_ACTORS, CREATOR_DRAG, LedgerEntry

CARD_KEY = "clip_order_card"
OPS_KEY = "clip_order_ops"
CARD_VERSION = 1

Gate = Literal["card", "text", "none"]


def card_gate(protocol: int | None) -> Gate:
    """What the sending app build gets for the order: ``card`` (the strip and the drag),
    ``text`` (the order listed as plain text) or ``none`` (nothing: flag off, or a caller that
    declares no build, such as the web).

    ``text`` is also what the kill switch (``CLIP_ORDER_CARD_MIN_CLIENT_PROTOCOL=999``) gives every
    declared build. Chat reorder works on every gate: the card is only a second way in.
    """
    from app.config import settings  # noqa: PLC0415

    if not ledger.clip_order_ledger_on() or protocol is None:
        return "none"
    return "card" if protocol >= settings.clip_order_card_min_client_protocol else "text"


def proposal_id(brief_version: int, order: Sequence[str]) -> str:
    """A stable digest of what the card shows: the brief version, the clips shown and their order.

    Two cards with the same id are the same offer; a client sends the id it showed and the server
    treats any other id as "the order moved since": it rebases, it never refuses.
    """
    encoded = json.dumps(
        {
            "v": CARD_VERSION,
            "brief": int(brief_version),
            "set": sorted(order),
            "order": list(order),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def card_applies(strategy: Mapping[str, Any] | None) -> bool:
    """Whether a draft's clip order is the upload order the creator can re-arrange.

    Fields only, never words. The same shape the render contract pins the upload order for
    (`creator_render_contract._unstated_order_defaults_to_attachment`): a guided edit format
    with the original or licensed audio, not the native program, and an order nobody asked to be
    random, group-first or by filming time (those orders are not the creator's to drag), with no
    ``order_by`` basis on any intent.
    """
    from app.agents._schemas.edit_format import GUIDED_EDIT_FORMATS  # noqa: PLC0415

    if not isinstance(strategy, Mapping):
        return False
    intents = strategy.get("resolved_clip_intents")
    return bool(
        strategy.get("edit_format") in GUIDED_EDIT_FORMATS
        and strategy.get("audio_strategy") in {"original_audio", "licensed_music"}
        and strategy.get("render_program") != "native"
        and strategy.get("ordering_choice") not in {"random", "group_first", "chronological"}
        and not any(
            isinstance(intent, Mapping) and intent.get("order_by")
            for intent in (intents if isinstance(intents, Sequence) else ())
        )
    )


def stated_ids(strategy: Mapping[str, Any] | None) -> frozenset[str]:
    """The clips whose own position the creator stated in this draft: a clip an `order` row names
    alone (a listed order, "end with the beach clip"). A clip inside a described group has the
    GROUP's place stated but not its own, so the order the clips play inside a group is still
    decided for the creator (the Toroko incident: groups ordered, the inner order unstated)."""
    from app.services.clip_order_sequence import sequence_rows  # noqa: PLC0415

    if not isinstance(strategy, Mapping):
        return frozenset()
    return frozenset(
        members[0]
        for _position, _name, members, status in sequence_rows(
            strategy.get("resolved_clip_intents"), seated=True
        )
        if status == "resolved" and len(members) == 1
    )


def played_ids(strategy: Mapping[str, Any] | None) -> frozenset[str] | None:
    """The clips the draft plays when it names a subset (`selected_media_ids`), else None (all of
    them). The strip lists only these: a clip the edit leaves out is not in the order the creator
    can see. Clips left out for another reason (a byte-identical copy dispatch drops, a source kept
    only for its voice) are not known here and are still listed."""
    selected = (strategy or {}).get("selected_media_ids")
    ids = frozenset(str(m) for m in selected or () if m)
    return ids or None


# ── reading the thread ─────────────────────────────────────────────────────────────────────────


def _card_of(payload: object) -> dict[str, Any] | None:
    raw = payload.get(CARD_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping) or not isinstance(raw.get("cards"), list):
        return None
    if not all(isinstance(c, Mapping) and isinstance(c.get("media_id"), str) for c in raw["cards"]):
        return None
    return dict(raw)


def last_card(events: Iterable[tuple[str, Mapping[str, Any] | None]]) -> dict[str, Any] | None:
    """The newest card the thread's assistant events carry: what the creator last saw."""
    found: dict[str, Any] | None = None
    for role, payload in events:
        if role == "assistant":
            found = _card_of(payload) or found
    return found


def card_order(card: Mapping[str, Any] | None) -> list[str]:
    return [str(c["media_id"]) for c in (card or {}).get("cards", ())]


def latest_event_is_a_drag(events: Sequence[tuple[str, Mapping[str, Any] | None]]) -> bool:
    """Whether the thread's newest assistant event is a drag's own: a second drag right after it
    is the same gesture, so it coalesces into one undo step (`LedgerEntry.coalesce`)."""
    for role, payload in reversed(events):
        if role != "assistant" or not isinstance(payload, Mapping):
            continue
        entries = [e for e in (payload.get(ledger.LEDGER_KEY) or ()) if isinstance(e, Mapping)]
        return bool(entries) and entries[-1].get("actor") == CREATOR_DRAG
    return False


def entries_after_draft(
    draft_id: str, events: Iterable[tuple[str, Mapping[str, Any] | None]]
) -> list[LedgerEntry]:
    """The ledger entries the thread wrote AFTER the draft ``draft_id`` was made, in order.

    ``events`` are ``(event_type, payload)`` of the assistant events. A malformed row is skipped.
    """
    seen = False
    out: list[LedgerEntry] = []
    for event_type, payload in events:
        if not isinstance(payload, Mapping):
            continue
        if event_type == "draft_applied" and payload.get("draft_id") == draft_id:
            seen, out = True, []
            continue
        if seen:
            out.extend(
                entry
                for entry in (
                    LedgerEntry.from_payload(raw) for raw in payload.get(ledger.LEDGER_KEY) or ()
                )
                if entry is not None and entry.before_ids != entry.after_ids
            )
    return out


def rebound(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any],
    entries: Sequence[LedgerEntry],
    attachment_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any], ledger.ClipOrderState]:
    """A draft's strategy and approval-bound media snapshot, carried to the order the ledger holds
    now: the snapshot stamps that order, and an order row the creator's drag moved away from is
    marked ``already_seated`` (the rule a fresh plan applies, `clip_order_ledger.mark_seated_rows`),
    so the render contract, readiness and the worker seat the dragged order and no older row undoes
    it. Pure: the caller decides whether the draft may be rewritten."""
    state = ledger.current_clip_order(attachment_ids=list(attachment_ids), entries=entries)
    new_strategy = dict(strategy)
    intents = strategy.get("resolved_clip_intents")
    if isinstance(intents, list) and intents:
        new_strategy["resolved_clip_intents"] = ledger.mark_seated_rows(
            intents,
            applied=ledger.applied_row_fingerprints(entries),
            fresh_order_ask=False,
            base_ids=state.order,
            known_ids=ledger.applied_known_ids(entries),
        )
    return new_strategy, ledger.with_clip_order_ids(media_snapshot, state), state


# ── the card ───────────────────────────────────────────────────────────────────────────────────


def decide_shown(
    *,
    total: int,
    sources: Mapping[str, str],
    previous: Mapping[str, Any] | None,
    applies: bool | None,
) -> bool:
    """Whether the strip is in the chat.

    Only with two or more clips, and only when something was decided FOR the creator: at least one
    position whose source is not a creator actor (`server_default`, `ai_story`). A draft
    (``applies`` known) also needs `card_applies`. A later card (a drag, a chat reorder,
    ``applies`` None) keeps the strip once it is in the chat, so the creator's own drag does not
    make it vanish; with no earlier card it follows the same "something was decided" rule.
    """
    if total < 2:
        return False
    if applies is False:
        return False
    if applies is None and previous is not None:
        return bool(previous.get("shown"))
    return any(source not in CREATOR_ACTORS for source in sources.values())


def build_card(
    state: ledger.ClipOrderState,
    assignments: Sequence[Mapping[str, Any]],
    *,
    events: Sequence[tuple[str, Mapping[str, Any] | None]],
    brief_version: int,
    applies: bool | None = None,
    groups: Mapping[str, str] | None = None,
    stated: Collection[str] = (),
    only: Collection[str] | None = None,
    confirm: bool | None = None,
    editable: bool = True,
    rebased: bool = False,
) -> dict[str, Any]:
    """The card for ``state`` (the order as it stands now), against what the creator last saw.

    ``moved`` marks the cards that moved since the newest card in ``events``; ``confirmed`` is the
    creator's "use this order" for exactly this order (a changed order is no longer confirmed);
    ``editable`` is False once a video exists (the timeline is the order, so a card cannot drag
    it). ``only`` limits the strip to the clips the edit plays. ``groups`` is the creator's own
    words for a clip's group; groups the previous card carried are kept, so a drag never loses a
    name.
    """
    previous = last_card(events)
    order = [m for m in state.order if only is None or m in only]
    before = card_order(previous)
    moved = set(ledger.moved_ids(before, order)) if before else set()
    carried = {
        str(c["media_id"]): str(c["group"])
        for c in (previous or {}).get("cards", ())
        if c.get("group")
    }
    words = {**carried, **(groups or {})}
    carried_source = {
        str(c["media_id"]): str(c["source"])
        for c in (previous or {}).get("cards", ())
        if c.get("source") in CREATOR_ACTORS
    }

    def source_of(media_id: str) -> str:
        # Who set the card's place: the ledger says who MOVED it; a clip nobody moved is still the
        # creator's when they stated its position (now, or on the card they last saw).
        moved_by = state.source_of(media_id)
        if moved_by != ledger.SERVER_DEFAULT:
            return moved_by
        if media_id in stated:
            return ledger.CREATOR_TEXT
        return carried_source.get(media_id, moved_by)

    sources = {media_id: source_of(media_id) for media_id in order}
    rows = [row for row in assignments if isinstance(row, Mapping)]
    labels = ledger.labels_for(order, rows, group_words=words)
    kind_of = {
        str(row.get("media_id")): "image" if row.get("kind") == "image" else "video" for row in rows
    }
    confirmed = (
        bool(confirm)
        if confirm is not None
        else bool(previous and previous.get("confirmed") and before == order)
    )
    cards = [
        {
            "media_id": media_id,
            "position": position,
            "label": labels.get(media_id) or f"Clip {position}",
            "kind": kind_of.get(media_id, "video"),
            "group": words.get(media_id),
            "source": sources[media_id],
            "moved": media_id in moved,
        }
        for position, media_id in enumerate(order, start=1)
    ]
    return {
        "version": CARD_VERSION,
        "proposal_id": proposal_id(brief_version, order),
        "shown": decide_shown(
            total=len(order),
            sources=sources,
            previous=previous,
            applies=applies,
        ),
        "confirmed": confirmed,
        "editable": editable,
        "cards": cards,
        "total": len(cards),
        **({"rebased": True} if rebased else {}),
    }


def listing_text(card: Mapping[str, Any]) -> str:
    """The order as plain numbered text, for a build that cannot show the strip."""
    cards = list(card.get("cards", ()))
    return ledger.order_listing(
        [str(c["media_id"]) for c in cards], {str(c["media_id"]): str(c["label"]) for c in cards}
    )


def with_listing(content: str | None, card: Mapping[str, Any]) -> str:
    """``content`` with the order listed after it (a draft message an old build reads)."""
    listing = listing_text(card)
    return f"{content}\n\n{listing}" if content else listing


# ── a drag ─────────────────────────────────────────────────────────────────────────────────────


def rebase_drag(current: Sequence[str], requested: Sequence[str]) -> list[str]:
    """The order a drag means, laid over the order as it stands now. Never refuses.

    The clips the creator named take their sequence in the slots those clips hold; a clip added
    since keeps its place; a clip removed since is dropped; a repeated or unknown id is ignored;
    an empty or all-unknown list changes nothing. A drag written against the current order is
    exactly that order. This IS `clip_order_ledger.apply_order` with the named clips as the known
    set, the same rebase a stale editor bundle gets.
    """
    return ledger.apply_order(current, requested, known=set(requested))


def drag_entry(
    current: Sequence[str],
    after: Sequence[str],
    turn_id: str | None,
    *,
    coalesce: bool,
) -> LedgerEntry | None:
    """The ledger entry for a drag that changed the order, else None."""
    if list(current) == list(after):
        return None
    return LedgerEntry(CREATOR_DRAG, tuple(current), tuple(after), turn_id, coalesce=coalesce)


def unavailable_reply() -> str:
    """What a build (or a server) that cannot take a card drag is told."""
    return say(
        en="I can't change the clip order from here right now. Tell me the order in the chat.",
        tr="Klip sırasını şu an buradan değiştiremiyorum. Sırayı bana sohbette yaz.",
    )


def stored_plan_note() -> str:
    """The one line a card turn's stored plan carries (admin view only, never a chat message)."""
    return say(
        en="Updated your clip order.",
        tr="Klip sıranı güncelledim.",
    )
