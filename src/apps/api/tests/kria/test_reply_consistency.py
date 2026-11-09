"""KRI-1346 / KRI-1347 (yorum3): one capability answer for every hold / draft / review line.

Failure modes these pin (docs/reviews/kri-1346-1347-honest-replies.md): a "can't" naming the
wrong capability (a closing screen answered as a colour behind the speaker), a heads-up "won't
be applied" for what the route's writer draws, a Done line beside a heads-up that denies it, a
"have a look" ask a notice says the plan changed, and a review Done the render receipt
contradicts. Each control also checks the case that must NOT change. The recorded yorum3 draft
is replayed end to end by `scripts/kria/check_reply_consistency.py` (all route fixtures).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import brief_checks
from app.kria.brief import BriefRequirement
from app.kria.contracts import RequirementReceipt
from app.kria.reply_consistency import (
    CLOSING_CARD,
    SPEAKER_BACKGROUND,
    VISUALS,
    contradictions,
    reconcile_disclosures,
    reconcile_notices,
    reconcile_review,
)
from app.kria.reply_language import reply_language_for
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities import talking_head
from app.kria.route_disclosure import Disclosure

# yorum3 r5 / r7 / r4, as the brief extractor recorded them.
CLOSING_CARD_ASK = BriefRequirement(
    id="r5",
    kind="text",
    scope="global",
    literal="follow for more brand and marketing content",
    description="End on a red screen with yellow text",
    facts={
        "closing": "follow for more brand and marketing content",
        "text_color": "#FFFF00",
        "background_color": "#FF0000",
    },
)
FULL_SCREEN_ASK = BriefRequirement(
    id="r7",
    kind="style",
    scope="global",
    description="When I mention the launch, show that picture full screen",
    facts={"display_mode": "fullscreen"},
)
LABEL_ASK = BriefRequirement(
    id="r4",
    kind="text",
    scope="global",
    literal="Yorumla",
    description="small label in the top left for the whole video",
    facts={"position": "top_left"},
)
COLOUR_BEHIND_ME = BriefRequirement(
    id="c1",
    kind="style",
    scope="global",
    description="a plain white background behind me",
    facts={"speaker_background": True, "background_color": "#FFFFFF"},
)
CLOSING_TEXT_ONLY = BriefRequirement(
    id="c2",
    kind="text",
    scope="global",
    literal="follow me",
    description='End with "follow me"',
    facts={"closing": "follow me"},
)
YORUM3_STRATEGY = {"edit_format": "subtitled", "optional_treatments": ["overlays"]}
LABEL_NOTICE = (
    '"Yorumla" shows as the opening title for the first few seconds. '
    "Talking edits can't keep text on screen the whole time yet."
)


@pytest.fixture
def honest(monkeypatch):
    monkeypatch.setattr(settings, "kria_honest_reply_lines_enabled", True)


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(settings, "kria_honest_reply_lines_enabled", False)


def _overlays_lane(monkeypatch, on: bool) -> None:
    monkeypatch.setattr("app.services.phone_rollout.phone_subtitled_overlays_supported", lambda: on)


def _met(req_id: str, **extra) -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id=req_id, status="met", verification="checked", stage="checked", **extra
    )


def _treatments_line(strategy=YORUM3_STRATEGY) -> str | None:
    build = talking_head.FIELD_HANDLING["optional_treatments"].build
    return build(strategy["optional_treatments"], strategy)


# --- switch off: byte-identical -----------------------------------------------------------


def test_flag_off_is_byte_identical_to_legacy(legacy, monkeypatch):
    """Off: the closing card is still read as a speaker background (old answer), the
    treatments line is the old one even with the overlays lane on, and every reconcile step
    returns its input unchanged."""
    _overlays_lane(monkeypatch, True)
    assert brief_checks.asks_speaker_background(CLOSING_CARD_ASK)
    assert not brief_checks.closing_card_declined(CLOSING_CARD_ASK)
    assert _treatments_line() == lines.treatments(["overlays"], YORUM3_STRATEGY)
    heads_up = [Disclosure("not_applied_on_route", "optional_treatments", _treatments_line())]
    receipts = [_met("r7")]
    assert (
        reconcile_disclosures(
            heads_up, requirements=[FULL_SCREEN_ASK], receipts=receipts, strategy=YORUM3_STRATEGY
        )
        == heads_up
    )
    unchecked = RequirementReceipt(
        requirement_id="r4", status="partial", verification="unchecked", stage="understood"
    )
    assert reconcile_notices([LABEL_ASK], [unchecked], [LABEL_NOTICE]) == (
        [unchecked],
        [LABEL_NOTICE],
    )
    done = _met("r7", generation_id="g1")
    assert reconcile_review([FULL_SCREEN_ASK], [done], {"phone_beat_receipt": {"placed": []}}) == [
        done
    ]


# --- 1. the right capability in a "can't" -------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "Ending on a coloured screen with your words on it isn't available yet."),
        ("tr", "Videoyu üstünde yazın olan renkli bir ekranla bitirmek henüz mümkün değil."),
    ],
)
def test_closing_screen_ask_names_the_closing_screen(honest, lang, expected):
    facts = brief_checks.PlanFacts()
    with reply_language_for(lang):
        receipt = brief_checks.check_requirement(CLOSING_CARD_ASK, facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == expected
    assert brief_checks.is_format_limit(receipt.reason)  # same hold flow as the old reason
    assert not contradictions([CLOSING_CARD_ASK], [receipt])


def test_colour_behind_the_speaker_stays_a_speaker_background(honest):
    assert brief_checks.asks_speaker_background(COLOUR_BEHIND_ME)
    assert not brief_checks.asks_closing_card(COLOUR_BEHIND_ME)


def test_closing_words_without_a_colour_keep_their_own_check(honest):
    assert not brief_checks.asks_closing_card(CLOSING_TEXT_ONLY)
    assert not brief_checks.closing_card_declined(CLOSING_TEXT_ONLY)


def test_closing_card_capability_when_it_ships_is_not_declined(honest, monkeypatch):
    monkeypatch.setattr(
        "app.services.phone_rollout.phone_closing_card_supported", lambda: True, raising=False
    )
    assert not brief_checks.closing_card_declined(CLOSING_CARD_ASK)
    assert not brief_checks.asks_speaker_background(CLOSING_CARD_ASK)


def test_wrong_capability_is_detected():
    """Negative control: the yorum3 answer (a speaker-background reason on a closing card)."""
    with reply_language_for("tr"):
        reason = brief_checks._sb(brief_checks._SBC_UNAVAILABLE)  # noqa: SLF001
    wrong = RequirementReceipt(
        requirement_id="r5", status="not_possible", verification="checked", reason=reason
    )
    [found] = contradictions([CLOSING_CARD_ASK], [wrong])
    assert (found.kind, found.capability) == ("wrong_capability", SPEAKER_BACKGROUND)


# --- 2. the heads-up follows the capability the writer reads -------------------------------


def test_treatments_line_leaves_out_overlays_the_phone_draws(honest, monkeypatch):
    _overlays_lane(monkeypatch, True)
    assert _treatments_line() is None
    mixed = {**YORUM3_STRATEGY, "optional_treatments": ["overlays", "transitions"]}
    assert _treatments_line(mixed) == lines.treatments(["transitions"], mixed)


def test_treatments_line_stays_when_the_overlays_lane_is_off(honest, monkeypatch):
    _overlays_lane(monkeypatch, False)
    assert _treatments_line() == lines.treatments(["overlays"], YORUM3_STRATEGY)


# --- 3. no Done beside a heads-up that denies it -------------------------------------------


def test_done_beside_a_denying_heads_up_drops_the_heads_up(honest):
    heads_up = [
        Disclosure(
            "not_applied_on_route",
            "optional_treatments",
            lines.treatments(["overlays", "transitions"], YORUM3_STRATEGY),
        ),
        Disclosure("not_applied_on_route", "pacing", "the video follows your speech"),
    ]
    strategy = {**YORUM3_STRATEGY, "optional_treatments": ["overlays", "transitions"]}
    [found] = contradictions(
        [FULL_SCREEN_ASK], [_met("r7")], disclosures=heads_up, strategy=strategy
    )
    assert (found.kind, found.capability) == ("draft_self", VISUALS)
    kept = reconcile_disclosures(
        heads_up, requirements=[FULL_SCREEN_ASK], receipts=[_met("r7")], strategy=strategy
    )
    assert [d.text for d in kept] == [
        lines.treatments(["transitions"], strategy),
        "the video follows your speech",
    ]
    assert not contradictions([FULL_SCREEN_ASK], [_met("r7")], disclosures=kept, strategy=strategy)


def test_couldnt_beside_the_heads_up_is_not_a_contradiction(honest):
    """Both lines say no: consistent, nothing dropped."""
    heads_up = [Disclosure("not_applied_on_route", "optional_treatments", _treatments_line())]
    no = RequirementReceipt(requirement_id="r7", status="not_possible", verification="checked")
    assert not contradictions(
        [FULL_SCREEN_ASK], [no], disclosures=heads_up, strategy=YORUM3_STRATEGY
    )


# --- 4. "have a look" + "it won't happen" -------------------------------------------------


def test_label_the_plan_shortened_is_one_partly_line(honest):
    unchecked = RequirementReceipt(
        requirement_id="r4", status="partial", verification="unchecked", stage="understood"
    )
    other = "Full-screen Visuals aren't available for this edit."
    [receipt], notices = reconcile_notices([LABEL_ASK], [unchecked], [LABEL_NOTICE, other])
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == LABEL_NOTICE.rstrip(".")
    assert notices == [other]


# --- 5. the review never says Done against the render receipt ------------------------------


def test_review_done_with_nothing_placed_becomes_have_a_look(honest):
    done = _met("r7", generation_id="g1")
    render = {"phone_lane_receipt": {"applied": []}, "phone_beat_receipt": {"placed": []}}
    [receipt] = reconcile_review([FULL_SCREEN_ASK], [done], render)
    assert (receipt.status, receipt.verification) == ("partial", "unchecked")
    placed = {"phone_beat_receipt": {"placed": [{"beat_id": "launch"}]}}
    assert reconcile_review([FULL_SCREEN_ASK], [done], placed) == [done]


def test_heads_up_denied_by_the_render_receipt_is_detected():
    heads_up = [{"subject": "optional_treatments", "text": lines.treatments(["overlays"], {})}]
    render = {"phone_lane_receipt": {"applied": ["overlays"], "dropped": []}}
    found = contradictions([], [], disclosures=heads_up, strategy=YORUM3_STRATEGY, render=render)
    assert [(c.kind, c.capability) for c in found] == [("render", VISUALS)]


def test_closing_card_is_its_own_capability():
    from app.kria.reply_consistency import requirement_capabilities

    assert requirement_capabilities(CLOSING_CARD_ASK) == {CLOSING_CARD}
    assert requirement_capabilities(COLOUR_BEHIND_ME) == {SPEAKER_BACKGROUND}


# --- the recorded incident, end to end -----------------------------------------------------


def test_yorum3_replay_has_no_contradiction_with_the_switch_on():
    """The recorded yorum3 draft and held draft, assembled by the draft turn's own functions
    (EN + TR): today's switch-off replies reproduce the incident's contradictions (negative
    control), the switch-on replies have none."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[5] / "scripts/kria/check_reply_consistency.py"
    spec = importlib.util.spec_from_file_location("check_reply_consistency", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fx = module.SR.load_fixture(module.OWN_FIXTURES / "yorum3")
    off = module.check(fx, False)
    on = module.check(fx, True)
    found_off = {
        (r["variant"], c["kind"], c["capability"]) for r in off for c in r["contradictions"]
    }
    assert found_off == {
        ("draft", "draft_self", VISUALS),
        ("draft", "render", VISUALS),
        ("held", "draft_self", VISUALS),
        ("held", "wrong_capability", SPEAKER_BACKGROUND),
    }
    assert [r["contradictions"] for r in on] == [[]] * len(on)
    held_en = next(r["reply"] for r in on if r["variant"] == "held" and r["lang"] == "en")
    assert "Ending on a coloured screen with your words on it isn't available yet" in held_en
    assert "plain colour behind you" not in held_en
    draft_en = next(r["reply"] for r in on if r["variant"] == "draft" and r["lang"] == "en")
    assert "Done: When I mention the launch" in draft_en
    assert "treatments" not in draft_en
