"""KRI-458: "start with the field and football, then beach volleyball, then the pub".

Prod 2026-10-06: the second request after a denied plan produced resolved ``order``
clip intents (first / then / last), but the unified planner never read them, so the
montage kept attachment order while the reply claimed the restructure. Synthetic clips:
a cafe shot, then pub, volleyball, football attached in that order.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_unified_montage,
    reply_from_receipts,
)
from app.pipeline.unified_montage import BriefView, plan_unified_montage
from tests.pipeline.test_unified_montage import clip

CAFE, PUB, VOLLEY_A, VOLLEY_B, FOOTBALL = "c0", "c1", "c2", "c3", "c4"


def _assign(*ids):
    return [{"media_id": i, "confidence": 0.9} for i in ids]


def _intent(name, position, *ids, status="resolved"):
    row = {
        "op": "order",
        "status": status,
        "intent_id": f"order_{name}",
        "attribute": name,
        "assignments": _assign(*ids),
    }
    if position:
        row["position"] = position
    return row


def _strategy(*intents):
    return {"resolved_clip_intents": list(intents)}


FULL = _strategy(
    _intent("the field and football", "first", FOOTBALL),
    _intent("beach volleyball", None, VOLLEY_A, VOLLEY_B),
    _intent("the pub", "last", PUB),
)


def _plan(strategy, *, enabled=True, view=None):
    clips = [clip(i) for i in range(5)]
    return plan_unified_montage(
        clips, view or BriefView(), strategy=strategy, clip_intents_enabled=enabled
    )


def test_described_sequence_reorders_attachment_order():
    plan = _plan(FULL)
    # football leads, volleyball follows, the unnamed cafe shot sits between, pub closes.
    assert plan.clip_ids == [FOOTBALL, VOLLEY_A, VOLLEY_B, CAFE, PUB]
    outcomes = [o for o in plan.record()["intent_outcomes"] if o["op"] == "order"]
    assert [o["status"] for o in outcomes] == ["met", "met", "met"]


def test_flag_off_keeps_attachment_order_byte_identical():
    plan = _plan(FULL, enabled=False)
    assert plan.clip_ids == [CAFE, PUB, VOLLEY_A, VOLLEY_B, FOOTBALL]
    assert "intent_outcomes" not in plan.record()


def test_unmatched_group_is_reported_not_claimed():
    strategy = _strategy(
        _intent("the field and football", "first"),  # nothing matched
        _intent("the pub", "last", PUB),
    )
    plan = _plan(strategy)
    assert plan.clip_ids == [CAFE, VOLLEY_A, VOLLEY_B, FOOTBALL, PUB]
    rows = {o["name"]: o for o in plan.record()["intent_outcomes"]}
    assert rows["first: the field and football"]["status"] == "not_possible"
    assert rows["last: the pub"]["status"] == "met"


def test_needs_creator_group_is_not_possible():
    strategy = _strategy(_intent("the beach", "first", status="needs_creator"))
    plan = _plan(strategy)
    assert plan.clip_ids == [CAFE, PUB, VOLLEY_A, VOLLEY_B, FOOTBALL]
    (row,) = plan.record()["intent_outcomes"]
    assert row["status"] == "not_possible"


def test_clip_named_twice_belongs_to_the_first_group():
    strategy = _strategy(
        _intent("football", "first", FOOTBALL),
        _intent("sport", "last", FOOTBALL, PUB),
    )
    plan = _plan(strategy)
    assert plan.clip_ids == [FOOTBALL, CAFE, VOLLEY_A, VOLLEY_B, PUB]


def _reply(plan, key="sequence"):
    req = BriefRequirement(
        id="r1",
        kind="order",
        scope="global",
        description="football, then volleyball, then pub",
        facts={"key": key},
    )
    brief = CreativeBrief(version=1, requirements=[req])
    record = plan.record()
    receipts = build_receipts(brief.live(), plan_facts_from_unified_montage(record))
    return receipts, reply_from_receipts(
        brief,
        receipts,
        summary="Restructured as you asked.",
        outcomes=record["intent_outcomes"] if "intent_outcomes" in record else (),
    )


def test_receipt_and_reply_say_done_when_the_sequence_landed():
    receipts, reply = _reply(_plan(FULL))
    assert [r.status for r in receipts] == ["met"]
    assert "Not everything" not in reply


def test_unapplied_description_is_never_silent_or_claimed():
    # No intents matched anything (or the flag is off): the order requirement must
    # produce a visible "partial", not a dropped neutral receipt.
    receipts, reply = _reply(_plan(_strategy(), enabled=False))
    assert [r.status for r in receipts] == ["partial"]
    assert "order you attached them" in reply
    assert reply.startswith("Not everything you asked for made it in")


def test_unmatched_sequence_makes_the_reply_a_failure_notice():
    strategy = _strategy(_intent("the field and football", "first"))
    receipts, reply = _reply(_plan(strategy))
    assert [r.status for r in receipts] == ["partial"]
    assert reply.startswith("Not everything you asked for made it in")
    assert "I found no clips of it" in reply
