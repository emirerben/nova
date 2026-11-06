"""R5-S1: a served card is a stored card (set identity, and a ring write that did not happen).

Failure catalogue (written first; each row is a test below):

  I1  A per-kind switch is turned ON after the thread froze a set without that kind. The set id is
      the digest's and the digest did not name the enabled kinds, so once the 24 h freeze lapsed
      the same digest was rebuilt WITH the new kind's card under the SAME set id. The ring entry of
      that id already held cards and is written once, so the card was served and never stored: the
      tap answered "no longer fits" and the refusal hid the card. Hook, duplicates, filler words
      and mute, English and Turkish.
  I2  The same set id, a different set of cards (the agent's picks differ after the freeze lapsed).
      Whatever the cause, every card the GET serves must be one the ring entry of the SERVED set id
      holds: the ring entry takes the cards it lacks, it never drops the ones the phone holds.
  I3  The identity is not stable when nothing changed: the same footage, edit and switches give the
      same set id and the same cards on every read, and a switch put back restores the set the
      thread had before.
  I4  Every per-kind switch off: the digest and so the set id are what they were before the
      change (the flag-off bytes). Pinned by building the set with the registry's flags off.
  I5  The ring write is skipped (the thread row is locked past the write's ~0.35 s of retries): the
      GET used to return the v2 cards anyway. It must return the chips only (the cards are an
      enhancement of a set that serves without them), with the same set id, and the next GET, with
      the row free, stores and serves the cards.
  I6  The skipped write is not a silent one: the phone gets a set it can use. A tap on a legacy chip
      of the busy-window set still resolves (the chips are the contract that already shipped).

Every check drives the real path: GET route -> worker body with a cassette -> GET -> ring.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.database import sync_session
from app.models import CreationThread
from app.services.suggestions import registry
from tests.e2e_suggestions.flow import ensure_stored
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import (
    KEYS,
    PER_KIND,
    build,
    card_of,
    executes,
    keep_mute_card,
    kinds_of,
    stored_ids,
    unstored,
)
from tests.tasks import kria_pinned_suggestion_harness as h

COMBOS = [(key, kind) for key in KEYS for kind in PER_KIND]
IDS = [f"{key}:{kind}" for key, kind in COMBOS]
FREEZE_LAPSED_S = 25 * 3600


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "kind"), COMBOS, ids=IDS)
async def test_i1_a_kind_switched_on_after_the_freeze_is_a_card_the_ring_holds(
    talking_world, monkeypatch, key, kind
):
    flag, profile = PER_KIND[kind]
    keep_mute_card(monkeypatch, kind)
    rec = Recorder(f"R5 identity I1 {key} {kind} switched on after the freeze")
    where = f"{key}/{profile}/{kind}"
    monkeypatch.setattr(settings, flag, False)
    scene = build(talking_world, key, profile)
    first = await scene.phone.serve()
    rec.negative(
        f"I1 {flag} off: the first set has no {kind} card (the control)",
        where,
        f"no {kind}",
        card_of(first, kind) is None and bool(first.suggestions),
        f"served {kinds_of(first)}",
    )

    monkeypatch.setattr(settings, flag, True)
    talking_world.redis.advance(FREEZE_LAPSED_S)
    out = await scene.phone.serve()
    card = card_of(out, kind)
    rec.negative(
        f"I1 {flag} on: the {kind} card is offered",
        where,
        f"{kind} served",
        card is not None,
        f"served {kinds_of(out)}",
    )
    rec.negative(
        f"I1 {flag} on: every served card is one the ring holds for the served set id",
        where,
        "no served card missing from its ring entry",
        not unstored(scene.phone, out) and await ensure_stored(scene.phone, out) == "route",
        f"unstored {unstored(scene.phone, out)}; set {first.set_id} -> {out.set_id}",
    )
    rec.negative(
        f"I1 {flag} on: the set that offers the card is not the set that did not",
        where,
        "a new set id",
        out.set_id != first.set_id,
        f"set {first.set_id} -> {out.set_id}",
    )
    if card is not None:
        tap = await scene.phone.tap(card, out)
        rec.negative(
            f"I1 {flag} on: the served {kind} card executes",
            where,
            "planned, one new revision",
            executes(tap),
            f"signal={tap.refused} reply={tap.reply[:70]!r} drafts +{tap.new_drafts}",
        )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_i2_i3_the_same_inputs_give_the_same_set_and_a_switch_put_back_restores_it(
    talking_world, monkeypatch, key
):
    rec = Recorder(f"R5 identity I2/I3 {key}")
    where = f"{key}/signals"
    keep_mute_card(monkeypatch, "mute_clip")
    scene = build(talking_world, key, "signals")
    first = await scene.phone.serve()
    again = await scene.phone.get()
    rec.negative(
        "I3 nothing changed: the same set id and the same cards on every read",
        where,
        "same set id, same card ids",
        again.set_id == first.set_id
        and [c.id for c in again.suggestions] == [c.id for c in first.suggestions],
        f"{first.set_id} {kinds_of(first)} vs {again.set_id} {kinds_of(again)}",
    )
    monkeypatch.setattr(settings, "kria_suggestion_duplicates_enabled", False)
    off = await scene.phone.serve()
    monkeypatch.setattr(settings, "kria_suggestion_duplicates_enabled", True)
    back = await scene.phone.get()
    rec.negative(
        "I3 a switch put back: the set the thread had before comes back, same id, same cards",
        where,
        "same set id and cards as before the switch was pulled",
        back.set_id == first.set_id
        and [c.id for c in back.suggestions] == [c.id for c in first.suggestions],
        f"before {first.set_id} {kinds_of(first)}; off {off.set_id} {kinds_of(off)}; "
        f"back {back.set_id} {kinds_of(back)}",
    )
    rec.negative(
        "I2 every card of the set that came back is stored under the served set id",
        where,
        "no unstored card",
        not unstored(scene.phone, back),
        f"unstored {unstored(scene.phone, back)}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_i2_a_ring_entry_that_lacks_a_served_card_takes_it(talking_world, monkeypatch, key):
    """The same set id with a card its ring entry does not hold (the picks differ after the freeze
    lapsed, a write was lost): the GET stores the missing card next to the ones the entry holds."""
    keep_mute_card(monkeypatch, "mute_clip")
    rec = Recorder(f"R5 identity I2 merge {key}")
    where = f"{key}/signals"
    scene = build(talking_world, key, "signals")
    out = await scene.phone.serve()
    assert len(out.suggestions) >= 3, kinds_of(out)
    lost = out.suggestions[-1]
    kept = [c.id for c in out.suggestions[:-1]]
    with sync_session() as db:
        thread = db.get(CreationThread, scene.seeded.thread_id, with_for_update=True)
        state = dict(thread.state)
        ring = [dict(e) for e in state["suggestion_sets"]]
        entry = next(e for e in ring if e["set_id"] == out.set_id)
        entry["suggestions"] = [c for c in entry["suggestions"] if c["id"] != lost.id]
        state["suggestion_sets"] = ring
        thread.state = state
        db.commit()
    rec.negative(
        "I2 the control: the entry really lacks the card",
        where,
        "one card missing from the entry",
        unstored(scene.phone, out) == [lost.kind],
        f"unstored {unstored(scene.phone, out)}",
    )
    again = await scene.phone.get()
    rec.negative(
        "I2 the next read stores the missing card; the cards the entry held stay",
        where,
        "every served card stored, the held ones untouched",
        not unstored(scene.phone, again) and set(kept) <= stored_ids(scene.phone, again.set_id),
        f"served {kinds_of(again)}; unstored {unstored(scene.phone, again)}",
    )
    card = card_of(again, lost.kind)
    if card is not None:
        tap = await scene.phone.tap(card, again)
        rec.negative(
            f"I2 the card the entry lacked executes ({lost.kind})",
            where,
            "planned, one revision",
            executes(tap),
            f"signal={tap.refused} reply={tap.reply[:70]!r}",
        )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_i4_every_per_kind_switch_off_leaves_the_digest_as_it_was(
    talking_world, monkeypatch, key
):
    """No per-kind switch on and no freshness key: the input digest is the frozen formula of the
    code before the enabled kinds joined the identity (base, candidate ids, prompt version)."""
    import hashlib
    import json
    from types import SimpleNamespace

    from app.database import AsyncSessionLocal
    from app.models import CreationThread
    from app.services import suggestion_chips as sc
    from app.services import suggestion_serve as serve

    for kind in registry.all_kinds():
        if kind.flag is not None:
            monkeypatch.setattr(settings, kind.flag, False)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    scene = build(talking_world, key, "editor")
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, scene.seeded.thread_id)
        user = SimpleNamespace(id=scene.seeded.user_id)
        ctx = await sc.load_suggestion_context(db, thread, user, None)
        ctx = await serve.with_transcripts(db, thread, ctx)
        plan = sc.build_candidates(ctx)
    cids = ([plan.default.cid] if plan.default else []) + [c.cid for c in plan.levers]
    frozen = hashlib.sha256(
        json.dumps(
            {"base": plan.base_digest, "cids": cids, "prompt_version": sc.PROMPT_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]
    assert cids, "the control thread offered no candidate"
    assert plan.input_digest == frozen


# ── I5 / I6: the ring write that did not happen ──────────────────────────────────────


class RowLock:
    """The thread row held ``FOR UPDATE`` by another connection (a turn commit, a decision)."""

    def __init__(self, seeded: h.Seeded) -> None:
        self.seeded = seeded
        self.db = None

    def __enter__(self) -> RowLock:
        from sqlalchemy import select

        self.db = sync_session().__enter__()
        self.db.execute(
            select(CreationThread.id)
            .where(CreationThread.id == self.seeded.thread_id)
            .with_for_update()
        ).first()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        assert self.db is not None
        self.db.rollback()
        self.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_i5_i6_a_skipped_ring_write_serves_no_card_and_the_next_read_retries(
    talking_world, key
):
    rec = Recorder(f"R5 identity I5/I6 {key}")
    where = f"{key}/editor"
    scene = build(talking_world, key, "editor")
    pending = await scene.phone.get()
    await scene.phone.run_worker()
    with RowLock(scene.seeded):
        busy = await scene.phone.get()
    rec.negative(
        "I5 the ring write was skipped: the GET offers the legacy chips and no v2 card",
        where,
        "chips served, no suggestion and no digest on the wire",
        bool(busy.chips)
        and not busy.suggestions
        and "suggestions" not in scene.phone.last_wire
        and "digest" not in scene.phone.last_wire,
        f"status {busy.status}; cards {kinds_of(busy)}; chips {[c.kind for c in busy.chips]}",
    )
    free = await scene.phone.get()
    rec.negative(
        "I5 the row is free: the next read stores the cards and serves them",
        where,
        "cards served, all stored, same set id as the busy read",
        bool(free.suggestions) and not unstored(scene.phone, free) and free.set_id == busy.set_id,
        f"busy set {busy.set_id}; free set {free.set_id}; cards {kinds_of(free)}; "
        f"unstored {unstored(scene.phone, free)}",
    )
    card = card_of(free, "captions") or free.suggestions[0]
    tap = await scene.phone.tap(card, free)
    rec.negative(
        "I6 a card served after the retry executes",
        where,
        "planned, one revision",
        executes(tap),
        f"signal={tap.refused} reply={tap.reply[:70]!r}",
    )
    _ = pending
    rec.finish()
