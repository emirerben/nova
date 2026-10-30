"""KRI-510: "End on the sip by the window." must not dead-end the chat.

Prod thread 8e5e9930 (2026-10-07, phone Montage, 8 clips): the brief recorded the ask as an
``order`` requirement with no capture key, the clip-intent resolver matched "the sip by the
window" to one clip (a resolved ``order`` intent, position ``last``) and the planner would
have seated it last. The question gate stays quiet about a rule the server placed
(``choice_questions._placed_sequence``), but the render contract still listed every
non-capture order rule as unresolved, so the planning preflight answered only "I can't
verify this ordering rule from the approved media." with no way forward.

Failure modes, written before the code (each is a test below):

* a placed end clip with no filming-order ask is still an unresolved rule (the incident)
* the preflight answers with the dead-end message instead of letting the draft through
* the question gate and the contract disagree (one quiet, the other refusing), either way
* lifting the rule lets a plan that does NOT end on the clip through: the brief receipt,
  which blocks the unified montage before any render, must catch it
* an ambiguous or Visuals-only target, or clip intents off, is waved through
* "in filming order, ending on X" asks for capture times twice or drops the seating

Everything runs the REAL contract builder, gate, planner, compilers and receipts.
"""

from __future__ import annotations

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
from app.services.choice_questions import CONFLICT_ORDER_BASIS, open_conflicts
from app.services.creator_render_contract import build_render_contract, verify_phone_recipe
from app.services.phone_sources import PhoneSourceBinding
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.pipeline.test_unified_montage import clip

N = 8
SIP = 5  # attached 6th: neither first nor last in attachment order
SPEC = "End on the sip by the window."
UNVERIFIABLE = "I can't verify this ordering rule from the approved media."
T0 = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
# Filming minute per clip index: the sip clip is NOT last by filming time either.
MINUTES = [30, 0, 70, 10, 50, 20, 60, 40]


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _id(index: int) -> str:
    return f"c{index}"


def _clips(*, filmed: bool = False):
    return [clip(i, duration=5.0, minutes=MINUTES[i] if filmed else None) for i in range(N)]


def _snapshot(*, filmed: bool = False) -> dict:
    rows = []
    for i in range(N):
        row: dict = {"media_id": _id(i), "kind": "video", "duration_s": 5.0}
        if filmed:
            stamp = (T0 + timedelta(minutes=MINUTES[i])).strftime("%Y-%m-%dT%H:%M:%SZ")
            row["capture"] = {"capture_time": stamp}
        rows.append(row)
    return {"clip_assignments": rows}


def _brief(facts: dict | None = None, *, filming_order: bool = False) -> CreativeBrief:
    requirements = [
        BriefRequirement(
            id="r1",
            kind="timing",
            scope="global",
            description="Make it 20 seconds",
            facts={"duration_s": 20},
        ),
        BriefRequirement(
            id="r6", kind="order", scope="global", description=SPEC, facts=facts or {}
        ),
    ]
    if filming_order:
        requirements.append(
            BriefRequirement(
                id="r7",
                kind="order",
                scope="global",
                description="in the order I filmed them",
                facts={"key": "capture_time"},
            )
        )
    return CreativeBrief(version=1, requirements=requirements)


def _intent(
    position: str | None, *media_ids: str, status: str = "resolved", question: str | None = None
) -> dict:
    row = {
        "op": "order",
        "status": status,
        "intent_id": "order_end_sip",
        "attribute": "the sip by the window",
        "assignments": [{"media_id": m, "confidence": 0.9} for m in media_ids],
    }
    if position:
        row["position"] = position
    if question:
        row["question"] = question
    return row


def _sip_last() -> dict:
    return _intent("last", _id(SIP))


def _strategy(*intents: dict) -> dict:
    return {
        "edit_format": "montage",
        "media_scope": "all",
        "selected_media_ids": [],
        "target_duration_s": 20,
        "resolved_clip_intents": list(intents),
    }


def _world(strategy: dict, brief: CreativeBrief, *, filmed: bool = False):
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-510"),
        brief,
        latest_message=SPEC,
        media_snapshot=_snapshot(filmed=filmed),
    )
    resolved = binding.resolve()
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=binding.media_snapshot
    )
    return resolved, contract


def _order_receipt(brief: CreativeBrief, plan):
    """The receipt exactly as the unified montage writes it for a brief-bound job."""
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), include_unchecked=True
    )
    return next(r for r in receipts if r.requirement_id == "r6")


def _recipe(plan):
    """The REAL guided compiler + REAL phone compiler over the plan's own snapshot."""
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
        for c in _clips()
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    return compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(execution), bindings)


# -- the incident ------------------------------------------------------------


@pytest.mark.parametrize(
    "facts",
    [{}, {"position": "last"}, {"key": "end_shot"}, {"key": "custom", "end": "the sip"}],
    ids=["no-facts", "position", "end-shot-key", "free-key"],
)
def test_incident_shape_a_placed_end_clip_is_not_an_unresolved_rule(facts: dict) -> None:
    _brief_, contract = _world(_strategy(_sip_last()), _brief(facts))
    assert contract.unresolved == ()  # was: (UNVERIFIABLE, "I need capture times ...")
    # Nothing to pin as a full order: the creator asked for no order of the rest.
    assert contract.order_required is False
    assert contract.order_ids == ()
    assert contract.duration_s == 20


