"""Says-done group E: ending words taught with a `closing` fact are judged by their own time.

Failure modes, in order:

1. The reviewer's case. The first draft of #2131's Main Creator teaching filed "End with THANK
   YOU for the last 2 seconds" as `text`/`global` with `facts` {}. #2129's whole-video text receipt
   reads that as a line meant for the WHOLE video and says "on screen for 2 s of the 11 s video, not
   the whole video" about an ending that is exactly right. The teaching now files {"closing":
   "<words>"}.
2. The contract control. The same words WITHOUT the typed `closing` fact are still read as a
   whole-video line: the typed fact, not the wording, is what separates the two (KRI-667).
3. The preserved negative control. A genuine whole-video ask held only as a short title is still
   not done: `test_whole_video_line_shown_briefly_is_not_done` in
   `tests/kria/test_whole_video_text_receipt.py` covers the TEXT line and is not duplicated;
   `test_a_real_whole_video_line_still_needs_the_whole_video` here re-checks it with a line that
   carries no `closing` fact.

Real unified montage planner + `compile_execution_plan` on an 11 s clip; no model, DB or network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from tests.kria.test_whole_video_text_receipt import BASE, CLIP, _review, _shown_receipt

Q1_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "agent_evals"
    / "main_creator"
    / "qendresa_q1_caption_two_words_tight.json"
)


def _thank_you(facts: dict) -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="text",
        scope="global",
        literal="THANK YOU",
        description="End with THANK YOU for the last 2 seconds",
        facts=facts,
    )


def test_ending_filed_as_taught_is_judged_by_its_closing_time() -> None:
    req = _thank_you({"closing": "THANK YOU"})
    receipt, spans, total = _shown_receipt(
        {**BASE, "opening_title": "MY TRIP", "closing_title": "THANK YOU"}, req
    )
    assert spans, "the ending words are not on screen"
    assert max(end for _i, _s, end in spans) >= total - 0.05, (spans, total)
    assert min(start for _i, start, _e in spans) > 0, spans
    assert receipt.status == "met", (receipt.reason, spans, total)


def test_the_same_words_without_the_typed_ending_read_as_a_whole_video_line() -> None:
    req = _thank_you({})
    receipt, spans, total = _shown_receipt(
        {**BASE, "opening_title": "MY TRIP", "closing_title": "THANK YOU"}, req
    )
    assert receipt.status != "met", (receipt.reason, spans, total)
    assert "whole video" in receipt.reason.casefold(), receipt.reason


def _requirement(update: dict, index: int) -> BriefRequirement:
    return BriefRequirement(
        id=f"r{index}",
        kind=update["kind"],
        scope=update["scope"],
        literal=update.get("literal"),
        description=update["description"],
        facts=update["facts"],
    )


def _shown_receipt_for(strategy: dict, reqs: list[BriefRequirement], judged: BriefRequirement):
    """`_shown_receipt` for a brief of several requirements; the verdict is for ``judged``."""
    brief = CreativeBrief(version=1, requirements=reqs)
    plan = plan_unified_montage([CLIP], brief_view(brief), strategy=strategy)
    rows = compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]
    spans = [
        (r["id"], r["start_s"], r["end_s"]) for r in rows if judged.literal in r.get("text", "")
    ]
    variant = {"text_elements": rows, "duration_s": plan.duration_s}
    return _review(judged, plan.record(), variant), spans, plan.duration_s


def test_her_q1_title_and_ending_brief_is_judged_by_the_ending_time() -> None:
    fixture = json.loads(Q1_FIXTURE.read_text())
    updates = json.loads(fixture["raw_text"])["brief_updates"]
    title_literal = "lets build an agent that reads an APP before I trust it!!"
    title = next(u for u in updates if u.get("literal") == title_literal)
    ending = next(u for u in updates if u.get("literal") == "comment READ")
    reqs = [_requirement(title, 1), _requirement(ending, 2)]
    assert reqs[0].scope == "title" and reqs[1].facts == {"closing": "comment READ"}
    receipt, spans, total = _shown_receipt_for(
        {**BASE, "opening_title": title_literal, "closing_title": "comment READ"},
        reqs,
        reqs[1],
    )
    assert spans and all(start > 0 for _i, start, _e in spans), spans  # no whole-video pin
    assert max(end for _i, _s, end in spans) >= total - 0.05, (spans, total)
    reason = (receipt.reason or "").casefold()
    assert "whole video" not in reason, receipt.reason
    # This montage path cannot draw her arrow (item 14 draws it only on the phone Talking
    # ending), so the honest verdict may be partial, and then it is about the arrow.
    assert receipt.status == "met" or "arrow" in reason, (receipt.status, receipt.reason)


@pytest.mark.xfail(
    strict=True,
    reason="group B's _whole_video_title_pin ignores the typed closing fact when no title is "
    "asked; follow-up, not this PR",
)
def test_known_gap_ending_only_brief_is_pinned_whole_video() -> None:
    req = _thank_you({"closing": "THANK YOU"})
    _receipt, spans, _total = _shown_receipt({**BASE, "closing_title": "THANK YOU"}, req)
    assert spans and all(start > 0 for _i, start, _e in spans), spans


def test_a_real_whole_video_line_still_needs_the_whole_video() -> None:
    req = BriefRequirement(
        id="r1",
        kind="text",
        scope="global",
        literal="VISIT ASSOS",
        description="show VISIT ASSOS for the whole video",
        facts={},
    )
    receipt, spans, total = _shown_receipt({**BASE, "opening_title": "VISIT ASSOS"}, req)
    covered = sum(end - start for _i, start, end in spans)
    if covered >= total - 0.05:  # a render that already holds it for the video is also fine
        assert receipt.status == "met", receipt.reason
        return
    assert receipt.status != "met", (receipt.reason, spans, total)
