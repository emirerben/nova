"""Real compiler verification gates replacement receipts; no provider or writer stand-ins."""

import copy
from dataclasses import FrozenInstanceError, replace

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, build_receipts, plan_facts_from_unified_montage
from app.kria.order_replacement import (
    CheckedOrderMoment,
    OrderReplacementProposal,
    source_fence_digest,
    validate_confirmation,
)
from app.kria.order_replacement_receipts import VerifiedOrderExecution, verify_order_execution
from app.kria.prepared_execution import SourceIdentity
from app.pipeline.guided_story import compile_execution_plan
from tests.kria.test_order_replacement import context, proposal, question
from tests.pipeline.test_guided_story import _guided_snapshot


def confirmed_execution():
    snapshot = _guided_snapshot()
    offered = compile_execution_plan(snapshot, track=None)
    sources = tuple(
        SourceIdentity(
            slot=f"clip:{i}",
            media_id=row["media_id"],
            path=row["gcs_path"],
            role="original",
            generation=row["generation"],
            duration_s=30.0,
        )
        for i, row in enumerate(offered["story_timeline"])
    )
    moments = tuple(
        CheckedOrderMoment(
            occurrence_id=row["moment_id"],
            intent_id=f"intent-{i}",
            label=row["topic"],
            source_slot=source.slot,
            source_fence_digest=source_fence_digest(source),
            media_id=row["media_id"],
            kind=row["kind"],
            gcs_path=row["gcs_path"],
            generation=row["generation"],
            source_start_s=row["source_start_s"] if row["kind"] == "video" else None,
            source_end_s=row["source_end_s"] if row["kind"] == "video" else None,
            hold_duration_s=row["duration_s"] if row["kind"] == "image" else None,
            evidence_origin="creator_answer" if row["kind"] == "video" else "selected_image",
            evidence_ref=f"checked-{i}",
            output_start_s=row["output_start_s"],
            output_end_s=row["output_end_s"],
            duration_s=row["duration_s"],
            playback_rate=row.get("playback_rate") or 1.0,
            freeze_hold_s=row.get("freeze_hold_s") or 0.0,
        )
        for i, (row, source) in enumerate(zip(offered["story_timeline"], sources, strict=True))
    )
    p = OrderReplacementProposal.model_validate(
        {**proposal().model_dump(mode="python"), "moments": moments}
    )
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **{**context(p), "sources": sources},
    )
    return proof, compile_execution_plan(snapshot, track=None), sources


def requirement(proof=None, req_id="r3"):
    return BriefRequirement(
        id=req_id,
        kind="order",
        scope="global",
        description="Confirmed remaining sequence",
        facts={"key": "any_order"},
        order_replacement=proof,
    )


def facts_with_omission():
    return plan_facts_from_unified_montage(
        {
            "ordering_basis": "creator_order",
            "intent_outcomes": [
                {
                    "intent_id": "interior",
                    "op": "order",
                    "name": "interior",
                    "status": "unresolved",
                    "code": "skipped",
                }
            ],
        }
    )


def status(req, facts):
    return build_receipts([req], facts, include_unchecked=True)[0].status


def test_real_compiler_verified_replacement_is_met_but_predecessor_stays_partial():
    proof, execution, sources = confirmed_execution()
    verified = verify_order_execution(proof, execution, sources=sources)
    facts = replace(facts_with_omission(), verified_order_execution=verified)
    assert status(requirement(proof), facts) == "met"
    assert status(requirement(req_id="r2"), facts) == "partial"
    assert facts.sequence_unlocated == ("skipped",)


@pytest.mark.parametrize("evidence", [None, True, {"verified": True}])
def test_typed_proof_without_factory_evidence_cannot_claim_met(evidence):
    proof, _, _ = confirmed_execution()
    facts = replace(PlanFacts(), verified_order_execution=evidence)
    assert status(requirement(proof), facts) == "partial"


def test_verified_record_is_immutable_and_has_no_public_constructor():
    proof, execution, sources = confirmed_execution()
    with pytest.raises(TypeError):
        VerifiedOrderExecution()
    evidence = verify_order_execution(proof, execution, sources=sources)
    with pytest.raises(FrozenInstanceError):
        evidence._proof_json = "forged"


