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

import json
from collections.abc import Awaitable, Callable
from typing import Any

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
# Payload keys every draft carries for its own bookkeeping; a tap's creative change is the rest.
BOOKKEEPING = frozenset(
    {
        "base_generation",
        "copilot_receipt_ids",
        "deletions",
        "learning_temporary",
        "remove_music",
        "retry_guided_revision",
    }
)
# Keys the server keeps to itself: the phone gets a prompt to send and text to read, never the
# change that will be made (the Action stays server-side).
SERVER_ONLY_KEYS = frozenset(
    {"action", "ops", "op", "base_digest", "witness", "witness_tier", "expected_version", "updates"}
)
# What a restating card's staged requirement must say, and what the render then carries.
RESTATE_FACT = {"filler_words": {"speech_cleanup": True}, "mute_clip": {"clip_sound": "off"}}


RenderProbe = Callable[[Any, str], Awaitable[None]]


async def ensure_stored(phone: Phone, out) -> str:  # noqa: ANN001
    """``route`` when the real GET stored EVERY card it served in the ring, else ``missing``.

    Nothing is written on the phone's behalf: a tap against a ring the route did not fill is the
    bug this reports, so the run fails on the ``stored`` cell instead of papering over it. A set
    that kept only some of its cards fails too: the missing one answers "no longer fits".
    """
    entry = phone.ring_entry(out.set_id) or {}
    stored = {c.get("id") for c in entry.get("suggestions") or []}
    served = {c.id for c in out.suggestions}
    return "route" if served and served <= stored else "missing"