def test_the_planning_preflight_lets_the_draft_through() -> None:
    brief = _brief()
    snapshot = _snapshot()
    assert (
        _unresolved_choice_plan(_strategy(_sip_last()), brief, snapshot, contract_brief=brief)
        is None
    )


def test_an_unplaced_rule_still_gets_a_question_not_the_dead_end() -> None:
    brief = _brief()
    blocked = _unresolved_choice_plan(_strategy(), brief, _snapshot(), contract_brief=brief)
    assert blocked is not None
    plan, _reason = blocked
    assert plan.turn_value == "question"
    assert plan.choice_question is not None


@pytest.mark.parametrize("placed", [True, False], ids=["placed", "unplaced"])
def test_the_question_gate_and_the_contract_agree(placed: bool) -> None:
    """Quiet gate + refusing contract was the dead end; asking + passing would let an
    unplaced rule through unchecked. Each must imply the other."""
    strategy = _strategy(_sip_last()) if placed else _strategy()
    brief, contract = _world(strategy, _brief())
    asks = any(c.kind == CONFLICT_ORDER_BASIS for c in open_conflicts(strategy, brief, _snapshot()))
    assert asks is (not placed)
    assert bool(contract.unresolved) is (not placed)


# -- the rule is still enforced ----------------------------------------------


def test_the_plan_ends_on_the_sip_and_its_checked_receipt_is_met() -> None:
    strategy = _strategy(_sip_last())
    brief, contract = _world(strategy, _brief())
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    assert plan.clip_ids[-1] == _id(SIP)
    receipt = _order_receipt(brief, plan)
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert verify_phone_recipe(
        contract, _recipe(plan), source_audio={_id(i): True for i in range(N)}
    )


def test_a_plan_that_does_not_end_on_the_sip_is_blocked_by_its_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract no longer refuses the rule, so the receipt is what stops a plan that
    ignores it: the unified montage blocks a brief-bound render on any checked receipt that
    is not met (`_run_phone_unified_montage_job`), before anything renders."""
    strategy = _strategy(_sip_last())
    brief, _contract = _world(strategy, _brief())
    monkeypatch.setattr(unified_montage, "_apply_sequence", lambda ordered, rows: list(ordered))
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    assert plan.clip_ids[-1] != _id(SIP)
    receipt = _order_receipt(brief, plan)
    assert receipt.verification == "checked"
    assert receipt.status == "not_possible"


# -- what stays unresolved, exactly as before -----------------------------------


def _assert_legacy_unresolved(contract) -> None:
    """The pre-KRI-510 shape for a rule nothing placed: the same first message, and the
    order still counted as required (the route resolver reads that)."""
    assert contract.unresolved[0] == UNVERIFIABLE
    assert contract.order_required is True


def test_an_unplaced_rule_is_unchanged() -> None:
    _brief_, contract = _world(_strategy(), _brief())
    _assert_legacy_unresolved(contract)


def test_an_ambiguous_end_clip_stays_unresolved_and_the_gate_asks() -> None:
    strategy = _strategy(
        _intent("last", status="needs_creator", question="Which clip is the sip by the window?")
    )
    brief, contract = _world(strategy, _brief())
    _assert_legacy_unresolved(contract)
    assert any(c.kind == CONFLICT_ORDER_BASIS for c in open_conflicts(strategy, brief, _snapshot()))


def test_an_end_target_that_is_only_a_visual_stays_unresolved() -> None:
    """The planner seats clips only (Visuals are spread between them), so a rule whose
    only match is a Visuals item is not placed."""
    _brief_, contract = _world(_strategy(_intent("last", "visual-photo-1")), _brief())
    _assert_legacy_unresolved(contract)


def test_one_placed_and_one_ambiguous_group_is_not_placed() -> None:
    strategy = _strategy(_sip_last(), _intent("first", status="needs_creator"))
    _brief_, contract = _world(strategy, _brief())
    _assert_legacy_unresolved(contract)


def test_with_clip_intents_off_the_rule_stays_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    _brief_, contract = _world(_strategy(_sip_last()), _brief())
    _assert_legacy_unresolved(contract)


# -- with a basis order --------------------------------------------------------


def test_filming_order_ending_on_the_sip_pins_the_seated_order() -> None:
    """Two requirements ("in the order I filmed them" + "end on the sip"): the KRI-503
    seating pins filming order with the sip clip last. Before, the described rule was an
    extra unresolved item on top of a resolvable capture order."""
    _brief_, contract = _world(_strategy(_sip_last()), _brief(filming_order=True), filmed=True)
    filming = sorted(range(N), key=lambda i: MINUTES[i])
    assert contract.unresolved == ()
    assert contract.order_basis == "capture_time"
    assert list(contract.order_ids) == [_id(i) for i in filming if i != SIP] + [_id(SIP)]


def test_filming_order_without_capture_times_asks_for_them_once() -> None:
    _brief_, contract = _world(_strategy(_sip_last()), _brief(filming_order=True))
    assert contract.unresolved == (
        "I need capture times for every selected clip to verify chronological order.",
    )
