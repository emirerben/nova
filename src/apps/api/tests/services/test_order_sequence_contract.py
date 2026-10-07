"""KRI-503: "chronological order, starting with the teal video" must not be a dead end.

The unified montage planner seats the clip the creator described first (a resolved ``order``
intent) on top of the brief's filming-time order. The render contract used to pin PURE filming
order for the same brief, so the plan that did what the creator said was refused after the
render ("couldn't keep the confirmed clip order") while the brief receipt, which only compared
the order's basis, read "met".

Failure modes, written before the code (each is a test below):

* the plan follows the creator's words but the contract pins pure filming order (the incident)
* the contract pins the sequence but the plan ignores it: verifier AND receipt must refuse
* the receipt reads "met" without checking that the stated clip really leads
* an unresolved start clip is guessed instead of reported
* a plain "chronological order" brief is changed (byte-identical contract required)
* legacy / unbound jobs get stricter verdicts than they had
* the cloud order gate rejects the new ``order_ids`` shape for a plan that follows it
* "ending with" and "starting with ... ending with" are mis-seated

Everything below runs the REAL planner, guided compiler, phone compiler, contract builder,
verifier and receipts; nothing is mocked except the one mutation tests that disable a rule.
"""

from __future__ import annotations

import dataclasses
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline import unified_montage
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.cloud_render_contract import (
    CloudRenderContractError,
    check_guided_plan_order,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip

T0 = datetime(2026, 9, 20, 7, 0, tzinfo=UTC)
# Attachment order != filming order, and the described clip is neither first nor last in
# either: capture minute per clip index.
MINUTES = [40, 10, 90, 20, 70, 0, 60, 30, 80, 50, 100]
FILMING = sorted(range(len(MINUTES)), key=lambda i: MINUTES[i])  # clip indices, oldest first
TEAL = 6  # filmed 7th, attached 7th
GREEN = 3
SPEC = "chronological order, starting with the teal video"


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _id(index: int) -> str:
    return f"c{index}"


def _clips():
    return [clip(i, minutes=MINUTES[i], duration=4.0) for i in range(len(MINUTES))]


def _snapshot() -> dict:
    rows = []
    for i, minutes in enumerate(MINUTES):
        stamp = (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"media_id": _id(i), "kind": "video", "capture": {"capture_time": stamp}})
    return {"clip_assignments": rows}


def _brief(description: str = SPEC, key: str = "capture_time") -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description=description,
                facts={"key": key},
            )
        ],
    )


def _intent(position: str | None, name: str, *indices: int, status: str = "resolved") -> dict:
    row = {
        "op": "order",
        "status": status,
        "intent_id": f"order_{name.replace(' ', '_')}",
        "attribute": name,
        "assignments": [
            {"media_id": _id(i) if isinstance(i, int) else i, "confidence": 0.9} for i in indices
        ],
    }
    if position:
        row["position"] = position
    return row


# The basis intent the resolver also emits for "chronological order": never a sequence rule.
_BASIS = {
    "op": "order",
    "status": "resolved",
    "intent_id": "order_chronological",
    "attribute": "everything",
    "order_by": "capture_time",
    "assignments": [{"media_id": _id(i), "confidence": 1.0} for i in range(len(MINUTES))],
}


def _strategy(*intents: dict) -> dict:
    return {
        "edit_format": "montage",
        "media_scope": "all",
        "selected_media_ids": [],
        "target_duration_s": 24,
        "resolved_clip_intents": [*intents, _BASIS] if intents else [_BASIS],
    }


def _world(strategy: dict, brief: CreativeBrief | None = None):
    brief = brief or _brief()
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-503"),
        brief,
        latest_message=SPEC,
        media_snapshot=_snapshot(),
    )
    resolved = binding.resolve()
    contract = build_render_contract(
        strategy,
        generation_id="gen",
        brief=resolved,
        media_snapshot=binding.media_snapshot,
    )
    return resolved, contract


def _plan(strategy: dict, brief: CreativeBrief):
    return plan_unified_montage(
        _clips(),
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=True,
    )


def _recipe(plan):
    """The REAL guided compiler + REAL phone compiler over the plan's own snapshot."""
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
    return compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(execution), bindings)


def _verify(contract, recipe):
    return verify_phone_recipe(contract, recipe, source_audio={_id(i): True for i in range(11)})


def _receipt(brief: CreativeBrief, plan, *, strict: bool = True):
    (receipt,) = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), strict_order=strict
    )
    return receipt


def _ids(indices) -> list[str]:
    return [_id(i) for i in indices]


# -- the incident ------------------------------------------------------------


