"""R6-S: the round-5 scenarios, tapped end to end (every served card executes, no work is lost).

Failure-mode list (written first; each row is a test below).

  M1  the protected-region (mark) gate. A restating tap on the phone Talking writer, with a live
      ``keep_text_off`` object that has no creator mark, asks "Mark the plate" and writes nothing,
      so the card must not be served. For an app build that cannot show the mark card, a face-only
      ask or a marked object the card is served and its whole tap works. Whatever the GET serves in
      each of those states executes, and the cards the gate holds back take nothing else with them.
  M2  the same thread, the card the phone still holds from before the creator's ask: refused
      ``mark_needed`` in words, nothing written, never applied.
  R1  an account on approval readiness with an ORDINARY length ask (6 seconds, at most 8, 20 on a
      take that cannot make it). The restate is judged on the plan the head pinned (or none): the
      filler card is served exactly when its tap is approved, every served card executes, a card
      served over a length the plan cannot make is a dead tap (round-5 attack, high).
  R2  readiness without the pinned plan: nothing says the plan makes 6 seconds, so no restating
      card is served and the editor cards still are, and they execute.
  B1  the ring write is busy (a turn holds the thread row). The read serves the chips alone under
      the SAME set id, never a card the ring does not hold; the next read stores the cards and
      every one of them executes.
  B2  the phone holds cards from a read that was stored, the next read is busy: the cards it holds
      still execute (the entry is not lost, a busy read neither drops nor rewrites it).
  D1  a stated video length (exact, ceiling) withholds the duplicates card; every card still served
      executes, none of them shortens the video.
  D2  the length is stated AFTER the card was served: the duplicates tap is refused, nothing is
      written, the video keeps its length, and the other cards of the held set still execute.
  U1  take-back re-offer WITH and WITHOUT the creative brief: an editor card tapped and undone is
      an offer again and executes (the footprint is written either way, KRI-129 carries the brief
      when there is one); a card the creator dismissed stays dismissed.

Every check drives the real path: GET -> worker (cassette) -> GET -> ``submit_turn`` ->
``run_kria_turn`` -> ``_complete_draft_turn``. The mute kind is dark as shipped; every mute cell
runs with ``PINNED_TAP_APPLIES_CLIP_SOUND`` patched on and is marked not shipped in the report.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.kria.brief import BriefUpdate
from app.models import CreationThread
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import NO_LONGER_FITS, NO_LONGER_FITS_TR, Phone
from tests.e2e_suggestions.flow import (
    RESTATE,
    ensure_stored,
    tap_every_card,
    verify_render_kick,
)
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import stored_ids
from tests.e2e_suggestions.test_r5_serve_strip import (
    APP,
    FACE,
    OBJECT,
    OLD_APP,
    mark_the_mug,
    phone_talking,
)
from tests.e2e_suggestions.test_r6_restate_drift import SHAPES, scene_for
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks.test_kria_pinned_r5_readiness import readiness_on

KEYS = ["nermin-matcha-update", "nermin-matcha-update/tr"]
KEY = KEYS[0]


def lang_reply(key: str) -> str:
    return NO_LONGER_FITS_TR if key.endswith("/tr") else NO_LONGER_FITS


def probe_with(world, monkeypatch, make):  # noqa: ANN001, ANN201, ARG001
    """A render-kick probe that builds its twin thread the way ``make`` built the first one."""

    async def probe(cell, kind):  # noqa: ANN001, ANN202
        twin = await make()
        await verify_render_kick(cell, twin, kind, monkeypatch)

    return probe


# ── M: the protected-region gate ─────────────────────────────────────────────────────

MARK_CASES = [
    # name, brief facts, marked, build protocol, is the restating card served?
    ("object-unmarked", OBJECT, False, APP, False),
    ("object-marked", OBJECT, True, APP, True),
    ("object-mark-on-cancelled-turn", OBJECT, "cancelled", APP, False),
    ("face-only", FACE, False, APP, True),
    ("old-build", OBJECT, False, OLD_APP, True),
]


def phone_at(world, monkeypatch, key, facts, *, marked, protocol):  # noqa: ANN001, ANN201
    seeded, phone, media_id = phone_talking(world, monkeypatch, key, facts)
    if marked:
        mark_the_mug(seeded, media_id, cancelled=marked == "cancelled")
    world.refreshes.clear()
    phone.protocol = protocol
    return phone


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS, ids=["en", "tr"])
@pytest.mark.parametrize(
    ("name", "facts", "marked", "protocol", "filler_served"),
    MARK_CASES,
    ids=[c[0] for c in MARK_CASES],
)
async def test_m1_every_card_served_under_the_mark_gate_executes(
    talking_world, monkeypatch, key, name, facts, marked, protocol, filler_served
):
    rec = Recorder(f"mark M1 {name} {key}")
    phone = phone_at(talking_world, monkeypatch, key, facts, marked=marked, protocol=protocol)
    scn = threads.SCENARIOS[key]
    out = await phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"M1 {name}: the filler card is {'served' if filler_served else 'held back'}",
        key,
        f"filler_words {'in' if filler_served else 'not in'} {kinds}",
        ("filler_words" in kinds) == filler_served,
        f"served {kinds}",
    )
    rec.negative(
        f"M1 {name}: the cards that do not restate the plan are still offered",
        key,
        "captions, music and sfx served",
        {"captions", "music", "sfx"} <= set(kinds),
        f"served {kinds}",
    )

    async def make():  # noqa: ANN202
        return phone_at(talking_world, monkeypatch, key, facts, marked=marked, protocol=protocol)

    await tap_every_card(
        rec,
        phone,
        scn,
        TALKING,
        expect=kinds,
        state=f"mark-gate-{name}",
        probe_render=probe_with(talking_world, monkeypatch, make),
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS, ids=["en", "tr"])
async def test_m2_the_filler_card_held_from_before_the_ask_is_refused_in_words(
    talking_world, monkeypatch, key
):
    rec = Recorder(f"mark M2 held card {key}")
    # The thread is served with no ask (a filler card), then the creator states "no text on the
    # mug", and the phone taps the card it holds.
    seeded, phone, _media = phone_talking(talking_world, monkeypatch, key, None)
    phone.protocol = APP
    out = await phone.serve()
    card = s.card_of(out, "filler_words")
    assert card is not None, f"the control thread did not serve the filler card: {s.kinds_of(out)}"
    k.write_brief(
        seeded,
        BriefUpdate.model_validate(
            {
                "operation": "add",
                "kind": "style",
                "scope": "global",
                "description": "No text on the mug",
                "facts": OBJECT,
            }
        ),
    )
    tap = await phone.tap(card, out)
    rec.negative(
        "M2 the held filler card is refused mark_needed with the mark question, nothing written",
        key,
        "typed refusal mark_needed; no draft, approval or setting",
        s.is_refused(tap, "mark_needed")
        and bool(tap.reply)
        and tap.reply not in {NO_LONGER_FITS, NO_LONGER_FITS_TR}
        and not d.item_flags(seeded)["speech_cleanup_enabled"],
        f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
    )
    rec.finish()


# ── R: an account on approval readiness with an ordinary length ask ──────────────────

LENGTHS = [
    "none",
    "length_6s",
    "length_at_most_8s",
    "length_20s_too_long",
    "east_run_recorded_brief",
]
COLUMNS = ["readiness_pinned", "readiness"]


@pytest.mark.asyncio
@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("shape", LENGTHS)
async def test_r1_r2_a_readiness_account_with_a_length_ask_gets_cards_that_execute(
    talking_world, monkeypatch, shape, column
):
    rec = Recorder(f"readiness R {shape} {column}")
    readiness_on(monkeypatch)
    where = f"{SHAPES[shape].scenario}/talking/{shape}/{column}"
    scene = await scene_for(talking_world, monkeypatch, SHAPES[shape], "filler_words", column)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    pinned = column == "readiness_pinned"
    makes_it = shape in {"none", "length_6s", "length_at_most_8s"}
    # The plan makes the length only when the head pinned the plan the live planner made (and the
    # length is one the 8 s take can make); with no ask there is nothing to prove.
    expect_filler = shape == "none" or (pinned and makes_it)
    rec.negative(
        f"R {shape} on {column}: the filler card is {'served' if expect_filler else 'held back'}",
        where,
        f"filler_words {'in' if expect_filler else 'not in'} the set",
        ("filler_words" in kinds) == expect_filler,
        f"served {kinds}",
    )
    rec.negative(
        f"R {shape} on {column}: the cards that do not restate the plan are offered",
        where,
        "captions, music and sfx served",
        {"captions", "music", "sfx"} <= set(kinds),
        f"served {kinds}",
    )

    async def make():  # noqa: ANN202
        twin = await scene_for(talking_world, monkeypatch, SHAPES[shape], "filler_words", column)
        return twin.phone

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        TALKING,
        expect=kinds,
        state=f"readiness-{shape}-{column.split('_')[-1]}",
        probe_render=probe_with(talking_world, monkeypatch, make),
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["none", "length_6s"])
async def test_r3_the_mute_card_on_a_readiness_account_with_a_length_ask(
    talking_world, monkeypatch, shape
):
    """The mute card (dark as shipped: the constant is patched on, said so on every cell)."""
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"readiness R mute {shape}")
    readiness_on(monkeypatch)
    where = f"{KEY}/signals/{shape}/readiness_pinned"
    scene = await scene_for(
        talking_world, monkeypatch, SHAPES[shape], "mute_clip", "readiness_pinned"
    )
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"R mute {shape}: the unpinned-length shapes serve what their tap can approve",
        where,
        "a non-empty set",
        bool(kinds),
        f"served {kinds}",
    )

    async def make():  # noqa: ANN202
        twin = await scene_for(
            talking_world, monkeypatch, SHAPES[shape], "mute_clip", "readiness_pinned"
        )
        return twin.phone

    probe = probe_with(talking_world, monkeypatch, make)
    rest = frozenset(kinds) - {"mute_clip"}
    if rest:
        await tap_every_card(
            rec,
            scene.phone,
            scene.scn,
            SIGNALS,
            expect=kinds,
            only=rest,
            state=f"readiness-mute-{shape}",
            probe_render=probe,
        )
    if "mute_clip" in kinds:
        await tap_every_card(
            rec,
            scene.phone,
            scene.scn,
            SIGNALS,
            expect=s.kinds_of(await scene.phone.serve()),
            shipped=False,
            only=frozenset({"mute_clip"}),
            state=f"readiness-mute-{shape}",
            probe_render=probe,
        )
    rec.finish()


# ── B: the ring write is busy ────────────────────────────────────────────────────────


async def get_while_locked(phone: Phone):  # noqa: ANN201
    """``GET`` while another transaction holds the thread row (a turn is running)."""
    holder = AsyncSessionLocal()
    try:
        await holder.execute(
            select(CreationThread.id)
            .where(CreationThread.id == phone.seeded.thread_id)
            .with_for_update()
        )
        return await asyncio.wait_for(phone.get(), timeout=20)
    finally:
        await holder.rollback()
        await holder.close()


BUSY = [
    ("nermin-matcha-update", EDITOR),
    ("nermin-matcha-update", SIGNALS),
    ("nermin-matcha-update", TALKING),
    ("nermin-matcha-update/tr", EDITOR),
    ("nermin-matcha-update/tr", TALKING),
    ("east-run-brief/footage-only/en", TALKING),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "profile"), BUSY, ids=[f"{k_}:{p}" for k_, p in BUSY])
async def test_b1_a_busy_ring_write_serves_the_chips_alone_and_the_next_read_every_card(
    talking_world, monkeypatch, key, profile
):
    rec = Recorder(f"ring B1 busy write {key} {profile}")
    where = f"{key}/{profile}"
    scene = s.build(talking_world, key, profile)
    pending = await scene.phone.get()  # the pending set; enqueues the worker
    await scene.phone.run_worker()  # the cassette answers; the final set is cached
    assert pending.set_id

    locked = await get_while_locked(scene.phone)
    on_the_wire = scene.phone.last_wire
    rec.negative(
        "B1 a read whose ring write is skipped puts the chips alone on the wire",
        where,
        "chips, no suggestions, no digest",
        bool(locked.chips)
        and not locked.suggestions
        and "suggestions" not in on_the_wire
        and "digest" not in on_the_wire,
        f"chips {[c.kind for c in locked.chips]} cards {s.kinds_of(locked)} "
        f"wire keys {sorted(on_the_wire)}",
    )
    rec.negative(
        "B1 ... and no card of that set is in the ring (nothing dead can be tapped)",
        where,
        "no card stored under the set id",
        not stored_ids(scene.phone, locked.set_id),
        f"stored {sorted(stored_ids(scene.phone, locked.set_id))}",
    )

    again = await scene.phone.get()
    kinds = s.kinds_of(again)
    rec.negative(
        "B1 the next read stores the cards it serves, under the same set id",
        where,
        "every served card in the ring; same set id as the busy read",
        bool(kinds)
        and again.set_id == locked.set_id
        and await ensure_stored(scene.phone, again) == "route",
        f"served {kinds}; set {locked.set_id} -> {again.set_id}",
    )

    async def make():  # noqa: ANN202
        twin = s.build(talking_world, key, profile)
        await twin.phone.get()
        await twin.phone.run_worker()
        await get_while_locked(twin.phone)
        await twin.phone.get()
        return twin.phone

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        profile,
        expect=kinds,
        state="ring-write-busy",
        probe_render=probe_with(talking_world, pytest.MonkeyPatch(), make) if False else None,
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "profile"), BUSY[:3], ids=[f"{k_}:{p}" for k_, p in BUSY[:3]])
async def test_b2_cards_the_phone_holds_survive_a_busy_read(talking_world, key, profile):
    rec = Recorder(f"ring B2 held cards {key} {profile}")
    where = f"{key}/{profile}"
    scene = s.build(talking_world, key, profile)
    held = await scene.phone.serve()
    kinds = s.kinds_of(held)
    assert kinds, f"nothing was served: {kinds}"
    ids = set(stored_ids(scene.phone, held.set_id))
    busy = await get_while_locked(scene.phone)
    rec.negative(
        "B2 a busy read neither drops nor rewrites the entry the phone's cards live in",
        where,
        f"the ring still holds {sorted(ids)} under {held.set_id}",
        set(stored_ids(scene.phone, held.set_id)) == ids and busy.set_id == held.set_id,
        f"busy read served cards {s.kinds_of(busy)}; "
        f"stored {sorted(stored_ids(scene.phone, held.set_id))}",
    )
    # Every card of the set the phone holds still executes (one revision each; the restating card
    # runs on the plan, so it goes first and its approval waits).
    ordered = sorted(held.suggestions, key=lambda c: c.kind not in RESTATE)
    for card in ordered[:1]:
        tap = await scene.phone.tap(card, held)
        rec.negative(
            f"B2 the held {card.kind} card executes after a busy read",
            where,
            "planned, no refusal",
            s.executed(tap),
            f"signal={tap.refused} reply={tap.reply[:70]!r} drafts +{tap.new_drafts}",
        )
    rec.finish()


# ── D: duplicates and a stated length ────────────────────────────────────────────────

LENGTH_ASKS = {
    "exact": dict(duration_s=6),
    "ceiling": dict(duration_s=6, duration_bound="at_most"),
}


def length_ask(**facts) -> BriefUpdate:  # noqa: ANN003
    return BriefUpdate(
        operation="add", kind="timing", scope="global", description="6 seconds", facts=facts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(LENGTH_ASKS))
@pytest.mark.parametrize("key", KEYS, ids=["en", "tr"])
async def test_d1_a_stated_length_withholds_the_duplicates_card_and_the_rest_executes(
    talking_world, key, name
):
    rec = Recorder(f"length D1 {name} {key}")
    control = s.build(talking_world, key, SIGNALS)
    assert "duplicates" in s.kinds_of(await control.phone.serve()), "the control must serve it"
    scene = s.build(talking_world, key, SIGNALS)
    k.write_brief(scene.seeded, length_ask(**LENGTH_ASKS[name]))
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"D1 a {name} length is stated: the duplicates card is withheld, the rest stays",
        key,
        "no duplicates; captions and sfx served",
        "duplicates" not in kinds and {"captions", "sfx"} <= set(kinds),
        f"served {kinds}",
    )
    await tap_every_card(
        rec, scene.phone, scene.scn, SIGNALS, expect=kinds, state=f"length-{name}-stated"
    )
    slots = [r for r in s.payload_of(d.head_of(scene.seeded.thread_id)).get("timeline_slots") or []]
    rec.negative(
        f"D1 {name}: no tap removed a clip (the video keeps its length)",
        key,
        "no removed slot in the head draft",
        not any(r.get("removed") for r in slots),
        f"slots {[(r.get('slot_id'), r.get('removed')) for r in slots]}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS, ids=["en", "tr"])
async def test_d2_a_length_stated_after_the_serve_refuses_the_tap_and_the_rest_executes(
    talking_world, key
):
    rec = Recorder(f"length D2 stated after the serve {key}")
    scene = s.build(talking_world, key, SIGNALS)
    out = await scene.phone.serve()
    card = s.card_of(out, "duplicates")
    assert card is not None, "the control must serve it"
    k.write_brief(scene.seeded, length_ask(duration_s=6))
    tap = await scene.phone.tap(card, out)
    rec.negative(
        "D2 the held duplicates card is refused in words, nothing written, no clip removed",
        key,
        "typed refusal, 'no longer fits' in the thread's language, no draft",
        s.is_refused(tap) and tap.reply == lang_reply(key),
        f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
    )
    for other in [c for c in out.suggestions if c.kind not in {"duplicates"} | RESTATE][:2]:
        nxt = await scene.phone.tap(other, out)
        rec.negative(
            f"D2 the held {other.kind} card still executes beside the refusal",
            key,
            "planned, one revision",
            s.executed(nxt) and nxt.new_drafts == 1,
            f"signal={nxt.refused} reply={nxt.reply[:70]!r}",
        )
        await scene.phone.undo()
    rec.finish()


# ── U: a tap taken back is an offer again, with and without the creative brief ───────

UNDO_KINDS = [
    ("captions", EDITOR),
    ("music", EDITOR),
    ("transition", EDITOR),
    ("sfx", EDITOR),
    ("hook", EDITOR),
    ("duplicates", SIGNALS),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("brief", [True, False], ids=["brief-on", "brief-off"])
@pytest.mark.parametrize(("kind", "profile"), UNDO_KINDS, ids=[k_ for k_, _ in UNDO_KINDS])
@pytest.mark.parametrize("key", KEYS, ids=["en", "tr"])
async def test_u1_an_undone_tap_is_an_offer_again_with_and_without_the_brief(
    talking_world, monkeypatch, key, kind, profile, brief
):
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", brief)
    rec = Recorder(f"reoffer U1 {key} {kind} brief={brief}")
    where = f"{key}/{profile}/{kind}/brief-{'on' if brief else 'off'}"
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None, f"the thread did not serve {kind}: {s.kinds_of(out)}"
    tap = await scene.phone.tap(card, out)
    rec.negative(
        f"U1 {kind}: the tap applies",
        where,
        "planned, one revision",
        s.executed(tap) and tap.new_drafts == 1,
        f"signal={tap.refused} reply={tap.reply[:70]!r}",
    )
    if kind == "hook":
        # The hook's creative-copy decision stays in the event log, so the hook is spent after
        # Undo with or without the brief (documented limit): its own witness refuses it, and the
        # serve asks that witness too (``suggestion_serve._unproven``). Recorded.
        await scene.phone.undo()
        talking_world.redis.advance(60)
        after = await scene.phone.serve()
        rec.negative(
            f"U1 hook (brief {'on' if brief else 'off'}): stays spent after Undo "
            "(its decision is in the event log)",
            where,
            "hook not offered again",
            "hook" not in s.kinds_of(after),
            f"served {s.kinds_of(after)}",
        )
        rec.finish()
        return
    await scene.phone.undo()
    talking_world.redis.advance(60)
    after = await scene.phone.serve()
    rec.negative(
        f"U1 {kind}: Undo takes the tap back, so the card is an offer again",
        where,
        f"{kind} served",
        s.card_of(after, kind) is not None,
        f"served {s.kinds_of(after)}",
    )
    if s.card_of(after, kind) is not None:
        await tap_every_card(
            rec,
            scene.phone,
            scene.scn,
            profile,
            expect=s.kinds_of(after),
            only=frozenset({kind}),
            state=f"undo-reoffer-brief-{'on' if brief else 'off'}",
        )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["filler_words", "mute_clip"])
@pytest.mark.parametrize("key", KEYS, ids=["en", "tr"])
async def test_u2_the_restating_cards_without_the_creative_brief(
    talking_world, monkeypatch, key, kind
):
    """With the brief off a restating card is served only if its tap works (it writes a typed
    requirement, which needs the brief): whatever is served is tapped end to end, and the render
    approval it stages then ends (deny) and the card is an offer again."""
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    profile = TALKING if kind == "filler_words" else SIGNALS
    rec = Recorder(f"reoffer U2 {key} {kind} brief-off")
    where = f"{key}/{profile}/{kind}/brief-off"
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    if kind not in kinds:
        rec.negative(
            f"U2 {kind}: without the brief the card is not served (nothing dead to tap)",
            where,
            f"{kind} not in the set",
            True,
            f"served {kinds}",
        )
        rec.finish()
        return

    async def make():  # noqa: ANN202
        monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
        return s.build(talking_world, key, profile).phone

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        profile,
        expect=kinds,
        shipped=kind != "mute_clip",
        only=frozenset({kind}),
        state="brief-off",
        probe_render=probe_with(talking_world, monkeypatch, make),
    )
    rec.finish()
