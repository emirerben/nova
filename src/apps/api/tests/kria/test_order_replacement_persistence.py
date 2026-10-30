"""Canonical replacement failures: stale target, concurrent order edits, copied proof."""

import uuid

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.order_replacement_persistence import (
    apply_order_replacement,
    requirement_digest,
)
from tests.kria.test_order_replacement import confirmation, proposal


def brief():
    return CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal="the interior"),
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description="Interior then palace",
                facts={"key": "any_order"},
            ),
        ],
    )


def proof_for(prior):
    raw = proposal().model_dump(mode="json")
    raw["predecessor_requirement_digest"] = requirement_digest(prior.requirements[1])
    p = type(proposal()).model_validate(raw)
    from app.kria.order_replacement import OrderReplacementProof

    base = confirmation(proposal()).model_dump(mode="json")
    base.update(proposal=p.model_dump(mode="json"), question_input_digest=p.input_digest)
    return OrderReplacementProof.model_validate(base)


def test_canonical_supersession_preserves_literals_history_and_unrelated_changes():
    prior = brief()
    original = prior.model_dump(mode="json")
    proof = proof_for(prior)
    turn_id = uuid.uuid4()
    result = apply_order_replacement(
        prior,
        [BriefUpdate(kind="timing", scope="global", description="Keep 12 seconds")],
        proof=proof,
        source_turn_id=str(turn_id),
    )
    assert prior.model_dump(mode="json") == original
    assert result.requirements[0] == prior.requirements[0]
    assert result.requirements[1].status == "superseded"
    assert result.requirements[2].kind == "timing"
    assert result.requirements[3].id == "r4"
    assert result.requirements[3].order_replacement == proof
    assert result.requirements[3].source_turn_id == str(turn_id)
    assert result.requirements[3].facts == {}


@pytest.mark.parametrize(
    "change",
    ["target", "version", "literal", "multiorder", "order_update", "target_update", "new_order"],
)
def test_ambiguous_or_concurrently_changed_order_cannot_be_replaced(change):
    prior = brief()
    proof = proof_for(prior)
    updates = []
    if change == "target":
        prior.requirements[1].description = "Changed order"
    elif change == "version":
        prior.version = 4
    elif change == "literal":
        prior.requirements[1].literal = "Keep exact words"
    elif change == "multiorder":
        prior.requirements.append(
            BriefRequirement(
                id="r3", kind="order", scope="clip:take", description="Reverse the take"
            )
        )
    elif change == "order_update":
        updates = [
            BriefUpdate(
                operation="change",
                target_requirement_id="r1",
                expected_version=3,
                kind="order",
                scope="global",
                description="Another order",
            )
        ]
    elif change == "target_update":
        updates = [
            BriefUpdate(
                operation="change",
                target_requirement_id="r2",
                expected_version=3,
                description="Different",
            )
        ]
    else:
        updates = [BriefUpdate(kind="order", scope="global", description="Different")]
    with pytest.raises(ValueError):
        apply_order_replacement(prior, updates, proof=proof, source_turn_id=str(uuid.uuid4()))


def test_ordinary_change_clears_previous_confirmation():
    prior = brief()
    result = apply_order_replacement(
        prior, [], proof=proof_for(prior), source_turn_id="answer-turn"
    )
    updated = apply_updates(
        result,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id="r3",
                expected_version=3,
                description="Changed words",
            )
        ],
        source_turn_id="next-turn",
    )
    assert updated.requirements[-1].order_replacement is None
    assert result.requirements[-1].order_replacement is not None