def test_incident_shape_contract_pins_the_stated_clip_first_then_filming_order() -> None:
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    _brief_, contract = _world(strategy)
    rest = [i for i in FILMING if i != TEAL]
    assert list(contract.order_ids) == _ids([TEAL, *rest])
    assert contract.order_basis == "capture_time"
    assert contract.order_required and not contract.unresolved
    # The incident precondition: the teal clip is NOT first by filming time.
    assert FILMING[0] != TEAL


def test_the_plan_that_follows_the_creator_verifies_and_the_receipt_agrees() -> None:
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, contract = _world(strategy)
    plan = _plan(strategy, brief)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _verify(contract, _recipe(plan))  # was: "couldn't keep the confirmed clip order"
    assert _receipt(brief, plan).status == "met"


def test_a_plan_that_ignores_the_start_clip_is_refused_by_verifier_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, contract = _world(strategy)
    # The planner drops the seating rule: pure filming order, as the contract used to pin.
    monkeypatch.setattr(unified_montage, "_apply_sequence", lambda ordered, rows: list(ordered))
    plan = _plan(strategy, brief)
    assert plan.clip_ids[0] == _id(FILMING[0]) != _id(TEAL)
    with pytest.raises(CreatorRenderContractError, match="confirmed clip order"):
        _verify(contract, _recipe(plan))
    receipt = _receipt(brief, plan)
    assert receipt.status == "not_possible"
    assert "teal video (first)" in receipt.reason


def test_a_start_clip_in_second_place_is_refused_by_verifier_and_receipt() -> None:
    """The plan keeps filming order but lets the earliest clip lead: the teal clip is only
    second. Only the first two spots differ, and both checks must still refuse."""
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, contract = _world(strategy)
    earliest = FILMING[0]
    second_place = _strategy(
        _intent("first", "the earliest video", earliest), _intent(None, "the teal video", TEAL)
    )
    plan = _plan(second_place, brief)
    assert plan.clip_ids[:2] == [_id(earliest), _id(TEAL)]
    with pytest.raises(CreatorRenderContractError, match="confirmed clip order"):
        _verify(contract, _recipe(plan))
    # The receipt is judged against what THIS plan was asked to seat, so judge the same plan
    # against the creator's rule: the teal clip is not first.
    ask = _strategy(_intent("first", "the teal video", TEAL))
    by_id = {c.media_id: c for c in _clips()}
    placed = [by_id[media_id] for media_id in plan.clip_ids]
    outcomes = unified_montage._sequence_outcomes(
        unified_montage._sequence_intents(ask, True), placed
    )
    record = {**plan.record(), "intent_outcomes": outcomes}
    (receipt,) = build_receipts(
        brief.live(), plan_facts_from_unified_montage(record), strict_order=True
    )
    assert receipt.status == "not_possible"


def test_receipt_judges_the_start_clip_not_just_the_basis() -> None:
    """Same basis (capture_time), same key: only the stated clip's spot differs."""
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, _contract = _world(strategy)
    good = _plan(strategy, brief)
    record = good.record()
    assert record["ordering_basis"] == "capture_time"
    wrong = {
        **record,
        "intent_outcomes": [
            {
                **row,
                "status": "partial",
                "code": "misplaced",
                "reason": "the clips did not end up there",
            }
            if row.get("op") == "order"
            else row
            for row in record["intent_outcomes"]
        ],
    }
    (receipt,) = build_receipts(
        brief.live(), plan_facts_from_unified_montage(wrong), strict_order=True
    )
    assert receipt.status == "not_possible"


# -- last / first+last / group -------------------------------------------------


def test_ending_with_a_clip_pins_it_last() -> None:
    strategy = _strategy(_intent("last", "the green video", GREEN))
    brief, contract = _world(strategy, _brief("chronological order, ending with the green video"))
    rest = [i for i in FILMING if i != GREEN]
    assert list(contract.order_ids) == _ids([*rest, GREEN])
    plan = _plan(strategy, brief)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _verify(contract, _recipe(plan))


def test_start_and_end_rules_together_and_a_multi_clip_group_stay_in_filming_order() -> None:
    strategy = _strategy(
        _intent("first", "the teal videos", TEAL, 8),
        _intent("last", "the green video", GREEN),
    )
    brief, contract = _world(strategy)
    group = [i for i in FILMING if i in (TEAL, 8)]
    rest = [i for i in FILMING if i not in (TEAL, 8, GREEN)]
    assert list(contract.order_ids) == _ids([*group, *rest, GREEN])
    plan = _plan(strategy, brief)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _verify(contract, _recipe(plan))
    assert _receipt(brief, plan).status == "met"


# -- unresolved + controls ------------------------------------------------------


