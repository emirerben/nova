"""KRI-1237: a phone Talking "show my screenshots when I talk about them" ask, before and after.

Qendresa Q1 (prod 2026-11-06, thread b931bc4e, TestFlight 1910): she asked "When I talk about
something, show the matching screenshot or screen recording in the top or bottom part of the
screen, not over my face". The draft judged it with the word-triggered pop-in check, which only
sees the planner's reaction beats, and said "This draft has no pop-ins timed to your words."
(`not_possible`, readiness `unsupported`): the hold "I can't make this yet". Yet the phone
Talking render grounds the Visuals pool as cards on the words they match
(`ground_phone_subtitled_overlays`): job e7d1e06f placed all five screenshots. Nothing read that
receipt afterwards either.

Ways the fix could fail (written before the fix):
1. The draft still answers `not_possible` / readiness `unsupported` for the Q1 ask (the bug).
2. The draft promises cards the render will not place: overlay lane closed, no Visuals in the
   pool, only screen recordings while video cards are off, the full-screen layout instead of
   cards, a Montage/cloud edit rather than a phone Talking one, or several speaking clips
   (the worker skips the lanes then).
3. The promise swallows a word-named pop-in ask ("when I say Spec Kit, pop up its sticker"),
   whose own reaction-beat verdict must stand.
4. The promise has no Turkish twin, or is not a format promise, so a Turkish reply leaks English
   or the draft asks "make it without these?" about something the render does.
5. After render, the receipt quotes the original take's seconds (the receipt's clock) instead of
   the finished video's: speech cleanup cut 6.6 s from Q1, so 0:08 would be wrong by 3 s.
6. After render, a Visual that did not appear is not named, or one dropped by the cut is called
   shown.
7. After render, "met" with nothing placed, or a verdict read off a receipt that is not about
   word-matched cards (a hand-authored lane, a failed matcher, the full-screen sequence).
8. After render, the names are the raw upload names (iOS UUID prefix, a cut-off ".p").
9. With no overlay receipt (an older render, lane off) a card verdict is invented.

Evidence level: offline replay of the real prod records (`fixtures/kri1237_qendresa_q1_cards.json`,
read-only admin GETs), no model call, no device.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import reply_language_for
from app.services import creator_capabilities as capabilities

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1237_qendresa_q1_cards.json").read_text(
        encoding="utf-8"
    )
)
CREATOR = uuid.UUID("00000000-0000-4000-8000-000000001237")
TAKE = FIXTURE["media"][0]["media_id"]
STRATEGY = FIXTURE["strategy"]
BRIEF_V1 = CreativeBrief.model_validate(FIXTURE["brief_v1"])
REQS = {req.id: req for req in BRIEF_V1.requirements}
CARDS = REQS["r4"]
PROD_ROWS = {row["requirement_id"]: row for row in FIXTURE["turn1_readiness_rows"]}
PROD_RECEIPTS = {row["requirement_id"]: row for row in FIXTURE["turn1_receipts"]}
VARIANT = FIXTURE["variant"]

PROMISE_EN = (
    "I'll show your Visuals as cards when you talk about them; one you never talk about "
    "won't appear"
)
# Independent expectation: the finished-video second each screenshot appears at, worked by hand
# from the receipt's source-take start minus every `silence_cut.removed` span before it
# (0-3.04 retake, 12.965-14.334, 21.105-21.71, 36.105-37.495 pauses):
#   8.48-3.04=5.44 | 15.32-4.409=10.911 | 22.82-5.014=17.806 | 31.06-5.014=26.046
#   | 39.32-6.404=32.916
SHOWN_AT = {
    "Screenshot 1 spec-kit": "0:05",
    "Screenshot 2 impeccable": "0:11",
    "Screenshot 3 tradingage": "0:18",
    "Screenshot 4 agents": "0:26",
    "Screenshot 5 gamestudio": "0:33",
}
# The receipt's own (source-take) clock: quoting these is failure mode 5.
SOURCE_CLOCK = ("0:08", "0:15", "0:23", "0:31", "0:39")
NOT_SHOWN = (
    "Screen Recording 1 spec",
    "Screen Recording 2 impe",
    "Screen Recording 3 trad",
    "Screen Recording 4 agen",
    "Screen Recording 5 game",
)


@pytest.fixture(autouse=True)
def _gate(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod as Q1 ran: the phone Talking writer for this account, overlay lanes on."""
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    # On in prod (job e7d1e06f carries `phone_text_style`; the code default is off).
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)


def _manifest(media: list[dict] | None = None):
    return capabilities.resolve_creator_manifest(
        item_id="item-kri-1237",
        edit_format="subtitled",
        media=FIXTURE["media"] if media is None else media,
        phone_source_media_ids=[TAKE],
        phone_rendering_allowed=True,
    )


