"""R6-O: a native Save, a card and an Undo, in every order a thumb can do them.

The rule under test: EVERY SERVED CARD EXECUTES AND NO CREATOR WORK IS LOST. A Save changes the
saved video without writing a draft, a card tap writes a draft on top of what the agent last read,
and Undo takes that draft back. The restating cards (filler words, mute) re-draft the PLAN and an
approved plan renders without anything the plan does not hold, so they may be offered, and may run,
only while the agent has read the saved video (the session's pointer, ``pinned.save_unread``). An
editor card is a delta on the saved video it names (``base_generation``), so it keeps the Save.

Failure-mode list (written first; each row is a test below).

  O1  Save -> editor card -> Undo. Undo lifted the fence the Save put up (GAP-12, closed in the
      round-5 seam lane): the filler card was served again over a plan that holds none of the Save,
      and approving it renders the video without the Save's edit.
  O2  editor card -> Save -> Undo. Undo restores a pointer the Save never moved: the Save is still
      unread, so the filler card must not come back either.
  O3  Save -> card A -> card B -> Undo -> Undo. The pointer follows the draft, not the Undo count:
      the fence is still down after the first Undo (card A's draft is the head and was built on the
      saved video) and the pointer is back where it was after the second.
  O4  Save -> card A -> a SECOND Save -> card B -> Undo -> Undo. B is built on the second Save, so
      it sits on the newest video; the fence stays up through every Undo.
  O5  Save -> card A -> Undo -> the agent reads the saved video (a Kria render completes). The
      fence comes down for good: the filler card is served and its whole tap works (approval,
      render hand-off). Undo never pulls back a pointer something else moved.
  O6  Save -> card A -> the same card again -> Undo. The repeat is a refusal in words that writes
      nothing, and the Undo that follows still gives the card back (one offer, one apply).

Along the way each editor tap is checked for the first half of "no work lost": it sits on the video
the Save minted (``base_generation``), applied once. After every order the second half: the saved
video is still the Save's (an Undo takes back a draft, never a Save), the card the phone held from
BEFORE the Save is refused ``base_moved`` with nothing written when it restates the plan, and the
unmodified GET followed by the full end-to-end tap of every card it serves. The Save is the real
``editor-commit`` route; the render worker's completion write and the agent reading the video are
the only stand-ins (see ``scenes``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.driver import NO_LONGER_FITS, NO_LONGER_FITS_TR
from tests.e2e_suggestions.flow import RESTATE, tap_every_card, verify_render_kick
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, TALKING

KEYS = ["nermin-matcha-update", "nermin-matcha-update/tr"]
IDS = ["en", "tr"]
# What a Save changes that no card is about: the caption size on a Talking take, the footage's own
# sound level on a montage.
FIRST = {TALKING: {"caption_meta": {"size_px": 96}}, EDITOR: {"mix": {"original_level": 0.8}}}
SECOND = {TALKING: {"caption_meta": {"size_px": 120}}, EDITOR: {"mix": {"original_level": 0.6}}}


def lang_reply(key: str) -> str:
    return NO_LONGER_FITS_TR if key.endswith("/tr") else NO_LONGER_FITS


@dataclass
class Played:
    """An order that has been done: the thread, the set the phone held BEFORE the Save, and the
    generations the Saves minted (the last one is the saved video now)."""

    scene: s.Scene
    first: object
    saves: list[str] = field(default_factory=list)
    pointer_at_start: str | None = None


async def tap_editor(
    played: Played, rec: Recorder | None, kind: str, *, where: str, note: str = ""
):  # noqa: ANN201
    """Serve, tap the ``kind`` card, and record that it applied once on the saved video."""
    scene = played.scene
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None, f"{where}: the thread did not serve {kind}: {s.kinds_of(out)}"
    tap = await scene.phone.tap(card, out)
    if rec is not None:
        saved = played.saves[-1] if played.saves else played.pointer_at_start
        base = s.payload_of(tap.head_after).get("base_generation")
        rec.negative(
            f"{kind} tapped {note}is applied once, on the video as it is saved now",
            where,
            f"planned, one revision, base_generation == {saved!r}",
            s.executed(tap) and tap.new_drafts == 1 and base == saved,
            f"planned={tap.planned} signal={tap.refused} drafts +{tap.new_drafts} "
            f"base_generation {base!r}, saved {saved!r}",
        )
    return tap


async def begin(world, key: str) -> Played:  # noqa: ANN001
    scene = s.build(world, key, TALKING)
    first = await scene.phone.serve()
    return Played(scene, first, pointer_at_start=s.pointer(scene.seeded))


async def save(played: Played, which: dict) -> str:
    generation = await s.route_save(played.scene.seeded, **which[TALKING])
    assert generation not in played.saves
    played.saves.append(generation)
    return generation


Order = Callable[[object, str, Recorder | None, str], Awaitable[Played]]


async def o1(world, key, rec, where) -> Played:  # noqa: ANN001
    played = await begin(world, key)
    await save(played, FIRST)
    await tap_editor(played, rec, "captions", where=where, note="after a Save ")
    await played.scene.phone.undo()
    return played


async def o2(world, key, rec, where) -> Played:  # noqa: ANN001
    played = await begin(world, key)
    await tap_editor(played, rec, "captions", where=where)
    await save(played, FIRST)
    await played.scene.phone.undo()
    return played


async def o3(world, key, rec, where) -> Played:  # noqa: ANN001
    played = await begin(world, key)
    await save(played, FIRST)
    await tap_editor(played, rec, "music", where=where, note="after a Save ")
    await tap_editor(played, rec, "sfx", where=where, note="on top of the first card ")
    await played.scene.phone.undo()
    mid = await played.scene.phone.serve()
    if rec is not None:
        rec.negative(
            "O3 after the first Undo the older draft is the head: no restating card",
            where,
            "filler_words not served",
            "filler_words" not in s.kinds_of(mid),
            f"served {s.kinds_of(mid)}",
        )
    await played.scene.phone.undo()
    return played


async def o4(world, key, rec, where) -> Played:  # noqa: ANN001
    played = await begin(world, key)
    await save(played, FIRST)
    await tap_editor(played, rec, "music", where=where, note="after a Save ")
    await save(played, SECOND)
    await tap_editor(played, rec, "sfx", where=where, note="after a second Save ")
    await played.scene.phone.undo()
    await played.scene.phone.undo()
    return played


async def o5(world, key, rec, where) -> Played:  # noqa: ANN001
    played = await begin(world, key)
    await save(played, FIRST)
    await tap_editor(played, rec, "music", where=where, note="after a Save ")
    await played.scene.phone.undo()
    generation = s.agent_reads_video(played.scene.seeded)
    if rec is not None:
        rec.negative(
            "O5 the agent's read moves the pointer to the saved video and Undo does not undo it",
            where,
            f"pointer == {generation!r}",
            s.pointer(played.scene.seeded) == generation == played.saves[-1],
            f"pointer {s.pointer(played.scene.seeded)!r}, saved {played.saves[-1]!r}",
        )
    return played


async def o6(world, key, rec, where) -> Played:  # noqa: ANN001
    played = await begin(world, key)
    await save(played, FIRST)
    out = await played.scene.phone.serve()
    card = s.card_of(out, "music")
    tap = await played.scene.phone.tap(card, out)
    again = await played.scene.phone.tap(card, out)
    if rec is not None:
        rec.negative(
            "O6 the same card tapped again is a refusal in words that writes nothing",
            where,
            "typed refusal, nothing written",
            s.executed(tap) and s.is_refused(again) and again.reply == lang_reply(key),
            f"first planned={tap.planned}; second signal={again.refused} "
            f"reply={again.reply[:60]!r} rows {again.before} -> {again.after}",
        )
    await played.scene.phone.undo()
    return played


# order -> (play, does the agent hold the saved video afterwards?)
ORDERS: dict[str, tuple[Order, bool]] = {
    "O1 Save, card, Undo": (o1, False),
    "O2 card, Save, Undo": (o2, False),
    "O3 Save, card, card, Undo, Undo": (o3, False),
    "O4 Save, card, Save, card, Undo, Undo": (o4, False),
    "O5 Save, card, Undo, the agent reads the video": (o5, True),
    "O6 Save, card, same card, Undo": (o6, False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(ORDERS), ids=[n.split(" ", 1)[0] for n in ORDERS])
@pytest.mark.parametrize("key", KEYS, ids=IDS)
async def test_every_order_ends_with_cards_that_execute_and_a_save_that_stands(
    talking_world, monkeypatch, key, name
):
    play, read = ORDERS[name]
    short = name.split(" ", 1)[0]
    rec = Recorder(f"save {short} order {key}")
    where = f"{key}/talking/{name}"
    played = await play(talking_world, key, rec, where)
    scene, held = played.scene, s.card_of(played.first, "filler_words")
    saved = played.saves[-1]

    rec.negative(
        f"{short}: an Undo takes a draft back, never the Save",
        where,
        f"the saved video is still the one the last Save minted ({saved!r})",
        s.baseline(scene.seeded) == saved,
        f"saved video now {s.baseline(scene.seeded)!r}",
    )
    # The card the phone held from before the Save restates the PLAN. It runs only if the agent
    # has read the saved video; otherwise it is refused with nothing written (checked on a twin
    # below, once the thread's own taps are done).
    talking_world.redis.advance(60)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"{short}: the filler card is "
        f"{'served (the agent has read the saved video)' if read else 'not served'}",
        where,
        f"filler_words {'in' if read else 'not in'} the set",
        ("filler_words" in kinds) == read,
        f"served {kinds}; pointer {s.pointer(scene.seeded)!r}, saved {s.baseline(scene.seeded)!r}",
    )
    rec.negative(
        f"{short}: the set still holds cards (the ones that do not restate the plan stay)",
        where,
        "a non-empty set",
        bool(kinds),
        f"served {kinds}",
    )

    async def probe(cell, kind):  # noqa: ANN001, ANN202
        if kind in RESTATE:
            twin = await play(talking_world, key, None, where)
            await verify_render_kick(cell, twin.scene.phone, kind, monkeypatch)
        else:
            cell.na("render_kick", "an editor-ops tap edits the draft")

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        TALKING,
        expect=kinds,
        state=f"order-{short.lower()}",
        probe_render=probe,
    )

    if held is not None and not read:
        # The same order again on a twin that kept its first set: the held filler card is refused
        # base_moved and writes nothing.
        twin = await play(talking_world, key, None, where)
        talking_world.redis.advance(60)
        tap = await twin.scene.phone.tap(s.card_of(twin.first, "filler_words"), twin.first)
        rec.negative(
            f"{short}: the filler card the phone held from before the Save is refused base_moved",
            where,
            "typed refusal base_moved, nothing written, the speech cleanup stays off",
            s.is_refused(tap, "base_moved"),
            f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
        )
    rec.finish()
