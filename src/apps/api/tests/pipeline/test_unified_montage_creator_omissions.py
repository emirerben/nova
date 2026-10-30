from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
    reply_from_receipts,
)
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import plan_unified_montage
from app.schemas.clip_intents import ClipIntent
from tests.pipeline.test_unified_montage import clip


def strategy(op="order"):
    intent = ClipIntent(intent_id="cabildo_interior", op=op, attribute="Cabildo interior")
    return {
        "story_structure": ["Palace exterior"],
        "selection_reconciliation": {
            "version": 1,
            "status": "unresolved",
            "unresolved_story_rows": ["Cabildo interior", "Palace exterior"],
            "rejected_selections": [
                {
                    "intent": intent.model_dump(mode="json"),
                    "question_id": "checked-question",
                    "category_key": "order:cabildo-interior",
                    "selection_sequence": 1,
                }
            ],
        },
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_creator_none_compiles_same_cut_and_reports_partial_without_stale_assurance(enabled):
    data = strategy()
    original = deepcopy(data)
    clips = [clip(0), clip(1)]
    baseline = plan_unified_montage(clips, clip_intents_enabled=enabled)
    plan = plan_unified_montage(clips, strategy=data, clip_intents_enabled=enabled)
    assert data == original
    assert plan.snapshot == baseline.snapshot
    assert plan.clip_ids == baseline.clip_ids
    assert compile_execution_plan(plan.guided_edit(), track=None) == compile_execution_plan(
        baseline.guided_edit(), track=None
    )
    outcomes = plan.record()["intent_outcomes"]
    assert outcomes == [
        {
            "intent_id": "cabildo_interior",
            "op": "order",
            "attribute": "Cabildo interior",
            "name": "Cabildo interior",
            "status": "unresolved",
            "code": "skipped",
            "reason": "creator selected None",
        }
    ]
    facts = plan_facts_from_unified_montage(plan.record())
    assert facts.sequence_unlocated == ("skipped",)
    req = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="Show all five scenes",
        facts={"mode": "any_order"},
    )
    receipts = build_receipts([req], facts, include_unchecked=True)
    assert receipts[0].status == "partial"
    reply = reply_from_receipts(
        CreativeBrief(requirements=[req]), receipts, summary="All five scenes are included."
    )
    assert "All five scenes are included." not in reply


@pytest.mark.parametrize("data", [{}, {"selection_reconciliation": None}])
def test_legacy_metadata_absence_keeps_record_identical(data):
    assert (
        plan_unified_montage([clip(0)], strategy=data).record()
        == plan_unified_montage([clip(0)]).record()
    )


def test_non_order_rejection_does_not_create_order_outcome():
    plan = plan_unified_montage([clip(0)], strategy=strategy("include"))
    assert "intent_outcomes" not in plan.record()


def test_repeated_rejection_emits_one_omission():
    data = strategy()
    data["selection_reconciliation"]["rejected_selections"] *= 2
    assert len(plan_unified_montage([clip(0)], strategy=data).record()["intent_outcomes"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"status": "met"},
        {"rejected_selections": [{"intent": {}}]},
    ],
)
def test_malformed_metadata_fails_closed(change):
    data = strategy()
    data["selection_reconciliation"].update(change)
    with pytest.raises(ValidationError):
        plan_unified_montage([clip(0)], strategy=data)


@pytest.mark.parametrize(
    "change", [{"question_id": ""}, {"category_key": ""}, {"selection_sequence": 0}]
)
def test_missing_provenance_fails_closed(change):
    data = strategy()
    data["selection_reconciliation"]["rejected_selections"][0].update(change)
    with pytest.raises(ValueError, match="provenance"):
        plan_unified_montage([clip(0)], strategy=data)


def test_rejected_intent_cannot_also_execute():
    data = strategy()
    data["resolved_clip_intents"] = [{"intent_id": "cabildo_interior", "op": "order"}]
    with pytest.raises(ValueError, match="remains executable"):
        plan_unified_montage([clip(0)], strategy=data)


@pytest.mark.parametrize(
    "data,expected", [(strategy(), "partial"), ({}, "met"), (strategy("include"), "met")]
)
def test_draft_receipt_reads_same_authoritative_omissions_as_worker(data, expected):
    facts = plan_facts_from_strategy(data)
    assert facts.sequence_unlocated == (("skipped",) if expected == "partial" else ())
    req = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="Show all five scenes",
        facts={"mode": "any_order"},
    )
    receipts = build_receipts([req], facts, include_unchecked=True)
    assert receipts[0].status == expected
    summary = "All five scenes are included."
    reply = reply_from_receipts(CreativeBrief(requirements=[req]), receipts, summary=summary)
    if expected == "partial":
        assert summary not in reply
    else:
        assert summary in reply


def test_draft_receipt_rejects_malformed_omission_metadata():
    data = strategy()
    data["selection_reconciliation"]["version"] = 2
    with pytest.raises(ValidationError):
        plan_facts_from_strategy(data)