def leaked_keys(node: Any, path: str = "") -> list[str]:
    """Server-only keys anywhere in a JSON body (the phone must never see the Action)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SERVER_ONLY_KEYS:
                found.append(f"{path}.{key}")
            found += leaked_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += leaked_keys(value, f"{path}[{index}]")
    return found


def verify_served(cell: Cell, phone: Phone, card) -> None:  # noqa: ANN001
    """The card the phone parses: in the JSON of the real GET, with its closed prompt, no Action."""
    wire = phone.last_wire
    raw = next((c for c in wire.get("suggestions") or [] if c.get("id") == card.id), None)
    leaks = leaked_keys(wire)
    ok = bool(raw and raw.get("kind") == card.kind and raw.get("prompt") == card.prompt)
    if raw and card.kind == "hook":
        ok = ok and raw.get("copy_text") == card.copy_text and card.copy_text not in raw["prompt"]
    cell.check(
        "served",
        ok and not leaks,
        f"wire card keys {sorted(raw) if raw else None}; server-only keys on the wire: {leaks}",
    )


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
    creative = set(diff) - BOOKKEEPING
    detail = f"lanes changed: {sorted(creative)}"
    real = bool(
        lane in diff
        and diff[lane][0] != diff[lane][1]
        and (after or {}).get("snap", {}).get("kind") == "editor"
    )
    ok_direction, how = direction(kind, tap)
    detail += f"; {how}"
    cell.check("change", real and ok_direction, detail)
    # One card, one change: no other lane moved (a tap that also touched the mix, the captions or
    # the cut would be a second change the card never promised).
    cell.check("one_change", creative == {lane}, f"creative lanes changed: {sorted(creative)}")
    receipts = tap.receipts
    met = bool(receipts) and all(r.get("status") == "met" for r in receipts)
    shown = (
        f"receipts={[(r.get('status'), r.get('verification'), r.get('reason')) for r in receipts]}"
    )
    cell.check("receipts", met, shown)
    cell.na("render_kick", "an editor-ops tap edits the draft; the phone exports the video")


def payload_of(draft: dict[str, Any] | None) -> dict[str, Any]:
    return ((draft or {}).get("snap") or {}).get("editor_payload") or {}


def direction(kind: str, tap: Tap) -> tuple[bool, str]:
    """The change went the way the card promised, not just somewhere in the right lane."""
    before, after, card = tap.head_before, tap.head_after, tap.card
    was, now = payload_of(before), payload_of(after)
    if kind == "captions":
        on = (now.get("caption_meta") or {}).get("enabled") is True
        return on, f"captions enabled: {on}"
    if kind == "music":
        level = (now.get("mix") or {}).get("music_level")
        prior = (was.get("mix") or {}).get("music_level")
        lower = isinstance(level, int | float) and level < (1.0 if prior is None else prior)
        return bool(lower), f"music level {prior} -> {level}"
    if kind == "sfx":
        added = len(now.get("sound_effects") or []) - len(was.get("sound_effects") or [])
        return added == 1, f"sound effects added: {added}"
    if kind == "transition":
        slots = now.get("timeline_slots") or []
        eased = [r["slot_id"] for r in slots if r.get("transition_after") not in (None, "cut")]
        return bool(eased), f"slots with a transition now: {eased}"
    if kind == "duplicates":
        gone = removed_slots(after)
        return gone == {"slot-0"}, f"removed {sorted(gone)}"
    if kind == "hook":
        title = title_of(after)
        source = (tap.applied_event.get("creative_copy_resolution") or {}).get("source")
        return (
            title == card.copy_text and source == "suggestion",
            f"title {title!r} vs card {card.copy_text!r}; decision source {source}",
        )
    return True, ""


def verify_restate(cell: Cell, phone: Phone, tap: Tap) -> None:
    """Mute and filler restate the plan: the tap's change is a typed brief version or an item
    setting, plus the normal render approval (the render itself is a separate creator tap)."""
    seeded = phone.seeded
    kind = tap.card.kind
    version = h.briefs(seeded.thread_id)[-1]
    req = version.requirements[-1]
    approvals = d.approvals_pending(seeded.thread_id)
    owned = req.get("source_turn_id") == tap.accepted.turn_id
    # One change: the plan is restated as it stands. The tap adds its requirement or setting and
    # the approval to render; it does not re-plan the story behind the creator's back.
    was = ((tap.head_before or {}).get("snap") or {}).get("strategy")
    now = ((tap.head_after or {}).get("snap") or {}).get("strategy")
    cell_kind = ((tap.head_after or {}).get("snap") or {}).get("kind")
    cell_ok = bool(cell_kind == "strategy" and was is not None and was == now)
    d_detail = f"head kind {cell_kind}; strategy unchanged: {was == now}"
    if kind == "filler_words":
        flags = d.item_flags(seeded)
        ok = bool(
            tap.flags_before.get("speech_cleanup_enabled") is False
            and flags["speech_cleanup_enabled"] is True
            and flags["user_edited"] is True
            and req.get("facts") == {"speech_cleanup": True}
            and owned
            and approvals == 1
            and h.events(seeded.thread_id, "approval_requested")
        )
        cell.check(
            "change",
            ok,
            f"item {tap.flags_before} -> {flags}, requirement {req.get('facts')}, "
            f"approvals {approvals}",
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
    cell.check("one_change", cell_ok, d_detail)
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
        # before the pinned seam does. Nothing is written; it carries the typed signal and asks
        # for fresh cards like every other second tap.
        typed = bool(refused.get("outcome") == "refused" and "pinned_stale" in triggers)
        cell.check("second_tap", bool(quiet and typed), detail)
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
    """Undo the tap, then check the creator is back where they were: the draft, the render approval,
    the brief and the item's settings. False (and a failed check) when there was nothing to undo."""
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
    # The typed requirement a tap wrote (a hook's title, a filler's speech_cleanup, a mute's
    # clip_sound) is the creator's word as far as KRI-129 goes. Undo must take it back, or the
    # next approval is blocked by a request nobody typed.
    brief = d.live_brief(phone.seeded.thread_id)
    cell.check(
        "undo_brief",
        brief == tap.brief_before,
        f"requirements in force before the tap {_facts(tap.brief_before)}; "
        f"after the tap {_facts(tap.brief_after)}; after Undo {_facts(brief)}",
    )
    flags = d.item_flags(phone.seeded)
    cell.check(
        "undo_setting",
        flags == tap.flags_before,
        f"item settings before the tap {tap.flags_before}; after Undo {flags}",
    )
    return True


