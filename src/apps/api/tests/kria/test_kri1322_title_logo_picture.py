"""KRI-1322: "with the GitHub logo above it" is a picture, never missing title text.

Qendresa Q2 run 2 (prod thread 9db44dc2, 2026-11-08 13:47Z): the brief extractor filed the logo
as its own ``text/title`` requirement (r2, literal "GitHub logo", triggers ["GitHub logo"]).
`_check_literal_text` compared "GitHub logo" with the opening title, found it missing, and said
"That exact text isn't in this draft." Approval readiness made that ``needs_choice`` and the whole
first draft was held behind "Make it without these". The logo is an uploaded image (Visuals,
"GitHub logo.png"), which the planner puts above the title as a title-anchored reaction beat.

Ways this can fail, written before the fix:
 F1 the incident: the picture ask is judged as text and holds the draft as missing words.
 F2 a planned title logo is "Done" before the render, although the render may drop it
    (KRI-1303: no_room_above_title); it must be a render promise (approvable, "Will do at
    render"), for this text/title row and for the style row ("show the logo above the title").
 F3 on the finished video: a placed logo is met, a dropped one is never met and says why.
 F4 no logo beat in the draft although the image is there: the reason must name the picture,
    not "exact text", and the ask is still not met (the hold is honest).
 F5 the beat's picture does not resolve: "couldn't find the photo or sticker".
 F6 over-reach: a real missing title text that happens to hold "logo" keeps the text verdict.
 F7 the title look is off: nothing can sit above the title, so the draft never promises it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    RENDER_PROMISE_REASONS,
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.services import creator_capabilities as capabilities

CREATOR = uuid.UUID("00000000-0000-4000-8000-000000001322")
TAKE = "analysis-proxy-ios-A.mp4"
LOGO = "asset-1f0c2a9e-0b7d-4c55-9d1e-6a3c2b1f1322"
TITLE = "5 PROJECTS / the AI builder community / can't stop talking about!!!!"
EXACT_TEXT = "That exact text isn't in this draft."

# r1 and r2 of the incident's brief v1, as prod stored them.
R1 = BriefRequirement(
    id="r1",
    kind="text",
    scope="title",
    literal=TITLE,
    description="bold caps, then serif, then handwritten",
)
R2 = BriefRequirement(
    id="r2",
    kind="text",
    scope="title",
    literal="GitHub logo",
    description="GitHub logo above the title",
    facts={"triggers": ["GitHub logo"]},
)
TITLE_BEAT = {"beat_id": "gh", "trigger": "GitHub logo", "visual_id": LOGO, "anchor": "title"}


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    # On in prod for both Q2 runs (run 1's render reported `no_room_above_title`).
    monkeypatch.setattr(settings, "title_look_enabled", True)


def _manifest():  # noqa: ANN202
    return capabilities.resolve_creator_manifest(
        item_id="item-1322",
        edit_format="subtitled",
        media=[
            {"media_id": TAKE, "kind": "video", "duration_s": 48.8},
            {"media_id": LOGO, "kind": "image", "label": "GitHub logo.png"},
        ],
        phone_source_media_ids=[TAKE],
        phone_rendering_allowed=True,
    )


def _strategy(*beats: dict[str, Any]) -> dict[str, Any]:
    strategy: dict[str, Any] = {
        "direction": "fast_montage",
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "render_program": "native",
        "target_duration_s": 40,
        "selected_media_ids": [TAKE],
        "opening_title": TITLE,
        "rationale": "Her talk with the stacked title.",
    }
    if beats:
        strategy["reaction_beats"] = list(beats)
    return strategy


def _draft_receipts(strategy: dict[str, Any], *reqs: BriefRequirement):  # noqa: ANN202
    facts = plan_facts_from_strategy(strategy, manifest=_manifest())
    return {r.requirement_id: r for r in build_receipts(list(reqs), facts, include_unchecked=True)}


def _readiness(strategy: dict[str, Any], *reqs: BriefRequirement):  # noqa: ANN202
    report = assess_readiness(
        CreativeBrief(version=1, requirements=list(reqs)),
        strategy,
        media={"clip_paths": [TAKE]},
        manifest=_manifest(),
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}, report


def test_the_manifest_can_place_a_title_logo() -> None:
    """Guard for the rest: the draft's beat survives the same repair approval runs."""
    facts = plan_facts_from_strategy(_strategy(TITLE_BEAT), manifest=_manifest())
    assert facts.reaction_beats_available is True
    assert [b.anchor for b in facts.reaction_beats or ()] == ["title"]


