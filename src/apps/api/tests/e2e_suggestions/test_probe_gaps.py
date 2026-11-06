"""Regression guards for the two defects that stopped a served card from being tappable at all.

They were the strict-xfail repros of GAP-1 and GAP-2 and now assert the behavior directly:

* ``test_probe_g1``  a thread, one real GET, and the ring entry of the set it just served;
* ``test_probe_g2``  a polished edit where only the new (v2-only) kinds are left to offer.

Run one alone:  ``pytest tests/e2e_suggestions/test_probe_gaps.py::test_probe_g1``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone

CASSETTE = Path(__file__).resolve().parent / "cassettes" / "nermin-matcha-update.json"


async def served_thread(world):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS["nermin-matcha-update"]
    seeded = threads.seed_thread(scn, threads.EDITOR)
    prefs = json.loads(CASSETTE.read_text(encoding="utf-8"))["prefs"]
    phone = Phone(seeded, world, prefs)
    return phone, await phone.serve()


def stored_kinds(phone, out) -> list[str] | None:  # noqa: ANN001
    entry = phone.ring_entry(out.set_id)
    if entry is None:
        return None
    return [c["kind"] for c in entry.get("suggestions") or []]


@pytest.mark.asyncio
async def test_probe_g1(world):
    phone, out = await served_thread(world)
    assert [c.kind for c in out.suggestions] == ["hook", "captions", "music", "transition", "sfx"]
    assert stored_kinds(phone, out) == [c.kind for c in out.suggestions]


@pytest.mark.asyncio
async def test_probe_g2(world):
    """A polished edit (title, captions, crossfades, a sound effect, a quiet song) with two takes of
    one shot: the only card left is the v2-only duplicates card, and the legacy chips are empty."""
    scn = threads.SCENARIOS["nermin-matcha-update"]
    seeded = threads.seed_thread(scn, threads.SIGNALS, finished=True)
    prefs = json.loads(CASSETTE.read_text(encoding="utf-8"))["prefs"]
    phone = Phone(seeded, world, prefs)
    out = await phone.serve()
    assert [c.kind for c in out.suggestions] == ["duplicates"] and out.chips == [], (
        [c.kind for c in out.suggestions],
        [c.kind for c in out.chips],
    )
    assert stored_kinds(phone, out) == ["duplicates"]
