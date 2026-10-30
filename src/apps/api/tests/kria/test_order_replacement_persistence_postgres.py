"""Real PostgreSQL authority/write boundary, using only synthetic owned events."""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import sync_session
from app.kria.brief import BriefUpdate, persist_brief_version_sync
from app.kria.order_replacement import OrderReplacementProof, OrderReplacementProposal
from app.kria.order_replacement_persistence import OrderReplacementWriteContext
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentTurn,
    User,
)
from tests.kria.test_order_replacement import question, source
from tests.kria.test_order_replacement_persistence import brief, proof_for

if not (make_url(settings.database_url).database or "").endswith("_test"):
    pytest.skip("requires an isolated test database", allow_module_level=True)


@pytest.fixture
def db():
    with sync_session() as session:
        yield session
        session.rollback()


def seed(db, change=None):
    creator_id, other_id, thread_id, other_thread = (uuid.uuid4() for _ in range(4))
    db.add_all(
        [
            User(id=creator_id, email=f"{creator_id}@test.local"),
            User(id=other_id, email=f"{other_id}@test.local"),
        ]
    )
    db.flush()
    db.add_all(
        [
            CreationThread(id=thread_id, creator_id=creator_id, runtime_version=2),
            CreationThread(id=other_thread, creator_id=other_id, runtime_version=2),
        ]
    )
    db.flush()
    prior = brief()
    original = prior.model_dump(mode="json")
    initial = proof_for(prior)
    raw = initial.proposal.model_dump(mode="json")
    raw["rejection_selection_sequence"] = 1
    p = OrderReplacementProposal.model_validate(raw)
    selected_sequence = 10 if change == "reanswered" else 9
    proof = OrderReplacementProof(
        proposal=p,
        question_id="replace-question",
        question_input_digest=p.input_digest,
        question_sequence=8,
        selection_sequence=selected_sequence,
    )
    q = question(p)
    selection = {"question_id": proof.question_id, "option_key": "accept_sequence"}
    if change == "delegated":
        selection["delegated"] = True
    elif change == "wrong_question":
        selection["question_id"] = "different"
    if change == "stale_predecessor":
        prior.requirements[1].description = "A later changed request"
    if change == "wrong_sequence":
        proof = proof.model_copy(update={"selection_sequence": 11})
    qturn_id, answer_turn_id = uuid.uuid4(), uuid.uuid4()
    clip_question = CreationThreadEvent(
        thread_id=thread_id,
        sequence=3,
        revision=3,
        role="assistant",
        event_type="assistant_response",
        payload={
            "clip_question": {
                "question_id": "clips-question",
                "allow_none": True,
                "categories": [
                    {"key": "interior", "intent_id": "interior", "op": "order", "label": "Interior"}
                ],
            }
        },
    )
    rejected = CreationThreadEvent(
        thread_id=thread_id,
        sequence=4,
        revision=4,
        role="user",
        event_type="user_message",
        content="None",
        payload={
            "clip_selection": {
                "question_id": "clips-question",
                "none_keys": ["interior"],
                "answers": [],
            }
        },
    )
    offered = CreationThreadEvent(
        thread_id=thread_id,
        sequence=8,
        revision=8,
        role="assistant",
        event_type="assistant_response",
        payload={"choice_question": q, "turn_id": str(qturn_id)},
    )
    answer = CreationThreadEvent(
        thread_id=other_thread if change == "foreign_source_event" else thread_id,
        sequence=selected_sequence,
        revision=selected_sequence,
        role="user",
        event_type="user_message",
        content="Use this sequence",
        payload={"choice_selection": selection},
    )
    db.add_all([clip_question, rejected, offered, answer])
    if change == "reanswered":
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=9,
                revision=9,
                role="user",
                event_type="user_message",
                payload={"choice_selection": selection},
            )
        )
    elif change == "later_user":
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=10,
                revision=10,
                role="user",
                event_type="user_message",
                content="Actually change it",
            )
        )
    elif change == "positive_clip_correction":
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=6,
                revision=6,
                role="user",
                event_type="user_message",
                payload={
                    "clip_selection": {
                        "question_id": "clips-question",
                        "none_keys": [],
                        "answers": [{"key": "interior", "media_ids": ["take"]}],
                    }
                },
            )
        )
    db.flush()
    saved = {"choice_question": q, "order_replacement_proposal": p.model_dump(mode="json")}
    if change == "missing_server_offer":
        saved.pop("order_replacement_proposal")
    elif change == "changed_server_offer":
        saved["order_replacement_proposal"]["moments"][0]["label"] = "Changed offer"
    db.add_all(
        [
            CreatorAgentTurn(
                id=qturn_id,
                thread_id=other_thread if change == "foreign_question_turn" else thread_id,
                source_event_id=rejected.id,
                client_event_id=uuid.uuid4().hex,
                request_digest="synthetic",
                status="completed",
                plan_json=saved,
            ),
            CreatorAgentTurn(
                id=answer_turn_id,
                thread_id=other_thread if change == "foreign_turn" else thread_id,
                source_event_id=answer.id,
                client_event_id=uuid.uuid4().hex,
                request_digest="synthetic",
                status="executing",
            ),
        ]
    )
    db.flush()
    db.add(
        CreativeBriefVersion(
            thread_id=thread_id,
            version=3,
            requirements=[r.model_dump(mode="json") for r in prior.requirements],
        )
    )
    db.flush()
    context = OrderReplacementWriteContext(
        creator_id=other_id if change == "wrong_owner" else creator_id,
        proof=proof,
        checked_moments=p.moments,
        sources=(source().model_copy(update={"generation": "g2"}),)
        if change == "stale_media"
        else (source(),),
    )
    db.execute(select(CreationThread).where(CreationThread.id == thread_id).with_for_update())
    return thread_id, answer_turn_id, context, original


