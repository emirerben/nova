"""The offered-set ring holds what the GET served: every card, so every card can be tapped.

Failure catalogue (written before the fixes; each row is a test below or in the sibling
``test_ring_*.py`` files). The rule: a card the phone shows is a card the ring can resolve.

  R1  the cards are built after the route's own ``db.rollback()``. The editor witness closes over
      the ORM ``Job``; a rollback expires it, ``compile_editor_ops`` raises ``MissingGreenlet``
      inside the witness (swallowed as "not proven"), every editor-ops card fails, nothing is
      attached, and a tap answers "no longer fits" (``unresolved``).
        a. editor triggers on, pinned taps on (the rollout configuration)
        b. editor triggers OFF, pinned taps on (a partial rollout): the route only called
           ``persist_suggestion_set`` (chips), so no card was ever written
        c. the ring write skipped because a turn holds the thread row: the NEXT read must store
           them (the cards are built again from a live session, not carried stale)
        d. pinned taps OFF: the entry is exactly the legacy entry, no ``suggestions`` key, and the
           GET never builds a card
  R2  ``persist_suggestion_set`` returned early on a set with no legacy chips, so a set whose only
      cards are v2-only kinds (a polished edit; the four legacy chips already handled) had no
      entry for the cards to be attached to.
        a. a polished edit on the real GET
        b. the write itself: cards and no chips write an entry; neither writes none (legacy)
  R3  GAP-8, ``test_ring_flag_off.py``     R4  ``test_ring_restate_offer.py``
  R5  ``test_ring_consumed.py``            R6  ``tests/test_raw_chat_text_readers.py``

Evidence is the whole pipeline: the real GET route, the real ``kria_suggester`` body with the real
``SuggestionAgent`` (a cassette answers), ``submit_turn`` and ``run_kria_turn`` on Postgres.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import CreationThread
from app.services import suggestion_chips as sc
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"
LEGACY_ENTRY_KEYS = {
    "set_id",
    "state",
    "digest",
    "media_digest",
    "created_at",
    "final",
    "wording",
    "prompt_version",
    "chips",
}


def prefs(key: str = KEY) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


def card_of(out, kind: str):  # noqa: ANN001, ANN202
    return next(c for c in out.suggestions if c.kind == kind)


async def served(world, profile: str = EDITOR, *, finished: bool = False):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile, finished=finished)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return phone, await phone.serve()


def stored_ids(phone: Phone, set_id: str) -> list[str] | None:
    entry = phone.ring_entry(set_id)
    if entry is None:
        return None
    return [c["id"] for c in entry.get("suggestions") or []]


# ── R1: the cards reach the ring ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_r1a_the_get_stores_every_card_it_served_and_the_tap_applies(world):
    phone, out = await served(world)
    assert [c.kind for c in out.suggestions] == ["hook", "captions", "music", "transition", "sfx"]
    assert stored_ids(phone, out.set_id) == [c.id for c in out.suggestions], (
        "the ring entry the tap is resolved against must hold the cards the GET returned"
    )
    entry = phone.ring_entry(out.set_id)
    assert entry is not None and entry["final"] is True

    tap = await phone.tap(card_of(out, "captions"), out)
    assert tap.planned, (tap.pin, tap.reply)
    assert tap.result["status"] == "completed" and tap.new_drafts == 1, tap.result
    assert tap.refused is None, tap.refused


@pytest.mark.asyncio
async def test_r1a_a_copy_card_applies_through_the_stored_card_too(world):
    """The hook card keeps its text in the ring (the tap is the approval): another action type."""
    phone, out = await served(world)
    hook = card_of(out, "hook")
    assert stored_ids(phone, out.set_id) and hook.id in stored_ids(phone, out.set_id)
    tap = await phone.tap(hook, out)
    assert tap.planned and tap.new_drafts == 1, (tap.pin, tap.reply)


@pytest.mark.asyncio
async def test_r1b_pinned_taps_on_editor_triggers_off_still_stores_the_cards(world, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    phone, out = await served(world)
    assert out.suggestions, "pinned taps are on: the wire carries cards"
    assert stored_ids(phone, out.set_id) == [c.id for c in out.suggestions]
    tap = await phone.tap(card_of(out, "captions"), out)
    assert tap.planned and tap.new_drafts == 1, (tap.pin, tap.reply)


@pytest.mark.asyncio
async def test_r1c_a_write_skipped_under_the_thread_lock_is_stored_by_the_next_read(world):
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, EDITOR)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    await phone.get()  # the pending set; enqueues the worker
    await phone.run_worker()  # the cassette answers; the final set is now cached

    holder = AsyncSessionLocal()
    try:
        await holder.execute(
            select(CreationThread.id).where(CreationThread.id == seeded.thread_id).with_for_update()
        )
        locked = await asyncio.wait_for(phone.get(), timeout=15)
    finally:
        await holder.rollback()
        await holder.close()
    assert locked.suggestions, "a locked thread still serves the cards"
    assert not stored_ids(phone, locked.set_id), "...but the write was skipped, nothing is stored"

    again = await phone.get()
    assert again.set_id == locked.set_id
    assert stored_ids(phone, again.set_id) == [c.id for c in again.suggestions]
    tap = await phone.tap(card_of(again, "captions"), again)
    assert tap.planned and tap.new_drafts == 1, (tap.pin, tap.reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("editor_triggers", [True, False], ids=["triggers-on", "triggers-off"])
async def test_r1d_pinned_taps_off_the_entry_is_the_legacy_entry(
    world, monkeypatch, editor_triggers
):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", editor_triggers)
    phone, out = await served(world)
    assert out.chips and not out.suggestions and out.digest is None
    assert "suggestions" not in json.loads(out.model_dump_json())
    entry = phone.ring_entry(out.set_id)
    assert entry is not None and set(entry) == LEGACY_ENTRY_KEYS, sorted(entry or {})
    assert [c["id"] for c in entry["chips"]] == [c.id for c in out.chips]


# ── R2: a set with cards and no legacy chips has an entry ────────────────────────────


@pytest.mark.asyncio
async def test_r2a_a_polished_edit_whose_only_card_is_v2_only_is_tappable(world):
    phone, out = await served(world, threads.SIGNALS, finished=True)
    assert [c.kind for c in out.suggestions] == ["duplicates"] and out.chips == [], (
        [c.kind for c in out.suggestions],
        [c.kind for c in out.chips],
    )
    entry = phone.ring_entry(out.set_id)
    assert entry is not None, "a set with cards and no chips still has a ring entry"
    assert entry["chips"] == []
    assert stored_ids(phone, out.set_id) == [c.id for c in out.suggestions]

    tap = await phone.tap(card_of(out, "duplicates"), out)
    assert tap.planned and tap.new_drafts == 1, (tap.pin, tap.reply)
    assert tap.refused is None, tap.refused


@pytest.mark.asyncio
async def test_r2b_the_write_needs_chips_or_cards_not_both(world):
    """The legacy rule survives: a set with nothing to offer writes no entry at all."""
    _phone, out = await served(world, threads.SIGNALS, finished=True)
    scn = threads.SCENARIOS[KEY]
    other = threads.seed_thread(scn, EDITOR)

    async def write(shown):  # noqa: ANN001, ANN202
        async with AsyncSessionLocal() as db:
            return await sc.persist_suggestion_set(
                db, other.thread_id, shown, media_digest="digest-r2b", protocol=None
            )

    def ring() -> list[dict]:
        return (k.thread_state(other.thread_id) or {}).get(sc.RING_KEY) or []

    nothing = out.model_copy(update={"chips": [], "suggestions": []})
    assert await write(nothing) is False and ring() == []

    only_cards = out.model_copy(update={"chips": []})
    assert only_cards.suggestions and not only_cards.chips
    assert await write(only_cards) is True
    entries = ring()
    assert [e["set_id"] for e in entries] == [out.set_id] and entries[0]["chips"] == []
    assert await write(only_cards) is False, "a final entry is never rewritten"
