"""Real owned-source lookup, indexed evidence, durable cache and stale-source controls."""

import uuid
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import ContentPlan, MediaAssetAnalysis, MediaAssetSource, Persona, PlanItem
from app.services import prompt_moment_inspection as subject
from app.services.named_moments import named_moments
from tests.integration.test_media_collections import collection_world, selection  # noqa: F401


@pytest.mark.asyncio
async def test_real_source_index_cache_and_planner_refuse_replaced_generation(
    collection_world,  # noqa: F811
    monkeypatch,
):
    from app.kria import planner
    from app.kria.brief import BriefRequirement, CreativeBrief
    from tests.kria.test_kri775_merope_m2_replay import gate_media, planned_turn, prod_flags

    client, assets, sources, owner, _ = collection_world
    async with AsyncSessionLocal() as db:
        transcript = await db.scalar(
            select(MediaAssetAnalysis).where(
                MediaAssetAnalysis.source_id == uuid.UUID(sources[0]),
                MediaAssetAnalysis.analyzer == "nova.media.transcript",
            )
        )
        transcript.result_json = {"words": []}
        source = await db.get(MediaAssetSource, uuid.UUID(sources[0]))
        path = source.object_path
        persona = Persona(user_id=owner.id, persona_status="ready", persona={})
        db.add(persona)
        await db.flush()
        plan = ContentPlan(user_id=owner.id, persona_id=persona.id)
        db.add(plan)
        await db.flush()
        assignment = {
            "media_id": "take",
            "kind": "video",
            "gcs_path": path,
            "storage_generation": "42",
            "duration_s": 180.0,
            "analysis": {},
            "library_asset_id": assets[0],
            "library_source_id": sources[0],
        }
        item = PlanItem(
            content_plan_id=plan.id, position=1, idea="A ferry visit", clip_assignments=[assignment]
        )
        db.add(item)
        await db.commit()
        item_id = item.id
    collection_path = f"/me/media-collections/{uuid.uuid4()}"
    assert (
        await client.put(collection_path, json=selection(assets, sources, count=1))
    ).status_code == 200
    indexed = await client.post(f"{collection_path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    strategy = {
        "edit_format": "montage",
        "selected_media_ids": ["take"],
        "resolved_clip_intents": [
            {
                "intent_id": "waves",
                "op": "order",
                "status": "resolved",
                "attribute": "hello wave, goodbye wave",
                "assignments": [{"media_id": "take"}],
            }
        ],
    }
    media = {"clip_assignments": [assignment]}
    named = named_moments(strategy, media)
    calls = []

    def transport(source, moment, question, *, ctx, agent_factory):
        calls.append(question)
        start = 0.0 if "hello" in question else 4.0
        return {
            "status": "found",
            "matches": [
                {
                    "start_s": start,
                    "end_s": start + 2.0,
                    "confidence": 0.95,
                    "evidence": "The requested wave is visible",
                }
            ],
            "sample_sha256": "a" * 64,
        }

    monkeypatch.setattr(subject, "inspect_interval", transport)
    async with AsyncSessionLocal() as db:
        cold = await subject.inspect_named_moments(
            db, creator_id=owner.id, item_id=item_id, moment_set=named, events=[]
        )
        assert not cold.groundings and not calls
        first = await subject.inspect_named_moments(
            db,
            creator_id=owner.id,
            item_id=item_id,
            moment_set=named,
            events=[],
            allow_provider=True,
        )
        assert len(first.groundings) == 2
        assert [(g.found.start_s, g.found.end_s) for g in first.groundings.values()] == [
            (155.0, 157.0),
            (159.0, 161.0),
        ]
        cached = await subject.inspect_named_moments(
            db, creator_id=owner.id, item_id=item_id, moment_set=named, events=[]
        )
        assert cached == first and len(calls) == 2
        changed_words = replace(
            named, moments=(replace(named.moments[0], words="a different wave"),)
        )
        changed = await subject.inspect_named_moments(
            db, creator_id=owner.id, item_id=item_id, moment_set=changed_words, events=[]
        )
        assert not changed.groundings and len(calls) == 2
        foreign = await subject.inspect_named_moments(
            db, creator_id=uuid.uuid4(), item_id=item_id, moment_set=named, events=[]
        )
        assert not foreign.groundings
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_prompt_moment_inspection_enabled", True)
    monkeypatch.setattr(settings, "kria_prompt_moment_inspection_allow_provider", False)
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="moments",
                kind="select",
                scope="global",
                description="Two waves",
                facts={"moment_count": 2, "moment_length_s": 2.0},
            )
        ],
    )
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    render = {"strategy": strategy, "media_snapshot": media, "draft_reply": "Two waves."}
    async with AsyncSessionLocal() as db:
        result = await planner._gate_unresolved_choices(
            db,
            planned_turn(render),
            thread_id=uuid.uuid4(),
            creator_id=owner.id,
            item_id=item_id,
            answers_moment_questions=True,
        )
        assert result.plan.moment_order_question is None
        assert result.plan.mode == "act"
        pinned = gate_media(result)["length_plan"]["moments"]
        assert [(p["source_start_s"], p["source_end_s"]) for p in pinned] == [
            (155.0, 157.0),
            (159.0, 161.0),
        ]
        assert all(p["evidence"]["evidence"]["uncertain"] for p in pinned)
        item = await db.get(PlanItem, item_id)
        item.clip_assignments = [{**assignment, "storage_generation": "43"}]
        await db.commit()
        stale = await subject.inspect_named_moments(
            db, creator_id=owner.id, item_id=item_id, moment_set=named, events=[]
        )
        assert not stale.groundings and len(calls) == 2
