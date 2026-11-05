"""R5-G: a staged render that never happens, and the card that staged it.

A restating card (filler words, mute) does three things besides writing its draft: it flips the
item's cleanup setting and ``user_edited`` (filler), it writes a live typed requirement the creator
never typed, and it marks the card applied on its ring entry. The approval it stages is a separate
tap. When that approval ends without a render (denied, lapsed, swept, the video moved on, the turn
cancelled, Undo) the tap must be taken back, and the card must be what it was: an offer that
EXECUTES when tapped again.

Failure catalogue (written first; each row is a check of every combination below):

  L1  leftovers. After the approval ends the item's ``speech_cleanup_enabled`` / ``user_edited``,
      the live brief and the pending approvals are exactly what they were before the tap.
  L2  a card that comes back dead. The unmodified GET serves the card again (the tap was taken
      back, so it is an offer), and its tap executes with the full end-to-end checks: accepted,
      one change, receipts, Undo restores everything, a second tap is refused with a fresh set.
  L3  a card on the phone that no longer works. The creator did not refresh: the card of the set
      they were already holding is tapped again and stages exactly ONE approval (never a stack,
      never "no longer fits").
  L4  a stack. After the card is tapped twice (once before the event, once after) the thread holds
      one pending approval and one live requirement of the card's kind, not two.
  L5  the card is withdrawn while a render is IN FLIGHT. An approved approval is not taken back:
      the setting and the requirement stay, and the card stays spent.
  L6  the lifecycle is not the same in the other language. Every combination runs in English and
      Turkish.

How the approval ends (the product's own entry points, no row is edited): ``decide_approval`` deny;
the approval's time runs out and the creator's next typed message closes it (``submit_turn``);
the expiry sweep (``_expire_pending_approvals``); the session reads a newer video and the creator
approves (``approval_target_stale``); the creator cancels the awaiting turn (``cancel_turn``); Undo.
The mute kind runs with its dark constant patched on (``PINNED_TAP_APPLIES_CLIP_SOUND``): not what
ships, said so in the report.
"""

from __future__ import annotations

import pytest

from app.kria.runtime import RuntimeFailure
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import gaps
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.flow import tap_every_card, verify_render_kick
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h

KEYS = ["nermin-matcha-update", "nermin-matcha-update/tr"]
KINDS = {"filler_words": TALKING, "mute_clip": SIGNALS}
HOWS = ["deny", "lazy-expiry", "sweep", "newer-video", "cancel-turn", "undo"]
# What the approval's row says afterwards ("" = only that none is pending).
STATUS = {
    "deny": "denied",
    "lazy-expiry": "expired",
    "sweep": "expired",
    "newer-video": "cancelled",
    "cancel-turn": "cancelled",
    "undo": "",
}
# ``reopened``: the creator opens the chat while the approval is pending (the phone polls the
# suggestions route with a backoff, so this is the ordinary case), or nobody looks until it ends.
COMBOS = [
    (k, kind, how, reopened)
    for k in KEYS
    for kind in KINDS
    for how in HOWS
    for reopened in (False, True)
]
IDS = [f"{k}:{kind}:{how}:{'reopened' if r else 'unseen'}" for k, kind, how, r in COMBOS]


async def end_the_approval(scene: s.Scene, how: str, tap: d.Tap, monkeypatch) -> None:  # noqa: ANN001
    seeded = scene.seeded
    if how == "deny":
        await s.decide(seeded, "deny")
    elif how == "lazy-expiry":
        s.lapse_approval(seeded)
        h.legacy_planner_answers(monkeypatch)
        # In the thread's own language: a creator who writes English on a Turkish thread changes the
        # language the card's sentence is rebuilt in, which is a different test.
        said = "Girişi kısalt" if scene.scn.language == "tr" else "Make the intro shorter"
        accepted, _ = await h.submit(seeded, said)
        await h.run(accepted.turn_id)
    elif how == "sweep":
        s.lapse_approval(seeded)
        s.sweep_expired_approvals()
    elif how == "newer-video":
        s.session_moves_on(seeded)
        with pytest.raises(RuntimeFailure) as stale:
            await s.decide(seeded, "approve")
        assert stale.value.code == "approval_target_stale"
    elif how == "cancel-turn":
        await s.cancel_awaiting_turn(seeded, tap.accepted.turn_id)
    elif how == "undo":
        await scene.phone.undo()
    else:  # pragma: no cover
        raise AssertionError(how)


