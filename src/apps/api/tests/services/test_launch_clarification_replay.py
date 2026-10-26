"""Clarification answers preserve membership and exact photo copy across replanning.

Failure modes are a repeated include/order question, an exclusion accidentally
inheriting an include, and a grounded photo caption disappearing from draft facts.
"""

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import check_requirement, plan_facts_from_strategy
from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_planning import apply_creator_selections
from app.services.clip_selection import build_clip_question, fold_clip_selections
from tests.services.test_kri779_single_take_moments import TAKE, _take


@pytest.mark.parametrize("op", ["include", "order", "group"])
def test_a_tapped_moment_survives_a_membership_operation_change(op: str) -> None:
    question = build_clip_question(
        [
            {
                "key": "include:the-queue",
                "op": "include",
                "label": "the queue",
                "intent_id": "include_queue",
                "candidate_media_ids": [TAKE],
            }
        ],
        question_id="queue-question",
    )
    selections = fold_clip_selections(
        [
            ("assistant", {"clip_question": question}),
            (
                "user",
                {
                    "clip_selection": {
                        "question_id": "queue-question",
                        "answers": [{"key": "include:the-queue", "media_ids": [TAKE]}],
                    }
                },
            ),
        ]
    )
    intent = ClipIntent(intent_id="order_queue", op=op, attribute="the queue")
    settled, remaining, dropped = apply_creator_selections(
        [intent], clips=[_take()], selections=selections, creator_request="the queue"
    )
    assert remaining == [] and dropped == set()
    assert settled[0].op == op
    assert settled[0].media_ids() == [TAKE]

    exclusion = intent.model_copy(update={"op": "exclude"})
    assert selections.match(exclusion) is None


@pytest.mark.parametrize("status", ["resolved", "needs_creator"])
def test_photo_caption_receipt_reads_the_resolved_creator_copy(status: str) -> None:
    text = "Ojo de bife 🥩"
    strategy = {
        "resolved_clip_intents": [
            {
                "intent_id": "caption_ribeye",
                "op": "caption",
                "status": status,
                "attribute": "the ribeye photo",
                "creator_text": text,
                "caption_text": text,
                "caption_grounding": "creator_text",
                "assignments": [{"media_id": "asset-ribeye", "confidence": 1.0}],
            }
        ]
    }
    facts = plan_facts_from_strategy(strategy, clip_ids=["asset-ribeye"])
    receipt = check_requirement(
        BriefRequirement(
            id="photo-caption",
            kind="text",
            scope="clip:asset-ribeye",
            description="label the ribeye photo",
            literal=text,
        ),
        facts,
    )
    assert (receipt.status == "met") is (status == "resolved")
    if status == "resolved":
        wrong = plan_facts_from_strategy(strategy, clip_ids=["different-clip"])
        assert (
            check_requirement(
                BriefRequirement(
                    id="photo-caption",
                    kind="text",
                    scope="clip:asset-ribeye",
                    description="label the ribeye photo",
                    literal=text,
                ),
                wrong,
            ).status
            != "met"
        )


def test_a_caption_does_not_hide_another_requested_label_on_the_same_photo():
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "label",
                "status": "resolved",
                "assignments": [
                    {"media_id": "photo", "value": "Palermo", "grounding": "creator_text"}
                ],
            },
            {
                "op": "caption",
                "status": "resolved",
                "caption_text": "Ojo de bife 🥩",
                "caption_grounding": "creator_text",
                "assignments": [{"media_id": "photo"}],
            },
        ]
    }
    facts = plan_facts_from_strategy(strategy, clip_ids=["photo"])
    for literal in ("Palermo", "Ojo de bife 🥩"):
        assert (
            check_requirement(
                BriefRequirement(
                    id="text",
                    kind="text",
                    scope="clip:photo",
                    description="the photo",
                    literal=literal,
                ),
                facts,
            ).status
            == "met"
        )
