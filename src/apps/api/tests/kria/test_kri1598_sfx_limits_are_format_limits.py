"""KRI-1598: why no sound effects were placed is the format's answer, said once and never held.

Before this, a draft whose effects gate was closed ("Sound effects are switched off right now")
was a plain shortfall: the first draft was held back behind "Should I make a simpler version?",
as if dropping the ask were a simpler plan. Every other format limit (a missing song, a Talking
edit's whole take) is said as "Couldn't: ..." and the draft goes ahead. And the receipts of the
montage with a voiceover were never read on the finished video at all.

Failure modes, written before the tests:

1. A limit line still makes a draft need the creator's choice, in English, Turkish or German.
2. "You asked for no sound effects" is registered too, though it is the creator's own choice.
3. A limit sentence with no German line, so a German draft still holds.
4. A finished montage-with-voiceover video's receipt is ignored, so a placed effect reads as
   "can't check" and a declined one says nothing.
5. A draft with the gate closed is judged differently from before, apart from no longer holding.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
)
from app.kria.copy_catalog import translate
from app.kria.reply_language import reply_language_for
from app.services.sfx_capability import FORMAT_LIMIT_LINES, limit_line
from app.services.sfx_chooser import SfxLimit
from tests.tasks import test_kri1561_phone_montage_sfx_worker as montage

ASK = BriefRequirement(
    id="s1",
    kind="audio",
    scope="global",
    description="add sound effects where they fit",
    facts={"audio_intent": "sound_effects"},
)
REGISTERED = [limit for limit in SfxLimit if limit is not SfxLimit.creator_said_no]


@pytest.fixture
def german(monkeypatch):
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)


def _declined(limit: SfxLimit) -> dict:
    return {"version": 1, "asked": True, "placed": [], "declined": limit.value}


@pytest.mark.parametrize("language", ["en", "tr", "de"])
@pytest.mark.parametrize("limit", REGISTERED)
def test_every_limit_but_the_creators_own_no_is_a_format_limit(limit, language, german):
    facts = PlanFacts(rendered_variant=True, sfx_choice=_declined(limit))
    with reply_language_for(language):
        [receipt] = build_receipts([ASK], facts)
        line = limit_line(limit)

    assert receipt.reason == line and receipt.status == "not_possible"
    assert is_format_limit(line)
    assert not needs_creator_choice(receipt)


@pytest.mark.parametrize("language", ["en", "tr", "de"])
def test_the_creators_own_no_is_not_registered(language, german):
    """Negative control: the one decline that is the creator's choice keeps today's reading."""
    with reply_language_for(language):
        line = limit_line(SfxLimit.creator_said_no)
    assert not is_format_limit(line)


def test_the_registered_lines_are_exactly_the_limit_lines_and_all_have_german():
    with reply_language_for("en"):
        english = {limit_line(limit) for limit in REGISTERED}
    with reply_language_for("tr"):
        turkish = {limit_line(limit) for limit in REGISTERED}
    assert {en for en, _tr in FORMAT_LIMIT_LINES} == english
    assert {tr for _en, tr in FORMAT_LIMIT_LINES} == turkish
    assert all(translate("de", en) for en in english)


def test_a_draft_with_the_gate_closed_goes_ahead_with_the_reason(monkeypatch):
    """The draft verdict is the one it was (not possible, the closed gate named); it no longer
    turns the first draft into the "simpler version?" question."""
    monkeypatch.setattr(settings, "sound_effects_enabled", False)
    [receipt] = build_receipts([ASK], PlanFacts(phone_montage=True))

    assert receipt.status == "not_possible" and receipt.stage == "checked"
    assert receipt.reason == limit_line(SfxLimit.flag_off)
    assert not needs_creator_choice(receipt)


def test_a_finished_voiceover_montage_says_what_it_placed(monkeypatch):
    job, _calls, _payloads = montage._run(monkeypatch)

    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "voiceover"
    [receipt] = build_receipts([ASK], plan_facts_from_phone_variant(variant))
    assert receipt.status == "met" and receipt.reason == "I added 2 sound effects"


def test_a_finished_voiceover_montage_names_why_it_placed_none(monkeypatch):
    job, _calls, _payloads = montage._run(monkeypatch, open_gate=False)

    variant = job.assembly_plan["variants"][0]
    [receipt] = build_receipts([ASK], plan_facts_from_phone_variant(variant))
    assert receipt.status == "not_possible"
    assert receipt.reason == limit_line(SfxLimit.flag_off)
    assert not needs_creator_choice(receipt)
