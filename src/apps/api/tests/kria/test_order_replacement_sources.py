"""Current source context from transactional PostgreSQL rows, without storage access."""

import copy
import os
import uuid
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.kria.order_replacement import (
    OrderReplacementProof,
    phone_binding_digest,
    source_fence_digest,
    verify_compiled_order,
)
from app.kria.order_replacement_sources import (
    load_order_replacement_source_context,
    load_order_replacement_source_context_sync,
)
from app.kria.prepared_execution import SourceIdentity
from app.models import ContentPlan, Persona, PlanItem, PlanItemAsset, User
from app.services.phone_sources import bind_phone_sources
from tests.kria.test_order_replacement import execution, moment, proposal
from tests.services.test_phone_sources import receipt


@pytest.fixture
def stored():
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        transaction = conn.begin()
        with Session(bind=conn) as db:
            owner = User(id=uuid.uuid4(), email=f"order-source-{uuid.uuid4()}@example.test")
            db.add(owner)
            db.flush()
            persona = Persona(id=uuid.uuid4(), user_id=owner.id, persona_status="ready")
            db.add(persona)
            db.flush()
            plan = ContentPlan(id=uuid.uuid4(), user_id=owner.id, persona_id=persona.id)
            db.add(plan)
            db.flush()
            item = PlanItem(
                id=uuid.uuid4(),
                content_plan_id=plan.id,
                position=1,
                idea="Synthetic order source",
                clip_gcs_paths=["clips/take.mp4"],
                clip_assignments=[
                    {
                        "media_id": "take",
                        "gcs_path": "clips/take.mp4",
                        "storage_generation": "g1",
                        "duration_s": 40.0,
                        "kind": "video",
                    }
                ],
            )
            db.add(item)
            db.flush()
            yield db, owner, item
        transaction.rollback()
    engine.dispose()


def load(stored, p=None, **kwargs):
    db, owner, item = stored
    return load_order_replacement_source_context_sync(
        db, item_id=item.id, creator_id=kwargs.get("creator_id", owner.id), proposal=p or proposal()
    )


def verify(p, ctx):
    proof = OrderReplacementProof(
        proposal=p,
        question_id="question",
        question_input_digest=p.input_digest,
        question_sequence=8,
        selection_sequence=9,
    )
    compiled = execution(p)
    for checked, row in zip(p.moments, compiled["story_timeline"], strict=True):
        if checked.source_slot.startswith("visual:"):
            row["media_id"] = checked.source_slot.removeprefix("visual:")
            row["lane"] = "asset"
        if checked.kind == "image":
            row["source_start_s"] = 0.0
            row["source_end_s"] = checked.hold_duration_s
    verify_compiled_order(proof, compiled, sources=ctx.sources, phone_bindings=ctx.phone_bindings)


def test_owned_current_sources_verify_and_context_is_frozen(stored):
    ctx = load(stored)
    verify(proposal(), ctx)
    assert ctx.creator_id == str(stored[1].id)
    with pytest.raises(FrozenInstanceError):
        ctx.creator_id = "someone-else"


def test_wrong_actor_cannot_load_item(stored):
    with pytest.raises(ValueError, match="not owned"):
        load(stored, creator_id=uuid.uuid4())


def visual(stored, *, other_item=False, kind="image"):
    db, owner, item = stored
    if other_item:
        item = PlanItem(
            id=uuid.uuid4(),
            content_plan_id=item.content_plan_id,
            position=2,
            idea="Other synthetic item",
        )
        db.add(item)
        db.flush()
    asset = PlanItemAsset(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        user_id=owner.id,
        gcs_path=f"visuals/{uuid.uuid4()}.jpg",
        gcs_generation="v1",
        kind=kind,
        duration_s=10.0 if kind == "video" else None,
        media_status="ready",
    )
    db.add(asset)
    db.flush()
    source = SourceIdentity(
        slot=f"visual:{asset.id}",
        media_id=f"asset-{asset.id}",
        path=asset.gcs_path,
        generation="v1",
        role="original",
        duration_s=asset.duration_s,
    )
    m = moment().model_copy(
        update={
            "source_slot": source.slot,
            "media_id": source.media_id,
            "gcs_path": source.path,
            "generation": "v1",
            "source_fence_digest": source_fence_digest(source),
            "kind": kind,
            "source_start_s": 0.0 if kind == "video" else None,
            "source_end_s": 2.0 if kind == "video" else None,
            "hold_duration_s": None if kind == "video" else 2.0,
            "evidence_origin": "creator_answer" if kind == "video" else "selected_image",
        }
    )
    return asset, proposal().model_copy(update={"moments": (m,)})


