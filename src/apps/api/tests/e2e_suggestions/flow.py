"""Tap every card a thread was served, and prove each tap, kind by kind.

One pass over a thread, in the order the phone would see it:

    serve -> tap -> (accepted, new revision with a real diff, receipts, chat line, consumed)
          -> tap the same card again (refused, nothing written, a refreshed set minted)
          -> Undo (the parent comes back) -> serve again -> the next card

Each assertion is recorded on a ``Cell`` instead of raised, so one broken kind never hides what
the others did; ``Recorder.finish`` fails the test with every failure at once. A check that fails
because of a defect in production code that this lane may not touch carries a named ``gap``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.database import AsyncSessionLocal
from app.models import CreationThread
from app.services import suggestion_chips as sc
from app.services import suggestion_serve as serve
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import gaps
from tests.e2e_suggestions.driver import Phone, Tap
from tests.e2e_suggestions.report import Cell, Recorder
from tests.e2e_suggestions.threads import Scenario
from tests.tasks import kria_pinned_suggestion_harness as h

# The editor lane a tap on each editor-ops kind is expected to move.
LANE = {
    "sfx": "sound_effects",
    "transition": "timeline_slots",
    "music": "mix",
    "captions": "caption_meta",
    "duplicates": "timeline_slots",
    "hook": "title",
}
RESTATE = frozenset({"mute_clip", "filler_words"})

GAP_RING = gaps.RING_NOT_STORED
GAP_NO_ENTRY = gaps.NO_ENTRY
GAP_TR_RECEIPT = gaps.TR_RECEIPT
GAP_RESTATE_DOUBLE_TAP = gaps.RESTATE_DOUBLE_TAP
GAP_RESTATE_NOT_READY = gaps.RESTATE_NOT_READY
GAP_REFUSED_BRIEF = gaps.REFUSED_BRIEF
GAP_FILLER_UNDO = gaps.FILLER_UNDO


async def ensure_stored(phone: Phone, out) -> str:  # noqa: ANN001
    """How this set's cards reached the ring: ``route`` (the real GET stored them), ``attached``
    (the set's entry was there without cards) or ``created`` (the GET wrote no entry at all).

    The last two are the gaps ``GAP_RING`` and ``GAP_NO_ENTRY``. The cards are then built the way
    the serve is meant to build them, inside a live session, with the serve's own helpers, and
    written the way ``persist_suggestion_set`` writes an entry, so the taps below still run
    against the ring the phone would have had. Once the route stores them this returns ``route``
    and nothing here runs.
    """
    entry = phone.ring_entry(out.set_id)
    if entry and entry.get("suggestions"):
        return "route"
    seeded = phone.seeded
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, seeded.thread_id)
        served = await serve.serve_suggestion_set(
            db, thread, SimpleNamespace(id=seeded.user_id), None
        )
        assert served.out.set_id == out.set_id, "the set moved while it was being stored"
        cards = await serve._server_cards(served)
        await db.rollback()
        assert cards, "the serve's own helper could not rebuild the cards in a live session"

        def write(state: dict) -> tuple[dict | None, str]:
            attached = serve.attach_cards(state, out.set_id, cards)
            if attached is not None:
                return attached, "written"
            ring = [e for e in state.get(sc.RING_KEY) or [] if isinstance(e, dict)]
            if any(e.get("set_id") == out.set_id for e in ring):
                return None, "current"
            entry = {
                "set_id": out.set_id,
                "state": out.state,
                "digest": served.input_digest,
                "media_digest": served.media_digest,
                "created_at": datetime.now(UTC).isoformat(),
                "final": True,
                "wording": out.wording,
                "prompt_version": sc.PROMPT_VERSION,
                "chips": [],
                "suggestions": cards,
            }
            return {**state, sc.RING_KEY: [*ring, entry][-sc.RING_SIZE :]}, "written"

        await serve._mutate_state(db, seeded.thread_id, write)
    return "attached" if entry else "created"


def removed_slots(head: dict[str, Any] | None) -> set[str]:
    payload = ((head or {}).get("snap") or {}).get("editor_payload") or {}
    return {r["slot_id"] for r in payload.get("timeline_slots") or [] if r.get("removed")}


def title_of(head: dict[str, Any] | None) -> Any:
    return (((head or {}).get("snap") or {}).get("editor_payload") or {}).get("title")


# ── Per-tap verification ─────────────────────────────────────────────────────────────


def verify_tap(cell: Cell, phone: Phone, tap: Tap) -> None:
    """Accepted, a new revision with a real diff, receipts and the chat line."""
    kind, card = tap.card.kind, tap.card
    restate = kind in RESTATE
    expected_status = "awaiting_approval" if restate else "completed"
    cell.check(
        "accepted",
        tap.result.get("status") == expected_status and tap.planned,
        f"status={tap.result.get('status')!r} pin={tap.pin} reply={tap.reply!r}",
    )
    cell.check(
        "chat",
        tap.chat == card.prompt and (kind != "hook" or card.copy_text not in tap.chat),
        f"chat={tap.chat!r} prompt={card.prompt!r}",
    )
    before, after = tap.head_before, tap.head_after
    moved = bool(
        tap.new_drafts == 1
        and after is not None
        and before is not None
        and after["parent"] == before["id"]
        and after["rev"] == before["rev"] + 1
    )
    cell.check(
        "revision",
        moved,
        f"drafts +{tap.new_drafts}, head rev "
        f"{before and before['rev']} -> {after and after['rev']}",
    )
    if restate:
        verify_restate(cell, phone, tap)
        return
    diff = d.lane_diff(before, after)
    lane = LANE[kind]
    detail = f"lanes changed: {sorted(diff)}"
    real = bool(
        lane in diff
        and diff[lane][0] != diff[lane][1]
        and (after or {}).get("snap", {}).get("kind") == "editor"
    )
    if kind == "duplicates":
        # The weaker take of the pair goes, not whichever clip sits at that index.
        real = real and removed_slots(after) == {"slot-0"}
        detail += f"; removed {sorted(removed_slots(after))}"
    if kind == "hook":
        resolution = tap.applied_event.get("creative_copy_resolution") or {}
        real = (
            real and title_of(after) == card.copy_text and resolution.get("source") == "suggestion"
        )
        detail += (
            f"; title {title_of(after)!r} vs card {card.copy_text!r}; "
            f"resolution {resolution.get('source')}"
        )
    cell.check("change", real, detail)
    receipts = tap.receipts
    met = bool(receipts) and all(r.get("status") == "met" for r in receipts)
    shown = (
        f"receipts={[(r.get('status'), r.get('verification'), r.get('reason')) for r in receipts]}"
    )
    partial_tr = bool(
        phone.language == "tr"
        and receipts
        and all(r.get("status") == "partial" and r.get("reason") for r in receipts)
    )
    cell.check("receipts", met, shown, gap=GAP_TR_RECEIPT if partial_tr else None)


def verify_restate(cell: Cell, phone: Phone, tap: Tap) -> None:
    """Mute and filler restate the plan: the tap's change is a typed brief version or an item
    setting, plus the normal render approval (the render itself is a separate creator tap)."""
    seeded = phone.seeded
    kind = tap.card.kind
    version = h.briefs(seeded.thread_id)[-1]
    req = version.requirements[-1]
    approvals = d.approvals_pending(seeded.thread_id)
    owned = req.get("source_turn_id") == tap.accepted.turn_id
    if kind == "filler_words":
        flags = d.item_flags(seeded)
        ok = bool(
            flags["speech_cleanup_enabled"] is True
            and flags["user_edited"] is True
            and req.get("facts") == {"speech_cleanup": True}
            and owned
            and approvals == 1
            and h.events(seeded.thread_id, "approval_requested")
        )
        cell.check(
            "change", ok, f"item {flags}, requirement {req.get('facts')}, approvals {approvals}"
        )
    else:
        ok = bool(
            req.get("kind") == "audio"
            and req.get("scope") == f"clip:{tap.card.anchor.media_id}"
            and req.get("facts") == {"clip_sound": "off"}
            and owned
            and approvals == 1
        )
        cell.check(
            "change",
            ok,
            f"requirement {req.get('scope')} {req.get('facts')}, approvals {approvals}",
        )
    if tap.receipts:
        cell.check(
            "receipts", all(r.get("status") == "met" for r in tap.receipts), str(tap.receipts)
        )
    else:
        cell.na(
            "receipts",
            "a restating tap stages a render approval; receipts are judged on the render",
        )


def verify_consumed(cell: Cell, phone: Phone, tap: Tap) -> None:
    entry = phone.ring_entry(tap.set_id) or {}
    marked = (entry.get("consumed") or {}).get(tap.card.id)
    cell.check(
        "consumed",
        marked == tap.accepted.turn_id,
        f"ring consumed={entry.get('consumed')} turn={tap.accepted.turn_id}",
    )


async def verify_second_tap(cell: Cell, phone: Phone, tap: Tap, out, language: str) -> bool:  # noqa: ANN001
    """The same card tapped again: refused plainly, nothing written, a refresh asked for.

    Returns whether the applying tap had asked the suggester for fresh cards (``draft_commit``).
    """
    world = phone.world
    commit_refresh = "draft_commit" in h.world_refreshes(world)
    world.refreshes.clear()
    again = await phone.tap(tap.card, out)
    expected = d.NO_LONGER_FITS_TR if language == "tr" else d.NO_LONGER_FITS
    triggers = h.world_refreshes(world)
    refused = again.refused or {}
    quiet = bool(again.new_drafts == 0 and again.before == again.after and again.reply)
    detail = (
        f"reply={again.reply!r} signal={refused} rows {again.before} -> {again.after} "
        f"refresh={triggers}"
    )
    if tap.card.kind in RESTATE:
        # The approval the first tap staged is still pending, so the pending-draft guard answers
        # before the pinned seam does. Nothing is written; the typed signal is what is missing.
        typed = bool(refused.get("outcome") == "refused" and "pinned_stale" in triggers)
        if quiet and typed:
            cell.check("second_tap", True, detail)
        elif quiet:
            cell.check("second_tap", False, detail, gap=GAP_RESTATE_DOUBLE_TAP)
        else:
            cell.check("second_tap", False, detail)
        return commit_refresh
    ok = bool(
        quiet
        and again.result.get("status") == "completed"
        and again.reply == expected
        and refused.get("outcome") == "refused"
        and refused.get("id") == tap.card.id
        and "pinned_stale" in triggers
    )
    cell.check("second_tap", ok, detail)
    return commit_refresh


async def verify_refreshed(
    cell: Cell,
    phone: Phone,
    tap: Tap,
    out,  # noqa: ANN001
    others: list[str],
    *,
    commit_refresh: bool,
) -> Any:
    """Both asks for fresh cards were made, and the next read is a NEW set without the card while
    the other cards stay tappable. A restating card leaves an approval pending, and a pending turn
    serves no cards at all (state ``none``): the ideas come back once it is resolved."""
    nxt = await phone.serve()
    ids = [c.id for c in nxt.suggestions]
    kinds = [c.kind for c in nxt.suggestions]
    if tap.card.kind in RESTATE:
        cell.check(
            "refreshed",
            commit_refresh and nxt.state == "none" and not nxt.suggestions,
            f"draft_commit refresh asked: {commit_refresh}; pending approval serves state "
            f"{nxt.state!r} with cards {kinds}",
        )
        return nxt
    ok = bool(
        commit_refresh
        and nxt.set_id != out.set_id
        and tap.card.id not in ids
        and all(kind in kinds for kind in others)
    )
    cell.check(
        "refreshed",
        ok,
        f"draft_commit refresh asked: {commit_refresh}; set {out.set_id} -> {nxt.set_id}; "
        f"kinds now {kinds}; expected still {others}",
    )
    return nxt


async def verify_undo(cell: Cell, phone: Phone, tap: Tap) -> bool:
    """Undo the tap. False (and a failed check) when the tap left nothing to undo."""
    if tap.new_drafts != 1:
        cell.check("undo", False, "the tap wrote no revision, so there is nothing to undo")
        return False
    parent = tap.head_before
    restored = await phone.undo()
    same = bool(restored is not None and parent is not None and restored["snap"] == parent["snap"])
    pending = d.approvals_pending(phone.seeded.thread_id)
    cell.check(
        "undo",
        same and pending == 0,
        f"head snapshot == parent: {same}; approvals pending {pending}",
    )
    if tap.card.kind == "filler_words":
        back = d.item_flags(phone.seeded)["speech_cleanup_enabled"] is False
        cell.check(
            "undo_setting",
            back,
            f"speech_cleanup_enabled back to False: {back}",
            gap=GAP_FILLER_UNDO,
        )
    return True


def verify_hidden_after(cell: Cell, card, out) -> None:  # noqa: ANN001
    """A handled card never comes back, even after Undo (the thread remembers it was offered)."""
    back = [c.kind for c in out.suggestions if c.kind == card.kind]
    prior = cell.checks.get("consumed", {"status": "pass", "detail": ""})
    if prior["status"] == "pass" and back:
        cell.check("consumed", False, f"ring marked it, but the card is served again: {back}")


def verify_refused_at_tap(cell: Cell, phone: Phone, tap: Tap, reason: str, gap: gaps.Gap) -> None:
    """A card the server serves but whose tap is refused with a typed reason (a named gap)."""
    refused = tap.refused or {}
    written = {k: tap.after[k] - tap.before[k] for k in tap.after if tap.after[k] != tap.before[k]}
    # A refused tap must change nothing a creator could see: no draft, approval or job. (The brief
    # version is checked on its own below.)
    quiet = (
        not (written.keys() - {"briefs"})
        and not d.item_flags(phone.seeded)["speech_cleanup_enabled"]
    )
    typed = refused.get("outcome") == "refused" and refused.get("reason") == reason
    cell.check(
        "accepted",
        False,
        f"refused {refused}; reply={tap.reply[:90]!r}; rows written {written}",
        gap=gap if (typed and quiet) else None,
    )
    cell.check(
        "refused_writes_no_brief",
        "briefs" not in written,
        f"the refused tap wrote {written.get('briefs', 0)} brief version(s): the card's "
        "`speech_cleanup: true` is now a live requirement the thread never applied",
        gap=GAP_REFUSED_BRIEF if "briefs" in written else None,
    )
    for name in ("change", "revision", "receipts", "consumed", "second_tap", "refreshed", "undo"):
        cell.na(name, "the tap was refused: there is no change to verify")
    cell.check("chat", tap.chat == tap.card.prompt, f"chat={tap.chat!r}")


# ── One thread, every card ───────────────────────────────────────────────────────────


async def tap_every_card(
    rec: Recorder,
    phone: Phone,
    scn: Scenario,
    profile: str,
    *,
    expect: list[str],
    shipped: bool = True,
    refusals: dict[str, tuple[str, gaps.Gap]] | None = None,
    only: frozenset[str] | None = None,
) -> list[Cell]:
    """Serve the thread, then tap each card in rank order. ``expect`` is the served kinds.

    ``refusals`` names a card the server serves but whose tap is refused today: ``{kind: (typed
    reason, gap text)}``. The check is a named gap that FAILS the run once the tap is accepted.
    """
    refusals = refusals or {}
    out = await phone.serve()
    served_kinds = [c.kind for c in out.suggestions]
    rec.negative(
        "served set is what the evidence and the cassette order give",
        f"{scn.key}/{profile}",
        f"cards {expect}",
        served_kinds == expect,
        f"served {served_kinds}",
    )
    cells: list[Cell] = []
    tapped: list[str] = []
    first_rank = {c.kind: c.rank for c in out.suggestions}
    while True:
        remaining = [
            c for c in out.suggestions if c.kind not in tapped and (only is None or c.kind in only)
        ]
        if not remaining:
            break
        card = remaining[0]
        cell = rec.cell(scn.key, profile, card.kind, scn.language)
        cell.shipped = shipped
        cell.served, cell.rank = True, first_rank.setdefault(card.kind, card.rank)
        cell.action_type, cell.title, cell.prompt = card.action_type, card.title, card.prompt
        cells.append(cell)
        cell.check("served", True, "")
        how = await ensure_stored(phone, out)
        cell.check(
            "stored",
            how == "route",
            f"stored by {how}",
            gap={"route": None, "attached": GAP_RING, "created": GAP_NO_ENTRY}[how],
        )
        tap = await phone.tap(card, out)
        tapped.append(card.kind)
        if card.kind in refusals:
            reason, gap = refusals[card.kind]
            if tap.refused is None:
                cell.check("accepted", True, "the tap was accepted", gap=gap)  # gap closed
            else:
                verify_refused_at_tap(cell, phone, tap, reason, gap)
                out = await phone.serve()
                continue
        else:
            verify_tap(cell, phone, tap)
        verify_consumed(cell, phone, tap)
        commit_refresh = await verify_second_tap(cell, phone, tap, out, scn.language)
        others = [c.kind for c in remaining[1:]]
        await verify_refreshed(cell, phone, tap, out, others, commit_refresh=commit_refresh)
        await verify_undo(cell, phone, tap)
        out = await phone.serve()
        verify_hidden_after(cell, card, out)
    return cells
