"""R5: a card is one offer, whichever set the tap names.

``pinned.is_consumed`` only looked at the ring entry of the tapped set. A card applied through one
set could therefore be applied again through another that listed the same card: a relative card
(the music level is 60 % of whatever it is now) rebuilds to a NEW change every time, so the second
tap looked like a fresh offer and silently stacked. The music level starts at 1.0 here so that
a second application is a real, valid change (0.6 -> 0.36) and only the mark stops it.

Failure modes:

  a. a refusal mints a derived set (``derive_entry`` copies the cards and strips every mark); a card
     applied through the derived set is then tapped again with the stale first set's id;
  b. the edit moved and the thread was served a NEW set (a new digest); the card applied through it
     is tapped again with the old set's id;
  c. the card id changed between the two sets (a new render generation): the same card is still
     the same card, by its kind and target;
  d. the guard over-reaches: another card of the same stale set, a card of the same kind aimed at
     another clip, or a card nobody applied is refused.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.suggestion_chips import RING_KEY
from app.services.suggestions import pinned
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.test_ring_store import KEY, card_of, prefs
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_suggestion_harness as h


def refused_consumed(tap: d.Tap) -> bool:
    signal = tap.refused or {}
    return bool(
        signal.get("outcome") == "refused"
        and signal.get("reason") == "consumed"
        and tap.new_drafts == 0
        and tap.before == tap.after
        and tap.reply == d.NO_LONGER_FITS
    )


def detail(tap: d.Tap) -> tuple:
    return (tap.pin, tap.refused, tap.reply, tap.before, tap.after)


async def thread(world):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, EDITOR)
    h.edit_variant(seeded, lambda variant: variant.update(mix=1.0))
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
    assert refused_consumed(stale), detail(stale)
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

    applied = await phone.tap(card_of(newer, "music"), newer)
    assert applied.planned and applied.new_drafts == 1, (applied.pin, applied.reply)

    stale = await phone.tap(music_then, first)
    assert refused_consumed(stale), detail(stale)
    head = d.head_of(seeded.thread_id)
    assert head is not None and head["rev"] == applied.head_after["rev"], "no second revision"


@pytest.mark.asyncio
async def test_r5e_a_card_nobody_applied_is_still_one_offer(talking_world):
    """The first tap of a card is never refused because ANOTHER card of its set was applied."""
    _seeded, phone, first = await thread(talking_world)
    captions = await phone.tap(card_of(first, "captions"), first)
    assert captions.planned, (captions.pin, captions.reply)
    # Another card of the SAME stale set (one revision behind) is still rebuilt and applied once.
    sfx = await phone.tap(card_of(first, "sfx"), first)
    assert sfx.planned and sfx.new_drafts == 1, detail(sfx)
    assert sfx.refused is None


# ── c/d: the ring alone (the ids and anchors a render generation changes) ────────────


def card(card_id: str, kind: str, *, media_id: str | None = None) -> dict:
    return {
        "id": card_id,
        "kind": kind,
        "anchor": {"media_id": media_id} if media_id else {},
    }


def state(*entries: dict) -> dict:
    return {RING_KEY: list(entries)}


def entry(set_id: str, cards: list[dict], consumed: dict | None = None) -> dict:
    out: dict = {"set_id": set_id, "suggestions": cards}
    if consumed:
        out[pinned.CONSUMED_FIELD] = consumed
    return out


def test_r5c_the_same_kind_and_target_is_the_same_card_under_another_id():
    ring = state(
        entry("sg_old", [card("ss_old", "music")]),
        entry("sg_new", [card("ss_new", "music")], consumed={"ss_new": "turn-1"}),
    )
    assert pinned.is_consumed(ring, "sg_new", "ss_new"), "its own mark, as before"
    assert pinned.is_consumed(ring, "sg_old", "ss_old"), (
        "the card the older set offered is the card the newer set applied"
    )


def test_r5d_another_target_or_another_kind_is_another_card():
    ring = state(
        entry(
            "sg_old",
            [card("ss_a", "duplicates", media_id="m1"), card("ss_b", "duplicates", media_id="m2")],
        ),
        entry(
            "sg_new",
            [card("ss_c", "duplicates", media_id="m1"), card("ss_d", "captions")],
            consumed={"ss_c": "turn-1"},
        ),
    )
    assert pinned.is_consumed(ring, "sg_old", "ss_a"), "same clip, same kind"
    assert not pinned.is_consumed(ring, "sg_old", "ss_b"), "same kind, another clip"
    assert not pinned.is_consumed(ring, "sg_new", "ss_d"), "another kind"
    assert not pinned.is_consumed(ring, "sg_gone", "ss_a"), "a set the ring never held"
    assert not pinned.is_consumed({}, "sg_old", "ss_a")
