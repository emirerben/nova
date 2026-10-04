"""KRI-282: the montage follows the creator's answer to chronological-vs-grouped.

Synthetic mixed-sport clips filmed interleaved: football, dodgeball, football,
dodgeball, a pub shot, football, dodgeball. ``group_first`` orders blocks by first
appearance with clips chronological inside; ``chronological`` is byte-identical to
a plan with no choice.
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

# capture minutes: c0..c6 filmed in order
FOOTBALL = ("c0", "c2", "c5")
DODGEBALL = ("c1", "c3", "c6")
PUB = "c4"


def _assign(*ids):
    return [{"media_id": i, "confidence": 0.9} for i in ids]


def _strategy(choice=None) -> dict:
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "group",
                "status": "resolved",
                "intent_id": "g1",
                "attribute": "football",
                "assignments": _assign(*FOOTBALL),
            },
            {
                "op": "group",
                "status": "resolved",
                "intent_id": "g2",
                "attribute": "dodgeball",
                "assignments": _assign(*DODGEBALL),
            },
        ]
    }
    if choice:
        strategy["ordering_choice"] = choice
    return strategy


def _plan(choice=None, *, intents_on=True, strategy=None, attach_reversed=False):
    # Attachment order deliberately scrambled so only capture time restores c0..c6.
    indexes = [3, 0, 6, 1, 5, 2, 4] if attach_reversed else list(range(7))
    clips = [clip(i, minutes=i * 10) for i in indexes]
    return plan_unified_montage(
        clips,
        BriefView(order_by_capture=True, wants_order=True),
        strategy=strategy or _strategy(choice),
        clip_intents_enabled=intents_on,
    )


def test_group_first_blocks_by_first_appearance_chronological_inside():
    plan = _plan("group_first")
    # Slots held by grouped clips: 0,1,2,3,5,6. The pub shot keeps slot 4.
    assert plan.clip_ids == ["c0", "c2", "c5", "c1", "c4", "c3", "c6"]
    assert plan.clip_ids.index(PUB) == 4  # ungrouped clip keeps its chronological slot
    football = [plan.clip_ids.index(c) for c in FOOTBALL]
    dodge = [plan.clip_ids.index(c) for c in DODGEBALL]
    assert football == sorted(football) and dodge == sorted(dodge)  # chronological inside
    assert max(football) < min(dodge)  # football appeared first -> its block is first
    assert plan.record()["ordering_choice"] == "group_first"


def test_group_first_ignores_attachment_order():
    assert _plan("group_first", attach_reversed=True).clip_ids == _plan("group_first").clip_ids


def test_chronological_is_byte_identical_to_no_choice():
    none, chrono = _plan(None), _plan("chronological")
    assert chrono.clip_ids == none.clip_ids == [f"c{i}" for i in range(7)]
    assert chrono.snapshot.model_dump(mode="json") == none.snapshot.model_dump(mode="json")


def test_flag_off_ignores_the_choice_entirely():
    off = _plan("group_first", intents_on=False)
    assert off.clip_ids == [f"c{i}" for i in range(7)]
    assert "ordering_choice" not in off.record()
    assert off.record() == _plan(None, intents_on=False).record()


def test_receipt_truthfully_reports_each_choice():
    grouped = {r["name"]: r for r in _plan("group_first").record()["intent_outcomes"]}
    row = grouped["group by football, dodgeball"]
    assert row["status"] == "met" and "you chose grouping first" in row["reason"]

    chrono = {r["name"]: r for r in _plan("chronological").record()["intent_outcomes"]}
    row = chrono["group by football, dodgeball"]
    assert row["status"] == "chosen"
    assert "you chose strictly chronological" in row["reason"]
    assert "football is in 3 stretches" in row["reason"]

    # Without a choice nothing changes: the old "partly" wording.
    old = {r["name"]: r for r in _plan(None).record()["intent_outcomes"]}
    assert old["group by football, dodgeball"]["status"] == "partial"


def test_reply_says_as_you_chose_and_is_not_a_failure_notice():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="o1", kind="order", scope="global", facts={"key": "chronological"})
        ],
    )
    for choice, order_reason in (
        ("chronological", None),
        ("group_first", "filming order inside each group"),
    ):
        record = _plan(choice).record()
        receipts = build_receipts(brief.live(), plan_facts_from_unified_montage(record))
        assert receipts and receipts[0].status == "met"
        reply = reply_from_receipts(
            brief, receipts, summary="ok", outcomes=record["intent_outcomes"]
        )
        assert "Not everything you asked for" not in reply
        if choice == "chronological":
            assert "As you chose: group by football, dodgeball" in reply
        else:
            assert "Done: group by football, dodgeball (you chose grouping first" in reply
        if order_reason:
            assert order_reason in reply


def test_multi_group_clip_counts_as_ungrouped_and_keeps_its_slot():
    strategy = _strategy("group_first")
    strategy["resolved_clip_intents"][1]["assignments"] = _assign("c1", "c3", "c5", "c6")
    plan = _plan(strategy=strategy)  # c5 is in both groups
    assert plan.clip_ids.index("c5") == 5
