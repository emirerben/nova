"""A typed list inside one order row retains its internal sequence.

Failure modes: attachment order replaces list order, a swapped list gets a met
receipt, and an unordered group changes its established filming order.
"""

import pytest

from app.pipeline import unified_montage
from app.schemas.clip_intents import ClipIntent
from tests.pipeline.test_kri941_listed_order_replay import (
    FLOWERS,
    GRILL,
    LISTED_CUT,
    PROD_CUT,
    _plan,
    _receipts,
    _recorded_strategy,
)


def _listed():
    strategy = _recorded_strategy()
    for intent in strategy["resolved_clip_intents"]:
        if intent["intent_id"] == "order_sequence_between":
            intent["ordered_members"] = True
    return strategy


def test_a_collapsed_list_is_seated_in_its_stated_member_order():
    plan = _plan(_listed())
    assert plan.clip_ids == LISTED_CUT
    assert _receipts(plan)["r2"].status == "met"


def test_a_wrong_order_inside_the_group_cannot_read_met(monkeypatch):
    seat = unified_montage._apply_sequence

    def swapped(ordered, rows):
        result = list(seat(ordered, rows))
        positions = {clip.media_id: i for i, clip in enumerate(result)}
        a, b = positions[GRILL], positions[FLOWERS]
        result[a], result[b] = result[b], result[a]
        return result

    monkeypatch.setattr(unified_montage, "_apply_sequence", swapped)
    assert _receipts(_plan(_listed()))["r2"].status != "met"


def test_an_unordered_group_preserves_its_previous_order():
    strategy = _recorded_strategy()
    for intent in strategy["resolved_clip_intents"]:
        if intent["intent_id"] == "order_sequence_between":
            intent["ordered_members"] = False
    plan = _plan(strategy)
    assert plan.clip_ids == PROD_CUT
    assert _receipts(plan)["r2"].status == "met"


def test_the_order_contract_keeps_the_list_marker():
    intent = ClipIntent.model_validate(
        {
            "intent_id": "middle",
            "op": "order",
            "attribute": "door, butcher, grill",
            "ordered_members": True,
        }
    )
    assert intent.model_dump()["ordered_members"] is True
    legacy = ClipIntent(intent_id="group", op="order", attribute="the volleyball clips")
    assert "ordered_members" not in legacy.model_dump()


def test_an_explicitly_unordered_group_keeps_its_certainty_when_saved():
    group = ClipIntent(
        intent_id="group", op="order", attribute="the volleyball clips", ordered_members=False
    )
    saved = group.model_dump(mode="json")
    assert saved["ordered_members"] is False
    assert ClipIntent.model_validate(saved).ordered_members is False


def test_reconfirming_a_saved_list_recovers_the_order_receipt():
    saved = _recorded_strategy()
    assert _receipts(_plan(saved))["r2"].status == "partial"
    for intent in saved["resolved_clip_intents"]:
        if intent["intent_id"] == "order_sequence_between":
            intent["ordered_members"] = True
    recovered = _plan(saved)
    assert recovered.clip_ids == LISTED_CUT
    assert _receipts(recovered)["r2"].status == "met"


@pytest.mark.asyncio
async def test_the_inventory_marker_reaches_the_resolver_and_resolved_strategy(monkeypatch):
    from tests.services.test_kri779_single_take_moments import (
        SECOND_TAKE,
        TAKE,
        _patch,
        _resolve,
        _take,
    )

    transport = _patch(
        monkeypatch,
        {
            "intents": [
                {
                    "intent_id": "middle",
                    "assignments": [
                        {"media": "m002", "confidence": 0.95, "evidence": "the door"},
                        {"media": "m001", "confidence": 0.95, "evidence": "the grill"},
                    ],
                }
            ]
        },
    )
    prompts = []
    invoke = transport.invoke

    def recorded(**kwargs):
        prompts.append(kwargs["prompt"])
        return invoke(**kwargs)

    monkeypatch.setattr(transport, "invoke", recorded)
    result = await _resolve(
        [ClipIntent(intent_id="middle", op="order", attribute="door, grill", ordered_members=True)],
        [_take(), _take(SECOND_TAKE)],
        request="in this order: door, grill",
    )
    assert result.status == "resolved"
    assert result.intents[0].ordered_members
    assert result.intents[0].media_ids() == [SECOND_TAKE, TAKE]
    assert "ordered_members=true" in prompts[0]
