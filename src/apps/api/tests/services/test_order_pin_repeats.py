"""KRI-764: a first/last pin the creator restates must never refuse the render.

Prod job 19f61092 (café M1, 2026-10-19): the draft held TWO "last" rows for one clip, the
creator's description ("the clip where we wave and say bye bye") and the file name they typed
later ("M1_03"). Both resolved to M1_03. The planner seated M1_03 last and the render
contract pinned exactly that, but the second row came back from the planner as
``code=contained, position=last``; the receipt counted it as "not where you asked" and the
KRI-503 branch refused the render: "Your clips are in filming order, but these aren't where
you asked: M1_03 (last)". The creator never asked for filming order: the brief carried
``key: capture_time`` only because every first/last ask is recorded that way.

Capture order of the 12 clips: 08,10,06,04,12,09,01,11,07,03,05,02 (M1_02 and M1_05 were
re-cut, so they are stamped AFTER M1_03). Upload order: 02,05,03,07,11,01,09,12,04,06,10,08.
Minutes below are the real gaps scaled to minutes; only the order matters.

Failure modes, written before the code (each is a test below):

* the same clip pinned last twice (description + file name) refuses the render (the incident)
* the same clip pinned first twice
* the same clip pinned first AND last (contradictory) refuses after the render instead of
  keeping it first and saying so, and says nothing before approval
* a pin that is not at its end by capture time (M1_03 vs the later M1_02) is treated as a
  conflict with a filming order nobody asked for
* weakening KRI-503: a pin the plan really failed to seat, or a pin with no clip, must still refuse
* a `then` group repeating a seated clip stays a skip (covered by test_order_sequence_contract)
* the new receipt fact does not travel with the montage order facts

Everything runs the REAL planner, contract builder, phone compiler, verifier and receipts.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    _MONTAGE_ORDER_FIELDS,
    PlanFacts,
    build_receipts,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
)
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline import unified_montage
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.clip_order_sequence import row_relations
from app.services.creator_render_contract import build_render_contract, verify_phone_recipe
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip

T0 = datetime(2026, 10, 16, 15, 22, tzinfo=UTC)
UPLOAD = ["02", "05", "03", "07", "11", "01", "09", "12", "04", "06", "10", "08"]
CAPTURE = ["08", "10", "06", "04", "12", "09", "01", "11", "07", "03", "05", "02"]
MINUTES = dict(zip(CAPTURE, [0, 4, 7, 10, 18, 21, 24, 27, 37, 39, 363, 367], strict=True))
SPEC = "ilk klip M1_08, son klip M1_03"


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _id(name: str) -> str:
    """A café clip's media id; an unknown name is already a media id (a deselected clip)."""
    return f"c{UPLOAD.index(name)}" if name in UPLOAD else name


def _clips():
    return [clip(i, minutes=MINUTES[name], duration=4.0) for i, name in enumerate(UPLOAD)]


def _snapshot() -> dict:
    rows = []
    for i, name in enumerate(UPLOAD):
        stamp = clip(i, minutes=MINUTES[name]).capture_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"media_id": f"c{i}", "kind": "video", "capture": {"capture_time": stamp}})
    return {"clip_assignments": rows}


def _brief() -> CreativeBrief:
    """What the Main Creator wrote for the café ask: a pin-only request carrying capture_time."""
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r4",
                kind="order",
                scope="global",
                description=SPEC,
                facts={"key": "capture_time", "first_clip": "M1_08", "last_clip": "M1_03"},
            )
        ],
    )


def _pin(position: str | None, words: str, *names: str) -> dict:
    row = {
        "op": "order",
        "status": "resolved",
        "intent_id": f"order_{position}_{words.replace(' ', '_')}",
        "attribute": words,
        "assignments": [{"media_id": _id(n), "confidence": 1.0} for n in names],
    }
    if position:
        row["position"] = position
    return row


def _strategy(*intents: dict) -> dict:
    return {
        "edit_format": "montage",
        "media_scope": "all",
        "selected_media_ids": [],
        "target_duration_s": 20,
        "resolved_clip_intents": list(intents),
    }