def persist(db, thread_id, turn_id, context, updates=()):
    return persist_brief_version_sync(
        db, thread_id=thread_id, turn_id=turn_id, updates=updates, order_replacement=context
    )


def test_persist_one_canonical_version_with_unrelated_update_and_exact_retry(db):
    thread, turn, context, original = seed(db)
    updates = [BriefUpdate(kind="timing", scope="global", description="12 seconds")]
    result = persist(db, thread, turn, context, updates)
    assert result.version == 4
    assert result.requirements[0].literal == "the interior"
    assert result.requirements[1].status == "superseded"
    assert result.requirements[-1].id == "r4"
    assert result.requirements[-1].order_replacement == context.proof
    db.expire_all()
    repeated = persist(db, thread, turn, context, updates)
    assert repeated == result
    stored = (
        db.execute(
            select(CreativeBriefVersion)
            .where(CreativeBriefVersion.thread_id == thread)
            .order_by(CreativeBriefVersion.version)
        )
        .scalars()
        .all()
    )
    assert len(stored) == 2
    assert stored[0].requirements == original["requirements"]
    assert stored[1].requirements[-1]["order_replacement"] == context.proof.model_dump(mode="json")


@pytest.mark.parametrize(
    "change",
    [
        "delegated",
        "wrong_question",
        "stale_predecessor",
        "wrong_sequence",
        "reanswered",
        "later_user",
        "positive_clip_correction",
        "missing_server_offer",
        "changed_server_offer",
        "foreign_source_event",
        "foreign_turn",
        "foreign_question_turn",
        "wrong_owner",
        "stale_media",
    ],
)
def test_unproven_choice_cannot_append_a_canonical_version(db, change):
    thread, turn, context, _ = seed(db, change)
    with pytest.raises(ValueError):
        persist(db, thread, turn, context)
    count = db.scalar(
        select(func.count())
        .select_from(CreativeBriefVersion)
        .where(CreativeBriefVersion.thread_id == thread)
    )
    assert count == 1


def test_retry_cannot_substitute_proof_or_owner(db):
    thread, turn, context, _ = seed(db)
    result = persist(db, thread, turn, context)
    bad = context.proof.model_copy(update={"question_id": "different"})
    for replacement in (replace(context, proof=bad), replace(context, creator_id=uuid.uuid4())):
        with pytest.raises(ValueError):
            persist(db, thread, turn, replacement)
    assert persist(db, thread, turn, context) == result


def test_legacy_no_argument_preserves_existing_requirements(db):
    thread, turn, context, original = seed(db)
    untouched = persist_brief_version_sync(db, thread_id=thread, turn_id=turn, updates=[])
    assert untouched.model_dump(mode="json") == original
    result = persist_brief_version_sync(
        db,
        thread_id=thread,
        turn_id=turn,
        updates=[BriefUpdate(kind="timing", scope="global", description="12 seconds")],
    )
    assert result.requirements[:2] == untouched.requirements
    assert result.requirements[-1].order_replacement is None
    with pytest.raises(ValueError, match="different order replacement"):
        persist(db, thread, turn, context)