def _facts(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{r['id']}:{r['kind']}:{json.dumps(r['facts'], sort_keys=True)}" for r in rows]


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
    )
    for name in (
        "change",
        "one_change",
        "revision",
        "receipts",
        "render_kick",
        "consumed",
        "second_tap",
        "refreshed",
        "undo",
        "undo_brief",
        "undo_setting",
    ):
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
    probe_render: RenderProbe | None = None,
    state: str = "baseline",
    after_tap: Callable[[Cell, Phone, Tap], None] | None = None,
) -> list[Cell]:
    """Serve the thread, then tap each card in rank order. ``expect`` is the served kinds.

    ``refusals`` names a card the server serves but whose tap is refused today: ``{kind: (typed
    reason, gap text)}``. The check is a named gap that FAILS the run once the tap is accepted.
    ``probe_render`` runs the render step of a restating card on a twin thread (approving the
    staged render consumes the approval Undo needs, so the thread under test is left alone).
    ``state`` names what the thread went through before it was served (it labels the cells);
    ``after_tap`` adds checks that belong to that state to each applied tap.
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
        cell = rec.cell(scn.key, profile, card.kind, scn.language, state=state)
        cell.shipped = shipped
        cell.served, cell.rank = True, first_rank.setdefault(card.kind, card.rank)
        cell.action_type, cell.title, cell.prompt = card.action_type, card.title, card.prompt
        cells.append(cell)
        verify_served(cell, phone, card)
        how = await ensure_stored(phone, out)
        cell.check("stored", how == "route", f"stored by {how}")
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
            if after_tap is not None:
                after_tap(cell, phone, tap)
        verify_consumed(cell, phone, tap)
        commit_refresh = await verify_second_tap(cell, phone, tap, out, scn.language)
        others = [c.kind for c in remaining[1:]]
        await verify_refreshed(cell, phone, tap, out, others, commit_refresh=commit_refresh)
        await verify_undo(cell, phone, tap)
        if card.kind in RESTATE and tap.planned:
            if probe_render is not None:
                await probe_render(cell, card.kind)
            else:
                cell.na("render_kick", "not probed in this run")
        out = await phone.serve()
        verify_hidden_after(cell, card, out)
    return cells


async def verify_render_kick(cell: Cell, twin: Phone, kind: str, monkeypatch) -> None:  # noqa: ANN001
    """The restating card's second half, on a twin thread: tap it, approve the render it staged,
    and the render worker is handed the plan with the card's requirement in force.

    Filler: the item's cleanup setting is on when the render is dispatched and the brief binding the
    renderer honors carries ``speech_cleanup: true``. Mute: it carries ``clip_sound: off`` for the
    clip (whether the phone then mutes it is a device question, not provable here).
    """
    out = await twin.serve()
    card = next((c for c in out.suggestions if c.kind == kind), None)
    if card is None:
        cell.check("render_kick", False, f"the twin thread did not serve {kind}")
        return
    tap = await twin.tap(card, out)
    if not (tap.planned and tap.result.get("status") == "awaiting_approval"):
        cell.check(
            "render_kick", False, f"the twin tap was not accepted: {tap.refused} {tap.reply}"
        )
        return
    run = await twin.approve_render(monkeypatch)
    call = run.calls[0] if run.calls else {}
    binding = ((call.get("creator_brief_binding") or {}).get("brief") or {}).get("requirements")
    wanted = RESTATE_FACT[kind]
    carried = any((r.get("facts") or {}) == wanted for r in binding or [])
    flags = d.item_flags(twin.seeded)
    ok = bool(
        run.decision == "approved"
        and len(run.calls) == 1
        and run.execution_status == "dispatched"
        and run.job_id
        and carried
        and (kind != "filler_words" or flags["speech_cleanup_enabled"] is True)
    )
    cell.check(
        "render_kick",
        ok,
        f"decision {run.decision}; dispatch calls {len(run.calls)}; execution "
        f"{run.execution_status}; job {bool(run.job_id)}; brief binding carries {wanted}: "
        f"{carried}; item {flags}",
    )