def _world(strategy: dict):
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-764"),
        _brief(),
        latest_message=SPEC,
        media_snapshot=_snapshot(),
    )
    brief = binding.resolve()
    contract = build_render_contract(
        strategy, generation_id="gen", brief=brief, media_snapshot=binding.media_snapshot
    )
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    (receipt,) = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), strict_order=True
    )
    return brief, contract, plan, receipt


def _verify(contract, plan) -> bool:
    clips = _clips()
    bindings = tuple(
        PhoneSourceBinding(
            media_id=c.media_id,
            proxy_path=c.proxy_path,
            generation=c.generation,
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=c.duration_s,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for c in clips
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(execution), bindings)
    return verify_phone_recipe(
        contract, recipe, source_audio={f"c{i}": True for i in range(len(UPLOAD))}
    )


def _ids(names) -> list[str]:
    return [_id(n) for n in names]


def _middle(*leading: str, closing: str | None = None) -> list[str]:
    """Filming order of everything not pinned, between the leading pins and the closing one."""
    pinned = {*leading, *([closing] if closing else [])}
    return [n for n in CAPTURE if n not in pinned]


# -- the incident ------------------------------------------------------------------


def test_incident_a_last_pin_restated_by_file_name_does_not_refuse_the_render() -> None:
    strategy = _strategy(
        _pin("first", "the clip Caro comes in with a black tray", "08"),
        _pin("last", 'the clip where we wave and say "bye bye"', "03"),
        _pin("first", "M1_08", "08"),
        _pin("last", "M1_03", "03"),  # the row prod refused: "M1_03 (last)"
    )
    _brief_, contract, plan, receipt = _world(strategy)
    expected = _ids(["08", *_middle("08", closing="03"), "03"])
    assert plan.clip_ids == expected
    # M1_03 is NOT last by capture time (M1_05 and M1_02 were stamped later): the pin still wins.
    assert CAPTURE.index("03") < CAPTURE.index("02")
    assert list(contract.order_ids) == expected
    assert _verify(contract, plan)
    assert (receipt.status, receipt.reason) == ("met", None)


def test_incident_exact_prod_shape_with_a_second_first_pin_on_another_clip() -> None:
    """Prod also resolved the typed file name "M1_08" to M1_12 (KRI-763, not fixed here): two
    different first groups both lead. That must not matter to the last pin's verdict."""
    strategy = _strategy(
        _pin("first", "the clip Caro comes in with a black tray", "08"),
        _pin("last", 'the clip where we wave and say "bye bye"', "03"),
        _pin("first", "M1_08", "12"),
        _pin("last", "M1_03", "03"),
    )
    _brief_, contract, plan, receipt = _world(strategy)
    assert plan.clip_ids[-1] == _id("03")
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)
    assert receipt.status == "met"


def test_a_first_pin_restated_twice_does_not_refuse_the_render() -> None:
    strategy = _strategy(
        _pin("first", "the tray clip", "08"),
        _pin("first", "M1_08", "08"),
        _pin("last", "M1_03", "03"),  # the brief states both ends, so both must be seated
    )
    _brief_, contract, plan, receipt = _world(strategy)
    assert plan.clip_ids[0] == _id("08") and plan.clip_ids[-1] == _id("03")
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)
    assert receipt.status == "met"


def test_a_pin_off_its_filming_slot_wins_over_a_filming_order_nobody_stated() -> None:
    """M1_03 filmed before M1_05 and M1_02 and still plays last; the rest keeps filming order."""
    strategy = _strategy(_pin("first", "M1_08", "08"), _pin("last", "M1_03", "03"))
    _brief_, contract, plan, receipt = _world(strategy)
    assert plan.clip_ids == _ids(["08", *_middle("08", closing="03"), "03"])
    assert plan.clip_ids.index(_id("02")) < plan.clip_ids.index(_id("03"))
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)
    assert receipt.status == "met"