@pytest.mark.parametrize("kind", ["image", "video"])
def test_exact_selected_visuals_from_item(stored, kind):
    asset, p = visual(stored, kind=kind)
    visual(stored)
    ctx = load(stored, p)
    assert [s.slot for s in ctx.sources if s.slot.startswith("visual:")] == [f"visual:{asset.id}"]
    verify(p, ctx)


def test_foreign_item_visual_is_rejected(stored):
    _, p = visual(stored, other_item=True)
    with pytest.raises(ValueError, match="not attached"):
        load(stored, p)


def test_visual_generation_change_is_seen_by_verifier(stored):
    asset, p = visual(stored)
    asset.gcs_generation = "v2"
    stored[0].flush()
    with pytest.raises(ValueError, match="identity changed"):
        verify(p, load(stored, p))


def test_arbitrary_visual_slot_cannot_borrow_asset(stored):
    _, p = visual(stored)
    bad = p.model_copy(
        update={"moments": (p.moments[0].model_copy(update={"source_slot": "visual:arbitrary"}),)}
    )
    with pytest.raises(ValueError):
        load(stored, bad)


def proxy(stored):
    db, _, item = stored
    row = receipt("take")
    item.clip_gcs_paths = [row["gcs_path"]]
    item.clip_assignments = [row]
    db.flush()
    binding = bind_phone_sources([row], [row["gcs_path"]])[0]
    source = SourceIdentity(
        slot="clip:0",
        media_id="take",
        path=row["gcs_path"],
        role="analysis_proxy",
        generation="123",
        duration_s=10.0,
    )
    m = moment().model_copy(
        update={
            "gcs_path": source.path,
            "generation": source.generation,
            "source_fence_digest": source_fence_digest(source),
            "phone_binding_digest": phone_binding_digest(binding),
        }
    )
    return proposal().model_copy(update={"moments": (m,)})


def test_proxy_uses_real_registered_receipt_binding(stored):
    p = proxy(stored)
    ctx = load(stored, p)
    assert len(ctx.phone_bindings) == 1
    verify(p, ctx)


@pytest.mark.parametrize("field,value", [("manifest_identity", "other"), ("duration_s", 3.0)])
def test_bad_proxy_receipt_is_rejected(stored, field, value):
    p = proxy(stored)
    item = stored[2]
    rows = copy.deepcopy(item.clip_assignments)
    rows[0][field] = value
    item.clip_assignments = rows
    stored[0].flush()
    with pytest.raises(ValueError):
        load(stored, p)


def test_proxy_original_fingerprint_change_invalidates_proof(stored):
    p = proxy(stored)
    item = stored[2]
    rows = copy.deepcopy(item.clip_assignments)
    rows[0]["upload_contract"]["proxy"]["original"]["sha256"] = "b" * 64
    item.clip_assignments = rows
    stored[0].flush()
    with pytest.raises(ValueError, match="original identity"):
        verify(p, load(stored, p))


@pytest.mark.asyncio
async def test_async_orchestration_uses_same_current_queries(stored):
    class AwaitableDB:
        async def execute(self, query):
            return stored[0].execute(query)

    ctx = await load_order_replacement_source_context(
        AwaitableDB(), item_id=stored[2].id, creator_id=stored[1].id, proposal=proposal()
    )
    assert ctx == load(stored)


@pytest.mark.parametrize("identity", ["native_uuid", "another_asset"])
def test_visual_source_context_keeps_canonical_identity(stored, identity):
    asset, p = visual(stored)
    media_id = str(asset.id) if identity == "native_uuid" else f"asset-{uuid.uuid4()}"
    bad = p.model_copy(
        update={"moments": (p.moments[0].model_copy(update={"media_id": media_id}),)}
    )
    with pytest.raises(ValueError, match="not canonical"):
        load(stored, bad)
