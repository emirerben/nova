"""KRI-1346 / KRI-1347 (yorum3): one capability answer for every hold / draft / review line.

Failure modes these pin (docs/reviews/kri-1346-1347-honest-replies.md): a "can't" naming the
wrong capability (a closing screen answered as a colour behind the speaker), a heads-up "won't
be applied" for what the route's writer draws, a Done line beside a heads-up that denies it, a
"have a look" ask a notice says the plan changed, and a review Done the render receipt
contradicts. Each control also checks the case that must NOT change. The recorded yorum3 draft
is replayed end to end by `scripts/kria/check_reply_consistency.py` (all route fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria import brief_checks
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import KriaTurnPlan, RequirementReceipt
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
from app.tasks.kria_runtime import _MAX_TURN_RESPONSE_CHARS, _fit_reply

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
    """Off: the treatments line is the old one even with the overlays lane on, and every
    reconcile step returns its input unchanged. (The closing card's own answer is #2256's
    `_check_closing_card`, not behind this switch.)"""
    _overlays_lane(monkeypatch, True)
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
#
# The closing card is answered by `brief_checks._check_closing_card` (#2256) through the shared
# provider `phone_rollout.phone_talking_closing_card_available`; these tests drive that real
# provider through its settings, never a stand-in name.


@pytest.fixture
def closing_card_on(monkeypatch):
    """The closing card switched on and device-qualified, read by the real provider."""
    from app.services import phone_rollout

    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)
    features = {
        *settings.phone_render_verified_features,
        *phone_rollout.PHONE_NARRATED_CAPTION_FEATURES,
        *phone_rollout.CLOSING_CARD_ENGINE_FEATURES,
    }
    monkeypatch.setattr(settings, "phone_render_verified_features", sorted(features))
    assert phone_rollout.phone_talking_closing_card_available()


@pytest.fixture
def closing_card_off(monkeypatch):
    from app.services import phone_rollout

    monkeypatch.setattr(settings, "phone_talking_closing_card_enabled", False)
    assert not phone_rollout.phone_talking_closing_card_available()


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_closing_screen_ask_names_the_closing_screen(honest, closing_card_off, lang):
    facts = brief_checks.PlanFacts(phone_talking=True, client_protocol=46)
    with reply_language_for(lang):
        receipt = brief_checks.check_requirement(CLOSING_CARD_ASK, facts)
        expected = brief_checks._loc(brief_checks._CC_UNAVAILABLE)  # noqa: SLF001
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith(expected)
    assert brief_checks.is_format_limit(receipt.reason)
    assert not contradictions([CLOSING_CARD_ASK], [receipt])


def test_colour_behind_the_speaker_stays_a_speaker_background(honest):
    assert brief_checks.asks_speaker_background(COLOUR_BEHIND_ME)
    assert not brief_checks.asks_closing_card(COLOUR_BEHIND_ME)


def test_closing_words_without_a_colour_keep_their_own_check(honest):
    assert not brief_checks.asks_closing_card(CLOSING_TEXT_ONLY)


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_available_closing_card_is_never_called_unsupported(
    honest, closing_card_on, monkeypatch, lang
):
    """Honest replies + closing card together (#2254 x #2256): the draft promises the card,
    the finished video is judged by its own `phone_closing_card` receipt, and no line says an
    available card is unsupported or names another capability."""
    _overlays_lane(monkeypatch, True)
    finished = {"resolved_archetype": "subtitled", "edit_format": "subtitled"}
    with reply_language_for(lang):
        draft = brief_checks.check_requirement(
            CLOSING_CARD_ASK, brief_checks.PlanFacts(phone_talking=True, client_protocol=46)
        )
        planned = brief_checks._loc(brief_checks._CC_PLAN)  # noqa: SLF001
        drawn = brief_checks.check_requirement(
            CLOSING_CARD_ASK,
            brief_checks.plan_facts_from_phone_variant(
                {**finished, "phone_closing_card": {"status": "applied", "text_color": "#FFFF00"}}
            ),
        )
        missing = brief_checks.check_requirement(
            CLOSING_CARD_ASK,
            brief_checks.plan_facts_from_phone_variant(finished),
        )
        not_drawn = brief_checks._loc(brief_checks._CC_NOT_DRAWN)  # noqa: SLF001
    assert (draft.status, draft.reason) == ("partial", planned)
    assert drawn.status == "met"
    assert missing.status == "not_possible" and missing.reason.startswith(not_drawn)
    unsupported = (
        brief_checks._loc(brief_checks._CC_UNAVAILABLE),  # noqa: SLF001
        brief_checks._loc(brief_checks._SBC_UNAVAILABLE),  # noqa: SLF001
    )
    for receipt in (draft, drawn, missing):
        assert not str(receipt.reason or "").startswith(unsupported)
        assert not contradictions([CLOSING_CARD_ASK], [receipt])


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
    }
    assert [r["contradictions"] for r in on] == [[]] * len(on)
    held_en = next(r["reply"] for r in on if r["variant"] == "held" and r["lang"] == "en")
    assert brief_checks._CC_UNAVAILABLE in held_en  # noqa: SLF001
    assert "plain colour behind you" not in held_en
    draft_en = next(r["reply"] for r in on if r["variant"] == "draft" and r["lang"] == "en")
    assert "Done: When I mention the launch" in draft_en
    assert "treatments" not in draft_en

    # #2254 x #2256: with the closing card switched on and device-qualified, the same replay
    # promises the card (EN + TR) and still has no contradiction.
    from app.services import phone_rollout
    from tests._prod_profile import PROD_VERIFIED_FEATURES

    card_on = module.check(
        fx,
        True,
        settings_overrides={
            "phone_talking_closing_card_enabled": True,
            "phone_render_verified_features": sorted(
                {*PROD_VERIFIED_FEATURES, *phone_rollout.CLOSING_CARD_ENGINE_FEATURES}
            ),
        },
    )
    assert [r["contradictions"] for r in card_on] == [[]] * len(card_on)
    held = {r["lang"]: r["reply"] for r in card_on if r["variant"] == "held"}
    assert brief_checks._CC_PLAN in held["en"]  # noqa: SLF001
    assert brief_checks._REASON_TR[brief_checks._CC_PLAN] in held["tr"]  # noqa: SLF001
    for reply in held.values():
        assert brief_checks._CC_UNAVAILABLE not in reply  # noqa: SLF001


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_old_app_build_is_told_to_update_not_that_the_card_is_unsupported(
    honest, closing_card_on, lang
):
    """#2256's build check: the card is available server-side but this app build can't hold
    the frame, so the reply names the update (a closing-card reason), never "unavailable"."""
    with reply_language_for(lang):
        receipt = brief_checks.check_requirement(
            CLOSING_CARD_ASK, brief_checks.PlanFacts(phone_talking=True, client_protocol=1)
        )
        too_old = brief_checks._loc(brief_checks._CC_APP_TOO_OLD)  # noqa: SLF001
        unavailable = brief_checks._loc(brief_checks._CC_UNAVAILABLE)  # noqa: SLF001
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith(too_old)
    assert not receipt.reason.startswith(unavailable)
    assert not contradictions([CLOSING_CARD_ASK], [receipt])


# --- KRI-1485: the held reply's receipt lines inside `KriaTurnPlan.response` ------------------
# Qendresa Q2 run 3 (job 77c63496): the first hold listed nine asks and ended "- Add this label
# when the Claude code game studio section star…", then the "I held this first draft back"
# sentence. That reply is bound by the contract's 1200 characters, so it is the one place rows
# can be counted instead of shown. Ways the fitter could fail here (written before it): a line
# cut in the middle; the count missing or wrong; the tail (what is held, the question) cut;
# rows that only differ in their quoted text not folded before any is counted; a Done row kept
# while a Couldn't row is counted; over 1200 characters; the switch off not the old cut.

_Q2_REPLIES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1485_q2_run3_replies.json").read_text(
        encoding="utf-8"
    )
)
_HOLD_TAIL = _Q2_REPLIES["hold"]["tail"]


def _held(body: str, tail: str = _HOLD_TAIL) -> list[str]:
    """The fitted reply's receipt lines; the reply is valid for the contract and ends on the
    whole tail."""
    reply = _fit_reply(body, tail)
    KriaTurnPlan(mode="respond", turn_value="question", response=reply)
    assert len(reply) <= _MAX_TURN_RESPONSE_CHARS
    assert reply.endswith(f"\n{tail}")
    return reply[: -len(tail) - 1].split("\n")


def test_q2_run3_hold_is_cut_at_a_line_and_says_how_many_more():
    body = _Q2_REPLIES["hold"]["body"]
    asked = body.split("\n")
    lines = _held(body)

    kept = [line for line in lines if not line.startswith("- …")]
    assert kept == asked[: len(kept)]  # whole lines, in order, nothing reworded
    assert len(kept) >= 10  # the heading, the reason and at least eight asks
    assert lines[-1] == f"- …and {len(asked) - len(kept)} more"
    assert not any(line.endswith("…") for line in lines)


def test_q2_run3_hold_with_the_switch_off_is_the_stored_reply(monkeypatch):
    monkeypatch.setattr(settings, "kria_reply_line_fit_enabled", False)
    assert _fit_reply(_Q2_REPLIES["hold"]["body"], _HOLD_TAIL) == _Q2_REPLIES["stored_hold_reply"]


def test_held_rows_that_only_differ_in_their_quoted_text_fold_before_any_is_counted():
    ask = (
        "Show this label as text on screen when I start talking about that repo, not as a clip "
        "label; replace the previous repo label."
    )
    reason = "None of the 8 clips got its own text in this draft."
    labels = ["2. Impeccable", "3. TradingAgents", "4. Agents", "5. Claude code game studio"]
    body = "\n".join(
        [
            "I can't make this yet:",
            "- Opening title in bold caps, then serif, then handwritten.: "
            + "It is not there. " * 12,
            *(f'- {ask} ("{label}"): {reason}' for label in labels),
            "- Cut all my pauses.: " + "Only the long pauses are cut. " * 8,
        ]
    )
    assert len(body) > _MAX_TURN_RESPONSE_CHARS
    lines = _held(body)

    quoted = ", ".join(f'"{label}"' for label in labels)
    assert f"- {ask} ({quoted}): {reason}" in lines
    assert sum(ask in line for line in lines) == 1
    assert lines[-1].startswith("- Cut all my pauses.: Only the long pauses are cut.")
    assert not any(line.startswith("- …") for line in lines)


def test_a_held_reply_counts_done_rows_before_it_counts_a_row_that_is_not_done():
    body = "\n".join(
        [
            "Not everything you asked for made it in:",
            *(
                f"- Done: Keep clip number {i} exactly as it was filmed, {'no changes ' * 9}"
                for i in range(8)
            ),
            "- Couldn't: Add a song (A Talking video can't do this, so it stays as filmed)",
        ]
    )
    lines = _held(body)

    assert "- Couldn't: Add a song (A Talking video can't do this, so it stays as filmed)" in lines
    counted = next(line for line in lines if line.startswith("- …and"))
    assert counted.endswith("more done") and lines[-1] == counted


def test_a_held_reply_far_too_long_still_fits_and_counts_every_row_it_leaves_out():
    """Forty different asks can't all be named in 1200 characters with the tail: the last ones
    become one honest count (the cards under the reply still list every ask)."""
    asked = [
        f"- Put the words for moment {i} right where I say them: I can't find that moment."
        for i in range(40)
    ]
    lines = _held("\n".join(["I can't make this yet:", *asked]))

    kept = lines[1:-1]
    assert kept == asked[: len(kept)] and kept
    assert lines[-1] == f"- …and {40 - len(kept)} more"


def test_a_reply_fitted_twice_keeps_one_count_line_with_the_whole_total():
    """Review of the Q2 commits: the receipt reply is fitted to 1200 ("…and 7 more done"), then
    the held reply fits it again into what is left beside its tail. The count line is a count,
    not a row: one line, every ask it stands for, and the same text as fitting it once."""
    asks = [
        BriefRequirement(
            id=f"d{i}",
            kind="text",
            scope="global",
            description=f"Keep clip number {i} exactly as it was filmed, with no changes",
            literal=f"CLIP {i}",
        )
        for i in range(20)
    ]
    miss = BriefRequirement(id="p1", kind="text", scope="global", description="Add a song")
    reason = brief_checks._CLOSING_ARROW_MISSING
    receipts = [
        *(
            RequirementReceipt(requirement_id=a.id, status="met", verification="checked")
            for a in asks
        ),
        RequirementReceipt(
            requirement_id="p1", status="partial", reason=reason, verification="checked"
        ),
    ]
    brief = CreativeBrief(version=1, requirements=[*asks, miss])
    once, _ = brief_checks.reply_and_lead_from_receipts(brief, receipts)
    assert sum(line.startswith("- …and") for line in once.split("\n")) == 1

    tail = (
        "I held this first draft back. " + "Tell me what to change and I'll prepare it again. " * 3
    )
    twice = _fit_reply(once, tail)
    lines = _held(once, tail)

    counts = [line for line in lines if line.startswith("- …and")]
    named = sum(line.startswith("- Done: Keep clip number") for line in lines)
    assert counts == [f"- …and {20 - named} more done"]
    assert f"- Partly: Add a song ({reason})" in lines
    # Fitting the already fitted reply equals fitting the whole list once.
    whole = "\n".join(
        [
            "Not everything you asked for made it in:",
            *(f'- Done: {a.description} ("{a.literal}")' for a in asks),
            f"- Partly: Add a song ({reason})",
        ]
    )
    assert twice == _fit_reply(whole, tail)
    assert _fit_reply(twice[: -len(tail) - 1], tail) == twice
