"""R5 defect 7 (GAP-13): a tap that is taken back is an offer again, even after the phone polled.

Failure-mode list (written before the code; each row is a fixture below).

A card that ran is hidden three ways: the ``consumed`` mark on its ring entry, the same
fingerprint folded into ``thread.state["suggestion_suppressed"]`` (the list that outlives the ring;
a GET writes it when it finds a mark), and a ``refused`` mark when a repeat of the tap was turned
away as ``consumed``. Taking the tap back (deny, expiry, the sweep, a cancel, the claim, Undo)
cleared only the first.

  W1  The phone polls the suggestions route while the approval is pending (the ordinary case: it
      polls on a backoff), the approval then ends without a render: the GET offers the card again
      and a tap on it stages exactly one approval. Every way an approval ends, the two cards that
      restate the plan (filler, mute), English and Turkish.
  W2  The card was ALREADY hidden for another reason before the tap (the creator dismissed it, the
      phone still held it): the take-back does not un-hide it. The kept fingerprint is the tap's
      to remove only when the tap is what added it.
  W3  A repeat of the tap, turned away as ``consumed``, left a ``refused`` mark that Undo never
      cleared, so the card stayed gone (and every GET minted a set without it).
  W4  The creative brief is off (the repo default): an editor-op tap writes no requirement, so it
      wrote no footprint at all, and Undo could not clear the applied mark. The card never came
      back. The footprint is written for every pinned tap.
  W5  A footprint that only holds the card's identity (no requirement, no setting) is for Undo
      alone: ending the staged approval leaves such a tap exactly as it was (the draft the tap
      wrote is still the head, so its card stays spent).
  W6  Not a take-back: an APPROVED render is in flight, the poll folds the mark, and the card stays
      hidden.

The driver is the one the pinned-tap fixtures use: the real GET route, the real worker body, the
real ``submit_turn`` and ``run_kria_turn``; Undo is ``undo_draft``; the approval ends through the
product's own entry points.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.runtime import cancel_turn
from app.models import CreationThread
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import conftest as e2e_world
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_r4_withdraw import (
    card_of,
    decide,
    lapse,
)

_dispose_async_engine = e2e_world._dispose_async_engine
world = e2e_world.world
talking_world = e2e_world.talking_world

KEYS = ["nermin-matcha-update", "nermin-matcha-update/tr"]
KINDS = {"filler_words": TALKING, "mute_clip": SIGNALS}
HOWS = ["deny", "lazy-expiry", "sweep", "cancel-turn", "undo"]
COMBOS = [
    (key, kind, how, reopened)
    for key in KEYS
    for kind in KINDS
    for how in HOWS
    for reopened in (False, True)
    if key == KEYS[0] or how in {"deny", "undo"}
]
IDS = [f"{k}:{kind}:{how}:{'polled' if r else 'unseen'}" for k, kind, how, r in COMBOS]


async def served(world, key: str = KEYS[0], profile: str = TALKING):  # noqa: ANN001, ANN201
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, world, prefs_of(key), language=scn.language)
    return seeded, phone, await phone.serve()


def prefs_of(key: str) -> list[dict]:
    import json

    from tests.tasks.test_kria_pinned_r4_withdraw import CASSETTES

    return json.loads((CASSETTES / f"{key.replace('/', '_')}.json").read_text("utf-8"))["prefs"]


def kinds(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


def kept(seeded) -> list[str]:  # noqa: ANN001
    with sync_session() as db:
        state = db.get(CreationThread, seeded.thread_id).state or {}
    return list(state.get("suggestion_suppressed") or [])


async def end_the_approval(seeded, phone, how: str, tap, monkeypatch) -> None:  # noqa: ANN001
    if how == "deny":
        await decide(seeded, "deny")
    elif how == "lazy-expiry":
        lapse(seeded)
        h.legacy_planner_answers(monkeypatch)
        accepted, _ = await h.submit(seeded, "Make the intro shorter")
        await h.run(accepted.turn_id)
    elif how == "sweep":
        from app.tasks.kria_runtime import _expire_pending_approvals

        lapse(seeded)
        _expire_pending_approvals()
    elif how == "cancel-turn":
        with sync_session() as db:
            revision = db.get(CreationThread, seeded.thread_id).revision
        async with AsyncSessionLocal() as db:
            await cancel_turn(
                db,
                thread_id=seeded.thread_id,
                turn_id=uuid.UUID(tap.accepted.turn_id),
                creator_id=seeded.user_id,
                expected_thread_revision=revision,
            )
    elif how == "undo":
        await phone.undo()
    else:  # pragma: no cover
        raise AssertionError(how)


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "kind", "how", "reopened"), COMBOS, ids=IDS)
async def test_w1_the_card_is_offered_again_once_its_approval_ended(
    talking_world, monkeypatch, key, kind, how, reopened
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    seeded, phone, out = await served(talking_world, key, KINDS[kind])
    card = card_of(out, kind)
    tap = await phone.tap(card, out)
    assert tap.planned and tap.result["status"] == "awaiting_approval", (tap.refused, tap.reply)
    if reopened:
        mid = await phone.get()
        assert not mid.suggestions, "a pending approval serves no card"

    await end_the_approval(seeded, phone, how, tap, monkeypatch)

    talking_world.redis.advance(60)
    again = await phone.serve()
    assert kind in kinds(again), (kinds(again), kept(seeded))
    retap = await phone.tap(card_of(again, kind), again)
    assert retap.planned and retap.new_drafts == 1, (retap.pin, retap.refused)
    assert d.approvals_pending(seeded.thread_id) == 1, "one approval waits, never a stack"


@pytest.mark.asyncio
async def test_w2_a_card_hidden_for_another_reason_stays_hidden(talking_world):
    from app.services.suggestion_serve import dismiss_suggestion

    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    async with AsyncSessionLocal() as db:
        outcome = await dismiss_suggestion(db, seeded.thread_id, out.set_id, card.id)
    assert outcome == "dismissed"
    assert "filler_words" not in kinds(await phone.get()), "dismissed: hidden"
    # The phone still holds the card (a dismiss is a hide, not a lock): it taps it anyway.
    tap = await phone.tap(card, out)
    assert tap.planned and tap.result["status"] == "awaiting_approval", (tap.refused, tap.reply)
    await phone.get()

    await decide(seeded, "deny")

    talking_world.redis.advance(60)
    again = await phone.serve()
    assert "filler_words" not in kinds(again), "the creator dismissed it; the take-back is not that"


@pytest.mark.asyncio
@pytest.mark.parametrize("triggers", [True, False], ids=["triggers-on", "triggers-off"])
@pytest.mark.parametrize("polled", [False, True], ids=["unseen", "polled"])
async def test_w3_a_repeat_turned_away_as_consumed_is_taken_back_with_the_tap(
    talking_world, monkeypatch, polled, triggers
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    seeded, phone, out = await served(talking_world, KEYS[0], EDITOR)
    card = card_of(out, "captions")
    first = await phone.tap(card, out)
    assert first.refused is None and first.new_drafts == 1, first.refused
    again = await phone.tap(card, out)
    assert (again.refused or {}).get("reason") == "consumed", again.refused
    if polled:
        await phone.get()

    await phone.undo()

    talking_world.redis.advance(60)
    after = await phone.serve()
    assert "captions" in kinds(after), (kinds(after), kept(seeded), after.set_id, out.set_id)
    retap = await phone.tap(card_of(after, "captions"), after)
    assert retap.refused is None and retap.new_drafts == 1, (retap.pin, retap.refused)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["captions", "music", "sfx"])
async def test_w4_without_the_creative_brief_undo_still_clears_the_applied_mark(
    talking_world, monkeypatch, kind
):
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    seeded, phone, out = await served(talking_world, KEYS[0], EDITOR)
    card = card_of(out, kind)
    tap = await phone.tap(card, out)
    assert tap.refused is None and tap.new_drafts == 1, tap.refused
    assert d.live_brief(seeded.thread_id) == [], "the brief is off: no requirement"

    await phone.undo()

    talking_world.redis.advance(60)
    after = await phone.serve()
    assert kind in kinds(after), kinds(after)
    retap = await phone.tap(card_of(after, kind), after)
    assert retap.refused is None and retap.new_drafts == 1, (retap.pin, retap.refused)


@pytest.mark.asyncio
async def test_w5_an_identity_only_footprint_is_for_undo_alone(talking_world, monkeypatch):
    """The draft an editor tap wrote is still the head after any approval of it ends, so ending
    one must not make its card an offer again; a tap that wrote a requirement or a setting is
    taken back as before."""
    from types import SimpleNamespace

    from app.kria.drafts import staged_tap_footprint_sync

    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    seeded, phone, out = await served(talking_world, KEYS[0], EDITOR)
    tap = await phone.tap(card_of(out, "captions"), out)
    assert tap.refused is None and tap.new_drafts == 1, tap.refused
    head = d.head_of(seeded.thread_id)
    approval = SimpleNamespace(draft_id=head["id"])
    with sync_session() as db:
        assert staged_tap_footprint_sync(db, approval) is None
    entry = phone.ring_entry(tap.set_id) or {}
    assert entry.get("consumed") == {tap.card.id: tap.accepted.turn_id}

    talking = await served(talking_world)
    filler_seeded, filler_phone, filler_out = talking
    await filler_phone.tap(card_of(filler_out, "filler_words"), filler_out)
    filler_head = d.head_of(filler_seeded.thread_id)
    with sync_session() as db:
        record = staged_tap_footprint_sync(db, SimpleNamespace(draft_id=filler_head["id"]))
    assert record is not None and record["setting"], record


@pytest.mark.asyncio
async def test_w6_an_approved_render_keeps_its_card_spent_after_a_poll(talking_world, monkeypatch):
    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    tap = await phone.tap(card, out)
    assert tap.planned
    await phone.get()

    dispatch = await phone.approve_render(monkeypatch)
    assert dispatch.decision == "approved"

    talking_world.redis.advance(60)
    again = await phone.serve()
    assert "filler_words" not in kinds(again)
