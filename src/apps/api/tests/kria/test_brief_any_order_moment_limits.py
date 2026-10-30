"""Any ordering does not prove that recorded unresolved named moments appeared."""

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_unified_montage,
    reply_from_receipts,
)


def receipt(outcomes, description="Show the interior and palace in any order"):
    requirement = BriefRequirement(
        id="sequence",
        kind="order",
        scope="global",
        description=description,
        facts={"mode": "any_order"},
    )
    facts = plan_facts_from_unified_montage(
        {
            "clip_ids": ["palace"],
            "ordering_basis": "creator_order",
            "intent_outcomes": outcomes,
        }
    )
    receipts = build_receipts([requirement], facts, include_unchecked=True)
    return requirement, facts, receipts


@pytest.mark.parametrize("code", ["not_located", "skipped", "delegated"])
def test_recorded_named_moment_limit_cannot_receive_done_receipt(code):
    requirement, facts, receipts = receipt(
        [
            {"op": "order", "attribute": "interior", "status": "unresolved", "code": code},
            {"op": "order", "attribute": "palace", "status": "met"},
        ]
    )
    assert facts.sequence_unlocated == (code,)
    assert receipts[0].status == "partial"
    assert receipts[0].verification == "checked"
    assert receipts[0].reason
    reply = reply_from_receipts(
        CreativeBrief(requirements=[requirement]),
        receipts,
        summary="All five scenes appeared exactly as requested.",
    )
    assert "All five scenes appeared exactly as requested." not in reply


@pytest.mark.parametrize(
    "outcomes",
    [
        [],
        [{"op": "order", "attribute": "palace", "status": "met"}],
        [{"op": "caption", "attribute": "interior", "status": "unresolved", "code": "skipped"}],
        [{"op": "include", "attribute": "interior", "status": "unresolved", "code": "not_located"}],
    ],
)
def test_any_order_without_recorded_sequence_limit_keeps_existing_receipt(outcomes):
    _, facts, receipts = receipt(outcomes, description="Use any order")
    assert facts.sequence_unlocated == ()
    assert receipts[0].status == "met"


def test_description_alone_does_not_invent_missing_moment_evidence():
    _, facts, receipts = receipt([])
    assert facts.sequence_unlocated == ()
    assert receipts[0].status == "met"