# -- one clip asked to be first AND last --------------------------------------------


def test_one_clip_asked_to_be_first_and_last_keeps_the_first_claim_and_says_so() -> None:
    strategy = _strategy(_pin("first", "M1_08", "08"), _pin("last", "the tray clip", "08"))
    _brief_, contract, plan, receipt = _world(strategy)
    assert plan.clip_ids[0] == _id("08") and plan.clip_ids[-1] != _id("08")
    assert list(contract.order_ids) == plan.clip_ids
    assert _verify(contract, plan)  # the render is NOT refused
    assert receipt.status == "partial"
    assert "two places" in receipt.reason and "first place you named" in receipt.reason


def test_the_first_and_last_conflict_is_said_before_approval_not_only_after_the_render() -> None:
    strategy = _strategy(_pin("first", "M1_08", "08"), _pin("last", "the tray clip", "08"))
    facts = plan_facts_from_strategy(strategy, clip_ids=[_id(n) for n in UPLOAD])
    assert facts.sequence_conflicts == ("the tray clip (last)",)
    (receipt,) = build_receipts(_brief().live(), facts, include_unchecked=True, strict_order=True)
    assert receipt.status == "partial"
    assert "two places" in receipt.reason


def test_a_draft_without_a_conflict_says_what_it_always_said() -> None:
    strategy = _strategy(_pin("first", "M1_08", "08"), _pin("last", "M1_03", "03"))
    facts = plan_facts_from_strategy(strategy, clip_ids=[_id(n) for n in UPLOAD])
    assert facts.sequence_conflicts == ()
    (receipt,) = build_receipts(
        _brief().live()
        and CreativeBrief(version=1, requirements=[_brief().requirements[0]]).live(),
        facts,
        include_unchecked=True,
        strict_order=True,
    )
    assert receipt.reason == "I can't confirm the order this draft uses."


# -- KRI-503 stays strict where the plan really failed -----------------------------


def test_a_pin_the_plan_failed_to_seat_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = _strategy(_pin("last", "the bye bye clip", "03"), _pin("last", "M1_03", "03"))
    monkeypatch.setattr(unified_montage, "_apply_sequence", lambda ordered, rows: list(ordered))
    _brief_, _contract, _plan_, receipt = _world(strategy)
    assert receipt.status == "not_possible"
    assert "bye bye clip (last)" in receipt.reason
    assert "M1_03 (last)" not in receipt.reason  # the restatement is not a second failure


def test_a_pin_with_no_clip_in_the_edit_still_refuses() -> None:
    strategy = _strategy(_pin("last", "M1_03", "deselected-clip"))
    _brief_, _contract, _plan_, receipt = _world(strategy)
    assert receipt.status == "not_possible"
    assert "no clips" in receipt.reason


# -- the pure rule -------------------------------------------------------------------


def _row(position, *members, status="resolved"):
    return (position, "x", list(members), status)


def test_row_relations_classify_each_row_against_the_rows_before_it() -> None:
    rows = [
        _row("first", "a"),
        _row("last", "b"),
        _row("first", "a"),  # repeat: same clip, same seat
        _row("last", "b", "a"),  # a is first, b is last: a conflict
        _row("last", "c"),  # new
        _row(None, "a"),  # a "then" group inside an earlier group stays a plain containment
        _row("first", "x", status="needs_creator"),  # not resolved: no members, no relation
    ]
    assert row_relations(rows) == [
        "new",
        "new",
        "repeat",
        "conflict",
        "new",
        "contained",
        "new",
    ]


def test_row_relations_ignore_clips_outside_the_edit() -> None:
    rows = [_row("last", "gone"), _row("last", "gone")]
    assert row_relations(rows, present={"kept"}) == ["new", "new"]


def test_the_conflict_fact_travels_with_the_montage_order_facts() -> None:
    assert "sequence_conflicts" in _MONTAGE_ORDER_FIELDS
    assert {f.name for f in dataclasses.fields(PlanFacts)} >= {"sequence_conflicts"}
