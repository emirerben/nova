"""Minimal repros of the two defects that stop a served card from being tappable at all.

Each test asserts its preconditions for real, then checks the BEHAVIOR WE WANT: while the defect
stands it ends in ``pytest.xfail`` (visible in the summary, the suite stays green), and the day the
defect is fixed it FAILS with "remove the marker". They are the exact repros the report names as
G1 and G2:

* ``test_probe_g1``  a thread, one real GET, and the ring entry of the set it just served;
* ``test_probe_g2``  a polished edit where only the new (v2-only) kinds are left to offer.

Run one alone:  ``pytest tests/e2e_suggestions/test_probe_gaps.py::test_probe_g1 -rx``
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


G1 = (
    "G1: GET /suggestions persists no v2 card (persist_served_set -> _server_cards runs the "
    "witnesses after db.rollback(): MissingGreenlet), so no tap can resolve"
)
G2 = (
    "G2: persist_suggestion_set returns early on a set with no legacy chips, so a polished edit "
    "whose only card is a v2-only kind has no ring entry"
)


def stored_kinds(phone, out) -> list[str] | None:  # noqa: ANN001
    entry = phone.ring_entry(out.set_id)
    if entry is None:
        return None
    return [c["kind"] for c in entry.get("suggestions") or []]


@pytest.mark.asyncio
async def test_probe_g1(world):
    phone, out = await served_thread(world)
    assert [c.kind for c in out.suggestions] == ["hook", "captions", "music", "transition", "sfx"]
    if stored_kinds(phone, out) == [c.kind for c in out.suggestions]:
        pytest.fail("G1 is fixed: the GET stores its cards. Delete this probe and the G1 markers.")
    pytest.xfail(G1)


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
    if stored_kinds(phone, out) == ["duplicates"]:
        pytest.fail("G2 is fixed: a set with cards and no chips is stored. Delete this probe.")
    pytest.xfail(G2)