def test_an_unresolved_start_clip_is_reported_not_guessed() -> None:
    strategy = _strategy(_intent("first", "the teal video", status="needs_creator"))
    _brief_, contract = _world(strategy)
    assert contract.unresolved and "teal video" in contract.unresolved[0]
    assert contract.order_ids == ()


def test_the_draft_backstop_turns_an_unresolved_start_clip_into_a_plain_message() -> None:
    from app.tasks.kria_runtime import _unresolved_choice_plan

    strategy = _strategy(_intent("first", "the teal video", status="needs_creator"))
    brief, _contract = _world(strategy)
    asked = _unresolved_choice_plan(
        strategy, brief, _snapshot(), contract_brief=brief, has_draft=False
    )
    assert asked is not None
    plan, reason = asked
    assert reason == "unresolved_requirement"
    assert "teal video" in plan.response


def test_plain_chronological_order_asks_nothing_and_is_unchanged() -> None:
    from app.services.choice_questions import open_conflicts

    plain = _strategy()
    brief, contract = _world(plain)
    assert open_conflicts(plain, brief, _snapshot()) == []
    assert list(contract.order_ids) == _ids(FILMING)
    assert contract.order_basis == "capture_time" and not contract.unresolved
    # Adding a start rule changes ONLY the order the contract pins.
    seated = _world(_strategy(_intent("first", "the teal video", TEAL)))[1]
    assert seated.order_ids != contract.order_ids
    blank = {"order_ids": (), "digest": "", "strategy_digest": None}
    assert seated.model_copy(update=blank) == contract.model_copy(update=blank)


def test_a_start_rule_with_no_clip_in_the_edit_changes_nothing_in_the_contract() -> None:
    # Nothing matched: the planner places nothing either, the receipt reports it (below).
    strategy = _strategy(_intent("first", "the teal video"))
    brief, contract = _world(strategy)
    assert list(contract.order_ids) == _ids(FILMING)
    plan = _plan(strategy, brief)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _receipt(brief, plan).status == "not_possible"


def test_flag_off_pins_the_pure_order_exactly_like_the_planner_ignores_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, contract = _world(strategy)
    assert list(contract.order_ids) == _ids(FILMING)
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=False
    )
    assert tuple(plan.clip_ids) == contract.order_ids


# -- legacy / unbound -----------------------------------------------------------


def test_a_job_without_a_brief_keeps_the_pure_filming_order() -> None:
    strategy = {
        **_strategy(_intent("first", "the teal video", TEAL)),
        "ordering_choice": "chronological",
    }
    contract = build_render_contract(strategy, generation_id="g", media_snapshot=_snapshot())
    assert list(contract.order_ids) == _ids(FILMING)
    assert contract.order_basis == "capture_time"


def test_unbound_receipts_keep_todays_verdict() -> None:
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, _contract = _world(strategy)
    record = _plan(strategy, brief).record()
    record["intent_outcomes"] = [
        {"op": "order", "name": "first: the teal video", "status": "partial"}
    ]
    facts = plan_facts_from_unified_montage(record)
    (legacy,) = build_receipts(brief.live(), facts, strict_order=False)
    assert (legacy.status, legacy.reason) == ("met", None)


# -- cloud ------------------------------------------------------------------------


def _assembly(contract) -> dict:
    return {CONTRACT_FIELD: contract.model_dump(mode="json")}


def _timeline(indices) -> dict:
    return {"story_timeline": [{"media_id": _id(i)} for i in indices]}


def test_cloud_guided_order_gate_accepts_the_stated_clip_first_and_refuses_pure_order() -> None:
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    _brief_, contract = _world(strategy)
    seated = [TEAL, *[i for i in FILMING if i != TEAL]]
    check_guided_plan_order(_assembly(contract), candidates=None, plan=_timeline(seated))
    with pytest.raises(CloudRenderContractError):
        check_guided_plan_order(_assembly(contract), candidates=None, plan=_timeline(FILMING))


# -- review fixes: groups that are not "a described group failed to land" --------


def _receipt_for(strategy: dict, *, visuals=()):
    brief, contract = _world(strategy)
    plan = plan_unified_montage(
        _clips(),
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=True,
        visuals=list(visuals),
    )
    return brief, contract, plan, _receipt(brief, plan)


def test_a_then_group_made_only_of_clips_an_earlier_group_seated_does_not_fail_the_order() -> None:
    """Prod shape (job aee52a3c): first(A) + last(B) + then("this chapter order", A, B)."""
    strategy = _strategy(
        _intent("first", "the teal video", TEAL),
        _intent("last", "the green video", GREEN),
        _intent(None, "this chapter order", TEAL, GREEN),
    )
    brief, contract, plan, receipt = _receipt_for(strategy)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _verify(contract, _recipe(plan))
    assert (receipt.status, receipt.reason) == ("met", None)


