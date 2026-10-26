"""Creative Brief receipts for exact-timed small cards (failure modes 8 and 12 in
``docs/reviews/timed-cards-montage-voiceover-failure-modes.md``).

The ask is the model's recorded ``facts["timed_card"]`` (KRI-667: no new word regex). A draft
is judged against the strategy REPAIRED by the same code approval runs; a finished phone render
against its own ``phone_timed_card_receipt`` -- never against the strategy's promise.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    check_requirement,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import reply_language_for
from app.services import creator_capabilities as capabilities
from tests.services.test_creator_capabilities import (
    _enable_guided,
    _enable_timed_cards,
    _timed_manifest,
)

_ASK = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="show the menu photo as a small card at 3 seconds for 2 seconds",
    facts={"timed_card": {"visual": "menu photo", "at_s": 3.0, "hold_s": 2.0}},
)
_STRATEGY = {
    "selected_media_ids": ["phone-a"],
    "timed_cards": [{"card_id": "menu", "visual_id": "menu photo", "at_s": 3.0, "hold_s": 2.0}],
}


def _receipt(req, facts):
    r = check_requirement(req, facts)
    return r.status, r.reason, r.verification


def test_draft_receipt_names_the_second_and_hold_only_when_the_card_survives_repair(
    monkeypatch,
) -> None:
    _enable_timed_cards(monkeypatch)
    manifest = _timed_manifest(monkeypatch)
    facts = plan_facts_from_strategy(_STRATEGY, manifest=manifest)
    assert facts.timed_cards_available is True
    assert _receipt(_ASK, facts) == ("met", "Small card of menu photo at 3.0 s for 2.0 s.", None)

    # The photo the model named does not resolve: approval drops it, so the draft says so.
    ghost = {**_STRATEGY, "timed_cards": [{**_STRATEGY["timed_cards"][0], "visual_id": "x"}]}
    status, reason, _ = _receipt(_ASK, plan_facts_from_strategy(ghost, manifest=manifest))
    assert status == "not_possible" and "couldn't find" in reason.casefold()

    # A clip-anchored card has no second yet.
    clip = {
        **_STRATEGY,
        "timed_cards": [{"card_id": "m", "visual_id": "menu photo", "clip_id": "phone-a"}],
    }
    status, reason, _ = _receipt(_ASK, plan_facts_from_strategy(clip, manifest=manifest))
    assert (
        status == "met"
        and reason == "Small card of menu photo during the clip you named for 2.0 s."
    )


def test_gate_off_draft_reads_couldnt_not_a_promise(monkeypatch) -> None:
    """9 / 12: the flag off (or any other edit) never promises a card."""
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "phone_timed_cards_enabled", False, raising=False)
    manifest = _timed_manifest(monkeypatch)
    facts = plan_facts_from_strategy(_STRATEGY, manifest=manifest)
    assert facts.timed_cards_available is False
    status, reason, _ = _receipt(_ASK, facts)
    assert status == "not_possible"
    assert reason == "Small cards at an exact time aren't available for this edit yet."
    # Talking never advertises it either.
    _enable_timed_cards(monkeypatch)
    talking = _timed_manifest(monkeypatch, "subtitled")
    assert plan_facts_from_strategy(_STRATEGY, manifest=talking).timed_cards_available is False


def _variant(receipt):
    return {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "ok": True,
        "phone_timed_card_receipt": receipt,
    }


def test_rendered_variant_receipt_reads_the_workers_placement() -> None:
    placed = {
        "version": 1,
        "matcher": "timed",
        "timeline_end_s": 6.0,
        "placed": [
            {"card_id": "menu", "label": "menu photo", "at_s": 3.0, "end_s": 5.0, "hold_s": 2.0}
        ],
        "unplaced": [],
    }
    facts = plan_facts_from_phone_variant(_variant(placed))
    assert _receipt(_ASK, facts) == (
        "met",
        "Small card of menu photo at 3.0 s for 2.0 s.",
        "checked",
    )

    past_end = {
        **placed,
        "placed": [],
        "unplaced": [
            {
                "card_id": "menu",
                "label": "menu photo",
                "at_s": 3.0,
                "reason": "past_end",
                "timeline_end_s": 2.4,
            }
        ],
    }
    status, reason, _ = _receipt(_ASK, plan_facts_from_phone_variant(_variant(past_end)))
    assert status == "not_possible"
    assert reason == (
        "3.0 s is past the end of the video (2.4 s), so the small card of menu photo isn't shown."
    )

    face = {
        **placed,
        "placed": [],
        "unplaced": [
            {"card_id": "menu", "label": "menu photo", "at_s": 3.0, "reason": "face_collision"}
        ],
    }
    status, reason, _ = _receipt(_ASK, plan_facts_from_phone_variant(_variant(face)))
    assert status == "not_possible" and "covering a face" in reason

    disabled = {
        "version": 1,
        "matcher": "disabled",
        "timeline_end_s": 6.0,
        "placed": [],
        "unplaced": [
            {"card_id": "menu", "label": "menu photo", "at_s": 3.0, "reason": "lane_disabled"}
        ],
    }
    status, reason, _ = _receipt(_ASK, plan_facts_from_phone_variant(_variant(disabled)))
    assert status == "not_possible" and reason.startswith("Small cards at an exact time aren't")

    # A render that never recorded the lane says nothing (no guess either way).
    facts = plan_facts_from_phone_variant(_variant(None))
    assert facts.timed_cards is None and facts.timed_cards_available is None
    assert _receipt(_ASK, facts)[0] == "partial"


def test_turkish_chat_gets_turkish_receipt_lines(monkeypatch) -> None:
    """8: never an English "Couldn't" in a Turkish chat."""
    _enable_timed_cards(monkeypatch)
    manifest = _timed_manifest(monkeypatch)
    ask = _ASK.model_copy(
        update={"description": "menü fotoğrafı 3. saniyede 2 saniye küçük kart olarak gelsin"}
    )
    with reply_language_for("tr"):
        status, reason, _ = _receipt(ask, plan_facts_from_strategy(_STRATEGY, manifest=manifest))
        assert (status, reason) == ("met", "menu photo 3.0 sn'de 2.0 sn küçük kart olarak.")
        past_end = {
            "version": 1,
            "matcher": "timed",
            "timeline_end_s": 2.4,
            "placed": [],
            "unplaced": [
                {
                    "card_id": "menu",
                    "label": "menu photo",
                    "at_s": 3.0,
                    "reason": "past_end",
                    "timeline_end_s": 2.4,
                }
            ],
        }
        status, reason, _ = _receipt(ask, plan_facts_from_phone_variant(_variant(past_end)))
        assert status == "not_possible" and "videonun sonundan sonra" in reason
        monkeypatch.setattr(
            capabilities.settings, "phone_timed_cards_enabled", False, raising=False
        )
        off = _timed_manifest(monkeypatch)
        status, reason, _ = _receipt(ask, plan_facts_from_strategy(_STRATEGY, manifest=off))
        assert reason == "Tam zamanlı küçük kartlar bu düzenleme için henüz yok."