def requirements_of(kind: str, brief: list[dict]) -> list[dict]:
    fact = {"filler_words": "speech_cleanup", "mute_clip": "clip_sound"}[kind]
    return [r for r in brief if fact in r["facts"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "kind", "how", "reopened"), COMBOS, ids=IDS)
async def test_l1_l4_the_card_executes_again_after_its_approval_ended(
    talking_world, monkeypatch, key, kind, how, reopened
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    profile = KINDS[kind]
    rec = Recorder(f"lifecycle {key} {kind} {how} {'reopened' if reopened else 'unseen'}")
    where = f"{key}/{profile}/{kind}/{how}/{'reopened' if reopened else 'unseen'}"
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None, f"the thread did not serve {kind}: {s.kinds_of(out)}"
    flags_before = d.item_flags(scene.seeded)
    brief_before = d.live_brief(scene.seeded.thread_id)
    tap = await scene.phone.tap(card, out)
    assert tap.planned and tap.result["status"] == "awaiting_approval", (tap.refused, tap.reply)

    if reopened:
        # The chat is reopened while the approval is pending: nothing is offered over it.
        mid = await scene.phone.get()
        rec.negative(
            "a pending approval serves no card",
            where,
            "no cards",
            not mid.suggestions,
            f"state {mid.state!r} cards {s.kinds_of(mid)}",
        )

    await end_the_approval(scene, how, tap, monkeypatch)

    # L1: what the tap wrote is gone.
    flags = d.item_flags(scene.seeded)
    brief = d.live_brief(scene.seeded.thread_id)
    expected = STATUS[how]
    statuses = s.approval_statuses(scene.seeded)
    rec.negative(
        f"L1 {how}: the item's settings are what they were before the tap",
        where,
        f"{flags_before}",
        flags == flags_before,
        f"now {flags}",
    )
    rec.negative(
        f"L1 {how}: the requirement the tap wrote is gone",
        where,
        "the live brief is what it was",
        brief == brief_before,
        f"before {[r['facts'] for r in brief_before]}; now {[r['facts'] for r in brief]}",
    )
    rec.negative(
        f"L1 {how}: no approval is left pending"
        + (f" and the row says {expected}" if expected else ""),
        where,
        "none pending",
        d.approvals_pending(scene.seeded.thread_id) == 0 and (not expected or expected in statuses),
        f"approval statuses {statuses}",
    )

    # L3: the creator did not refresh; the card they hold works once. After a newer video it must
    # not: the plan it restates holds none of that video's edits, so it is refused base_moved.
    old = await (
        await lived_twin(talking_world, how, kind, profile, key, monkeypatch, reopened)
    ).tap_old_card(refused="base_moved" if how == "newer-video" else None)
    rec.negative(
        f"L3 {how}: the card on the phone (not refreshed) "
        + (
            "is refused base_moved, nothing written"
            if how == "newer-video"
            else "stages exactly one approval"
        ),
        where,
        "typed refusal, nothing written"
        if how == "newer-video"
        else "planned, awaiting approval, one pending approval, one live requirement of its kind",
        old["ok"],
        old["detail"],
    )

    # L2: the unmodified GET serves the card again, and it executes end to end.
    talking_world.redis.advance(60)
    again = await scene.phone.serve()
    served_again = s.card_of(again, kind) is not None
    if how == "newer-video":
        # The session reads a video the plan does not hold: no restating card may be served.
        rec.negative(
            "L2 newer-video: the GET does not serve a restating card over a plan that lacks the "
            "newer video",
            where,
            f"no {kind}",
            not served_again,
            f"served {s.kinds_of(again)}",
        )
        rec.finish()
        return
    rec.negative(
        f"L2 {how}: the unmodified GET serves the card again (the tap was taken back)",
        where,
        f"{kind} served",
        served_again,
        f"served {s.kinds_of(again)} (state {again.state!r}); the chat was "
        f"{'reopened while the approval was pending' if reopened else 'not opened'}",
        gap=gaps.WITHDRAWN_CARD_STAYS_HIDDEN if reopened else None,
    )
    if served_again:

        async def probe(cell, probed):  # noqa: ANN001, ANN202
            twin = s.build(talking_world, key, profile)
            await verify_render_kick(cell, twin.phone, probed, monkeypatch)

        await tap_every_card(
            rec,
            scene.phone,
            scene.scn,
            profile,
            expect=s.kinds_of(again),
            shipped=kind != "mute_clip",
            only=frozenset({kind}),
            state=f"after-{how}",
            probe_render=probe,
        )
    rec.finish()


# ── L3's driver: a twin that lived the same event, then taps the card it was holding ─────────


class _Twin:
    def __init__(self, scene: s.Scene, kind: str, card, out) -> None:  # noqa: ANN001
        self.scene, self.kind, self.card, self.out = scene, kind, card, out

    async def tap_old_card(self, refused: str | None = None) -> dict:
        scene, kind = self.scene, self.kind
        talking_before = d.live_brief(scene.seeded.thread_id)
        tap = await scene.phone.tap(self.card, self.out)
        if refused is not None:
            return {
                "ok": s.is_refused(tap, refused),
                "detail": f"signal={tap.refused} rows {tap.before} -> {tap.after}",
            }
        pending = d.approvals_pending(scene.seeded.thread_id)
        brief = d.live_brief(scene.seeded.thread_id)
        mine = requirements_of(kind, brief)
        ok = bool(
            tap.planned
            and tap.result["status"] == "awaiting_approval"
            and tap.new_drafts == 1
            and pending == 1
            and len(mine) == len(requirements_of(kind, talking_before)) + 1
        )
        return {
            "ok": ok,
            "detail": f"planned={tap.planned} refused={tap.refused} reply={tap.reply[:60]!r} "
            f"pending approvals {pending}; live requirements of the kind {len(mine)}",
        }


async def lived_twin(world, how, kind, profile, key, monkeypatch, reopened) -> _Twin:  # noqa: ANN001
    """A new thread that lives the same event, holding the card it was served before it."""
    twin = s.build(world, key, profile)
    out = await twin.phone.serve()
    card = s.card_of(out, kind)
    tap = await twin.phone.tap(card, out)
    if reopened:
        await twin.phone.get()
    await end_the_approval(twin, how, tap, monkeypatch)
    world.redis.advance(60)
    return _Twin(twin, kind, card, out)


# ── L5: a render in flight is not taken back ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(KINDS))
async def test_l5_an_approved_render_is_not_taken_back(talking_world, monkeypatch, kind):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    profile = KINDS[kind]
    rec = Recorder(f"lifecycle L5 {kind} approved")
    scene = s.build(talking_world, "nermin-matcha-update", profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    tap = await scene.phone.tap(card, out)
    run = await scene.phone.approve_render(monkeypatch)
    entry = scene.phone.ring_entry(tap.set_id) or {}
    brief = d.live_brief(scene.seeded.thread_id)
    rec.negative(
        f"L5 {kind}: approving the render keeps the tap (the render is in flight)",
        "nermin-matcha-update",
        "dispatched once; the requirement is live; the card stays spent; the filler setting is on",
        run.decision == "approved"
        and len(run.calls) == 1
        and bool(requirements_of(kind, brief))
        and (entry.get("consumed") or {}).get(card.id) == tap.accepted.turn_id
        and (kind != "filler_words" or d.item_flags(scene.seeded)["speech_cleanup_enabled"]),
        f"decision {run.decision}; dispatch calls {len(run.calls)}; consumed "
        f"{entry.get('consumed')}; requirements {[r['facts'] for r in brief]}",
    )
    rec.finish()
