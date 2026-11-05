"""R4-E: a per-kind switch that is off means the card is off, on the next GET.

Failure catalogue (written first):

  E1  a per-kind switch (HOOK_COPY, DUPLICATES, FILLER_WORDS, MUTE_CLIP) pulled after a set was
      served leaves that kind's card on the phone for the rest of the 24 h freeze. Only the master
      switch (pinned taps) stripped a frozen set. The tap was refused in words, but it was a dead
      tap and the refusal minted a new set. The next GET must not show the card.
  E2  the strip takes more than the kind it names: another kind's card is dropped, the ranks keep
      a hole, or the legacy chips of the same kind disappear with it.
  E3  the strip leaves a set the phone reads differently from a legacy one: every card of the set
      is gone but the wire still carries the v2 digest.
  E4  the freeze is edited. The set the worker stored is untouched (the strip is on the way out),
      so the switch coming back on brings the card back without a new run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.flow import ensure_stored
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"

PER_KIND = {
    "hook": ("kria_suggestion_hook_copy_enabled", EDITOR),
    "duplicates": ("kria_suggestion_duplicates_enabled", SIGNALS),
    "filler_words": ("kria_suggestion_filler_words_enabled", TALKING),
    "mute_clip": ("kria_suggestion_mute_clip_enabled", SIGNALS),
}


def prefs(key: str = KEY) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def served(world, profile):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return seeded, phone, await phone.serve()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(PER_KIND))
async def test_e1_e4_a_per_kind_switch_strips_the_card_from_a_frozen_set(
    talking_world, monkeypatch, kind
):
    flag, profile = PER_KIND[kind]
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"negative E1 {kind} switched off")
    _seeded, phone, out = await served(talking_world, profile)
    assert await ensure_stored(phone, out) == "route"
    before = [c.kind for c in out.suggestions]
    chips_before = [c.kind for c in out.chips]
    assert kind in before, f"the control did not serve {kind}: {before}"

    monkeypatch.setattr(settings, flag, False)
    after = await phone.get()
    kept = [c.kind for c in after.suggestions]
    rec.negative(
        f"{flag} off after the serve: the next GET has no {kind} card",
        KEY,
        f"{kind} absent from the frozen set, the other cards in their order",
        kind not in kept and kept == [x for x in before if x != kind],
        f"before {before}; after {kept}",
    )
    rec.negative(
        f"{flag} off: the ranks of the cards that stay have no hole",
        KEY,
        "ranks 1..n",
        [c.rank for c in after.suggestions] == list(range(1, len(kept) + 1)),
        f"ranks {[c.rank for c in after.suggestions]}",
    )
    rec.negative(
        f"{flag} off: the legacy chips are not touched",
        KEY,
        "same chips, same set id",
        [c.kind for c in after.chips] == chips_before and after.set_id == out.set_id,
        f"chips {[c.kind for c in after.chips]} vs {chips_before}",
    )
    if len(before) == 1:
        rec.negative(
            f"{flag} off: a set with no card left carries no v2 digest",
            KEY,
            "no suggestions, no digest on the wire",
            not after.suggestions and after.digest is None and "digest" not in phone.last_wire,
            f"digest {after.digest}",
        )
    monkeypatch.setattr(settings, flag, True)
    back = [c.kind for c in (await phone.get()).suggestions]
    rec.negative(
        f"{flag} back on: the frozen set shows the card again (the stored set was not edited)",
        KEY,
        "the same cards as before the switch was pulled",
        back == before,
        f"back {back}; before {before}",
    )
    rec.finish()
