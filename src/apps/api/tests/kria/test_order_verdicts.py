"""KRI-470 PR-G: a required order that was not met is a failing verdict, never neutral.

Before: an order requirement the checker could not confirm ("I can't verify this
ordering automatically", "I can't confirm the order this draft uses") was a NEUTRAL
receipt: dropped from the reply and never blocking, so a render that ignored the
creator's order read as a success. The contract already treats every brief `order`
requirement as required (`order_required`); the receipts now agree.

Failure modes, written before the code (each is a row below):

* the plan applied a different order than asked and says so softly ("Partly")
* the plan carries no order at all after rendering, and the ask vanishes from the reply
* the creator's own sequence rule (key unknown to the checker) was not applied
* a genuinely optional order preference is turned into a failure
* a DRAFT (nothing rendered yet) is failed for an order it could not have recorded
* an order that WAS met, or partly met on an honest fallback, is made to fail
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_judged,
    needs_creator_choice,
    plan_facts_from_speech_montage,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
    reply_from_receipts,
)


def _order(key: str | None = "capture_time", **facts) -> BriefRequirement:
    return BriefRequirement(
        id="o1",
        kind="order",
        scope="global",
        description="in the order I filmed them",
        facts={**({"key": key} if key else {}), **facts},
    )


def _record(**changes) -> dict:
    return {"clip_ids": ["a", "b", "c"], "duration_s": 12.0, **changes}


def _verdict(req, facts, *, include_unchecked=False, strict=True):
    """The receipt for one order requirement. ``strict`` is the order authority being bound
    (a contract-stamped job or a brief-binding cohort); legacy jobs pass ``strict=False``."""
    (receipt,) = build_receipts(
        [req], facts, include_unchecked=include_unchecked, strict_order=strict
    ) or [None]
    return receipt


@pytest.mark.parametrize(
    ("req", "record", "reason_has"),
    [
        # plan applied attachment order, creator asked for filming order
        (_order(), _record(ordering_basis="attachment"), "attachment"),
        # lip-sync cut to the song, creator asked for filming order
        (_order(), _record(ordering_basis="song_time"), "song time"),
        # a rule only the creator's words describe; nothing in the plan applied it
        (_order(None), _record(ordering_basis="attachment"), "couldn't match"),
        # a rule the checker has no key for, plan sorted by capture time: not what was asked
        (_order("alphabetical"), _record(ordering_basis="capture_time"), "verify"),
    ],
)
def test_a_required_order_the_plan_did_not_apply_is_a_failed_verdict(req, record, reason_has):
    receipt = _verdict(req, plan_facts_from_unified_montage(record))
    assert receipt.status == "not_possible"
    assert reason_has in (receipt.reason or "")
    assert is_judged(req, receipt)
    assert needs_creator_choice(receipt.model_copy(update={"verification": "checked"}))


def test_a_rendered_plan_with_no_recorded_order_fails_instead_of_vanishing():
    facts = plan_facts_from_speech_montage({"duration_s": 9.0})  # a render, no ordering_basis
    req = _order()
    assert build_receipts([req], facts, strict_order=True) != []  # not dropped
    receipt = _verdict(req, facts)
    assert receipt.status == "not_possible"
    assert "confirm the order" in receipt.reason
    brief = CreativeBrief(version=1, requirements=[req])
    reply = reply_from_receipts(brief, [receipt])
    assert reply.startswith("Not everything you asked for made it in:")
    assert "Couldn't:" in reply and "Partly:" not in reply


def test_a_draft_that_has_not_rendered_yet_is_not_judged_on_an_order_it_cannot_record():
    draft = plan_facts_from_strategy({"edit_format": "montage"}, clip_ids=["a", "b"])
    req = _order()
    assert build_receipts([req], draft, strict_order=True) == []
    unchecked = _verdict(req, draft, include_unchecked=True)
    assert unchecked.verification == "unchecked"  # still open: the render's receipts decide


@pytest.mark.parametrize(
    "preference", [{"strength": "preference"}, {"required": False}, {"strength": "optional"}]
)
def test_an_optional_order_preference_keeps_the_unchecked_path(preference):
    req = _order("alphabetical", **preference)
    facts = plan_facts_from_unified_montage(_record(ordering_basis="capture_time"))
    assert build_receipts([req], facts, strict_order=True) == []
    assert _verdict(req, facts, include_unchecked=True).verification == "unchecked"
    no_order = plan_facts_from_speech_montage({"duration_s": 9.0})
    assert build_receipts([_order(**preference)], no_order, strict_order=True) == []


def test_a_met_order_and_an_honest_fallback_are_not_made_to_fail():
    in_filming_order = plan_facts_from_unified_montage(_record(ordering_basis="capture_time"))
    met = _verdict(_order(), in_filming_order)
    assert (met.status, met.reason) == ("met", None)
    partial = _verdict(
        _order(),
        plan_facts_from_unified_montage(
            _record(ordering_basis="capture_time", ordering_fallback_clip_ids=["b"])
        ),
    )
    assert partial.status == "partial"  # 1 clip had no capture time: order holds for the rest
    assert "no capture time" in partial.reason


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("met", "met"), "met"),
        (("met", "unmet"), "partial"),  # some groups landed: partly met
        (("unmet", "unmet"), "not_possible"),  # none landed: not met
    ],
)
def test_a_sequence_the_server_placed_is_judged_by_how_many_groups_landed(statuses, expected):
    record = _record(
        ordering_basis="attachment",
        intent_outcomes=[{"op": "order", "status": s} for s in statuses],
    )
    receipt = _verdict(_order(None), plan_facts_from_unified_montage(record))
    assert receipt.status == expected


def test_the_receipts_share_the_contracts_capture_order_keys():
    from app.kria import brief_checks
    from app.services import clip_facts

    assert brief_checks._CAPTURE_ORDER_KEYS is clip_facts.CAPTURE_ORDER_KEYS
    for key in sorted(clip_facts.CAPTURE_ORDER_KEYS):
        receipt = _verdict(
            _order(key), plan_facts_from_unified_montage(_record(ordering_basis="capture_time"))
        )
        assert receipt.status == "met", key


def test_plan_facts_default_is_not_a_rendered_output():
    assert PlanFacts().rendered_output is False


def test_a_lip_sync_montage_keeps_the_song_placement_as_the_order_authority():
    """#1451: "Use this order: clips ..." is a keyless order requirement and the song
    placement owns it. It must stay unjudged here, not become a blocking failure."""
    facts = plan_facts_from_unified_montage(_record(ordering_basis="song_time"))
    assert build_receipts([_order(None)], facts, strict_order=True) == []
    # ...but an explicit ask for filming order on a song-time cut is still unmet.
    assert _verdict(_order("capture_time"), facts).status == "not_possible"


# --- the stricter verdict applies only where an authority can verify the order --------------


def _unified(basis):
    return plan_facts_from_unified_montage(_record(ordering_basis=basis))


@pytest.mark.parametrize(
    ("req", "facts", "main_status"),
    [
        # Each row: what main (before PR-G) returned, byte for byte, for an unbound job.
        (_order(), _unified("attachment"), "partial"),
        (_order(), _unified("song_time"), "partial"),
        (_order(None), _unified("attachment"), "partial"),
        (_order("alphabetical"), _unified("capture_time"), "partial"),
        (_order(), plan_facts_from_speech_montage({"duration_s": 9.0}), "partial"),
    ],
)
def test_a_legacy_unbound_job_keeps_main_verdict_semantics(req, facts, main_status):
    receipt = _verdict(req, facts, strict=False, include_unchecked=False)
    if receipt is None:  # main dropped the neutral receipt entirely
        assert main_status == "partial"
        return
    assert receipt.status == main_status != "not_possible"


def test_legacy_neutral_receipts_are_still_dropped_for_an_unbound_job():
    no_order = plan_facts_from_speech_montage({"duration_s": 9.0})
    assert build_receipts([_order()], no_order) == []
    unknown_rule = plan_facts_from_unified_montage(_record(ordering_basis="capture_time"))
    assert build_receipts([_order("alphabetical")], unknown_rule) == []
    # Strictness follows the writer's binding by default, exactly like `include_unchecked`.
    assert build_receipts([_order()], no_order, include_unchecked=True)[0].status == "not_possible"


def test_an_attachment_ordered_speech_montage_is_not_a_false_couldnt():
    """The job now records the order it used. An unstamped brief that asked for attachment
    order is MET; a rule the plan cannot show is judged only when an authority is bound."""
    facts = plan_facts_from_speech_montage({"duration_s": 9.0, "ordering_basis": "attachment"})
    attachment = _order("attachment")
    assert _verdict(attachment, facts, strict=False).status == "met"
    assert _verdict(attachment, facts, strict=True).status == "met"
    keyless = _order(None)
    assert _verdict(keyless, facts, strict=False).status != "not_possible"