@pytest.mark.parametrize(
    "change",
    ["source", "interval", "generation", "order", "timing", "rate", "freeze", "extra", "missing"],
)
def test_full_actual_timeline_mismatch_cannot_mint_evidence(change):
    proof, execution, sources = confirmed_execution()
    actual = copy.deepcopy(execution)
    rows = actual["story_timeline"]
    video = next(row for row in rows if row["kind"] == "video")
    if change == "source":
        video["gcs_path"] = "clips/other.mp4"
    elif change == "interval":
        video["source_start_s"] += 0.25
        video["source_end_s"] += 0.25
    elif change == "generation":
        video["generation"] = "new-generation"
    elif change == "order":
        rows.reverse()
    elif change == "timing":
        video["output_start_s"] += 0.25
        video["output_end_s"] += 0.25
    elif change == "rate":
        video["playback_rate"] = 2.0
    elif change == "freeze":
        video["freeze_hold_s"] = 0.25
    elif change == "extra":
        rows.append(copy.deepcopy(rows[-1]))
    else:
        rows.pop()
    with pytest.raises(ValueError):
        verify_order_execution(proof, actual, sources=sources)


def test_missing_or_stale_trusted_source_context_cannot_mint_evidence():
    proof, execution, sources = confirmed_execution()
    with pytest.raises(TypeError):
        verify_order_execution(proof, execution)
    for current in ((), (sources[0].model_copy(update={"generation": "new"}), *sources[1:])):
        with pytest.raises(ValueError):
            verify_order_execution(proof, execution, sources=current)


def test_verified_execution_for_another_confirmation_cannot_satisfy_requirement():
    proof, execution, sources = confirmed_execution()
    evidence = verify_order_execution(proof, execution, sources=sources)
    other = proof.model_copy(update={"question_id": "another-question"})
    assert (
        status(requirement(other), replace(PlanFacts(), verified_order_execution=evidence))
        == "partial"
    )


@pytest.mark.parametrize(
    "code,intent_id",
    [
        ("not_located", "intent-0"),
        ("skipped", "intent-0"),
        ("delegated", "intent-0"),
        ("skipped", None),
        ("not_located", "interior"),
    ],
)
def test_remaining_or_unmapped_moment_limits_stay_partial(code, intent_id):
    proof, execution, sources = confirmed_execution()
    evidence = verify_order_execution(proof, execution, sources=sources)
    facts = replace(
        facts_with_omission(),
        verified_order_execution=evidence,
        sequence_unlocated=(code,),
        sequence_unlocated_intent_ids=(intent_id,),
    )
    assert status(requirement(proof), facts) == "partial"


def test_unidentified_limit_cannot_be_ignored_by_a_verified_replacement():
    proof, execution, sources = confirmed_execution()
    facts = replace(
        PlanFacts(),
        verified_order_execution=verify_order_execution(proof, execution, sources=sources),
        sequence_unlocated=("skipped",),
    )
    assert status(requirement(proof), facts) == "partial"


def test_legacy_any_order_without_replacement_keeps_existing_behavior():
    assert status(requirement(), PlanFacts()) == "met"
    assert status(requirement(), facts_with_omission()) == "partial"


def test_phone_factory_requires_current_verified_original_binding():
    from app.services.phone_sources import bind_phone_sources
    from tests.kria.test_order_replacement import execution, phone_case

    assignment, bindings, proxy, p, args = phone_case()
    proof = validate_confirmation(
        p,
        question=question(p),
        selection={"question_id": "replace-question", "option_key": "accept_sequence"},
        question_sequence=8,
        selection_sequence=9,
        **args,
    )
    with pytest.raises(ValueError, match="phone binding"):
        verify_order_execution(proof, execution(p), sources=(proxy,))
    evidence = verify_order_execution(
        proof, execution(p), sources=(proxy,), phone_bindings=bindings
    )
    assert evidence.matches(proof)
    changed = copy.deepcopy(assignment)
    changed["upload_contract"]["proxy"]["original"]["sha256"] = "b" * 64
    stale = bind_phone_sources([changed], [changed["gcs_path"]])
    with pytest.raises(ValueError, match="identity or timing"):
        verify_order_execution(proof, execution(p), sources=(proxy,), phone_bindings=stale)


def test_different_predecessor_is_not_satisfied_by_verified_execution():
    proof, execution, sources = confirmed_execution()
    evidence = verify_order_execution(proof, execution, sources=sources)
    changed = proof.proposal.model_copy(update={"predecessor_requirement_id": "r1"})
    other = proof.model_copy(
        update={"proposal": changed, "question_input_digest": changed.input_digest}
    )
    facts = replace(facts_with_omission(), verified_order_execution=evidence)
    assert status(requirement(other), facts) == "partial"
