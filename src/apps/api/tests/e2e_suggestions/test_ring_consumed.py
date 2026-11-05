"""R5: a card is one offer, whichever set the tap names.

``pinned.is_consumed`` only looked at the ring entry of the tapped set. A card applied through one
set could therefore be applied again through another that listed the same card: a relative card
(the music level is 60 % of whatever it is now) rebuilds to a NEW change every time, so the second
tap looked like a fresh offer and silently stacked.

Failure modes:

  a. a refusal mints a derived set (``derive_entry`` copies the cards and strips every mark); a card
     applied through the derived set is then tapped again with the stale first set's id;
  b. the edit moved and the thread was served a NEW set (a new digest, new card ids that follow the
     edit); the same kind and target applied through it is tapped again with the old set's id;
  c. the guard over-reaches: a different card of the same set, or the first tap of a card nobody
     applied, is refused.
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.test_ring_store import KEY, card_of, prefs
from tests.e2e_suggestions.threads import EDITOR


def refused_consumed(tap: d.Tap) -> bool:
    signal = tap.refused or {}
    return bool(
        signal.get("outcome") == "refused"
        and signal.get("reason") == "consumed"
        and tap.new_drafts == 0
        and tap.before == tap.after
        and tap.reply == d.NO_LONGER_FITS
    )


async def thread(world):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, EDITOR)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return seeded, phone, await phone.serve()


@pytest.mark.asyncio
async def test_r5a_a_card_applied_through_a_derived_set_is_refused_through_the_first(
    talking_world, monkeypatch
):
    seeded, phone, first = await thread(talking_world)
    music = card_of(first, "music")

    # A refusal (the hook kind is pulled after the serve) mints the derived set.
    monkeypatch.setattr(settings, "kria_suggestion_hook_copy_enabled", False)
    refused = await phone.tap(card_of(first, "hook"), first)
    assert (refused.refused or {}).get("outcome") == "refused", refused.refused
    monkeypatch.setattr(settings, "kria_suggestion_hook_copy_enabled", True)
    derived = await phone.serve()
    assert derived.set_id != first.set_id, "a refusal mints a new set"
    assert music.id in [c.id for c in derived.suggestions], "the card is in the derived set"

    applied = await phone.tap(card_of(derived, "music"), derived)
    assert applied.planned and applied.new_drafts == 1, (applied.pin, applied.reply)

    stale = await phone.tap(music, first)
    assert refused_consumed(stale), (
        stale.pin,
        stale.refused,
        stale.reply,
        stale.before,
        stale.after,
    )
    head = d.head_of(seeded.thread_id)
    assert head is not None and head["rev"] == applied.head_after["rev"], "no second revision"


@pytest.mark.asyncio
async def test_r5b_a_card_applied_through_a_newer_set_is_refused_through_the_old_one(talking_world):
    seeded, phone, first = await thread(talking_world)
    music_then = card_of(first, "music")

    captions = await phone.tap(card_of(first, "captions"), first)
    assert captions.planned and captions.new_drafts == 1, (captions.pin, captions.reply)
    newer = await phone.serve()
    assert newer.set_id != first.set_id, "the edit moved: a new set"
    music_now = card_of(newer, "music")
    assert music_now.id != music_then.id, "card ids follow the edit, so the old id is not the new"

    applied = await phone.tap(music_now, newer)
    assert applied.planned and applied.new_drafts == 1, (applied.pin, applied.reply)

    stale = await phone.tap(music_then, first)
    assert refused_consumed(stale), (
        stale.pin,
        stale.refused,
        stale.reply,
        stale.before,
        stale.after,
    )
    head = d.head_of(seeded.thread_id)
    assert head is not None and head["rev"] == applied.head_after["rev"], "no second revision"


@pytest.mark.asyncio
async def test_r5c_only_the_applied_card_is_refused(talking_world):
    _seeded, phone, first = await thread(talking_world)
    captions = await phone.tap(card_of(first, "captions"), first)
    assert captions.planned, (captions.pin, captions.reply)
    # Another card of the SAME stale set (one revision behind) is still rebuilt and applied once.
    sfx = await phone.tap(card_of(first, "sfx"), first)
    assert sfx.planned and sfx.new_drafts == 1, (sfx.pin, sfx.refused, sfx.reply)
    assert sfx.refused is None
