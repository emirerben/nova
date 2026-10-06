"""Phase-3 execution checks for a revised activity order.

The earlier rejected plan is represented here only by a changed strategy input;
this test does not claim that a database denial or a previous render occurred.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, build_receipts
from tests.pipeline.test_unified_montage_sequence import (
    FULL,
    _plan,
    _reply,
)


def test_revised_activity_order_constructs_shots_and_receipt_after_denied_alternative() -> None:
    # A creator's follow-up replaces the rejected alternative with resolved groups:
    # football first, volleyball next, and the pub last. The planner must build
    # that order, rather than merely echoing the requested sequence.
    plan = _plan(FULL)

    assert plan.clip_ids == ["c4", "c2", "c3", "c0", "c1"]
    receipts, reply = _reply(plan)
    assert [receipt.status for receipt in receipts] == ["met"]
    assert "Not everything" not in reply


def test_missing_lisbon_target_cannot_use_unrelated_text_as_clip_evidence() -> None:
    # A label elsewhere that says Pink Street does not prove the requested Lisbon
    # target clip received that label; the receipt remains unchecked and identifies
    # the exact missing target.
    requirement = BriefRequirement(
        id="lisbon-place",
        kind="text",
        scope="clip:lisbon-shot",
        literal="Pink Street",
    )
    [receipt] = build_receipts(
        [requirement],
        PlanFacts(editor=True, texts=("Pink Street",), has_clip_structure=False),
        include_unchecked=True,
    )

    assert receipt.status == "partial"
    assert receipt.verification == "unchecked"
    assert receipt.target_media_ids == ["lisbon-shot"]