def _draft_facts(manifest=None, *, phone_talking: bool = True, **strategy):  # noqa: ANN001, ANN003
    facts = plan_facts_from_strategy(
        {**STRATEGY, **strategy},
        clip_ids=[TAKE],
        manifest=_manifest() if manifest is None else manifest,
    )
    return dataclasses.replace(facts, phone_talking=phone_talking)


def _receipt(req: BriefRequirement, facts):  # noqa: ANN001, ANN202
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def _readiness(**strategy):  # noqa: ANN003, ANN202
    report = assess_readiness(
        BRIEF_V1,
        {**STRATEGY, **strategy},
        media={"clip_paths": [TAKE]},
        manifest=_manifest(),
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}


def _promise() -> str:
    """The new reason constant, imported per test so origin/main shows each test's failure."""
    from app.kria.brief_checks import TALKING_CARDS_PLAN  # noqa: PLC0415

    return TALKING_CARDS_PLAN


# ------------------------------------------------------------------- 1. the Q1 draft (turn 1)


def test_q1_replay_reproduces_prod_for_the_asks_this_fix_does_not_touch() -> None:
    """The replay is faithful: the title, the five list labels and "no music" read as prod did.

    (r1 / r2 also hang on the item 12 / 13 switches, which this replay leaves at their defaults.)
    """
    rows = _readiness()
    for rid in ("r3", "r5", "r6", "r7", "r8", "r9", "r11"):
        assert rows[rid]["disposition"] == PROD_ROWS[rid]["disposition"], rid
        assert rows[rid]["reason"] == PROD_ROWS[rid]["reason"], rid
    assert PROD_ROWS["r4"]["disposition"] == "unsupported"
    assert PROD_RECEIPTS["r4"]["reason"] == "This draft has no pop-ins timed to your words."


def test_q1_cards_ask_is_a_render_promise_not_a_refusal() -> None:
    """Failure mode 1: prod said "This draft has no pop-ins timed to your words"."""
    rows = _readiness()
    assert rows["r4"]["disposition"] != "unsupported", rows["r4"]
    assert rows["r4"]["reason"] == PROMISE_EN == _promise()
    receipt = _receipt(CARDS, _draft_facts())
    assert (receipt.status, receipt.reason) == ("partial", PROMISE_EN)
    assert receipt.verification == "checked"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KRI-1235 owns approval_readiness: the promise turns approvable once its render-promise "
        "tuple holds TALKING_CARDS_PLAN. Whichever PR lands second drops this marker."
    ),
)
def test_q1_cards_promise_is_approvable() -> None:
    assert _readiness()["r4"]["disposition"] in APPROVABLE


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_promise_is_a_format_promise_in_both_languages(language: str) -> None:
    """Failure mode 4: a Turkish reply says it in Turkish; the reply never asks to simplify."""
    with reply_language_for(language):
        receipt = _receipt(CARDS, _draft_facts())
    assert receipt.status == "partial"
    if language == "tr":
        assert receipt.reason != PROMISE_EN
        assert "kart" in receipt.reason
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)


# ------------------------------------------------- 2. no promise where the render places none


def _no_promise(facts) -> None:  # noqa: ANN001
    receipt = _receipt(CARDS, facts)
    assert receipt.reason != PROMISE_EN, receipt
    assert receipt.status != "met", receipt


def test_no_promise_when_the_overlay_lane_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", False)
    _no_promise(_draft_facts())


def test_no_promise_without_visuals_in_the_pool() -> None:
    _no_promise(_draft_facts(_manifest([FIXTURE["media"][0]])))


def test_no_promise_for_screen_recordings_only_while_video_cards_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    videos = [row for row in FIXTURE["media"] if row["kind"] == "video"]
    monkeypatch.setattr(settings, "phone_subtitled_video_overlays_enabled", False)
    _no_promise(_draft_facts(_manifest(videos)))
    # ... and the screenshots alone are enough while video cards are off.
    images = [FIXTURE["media"][0], *(row for row in FIXTURE["media"] if row["kind"] == "image")]
    assert _receipt(CARDS, _draft_facts(_manifest(images))).reason == PROMISE_EN


def test_no_card_promise_on_the_full_screen_layout() -> None:
    _no_promise(_draft_facts(overlay_display="fullscreen"))


def test_no_promise_off_the_phone_talking_writer() -> None:
    _no_promise(_draft_facts(phone_talking=False))


def test_no_promise_when_several_clips_may_speak() -> None:
    """The worker skips the overlay lanes when several clips speak."""
    second = {"media_id": "analysis-proxy-ios-SECOND.mp4", "kind": "video", "duration_s": 20.0}
    media = [FIXTURE["media"][0], second, *FIXTURE["media"][1:]]
    _no_promise(_draft_facts(_manifest(media), selected_media_ids=[TAKE, second["media_id"]]))


