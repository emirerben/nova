"""KRI-1346: "end on a red screen with yellow text '...'" is the closing card, not a background.

C1: the ask records `closing` + `background_color` on a `text` requirement. It must NOT route to the
speaker-background checker ("a plain colour behind you"), while a real speaker-background colour
ask still does. The draft and rendered verdicts are judged by `_check_closing_card`.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    asks_speaker_background,
    build_receipts,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import reply_language_for

_PHONE_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "freezeFrame",
    "visualBlocks",
    "closingCard",
]


def _card_req(**facts) -> BriefRequirement:
    return BriefRequirement(
        id="r5",
        kind="text",
        scope="global",
        description="End on a red screen with yellow text 'follow for more'",
        facts={
            "closing": "follow for more",
            "text_color": "#FFFF00",
            "background_color": "#FF0000",
            **facts,
        },
    )


def _background_req() -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="style",
        scope="global",
        description="Green screen behind me",
        facts={"speaker_background": True, "background_color": "green"},
    )


def _receipt(req: BriefRequirement, facts: PlanFacts):
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


@pytest.fixture
def card_open(monkeypatch):
    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", _PHONE_FEATURES)


@pytest.fixture
def card_closed(monkeypatch):
    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", False)


# --- C1: routing ------------------------------------------------------------------------------


def test_c1_a_closing_card_ask_is_not_a_speaker_background():
    assert not asks_speaker_background(_card_req())


def test_c1_a_colour_behind_the_speaker_still_routes_to_the_speaker_background():
    assert asks_speaker_background(_background_req())
    # A colour alone (no ending words) is a background too.
    plain = BriefRequirement(
        id="r3", kind="style", scope="global", description="x", facts={"background_color": "red"}
    )
    assert asks_speaker_background(plain)


def test_c1_an_explicit_speaker_background_fact_wins_over_the_card_shape():
    assert asks_speaker_background(_card_req(speaker_background=True))


def test_c1_the_card_verdict_never_reads_as_a_background_verdict(card_open):
    receipt = _receipt(_card_req(), PlanFacts(phone_talking=True))

    assert "behind you" not in (receipt.reason or "")
    assert "colour screen" in (receipt.reason or "")


# --- drafts ----------------------------------------------------------------------------------


def test_draft_on_a_non_talking_edit_is_not_possible():
    receipt = _receipt(_card_req(), PlanFacts(phone_talking=False))

    assert receipt.status == "not_possible"
    assert "talking-to-camera" in receipt.reason


def test_draft_with_the_flag_off_says_it_isnt_available_yet(card_closed):
    receipt = _receipt(_card_req(), PlanFacts(phone_talking=True))

    assert receipt.status == "not_possible"
    assert "isn't available on your iPhone yet" in receipt.reason


def test_draft_with_the_flag_on_is_a_render_promise(card_open):
    receipt = _receipt(_card_req(), PlanFacts(phone_talking=True))

    assert receipt.status == "partial"
    assert "after your last word" in receipt.reason


def test_draft_with_an_unnameable_colour_asks_for_a_name(card_open):
    receipt = _receipt(_card_req(background_color="sunset"), PlanFacts(phone_talking=True))

    assert receipt.status == "partial"
    assert "#FF0000" in receipt.reason


# --- rendered variants ---------------------------------------------------------------------


def _rendered(card) -> PlanFacts:
    return PlanFacts(phone_talking=True, rendered_variant=True, closing_card=card)


@pytest.mark.parametrize("card", [None, {}, {"status": "dropped", "reason": "not_compiled"}])
def test_rendered_without_an_applied_card_is_not_possible(card):
    receipt = _receipt(_card_req(), _rendered(card))

    assert receipt.status == "not_possible"
    assert receipt.reason == "The finished video doesn't end on your colour screen"


def test_rendered_applied_card_is_met():
    receipt = _receipt(_card_req(), _rendered({"status": "applied", "text_color": "#FFFF00"}))

    assert receipt.status == "met"


def test_rendered_swapped_text_colour_is_partial_and_names_the_new_colour():
    card = {"status": "applied", "text_color": "#000000", "text_color_swapped": True}

    receipt = _receipt(_card_req(background_color="#FFFF00"), _rendered(card))

    assert receipt.status == "partial"
    assert "black" in receipt.reason


def test_rendered_swapped_reply_reads_in_turkish():
    card = {"status": "applied", "text_color": "#FFFFFF", "text_color_swapped": True}

    with reply_language_for("tr"):
        receipt = _receipt(_card_req(), _rendered(card))

    assert receipt.status == "partial"
    assert "beyaz" in receipt.reason and "wouldn't" not in receipt.reason


def test_the_variants_phone_closing_card_receipt_reaches_the_check():
    facts = plan_facts_from_phone_variant(
        {
            "resolved_archetype": "subtitled",
            "edit_format": "subtitled",
            "phone_closing_card": {"status": "applied", "text_color": "#FFFF00"},
        }
    )
    assert facts.rendered_variant
    assert dataclasses.asdict(facts)["closing_card"]["status"] == "applied"
    assert _receipt(_card_req(), facts).status == "met"