def test_a_then_group_that_repeats_the_start_clip_does_not_fail_the_order() -> None:
    strategy = _strategy(
        _intent("first", "the teal video", TEAL), _intent(None, "the teal one again", TEAL)
    )
    brief, contract, plan, receipt = _receipt_for(strategy)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _verify(contract, _recipe(plan))
    assert receipt.status == "met"


def test_a_then_group_of_visuals_only_does_not_fail_the_order() -> None:
    """Prod shape (job 7d53b0fd): a chapter whose members are Visuals-pool ids, not clips."""
    from tests.pipeline.test_unified_montage import visual

    photo = dataclasses.replace(visual(1), capture_time=T0)
    strategy = _strategy(
        _intent("first", "the teal video", TEAL), _intent(None, "the photos", photo.ref_id)
    )
    _brief_, contract, plan, receipt = _receipt_for(strategy, visuals=[photo])
    assert contract.order_ids[0] == _id(TEAL)
    assert receipt.status == "met"


def test_a_then_group_that_introduces_new_clips_still_counts_and_stays_met_when_seated() -> None:
    strategy = _strategy(
        _intent("first", "the teal video", TEAL), _intent(None, "the chapter", TEAL, 8)
    )
    brief, contract, plan, receipt = _receipt_for(strategy)
    assert list(contract.order_ids)[:2] == _ids([TEAL, 8])
    assert tuple(plan.clip_ids) == contract.order_ids
    assert receipt.status == "met"


def test_a_group_with_no_clip_in_the_edit_is_reported_as_absent_not_misplaced() -> None:
    for position in ("first", "last", None):
        strategy = _strategy(_intent(position, "the teal video", "deselected-clip"))
        _brief_, _contract, _plan_, receipt = _receipt_for(strategy)
        assert receipt.status == "not_possible", position
        assert "no clips" in receipt.reason and "teal video" in receipt.reason, position
        assert "aren't where you asked" not in receipt.reason, position


def test_a_misplaced_group_is_still_reported_when_a_contained_group_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy(
        _intent("first", "the teal video", TEAL), _intent(None, "the teal one again", TEAL)
    )
    brief, _contract = _world(strategy)
    monkeypatch.setattr(unified_montage, "_apply_sequence", lambda ordered, rows: list(ordered))
    receipt = _receipt(brief, _plan(strategy, brief))
    assert receipt.status == "not_possible"
    assert "teal video (first)" in receipt.reason
    assert "teal one again" not in receipt.reason


def test_sequence_rows_accept_a_tuple_like_the_planner_always_did() -> None:
    from app.services.clip_order_sequence import sequence_rows

    rows = sequence_rows((_intent("first", "x", TEAL),))
    assert [(r[0], r[2]) for r in rows] == [("first", [_id(TEAL)])]
    assert sequence_rows("not a list") == [] and sequence_rows(None) == []


def test_an_unresolved_start_clip_message_names_what_to_disambiguate() -> None:
    asked = _intent("first", "the teal video", status="needs_creator")
    asked["question"] = "Which of these is the teal video?"
    _brief_, with_question = _world(_strategy(asked))
    assert with_question.unresolved == ("Which of these is the teal video?",)
    bare = _intent("first", "the teal video", status="needs_creator")
    _brief_, without = _world(_strategy(bare))
    assert "teal video" in without.unresolved[0] and without.order_ids == ()


def test_capture_time_ties_seat_the_same_way_in_plan_and_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tied = list(MINUTES)
    tied[2] = tied[4]  # two clips filmed in the same minute
    monkeypatch.setattr(sys.modules[__name__], "MINUTES", tied)
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    brief, contract = _world(strategy)
    assert contract.order_ids[0] == _id(TEAL)
    plan = _plan(strategy, brief)
    assert tuple(plan.clip_ids) == contract.order_ids
    assert _verify(contract, _recipe(plan))


def test_an_answered_attachment_order_gets_the_same_start_clip_seating() -> None:
    answer = {
        "conflict": "order_basis",
        "kind": "order_basis",
        "option": "attachment_order",
        "input_digest": "d",
        "requirement_ids": ["r1"],
    }
    strategy = {**_strategy(_intent("first", "the teal video", TEAL)), "choice_answers": [answer]}
    _brief_, contract = _world(strategy)
    assert contract.order_basis == "attachment_order"
    assert list(contract.order_ids) == _ids([TEAL, *[i for i in range(11) if i != TEAL]])