def test_a_named_word_pop_in_keeps_its_own_verdict() -> None:
    """Failure mode 3: a beat asked on a named word is the planner's, judged as before."""
    named = BriefRequirement(
        id="r4",
        kind="style",
        scope="global",
        description='When I say "Spec Kit", pop up the Spec Kit screenshot',
        facts={"triggers": ["Spec Kit"]},
    )
    receipt = _receipt(named, _draft_facts())
    # Today's verdict for a named beat the planner did not author, unchanged by the fix.
    assert (receipt.status, receipt.reason) == (
        "not_possible",
        "This draft has no pop-ins timed to your words.",
    )


# ------------------------------------------------------------- 3. the finished video (job e7d1e06f)


def test_finished_q1_names_each_screenshot_and_when_it_shows() -> None:
    """Failure modes 5, 6 and 8 on the real receipt."""
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(VARIANT))
    assert receipt.status == "met", receipt
    assert receipt.verification == "checked"
    reason = receipt.reason
    for name, at in SHOWN_AT.items():
        assert f"{name} at {at}" in reason, (name, reason)
    assert not any(clock in reason for clock in SOURCE_CLOCK), reason
    for name in NOT_SHOWN:
        assert name in reason, (name, reason)
    assert "174F3FF6" not in reason and ".p," not in reason and ".png" not in reason


def test_finished_q1_in_turkish() -> None:
    with reply_language_for("tr"):
        receipt = _receipt(CARDS, plan_facts_from_phone_variant(VARIANT))
    assert receipt.status == "met"
    assert "Screenshot 1 spec-kit" in receipt.reason
    assert "0:05" in receipt.reason
    assert "Shown" not in receipt.reason and "Not shown" not in receipt.reason


def _variant(**receipt_changes):  # noqa: ANN003, ANN202
    variant = copy.deepcopy(VARIANT)
    variant["phone_overlay_receipt"] = {**variant["phone_overlay_receipt"], **receipt_changes}
    return variant


def test_wrong_content_control_nothing_placed_is_never_met() -> None:
    """Failure mode 7: the same Visuals, none on screen (the matcher found no moment)."""
    receipt_rows = VARIANT["phone_overlay_receipt"]
    moved = [
        {"media_id": row["media_id"], "label": row["label"], "reason": "no_spoken_match"}
        for row in receipt_rows["placed"]
    ]
    variant = _variant(placed=[], unplaced=[*receipt_rows["unplaced"], *moved])
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(variant))
    assert receipt.status == "not_possible", receipt
    assert "Screenshot 1 spec-kit" in receipt.reason
    assert not any(f"at {at}" in receipt.reason for at in SHOWN_AT.values())


def test_a_card_the_cleanup_cut_is_named_as_cut_not_shown() -> None:
    """Failure mode 6: the worker demotes a card whose moment speech cleanup removed."""
    receipt_rows = VARIANT["phone_overlay_receipt"]
    first, *rest = receipt_rows["placed"]
    demoted = {
        "media_id": first["media_id"],
        "label": first["label"],
        "reason": "speech_cleanup_cut",
    }
    variant = _variant(placed=rest, unplaced=[*receipt_rows["unplaced"], demoted])
    reason = _receipt(CARDS, plan_facts_from_phone_variant(variant)).reason
    assert "Screenshot 1 spec-kit at" not in reason
    assert "Screenshot 1 spec-kit" in reason
    assert "Screenshot 2 impeccable at 0:11" in reason


def test_no_times_when_the_cut_record_is_missing() -> None:
    """Failure mode 5: cleanup ran but its record is gone: name the cards, quote no second."""
    variant = copy.deepcopy(VARIANT)
    variant.pop("silence_cut")
    reason = _receipt(CARDS, plan_facts_from_phone_variant(variant)).reason
    assert "Screenshot 1 spec-kit" in reason
    assert not any(f"at {at}" in reason for at in (*SHOWN_AT.values(), *SOURCE_CLOCK)), reason


@pytest.mark.parametrize("matcher", ["manual", "failed", "sequence"])
def test_no_card_verdict_off_a_receipt_about_something_else(matcher: str) -> None:
    """Failure modes 7 and 9: only a word-matched card receipt speaks to this ask."""
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(_variant(matcher=matcher)))
    assert not any(name in (receipt.reason or "") for name in SHOWN_AT), receipt
    assert receipt.status != "met"


def test_no_card_verdict_without_an_overlay_receipt() -> None:
    variant = copy.deepcopy(VARIANT)
    variant.pop("phone_overlay_receipt")
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(variant))
    assert not any(name in (receipt.reason or "") for name in SHOWN_AT), receipt
    assert receipt.status != "met"
