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
from app.pipeline import unified_montage
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


# -- KRI-941: judge every row against the finished cut ----------------------------------
# Prod 2026-10-25 (job b3820ddc): "start on the sign, end on my friend, in between, in this
# order: the door, the butcher, the grill, ...". Every clip was named by some row, so the
# first / last rows had no stray clip to be judged against (vacuously met), and a row with no
# position was always met whatever came before it.


def _codes(strategy, *order):
    """The sequence rows' ``code`` for a cut already in ``order`` (no seating rule in between)."""
    by_ref = {f"c{i}": clip(i) for i in range(5)}
    rows = unified_montage._sequence_intents(strategy, True)
    return [o["code"] for o in unified_montage._sequence_outcomes(rows, [by_ref[r] for r in order])]


def test_a_then_clip_seated_before_the_first_clip_is_misplaced():
    strategy = _strategy(
        _intent("football", "first", FOOTBALL),
        _intent("volleyball", None, VOLLEY_A, VOLLEY_B),
    )
    assert _codes(strategy, FOOTBALL, VOLLEY_A, VOLLEY_B, CAFE, PUB) == ["landed", "landed"]
    # A volleyball clip plays before the football clip the creator said to start on.
    assert _codes(strategy, VOLLEY_A, FOOTBALL, VOLLEY_B, CAFE, PUB) == ["misplaced", "misplaced"]


def test_a_then_clip_seated_after_the_last_clip_is_misplaced():
    strategy = _strategy(
        _intent("pub", "last", PUB),
        _intent("volleyball", None, VOLLEY_A, VOLLEY_B),
    )
    assert _codes(strategy, CAFE, VOLLEY_A, VOLLEY_B, FOOTBALL, PUB) == ["landed", "landed"]
    assert _codes(strategy, CAFE, VOLLEY_A, FOOTBALL, PUB, VOLLEY_B) == ["misplaced", "misplaced"]


def test_listed_then_rows_seated_reversed_are_misplaced():
    # "in this order: the pub, then volleyball": two rows, listed order = row order.
    strategy = _strategy(_intent("the pub", None, PUB), _intent("volleyball", None, VOLLEY_A))
    assert _codes(strategy, CAFE, PUB, VOLLEY_A, VOLLEY_B, FOOTBALL) == ["landed", "landed"]
    # Row 2 (volleyball) plays before row 1 (the pub): the later row is the one out of place.
    assert _codes(strategy, CAFE, VOLLEY_A, PUB, VOLLEY_B, FOOTBALL) == ["landed", "misplaced"]


def test_a_first_clip_seated_second_is_misplaced_when_every_clip_is_named():
    strategy = _strategy(
        _intent("football", "first", FOOTBALL),
        _intent("the rest", None, VOLLEY_A, VOLLEY_B, CAFE, PUB),
    )
    # Every clip is named, so nothing is "unnamed" to judge the first clip against: it must
    # still lead the whole cut.
    assert _codes(strategy, FOOTBALL, CAFE, PUB, VOLLEY_A, VOLLEY_B) == ["landed", "landed"]
    assert _codes(strategy, CAFE, FOOTBALL, PUB, VOLLEY_A, VOLLEY_B) == ["misplaced", "misplaced"]


def test_a_last_clip_seated_second_to_last_is_misplaced_when_every_clip_is_named():
    strategy = _strategy(
        _intent("pub", "last", PUB),
        _intent("the rest", None, VOLLEY_A, VOLLEY_B, CAFE, FOOTBALL),
    )
    assert _codes(strategy, CAFE, FOOTBALL, VOLLEY_A, VOLLEY_B, PUB) == ["landed", "landed"]
    assert _codes(strategy, CAFE, FOOTBALL, VOLLEY_A, PUB, VOLLEY_B) == ["misplaced", "misplaced"]


def test_a_multi_clip_first_group_is_met_as_a_block_and_misplaced_when_split():
    strategy = _strategy(
        _intent("volleyball", "first", VOLLEY_A, VOLLEY_B),
        _intent("the rest", None, CAFE, PUB, FOOTBALL),
    )
    # Inside the group any order is fine: a group has no listed order of its own.
    assert _codes(strategy, VOLLEY_B, VOLLEY_A, CAFE, PUB, FOOTBALL) == ["landed", "landed"]
    assert _codes(strategy, VOLLEY_A, CAFE, VOLLEY_B, PUB, FOOTBALL) == ["misplaced", "misplaced"]


def test_unnamed_clips_between_groups_do_not_fail_the_order():
    strategy = _strategy(
        _intent("football", "first", FOOTBALL),
        _intent("volleyball", None, VOLLEY_A, VOLLEY_B),
        _intent("pub", "last", PUB),
    )
    # The cafe shot is named by nobody: it may sit anywhere between the first and last clips.
    for order in (
        (FOOTBALL, CAFE, VOLLEY_A, VOLLEY_B, PUB),
        (FOOTBALL, VOLLEY_A, CAFE, VOLLEY_B, PUB),
        (FOOTBALL, VOLLEY_A, VOLLEY_B, CAFE, PUB),
    ):
        assert _codes(strategy, *order) == ["landed", "landed", "landed"], order
    # ...but not before the first clip or after the last one.
    assert _codes(strategy, CAFE, FOOTBALL, VOLLEY_A, VOLLEY_B, PUB) == [
        "misplaced",
        "landed",
        "landed",
    ]
    assert _codes(strategy, FOOTBALL, VOLLEY_A, VOLLEY_B, PUB, CAFE) == [
        "landed",
        "landed",
        "misplaced",
    ]


def test_two_first_rows_and_two_last_rows_are_met_in_their_listed_order():
    strategy = _strategy(
        _intent("football", "first", FOOTBALL),
        _intent("the cafe", "first", CAFE),
        _intent("volleyball", None, VOLLEY_A, VOLLEY_B),
        _intent("the pub", "last", PUB),
    )
    assert _codes(strategy, FOOTBALL, CAFE, VOLLEY_A, VOLLEY_B, PUB) == ["landed"] * 4
    # Swapped: the first row's clip is behind the second row's, which is the one out of place.
    assert _codes(strategy, CAFE, FOOTBALL, VOLLEY_A, VOLLEY_B, PUB) == [
        "misplaced",
        "landed",
        "landed",
        "landed",
    ]
    ends = _strategy(
        _intent("volleyball", None, VOLLEY_A, VOLLEY_B, FOOTBALL),
        _intent("the cafe", "last", CAFE),
        _intent("the pub", "last", PUB),
    )
    assert _codes(ends, VOLLEY_A, VOLLEY_B, FOOTBALL, CAFE, PUB) == ["landed"] * 3
    assert _codes(ends, VOLLEY_A, VOLLEY_B, FOOTBALL, PUB, CAFE) == [
        "landed",
        "landed",
        "misplaced",
    ]


def test_a_then_row_naming_a_first_clip_again_is_judged_on_its_own_new_clips():
    strategy = _strategy(
        _intent("football", "first", FOOTBALL),
        _intent("football then volleyball", None, FOOTBALL, VOLLEY_A),
    )
    assert _codes(strategy, FOOTBALL, VOLLEY_A, CAFE, PUB, VOLLEY_B) == ["landed", "landed"]
    # The football clip belongs to the first row; the then row owns only the volleyball clip.
    assert _codes(strategy, VOLLEY_A, FOOTBALL, CAFE, PUB, VOLLEY_B) == ["misplaced", "misplaced"]