def test_f1_the_incident_draft_is_not_held_for_the_logo() -> None:
    rows, report = _readiness(_strategy(TITLE_BEAT), R1, R2)
    assert rows["r2"]["disposition"] in APPROVABLE, rows["r2"]
    assert rows["r2"]["reason"] != EXACT_TEXT
    assert report["ready"] is True, rows


def test_f2_a_planned_title_logo_is_a_render_promise_not_done() -> None:
    receipt = _draft_receipts(_strategy(TITLE_BEAT), R1, R2)["r2"]
    assert receipt.status == "partial"
    assert receipt.reason in RENDER_PROMISE_REASONS
    assert "above your title" in receipt.reason
    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[R1, R2]),
        [_draft_receipts(_strategy(TITLE_BEAT), R1, R2)[rid] for rid in ("r1", "r2")],
        summary="Here is your draft.",
    )
    assert "Will do at render: GitHub logo above the title" in reply


def test_f2_the_style_logo_ask_is_a_render_promise_too() -> None:
    style = BriefRequirement(
        id="b1",
        kind="style",
        scope="global",
        description="Show the GitHub logo above the title",
        facts={"triggers": ["GitHub logo"]},
    )
    receipt = _draft_receipts(_strategy(TITLE_BEAT), style)["b1"]
    assert receipt.status == "partial"
    assert receipt.reason in RENDER_PROMISE_REASONS
    rows, _ = _readiness(_strategy(TITLE_BEAT), style)
    assert rows["b1"]["disposition"] in APPROVABLE, rows["b1"]


def _variant(*, placed: list | None = None, unplaced: list | None = None) -> dict[str, Any]:
    return {
        "text_elements": [
            {
                "id": "opening-title",
                "text": TITLE,
                "role": "generative_intro",
                "start_s": 0.0,
                "end_s": 3.0,
                "source_params": {"source": "opening_title"},
            }
        ],
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "phone_beat_receipt": {
            "version": 1,
            "matcher": "phrase",
            "placed": placed or [],
            "unplaced": unplaced or [],
            "closing": {"status": "none", "badge": "none"},
        },
    }


def _rendered(variant: dict[str, Any]):  # noqa: ANN202
    facts = plan_facts_from_phone_variant(variant)
    (receipt,) = build_receipts([R2], facts, include_unchecked=True)
    return receipt


def test_f3_a_placed_logo_is_met_on_the_finished_video() -> None:
    placed = {**TITLE_BEAT, "at_s": 0.0, "end_s": 3.0, "visual_label": "GitHub logo.png"}
    receipt = _rendered(_variant(placed=[placed]))
    assert receipt.status == "met", receipt
    assert "above your title" in (receipt.reason or "")


def test_f3_a_dropped_logo_is_never_met_and_says_why() -> None:
    miss = {"beat_id": "gh", "trigger": "GitHub logo", "reason": "no_room_above_title"}
    receipt = _rendered(_variant(unplaced=[{**miss, "anchor": "title"}]))
    assert receipt.status == "partial"
    assert "room above your title" in (receipt.reason or "")
    assert receipt.reason != EXACT_TEXT


def test_f4_no_logo_beat_names_the_picture_and_is_not_met() -> None:
    receipt = _draft_receipts(_strategy(), R1, R2)["r2"]
    assert receipt.status != "met"
    assert receipt.reason != EXACT_TEXT
    assert "GitHub logo" in (receipt.reason or "") and "above" in (receipt.reason or "")


def test_f5_a_picture_that_does_not_resolve_is_named() -> None:
    lost = {**TITLE_BEAT, "visual_id": "asset-not-in-this-project"}
    receipt = _draft_receipts(_strategy(lost), R1, R2)["r2"]
    assert receipt.status != "met"
    assert "couldn't find" in (receipt.reason or "").lower()


def test_f6_a_real_missing_title_text_keeps_the_text_verdict() -> None:
    words = BriefRequirement(id="r3", kind="text", scope="title", literal="My logo story")
    receipt = _draft_receipts(_strategy(TITLE_BEAT), words)["r3"]
    assert receipt.status == "partial"
    assert receipt.reason == EXACT_TEXT


def test_f7_with_the_title_look_off_nothing_is_promised(monkeypatch) -> None:
    monkeypatch.setattr(settings, "title_look_enabled", False)
    receipt = _draft_receipts(_strategy(TITLE_BEAT), R1, R2)["r2"]
    assert receipt.reason not in RENDER_PROMISE_REASONS
    assert receipt.reason != EXACT_TEXT
    assert receipt.status != "met"
