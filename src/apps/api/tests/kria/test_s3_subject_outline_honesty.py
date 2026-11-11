"""KRI-1327 (Çiğdem S3, thread cc0e3706): "put an orange line around me" in the car take.

No edit draws a line around a person: the phone's person matte only cuts the speaker out for a
background. The brief stored the ask with no facts, so no checker claimed it: every hold said "I
can't verify this one automatically yet", the draft put it under "Have a look at these in the
video", and the render had no line. The creator learns only after the render, if at all.

Ways this could go wrong, written down before the code (each has a case):

1. The outline ask still reaches the approval card as "have a look" instead of "Couldn't".
2. It gets a receipt that is not a format limit, so the first draft turns into the "should I
   make a simpler version?" question about a plan that has nothing simpler.
3. Something that is not an outline is read as one: an ask without the model's
   `subject_outline` fact (KRI-667: words are never re-read on the server), or a non-true value.
4. The Turkish reply carries the English reason, or the Turkish reason stops counting.
5. A finished video says "have a look"; an editor turn is rerouted.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_strategy,
    reply_from_receipts,
    wants_subject_outline,
)
from app.kria.reply_language import reply_language_for

S3_ASK = "In the last part in the car, put an orange line around me"
CAR_CLIP = "clip:analysis-proxy-ios-9020AEC9-79FA-42C2-AA33-274C0AE93899.mp4"
R7 = BriefRequirement(
    id="r7", kind="style", scope=CAR_CLIP, description=S3_ASK, facts={"subject_outline": True}
)
TALKING_STRATEGY = {"edit_format": "subtitled", "caption_style": "clean"}


def _style(description: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="style", scope="global", description=description, facts=facts
    )


def test_s3_outline_ask_is_a_couldnt_line_before_the_render() -> None:
    brief = CreativeBrief(version=5, requirements=[R7])
    receipts = build_receipts(
        brief.live(), plan_facts_from_strategy(TALKING_STRATEGY), include_unchecked=True
    )

    outline = receipts[0]
    assert (outline.status, outline.verification) == ("not_possible", "checked")
    assert is_format_limit(outline.reason)
    assert not any(needs_creator_choice(r) for r in receipts)  # the draft goes ahead

    reply = reply_from_receipts(brief, receipts, summary="Your three takes, cleaned up.")
    assert f"Couldn't: {S3_ASK}" in reply
    assert "can't draw a line around someone" in reply
    assert "Have a look" not in reply


def test_a_finished_render_says_the_same_and_an_editor_turn_keeps_its_own_path() -> None:
    rendered = PlanFacts(editor=True, rendered_variant=True, edit_format="subtitled")
    assert check_requirement(R7, rendered).status == "not_possible"

    editor_turn = PlanFacts(editor=True, edit_format="subtitled")
    assert check_requirement(R7, editor_turn).status != "not_possible"


@pytest.mark.parametrize(
    "req",
    [
        _style(S3_ASK),  # no fact: the server never re-reads the words (KRI-667)
        _style(S3_ASK, subject_outline="orange"),
        _style(S3_ASK, subject_outline=False),
        _style("captions with an orange outline", caption_color="#FFA500"),
    ],
)
def test_without_the_fact_nothing_is_read_as_an_outline(req: BriefRequirement) -> None:
    assert not wants_subject_outline(req)


def test_turkish_reply_carries_the_turkish_reason() -> None:
    req = _style("arabadaki kısımda etrafıma turuncu bir çizgi çiz", subject_outline=True)
    with reply_language_for("tr"):
        receipt = check_requirement(req, plan_facts_from_strategy(TALKING_STRATEGY))
        reply = reply_from_receipts(
            CreativeBrief(version=1, requirements=[req]), [receipt], summary="Bir taslak."
        )
    assert receipt.status == "not_possible"
    assert receipt.reason is not None and "çizgi" in receipt.reason
    assert is_format_limit(receipt.reason)
    assert "Yapamadım: arabadaki kısımda etrafıma turuncu bir çizgi çiz" in reply


def test_the_draft_goes_ahead_without_the_outline() -> None:
    from app.tasks.kria_runtime import _automatic_omission_reason

    reason = _automatic_omission_reason(R7, TALKING_STRATEGY)
    assert reason is not None and "line around" in reason
