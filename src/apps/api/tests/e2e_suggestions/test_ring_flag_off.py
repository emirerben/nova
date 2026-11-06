"""R3: switching KRIA_SUGGESTION_PINNED_TAPS_ENABLED off stops the cards, frozen set included.

A served set is frozen in Redis per input digest for 24 h and the GET used to return it as stored.
With the pinned-taps flag off the submit no longer stamps a pin, so a tap on a card the phone still
holds is just words for the legacy planner: the exact failure the pinned seam exists to prevent.

Failure modes:

  a. the GET keeps serving ``suggestions`` and ``digest`` from the frozen set after the flag is off;
  b. the strip touches the legacy chips, their ids, the set id or the status (byte-identity);
  c. the strip is written back into the frozen set, so switching the flag on again cannot bring the
     cards back inside the 24 h (a rollback must be reversible);
  d. the same strip is missed on the other read, the one that finds no frozen set and builds it.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.test_ring_store import KEY, prefs, served


def legacy_view(out):  # noqa: ANN001, ANN202
    """A set without the v2 fields: the bytes the flag-off server has always sent."""
    return out.model_copy(update={"suggestions": [], "digest": None})


@pytest.mark.asyncio
async def test_r3_pinned_taps_off_the_frozen_set_serves_its_chips_only(world, monkeypatch):
    phone, on = await served(world)
    assert on.suggestions and on.digest is not None, "precondition: the flag-on set has cards"

    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    off = await phone.get()
    wire = json.loads(off.model_dump_json())
    assert off.suggestions == [] and off.digest is None
    assert "suggestions" not in wire and "digest" not in wire, sorted(wire)
    assert off.model_dump_json() == legacy_view(on).model_dump_json(), (
        "the chips, ids, set id and status are exactly what the frozen set held"
    )

    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    back = await phone.get()
    assert back.model_dump_json() == on.model_dump_json(), (
        "the freeze itself was not edited: switching the flag on again brings the cards back"
    )


@pytest.mark.asyncio
async def test_r3_a_set_built_with_the_flag_off_is_the_same_bytes(world, monkeypatch):
    """The read that builds the set (no frozen entry yet) and the read that strips a frozen one
    send the same bytes, so a rollback cannot be told apart from a flag that was never on."""
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, threads.EDITOR)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    built = await phone.serve()
    assert built.chips and not built.suggestions and built.digest is None
    assert "suggestions" not in json.loads(built.model_dump_json())
    assert (await phone.get()).model_dump_json() == built.model_dump_json()
