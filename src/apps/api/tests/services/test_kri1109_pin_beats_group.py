"""KRI-1109: a first / last pin takes its clip out of a general positionless group.

Leverkusen M3 run 1 (thread 83f2dcaa, 13:24 + 13:35, replayed byte for byte by
``tests/kria/test_m3_kri1109_repro.py``): "in the order I filmed them, ending on IMG_1043"
reached the sequence rule as a positionless group over every video, listed first, plus a
``last`` pin on IMG_1043. The rule gave every clip to the first row naming it (KRI-764), so
the group seated IMG_1043 at "then" and the pin read as "the same clip in two places",
holding approval although both asks agree. Listed the other way round, the pin won and
nothing was said. The approved 13:41 job (one positionless row per file, in filming order,
then the same ``last`` pin) came back ``code=conflict`` while its cut ended on IMG_1043.

Ways the fix can fail, written before the code (each is a test below). The café world of
``test_order_pin_repeats`` (12 dated clips, real planner, render contract, phone recipe
verifier and receipts) with a filming-order brief that names only an ending:

* P1 the incident: a filming order written as a group over every clip plus a ``last`` pin on
  the last clip filmed is a conflict before approval, or a partial receipt after the render.
* P2 "filming order, but end on M1_03" (not the last clip filmed): the explicit ending is
  ignored (M1_03 plays at its filming slot), or the planner seats it last while the render
  contract still pins filming order (KRI-503: refused after rendering), or the group is then
  judged "misplaced" because the clip the pin took plays after it.
* P3 the verdict, the cut or the contract depends on which of the two intents the planner
  happened to list first.
* P4 the 13:41 shape: one positionless row per file plus a ``last`` pin on the final file
  reports a conflict, or the emptied row is judged "misplaced" instead of a skipped
  containment.
* P5 weakening KRI-764: one clip asked to be first AND last must stay a conflict (the earlier
  pin keeps it), wherever a group listing that clip sits; a pin restated by file name after
  a group must stay a ``repeat``, never a second place.
* P6 a ``then`` row listed after a group that already seated its clips stays ``contained``.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
)
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.clip_order_sequence import apply_sequence, row_relations, sequence_rows
from app.services.creator_render_contract import build_render_contract
from tests.services.test_order_pin_repeats import (
    CAPTURE,
    UPLOAD,
    _clips,
    _id,
    _ids,
    _pin,
    _snapshot,
    _strategy,
    _verify,
)

ALL = [_id(n) for n in UPLOAD]


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _brief(ending: str) -> CreativeBrief:
    """What the Main Creator wrote for "in the order I filmed them, ending on <ending>"."""
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description=f"in the order I filmed them, ending on {ending}",
                facts={"key": "capture_time", "last_clip": ending},
            )
        ],
    )


def _group(words: str, *names: str) -> dict:
    """The planner's "chronological" as a positionless described group (no ``order_by``).
    Under a filming-order ask the server marks its inner order as that ask
    (``order_mode.confirm_unordered_groups``)."""
    return {**_pin(None, words, *names), "ordered_members": False}


def _world(ending: str, strategy: dict):
    brief = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-1109"),
        _brief(ending),
        latest_message=ending,
        media_snapshot=_snapshot(),
    ).resolve()
    contract = build_render_contract(
        strategy, generation_id="gen", brief=brief, media_snapshot=_snapshot()
    )
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    (receipt,) = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), strict_order=True
    )
    return contract, plan, receipt


def _codes(plan) -> list[str]:
    return [o["code"] for o in plan.record()["intent_outcomes"] if o["op"] == "order"]


def _relations(strategy: dict) -> list[str]:
    return row_relations(sequence_rows(strategy["resolved_clip_intents"]))


def _conflicts(strategy: dict) -> tuple[str, ...]:
    return plan_facts_from_strategy(strategy, clip_ids=ALL).sequence_conflicts


# -- P1: the incident -------------------------------------------------------------------


def test_a_filming_order_group_and_an_ending_on_the_last_clip_filmed_agree() -> None:
    last_filmed = CAPTURE[-1]
    strategy = _strategy(
        _group("the café visit from start to end", *UPLOAD),
        _pin("last", f"M1_{last_filmed}", last_filmed),
    )
    assert _relations(strategy) == ["new", "new"]
    assert _conflicts(strategy) == ()
    contract, plan, receipt = _world(f"M1_{last_filmed}", strategy)
    assert plan.clip_ids == _ids(CAPTURE)
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)
    assert _codes(plan) == ["landed", "landed"]
    assert (receipt.status, receipt.reason) == ("met", None)


# -- P2: an ending that is not the last clip filmed ---------------------------------------


def test_filming_order_but_end_on_another_clip_ends_on_it() -> None:
    strategy = _strategy(
        _group("the café visit from start to end", *UPLOAD),
        _pin("last", "M1_03", "03"),
    )
    assert CAPTURE[-1] != "03"
    assert _conflicts(strategy) == ()
    contract, plan, receipt = _world("M1_03", strategy)
    expected = _ids([*(n for n in CAPTURE if n != "03"), "03"])
    assert plan.clip_ids == expected
    assert list(contract.order_ids) == expected
    assert _verify(contract, plan)
    assert _codes(plan) == ["landed", "landed"]
    assert (receipt.status, receipt.reason) == ("met", None)


# -- P3: listed order decides nothing ----------------------------------------------------------


@pytest.mark.parametrize("ending", [CAPTURE[-1], "03"])
def test_which_intent_the_planner_listed_first_decides_nothing(ending: str) -> None:
    group = _group("the café visit from start to end", *UPLOAD)
    pin = _pin("last", f"M1_{ending}", ending)
    seen = []
    for strategy in (_strategy(group, pin), _strategy(pin, group)):
        contract, plan, receipt = _world(f"M1_{ending}", strategy)
        assert "conflict" not in _relations(strategy)
        assert _conflicts(strategy) == ()
        assert _verify(contract, plan)
        seen.append((plan.clip_ids, list(contract.order_ids), receipt.status))
    assert seen[0] == seen[1]
    assert seen[0][0][-1] == _id(ending)
    rows = sequence_rows(_strategy(group, pin)["resolved_clip_intents"])
    assert apply_sequence(ALL, rows) == apply_sequence(ALL, list(reversed(rows)))


# -- P4: the approved 13:41 job's shape ---------------------------------------------------------


def test_one_row_per_file_in_filming_order_ending_on_the_pinned_file_agrees() -> None:
    strategy = _strategy(
        *(_pin(None, f"M1_{name}", name) for name in CAPTURE),
        _pin("last", f"M1_{CAPTURE[-1]}", CAPTURE[-1]),
    )
    assert "conflict" not in _relations(strategy)
    assert _conflicts(strategy) == ()
    contract, plan, receipt = _world(f"M1_{CAPTURE[-1]}", strategy)
    assert plan.clip_ids == _ids(CAPTURE)
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)
    # The emptied row is a skipped containment; every other row and the pin landed.
    assert _codes(plan) == [*(["landed"] * (len(CAPTURE) - 1)), "contained", "landed"]
    assert (receipt.status, receipt.reason) == ("met", None)


# -- P5: KRI-764 stays ---------------------------------------------------------------------


@pytest.mark.parametrize("group_at", [0, 1, 2], ids=["group-first", "group-between", "group-last"])
def test_one_clip_asked_first_and_last_is_still_two_places_wherever_the_group_sits(
    group_at: int,
) -> None:
    pins = [_pin("first", "the tray clip", "08"), _pin("last", "M1_08", "08")]
    intents = [*pins]
    intents.insert(group_at, _group("the café visit from start to end", *UPLOAD))
    strategy = _strategy(*intents)
    relations = _relations(strategy)
    assert relations[intents.index(pins[1])] == "conflict"
    assert relations.count("conflict") == 1
    assert _conflicts(strategy) == ("M1_08 (last)",)
    contract, plan, receipt = _world("M1_08", strategy)
    # The earlier pin keeps the clip; planner and contract agree, the receipt says so.
    assert plan.clip_ids[0] == _id("08")
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)
    assert receipt.status == "partial" and "two places" in receipt.reason


def test_an_ending_restated_by_file_name_after_a_group_is_a_repeat() -> None:
    strategy = _strategy(
        _group("the café visit from start to end", *UPLOAD),
        _pin("last", 'the clip where we wave and say "bye bye"', "03"),
        _pin("last", "M1_03", "03"),
    )
    assert _relations(strategy) == ["new", "new", "repeat"]
    assert _conflicts(strategy) == ()
    contract, plan, receipt = _world("M1_03", strategy)
    assert plan.clip_ids[-1] == _id("03")
    assert list(contract.order_ids) == plan.clip_ids
    assert _codes(plan) == ["landed", "landed", "repeat"]
    assert receipt.status == "met"


# -- P6: then-after-then is unchanged ------------------------------------------------------------


def test_a_then_row_inside_an_earlier_group_stays_contained() -> None:
    rows = [
        (None, "the visit", ["a", "b", "c"], "resolved"),
        ("last", "the bye", ["c"], "resolved"),
        (None, "the tray", ["a"], "resolved"),
    ]
    assert row_relations(rows) == ["new", "new", "contained"]
    assert apply_sequence(["c", "b", "a"], rows) == ["b", "a", "c"]
