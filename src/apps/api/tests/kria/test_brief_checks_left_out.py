"""KRI-1304: the finished-video reply says what the planner left out of the recordings.

Q2 run 1: "show the right one when I talk about each repo; show the trending page when I talk about
what's hot". Impeccable had no recording, so Spec Kit stayed on through its talk, and the trending
page was never used because she never talks about it. Both reasons lived only in the planner's
private summary. Ways this can fail, each pinned below:

 1. a repo with no recording is not named, or is called "met" (the previous picture stays on);
 2. a recording never used because she never talks about it is not told, or is called a failure;
 3. a render with nothing left out changes its reply (byte-identical to before);
 4. junk from the model (bad reason, non-list) breaks the schema or the receipt;
 5. the planner's list does not survive the schema's dump (it would never reach the worker).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents._schemas.reaction_beats import BackgroundMedia
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant

ASK = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="Show the right recording when I talk about each repo",
    facts={"speaker_background": True, "background_changes": True},
)


def _variant(left_out: Any = None) -> dict[str, Any]:
    background: dict[str, Any] = {"windows": 4, "media": 4}
    if left_out is not None:
        background["left_out"] = left_out
    return {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "caption_cues": [{"text": "Four repos.", "start_s": 0.0, "end_s": 1.6}],
        "text_elements": [],
        "phone_lane_receipt": {"applied": ["background"], "dropped": [], "background": background},
    }


def _receipt(left_out: Any = None):
    facts = plan_facts_from_phone_variant(_variant(left_out))
    (receipt,) = build_receipts([ASK], facts, include_unchecked=True)
    return receipt


def test_a_repo_with_no_recording_is_named_and_the_ask_is_partial():
    receipt = _receipt([{"name": "Impeccable", "why": "no_recording"}])
    assert receipt.status == "partial"
    assert "No recording for “Impeccable”" in receipt.reason
    assert "before stays on" in receipt.reason


def test_a_recording_she_never_talks_about_is_told_but_not_a_failure():
    receipt = _receipt([{"name": "the trending page", "why": "not_said"}])
    assert receipt.status == "met"
    assert "you don't talk about “the trending page”" in receipt.reason


def test_both_reasons_are_told_together():
    receipt = _receipt(
        [
            {"name": "Impeccable", "why": "no_recording"},
            {"name": "the trending page", "why": "not_said"},
        ]
    )
    assert receipt.status == "partial"
    assert "Impeccable" in receipt.reason and "trending page" in receipt.reason


def test_nothing_left_out_keeps_the_old_reply():
    assert _receipt().reason == _receipt([]).reason
    assert "No recording" not in _receipt().reason
    assert _receipt().status == "met"


@pytest.mark.parametrize("junk", ["text", 5, [None], [{"name": "x"}], [{"why": "no_recording"}]])
def test_junk_left_out_never_breaks_the_receipt(junk):
    receipt = _receipt(junk)
    assert receipt.status == "met"
    assert "No recording" not in receipt.reason


def test_the_schema_keeps_valid_rows_and_drops_junk():
    bg = BackgroundMedia(
        media_id="asset-1",
        left_out=[
            {"name": " Impeccable ", "why": "no_recording"},
            {"name": "x", "why": "bogus"},
            3,
        ],
    )
    assert bg.model_dump()["left_out"] == ({"name": "Impeccable", "why": "no_recording"},)
    assert "left_out" not in BackgroundMedia(media_id="asset-1").model_dump()
