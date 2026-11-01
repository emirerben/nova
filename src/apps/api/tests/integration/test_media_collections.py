"""KRI-729/730 HTTP → real PostgreSQL, before the new collection implementation.

Failure cases are preregistered in docs/reviews/camera-roll-recommendations/acceptance.md.
No provider, storage download, upload, render or original backup is permitted here.
"""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update

from app.auth import get_current_user, is_native_client
from app.database import AsyncSessionLocal, get_db
from app.models import (
    ContentPlan,
    Job,
    MediaAsset,
    MediaAssetAnalysis,
    MediaAssetSource,
    PlanItem,
    User,
)
from app.services import media_library_analysis as facts

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Requires disposable Postgres", allow_module_level=True)


@pytest.fixture
async def collection_world(monkeypatch, request):
    from app import storage
    from app.config import settings
    from app.pipeline import clip_cache
    from app.routes import media_collections

    monkeypatch.setattr(settings, "media_library_enabled", True)
    monkeypatch.setattr(settings, "media_collection_proposals_enabled", True)
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: None)
    # No downloads or provider calls: source availability is an exact-generation HEAD.
    monkeypatch.setattr(
        storage, "object_metadata", lambda path: SimpleNamespace(generation="42", size=1234)
    )

    monkeypatch.setattr(
        storage, "object_metadata_once", lambda path, timeout_s: storage.object_metadata(path)
    )

    def no_download(*args, **kwargs):
        raise AssertionError("Collection indexing must not read media bytes")

    monkeypatch.setattr(storage, "download_generation_to_file", no_download)
    monkeypatch.setattr(storage, "download_to_file", no_download)
    monkeypatch.setattr(facts, "_analyze_source", no_download)
    owner = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@collection.test")
    monkeypatch.setattr(settings, "media_collection_cache_only", False)
    other = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@collection.test")
    asset_ids, source_ids = [], []
    async with AsyncSessionLocal() as db:
        db.add_all([owner, other])
        await db.flush()
        for index in range(3):
            asset = MediaAsset(
                user_id=owner.id if index < 2 else other.id,
                kind="video",
                original_availability="device_only",
                status="ready",
                metadata_json={"filename": f"take-{index}.mp4", "duration_s": 180.0},
                creator_context={
                    "capture_time": (
                        datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=index * 5)
                    ).isoformat()
                },
            )
            db.add(asset)
            await db.flush()
            source = MediaAssetSource(
                user_id=asset.user_id,
                asset_id=asset.id,
                role="analysis_proxy",
                object_path=(
                    f"users/{asset.user_id}/media-library/"
                    + ("analysis-proxy-" if getattr(request, "param", None) == "native" else "")
                    + f"{asset.id}.mp4"
                ),
                object_generation="42",
                uploaded_sha256=hashlib.sha256(str(asset.id).encode()).hexdigest(),
                byte_count=1234,
                content_type="video/mp4",
                status="available",
            )
            db.add(source)
            await db.flush()
            ref = facts._Source(
                str(asset.id),
                str(source.id),
                str(asset.user_id),
                "video",
                "analysis_proxy",
                source.object_path,
                "42",
                source.uploaded_sha256,
                "available",
            )
            key = facts._key(ref, None)
            db.add(
                MediaAssetAnalysis(
                    user_id=asset.user_id,
                    asset_id=asset.id,
                    source_id=source.id,
                    analyzer=key.analyzer,
                    model=key.model,
                    prompt_version=key.prompt_version,
                    schema_version=key.schema_version,
                    source_identity=key.source_identity,
                    status="ready",
                    result_json={
                        "understanding": {
                            "visibility": (
                                "Dark evening footage; the illuminated house is the subject"
                            ),
                            "notable_moments": [
                                {
                                    "start_s": 155,
                                    "end_s": 162,
                                    "description": "An illuminated house beside the ferry",
                                },
                                {
                                    "start_s": 9,
                                    "end_s": 8,
                                    "description": "Invalid reversed interval",
                                },
                            ],
                        },
                        "unusable_spans": [
                            {"start_s": 170, "end_s": 171, "reason": "lens covered"}
                        ],
                    },
                )
            )
            transcript_key = facts._key(ref, "transcript")
            db.add(
                MediaAssetAnalysis(
                    user_id=asset.user_id,
                    asset_id=asset.id,
                    source_id=source.id,
                    analyzer=transcript_key.analyzer,
                    model=transcript_key.model,
                    prompt_version=transcript_key.prompt_version,
                    schema_version=transcript_key.schema_version,
                    source_identity=transcript_key.source_identity,
                    status="ready",
                    result_json={
                        "words": [
                            {"word": "We", "start": 170, "end": 170.4},
                            {"word": "finally", "start": 170.3, "end": 170.7},
                            {"word": "arrived.", "start": 170.8, "end": 171.5},
                            {"word": "Invalid", "start": 180, "end": 9999},
                        ],
                    },
                )
            )
            asset_ids.append(str(asset.id))
            source_ids.append(str(source.id))
        await db.commit()
    app = FastAPI()
    app.include_router(media_collections.router)
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[is_native_client] = lambda: True

    async def database():
        async with AsyncSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = database

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, asset_ids, source_ids, owner, app
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PlanItem)
            .where(
                PlanItem.content_plan_id.in_(
                    select(ContentPlan.id).where(ContentPlan.user_id.in_([owner.id, other.id]))
                )
            )
            .values(current_job_id=None)
        )
        await db.execute(delete(Job).where(Job.user_id.in_([owner.id, other.id])))
        await db.execute(delete(User).where(User.id.in_([owner.id, other.id])))
        await db.commit()


def selection(assets, sources, count=2):
    return {
        "name": "Ferry outing",
        "expected_revision": 0,
        "members": [{"asset_id": assets[i], "source_id": sources[i]} for i in range(count)],
    }


async def test_collection_capability_and_routes_share_account_and_rollback_gates(
    collection_world, monkeypatch
):
    from app.config import settings
    from app.routes.creation_threads import router

    client, _, _, owner, app = collection_world
    app.include_router(router, prefix="/creation-threads")
    response = await client.get("/creation-threads/capabilities")
    assert response.status_code == 200, response.text
    assert response.json()["media_collections"] is True
    monkeypatch.setattr(settings, "media_collection_proposals_enabled", False)
    assert (await client.get("/creation-threads/capabilities")).json()["media_collections"] is False
    assert (await client.get(f"/me/media-collections/{uuid.uuid4()}")).status_code == 404


@pytest.mark.parametrize("collection_size", [50, 500])
async def test_story_candidates_cover_large_collections_in_bounded_passes(
    collection_world, monkeypatch, collection_size
):
    from app import storage
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )
    from app.models import MediaSourceMoment
    from app.services.media_moments import FAIR_CANDIDATE_CAP, _index_spec

    client, _, _, owner, _ = collection_world
    asset_ids, source_ids = [], []
    assets, sources, moments = [], [], []
    async with AsyncSessionLocal() as db:
        for index in range(collection_size):
            asset = MediaAsset(
                id=uuid.uuid4(),
                user_id=owner.id,
                kind="video",
                status="ready",
                original_availability="device_only",
                metadata_json={"duration_s": 180},
            )
            assets.append(asset)
            source = MediaAssetSource(
                id=uuid.uuid4(),
                user_id=owner.id,
                asset_id=asset.id,
                role="analysis_proxy",
                object_path=f"users/{owner.id}/fifty/{index}.mp4",
                object_generation="42",
                uploaded_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
                byte_count=1234,
                content_type="video/mp4",
                status="available",
            )
            sources.append(source)
            for part in range(3):
                moments.append(
                    MediaSourceMoment(
                        id=uuid.uuid4(),
                        user_id=owner.id,
                        asset_id=asset.id,
                        source_id=source.id,
                        fact_version="test-frozen-facts",
                        kind="visual",
                        start_s=150 + part * 7,
                        end_s=155 + part * 7,
                        text=f"Source {index} ferry arrival part {part}"
                        + (" finalharbour" if index == collection_size - 1 else ""),
                        uncertain=True,
                        evidence_json={
                            "index_spec": _index_spec("video"),
                            "generation": "42",
                            "uploaded_sha256": source.uploaded_sha256,
                            "complete_timed_speech": True,
                        },
                    )
                )
            asset_ids.append(str(asset.id))
            source_ids.append(str(source.id))
        db.add_all(assets)
        await db.flush()
        db.add_all(sources)
        await db.flush()
        db.add_all(moments)
        await db.commit()
    path = f"/me/media-collections/{uuid.uuid4()}"
    response = await client.put(path, json=selection(asset_ids, source_ids, count=collection_size))
    assert response.status_code == 200, response.text
    searched = await client.get(
        f"{path}/moments", params={"expected_revision": 1, "query": "finalharbour"}
    )
    assert searched.status_code == 200, searched.text
    assert {value["asset_id"] for value in searched.json()["moments"]} == {asset_ids[-1]}
    checked = set()

    def metadata(object_path):
        checked.add(object_path)
        return SimpleNamespace(generation="42", size=1234)

    monkeypatch.setattr(storage, "object_metadata", metadata)
    candidates = []

    def ideas(self, input, *, ctx):
        assert len(input.candidates) <= FAIR_CANDIDATE_CAP
        selected_sources = {value.source for value in input.candidates}
        assert len(selected_sources) == min(collection_size, FAIR_CANDIDATE_CAP)
        assert selected_sources <= set(source_ids)
        if collection_size <= FAIR_CANDIDATE_CAP:
            assert selected_sources == set(source_ids), "Every added source must remain eligible"
        assert source_ids[-1] in selected_sources
        candidates.extend(value.id for value in input.candidates)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title="Ferry arrival",
                    hook="Crossing",
                    payoff="Arrival",
                    rationale="Recorded arrival",
                    moment_ids=[input.candidates[0].id],
                    content_plan=dict(
                        audience="Explorers",
                        angle="An arrival",
                        why_it_works="The opening question resolves at the destination.",
                        opening_text="Where does this lead?",
                        beat_reasons=["Shows the arrival"],
                    ),
                )
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    stories = await client.post(
        f"{path}/stories", json={"expected_revision": 1, "query": "finalharbour"}
    )
    assert stories.status_code == 200, stories.text
    assert len(checked) == min(collection_size, FAIR_CANDIDATE_CAP)
    late = max(searched.json()["moments"], key=lambda value: value["end_s"])
    assert late["id"] not in candidates
    story = stories.json()["stories"][0]
    corrected = await client.patch(
        f"{path}/stories/{story['id']}",
        json={
            "expected_revision": 1,
            "expected_story_revision": 1,
            "client_event_id": "choose-late-interval",
            "replace_index": 0,
            "moment_id": late["id"],
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["moments"][0]["id"] == late["id"]
    last_page = await client.post(
        f"{path}/index", json={"expected_revision": 1, "offset": collection_size - 4, "limit": 32}
    )
    assert last_page.status_code == 200, last_page.text
    assert last_page.json()["next_offset"] is None
    assert set(last_page.json()["missing_analysis_asset_ids"]) == set(asset_ids[-4:])


@pytest.mark.asyncio
async def test_collection_revision_correction_and_resume(collection_world):
    client, assets, sources, _, _ = collection_world
    identifier = uuid.uuid4()
    path = f"/me/media-collections/{identifier}"
    body = selection(assets, sources)
    created = await client.put(path, json=body)
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["revision"] == 1
    assert len(data["members"]) == 2
    # Retransmission is idempotent, including automatic event assignments.
    assert (await client.put(path, json=body)).json() == data
    canonical = {"name": data["name"], "members": data["members"]}
    for revision in (1, 0, 1):
        unchanged = await client.put(path, json={**canonical, "expected_revision": revision})
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json() == data
    future = await client.put(path, json={**canonical, "expected_revision": 999})
    assert future.status_code == 409
    assert future.json()["detail"]["code"] == "collection_changed"
    changed = {
        **body,
        "expected_revision": 1,
        "members": [{"asset_id": assets[0], "source_id": sources[0], "group": "Creator's event"}],
    }
    corrected = await client.put(path, json=changed)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["revision"] == 2
    assert corrected.json()["members"][0]["group"] == "Creator's event"
    assert (await client.get(path)).json() == corrected.json()
    assert (await client.put(path, json=body)).status_code == 409


@pytest.mark.asyncio
async def test_collection_never_accepts_foreign_or_mismatched_sources(collection_world):
    client, assets, sources, _, app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    body = selection(assets, sources, 1)
    body["members"][0]["source_id"] = sources[1]
    assert (await client.put(path, json=body)).status_code == 409
    body["members"] = [{"asset_id": assets[2], "source_id": sources[2]}]
    assert (await client.put(path, json=body)).status_code == 404
    body = selection(assets, sources, 1)
    assert (await client.put(path, json=body)).status_code == 200
    app.dependency_overrides[is_native_client] = lambda: False
    assert (await client.get(path)).status_code == 403


@pytest.mark.asyncio
async def test_late_moments_transcripts_and_invalidated_source(collection_world, monkeypatch):
    from app.models import MediaSourceMoment

    client, assets, sources, owner, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    incomplete = await client.get(
        f"{path}/moments", params={"query": "ferry", "expected_revision": 1}
    )
    assert set(incomplete.json()["unindexed_asset_ids"]) == set(assets[:2])
    first = await client.post(
        f"{path}/index", json={"expected_revision": 1, "offset": 0, "limit": 1}
    )
    assert first.status_code == 200, first.text
    assert first.json()["next_offset"] == 1
    assert first.json()["indexed_sources"] == 1
    second = await client.post(
        f"{path}/index", json={"expected_revision": 1, "offset": 1, "limit": 1}
    )
    assert second.json()["next_offset"] is None
    search = await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    assert search.status_code == 200, search.text
    assert len(search.json()["moments"]) == 2
    assert all(
        row["start_s"] == 155 and row["generation"] == "42" for row in search.json()["moments"]
    )
    speech = await client.get(
        f"{path}/moments", params={"query": "arrived", "expected_revision": 1}
    )
    assert all(row["start_s"] == 170 and row["end_s"] == 171.5 for row in speech.json()["moments"])
    assert speech.json()["moments"][0]["kind"] == "speech"
    assert set(speech.json()["incomplete_speech_asset_ids"]) == set(assets[:2])
    transcript = await client.get(
        f"{path}/sources/{sources[0]}/transcript",
        params={"start_s": 169, "end_s": 173, "expected_revision": 1},
    )
    assert transcript.status_code == 200, transcript.text
    assert transcript.json()["moments"][0]["text"] == "We finally arrived."
    absent = await client.get(
        f"{path}/moments", params={"query": "helicopter", "expected_revision": 1}
    )
    assert absent.json()["moments"] == []
    async with AsyncSessionLocal() as db:
        before = await db.scalar(
            select(func.count())
            .select_from(MediaSourceMoment)
            .where(MediaSourceMoment.user_id == owner.id)
        )
    await client.post(f"{path}/index", json={"expected_revision": 1, "offset": 0, "limit": 2})
    async with AsyncSessionLocal() as db:
        after = await db.scalar(
            select(func.count())
            .select_from(MediaSourceMoment)
            .where(MediaSourceMoment.user_id == owner.id)
        )
        assert before == after == 4, "Repeated index must not duplicate persisted evidence"
        source = await db.get(MediaAssetSource, uuid.UUID(sources[0]))
        source.status = "missing"
        await db.commit()
    fresh = await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    assert len(fresh.json()["moments"]) == 1
    assert fresh.json()["moments"][0]["source_id"] == sources[1]
    assert assets[0] in fresh.json()["unavailable_asset_ids"]
    from app import storage

    monkeypatch.setattr(
        storage, "object_metadata", lambda path: SimpleNamespace(generation="43", size=1234)
    )
    replaced = await client.get(
        f"{path}/moments", params={"query": "ferry", "expected_revision": 1}
    )
    assert replaced.json()["moments"] == [], "Replaced cloud bytes are not usable moments"
    assert (
        await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 0})
    ).status_code == 409


@pytest.mark.asyncio
async def test_bounded_validation_and_default_off(collection_world, monkeypatch):
    from app.config import settings

    client, assets, sources, _, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    body = selection(assets, sources, 1)
    body["members"][0]["photos_identifier"] = "private-device-id"
    assert (await client.put(path, json=body)).status_code == 422
    body = selection(assets, sources, 1)
    body["members"] *= 501
    assert (await client.put(path, json=body)).status_code == 422
    assert (
        await client.post(f"{path}/index", json={"expected_revision": 1, "limit": 51})
    ).status_code == 422
    monkeypatch.setattr(settings, "media_collection_proposals_enabled", False)
    assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_concurrent_corrections_and_removed_member_invalidate_old_search(collection_world):
    client, assets, sources, _, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    initial = selection(assets, sources)
    assert (await client.put(path, json=initial)).status_code == 200
    await client.post(f"{path}/index", json={"expected_revision": 1})
    left = {**initial, "expected_revision": 1, "name": "Left"}
    right = {**initial, "expected_revision": 1, "name": "Right"}
    results = await asyncio.gather(client.put(path, json=left), client.put(path, json=right))
    assert sorted(result.status_code for result in results) == [200, 409]
    changed = {**initial, "expected_revision": 2, "members": [initial["members"][1]]}
    assert (await client.put(path, json=changed)).status_code == 200
    found = await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 3})
    assert len(found.json()["moments"]) == 1
    assert found.json()["moments"][0]["asset_id"] == assets[1]


@pytest.mark.asyncio
async def test_version_change_and_transient_storage_failure_are_explicit(
    collection_world, monkeypatch
):
    from app import storage

    client, assets, sources, _, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})

    def temporary_failure(path):
        raise TimeoutError("Temporary storage failure")

    monkeypatch.setattr(storage, "object_metadata", temporary_failure)
    response = await client.get(
        f"{path}/moments", params={"query": "ferry", "expected_revision": 1}
    )
    assert response.status_code == 503
    async with AsyncSessionLocal() as db:
        assert (await db.get(MediaAssetSource, uuid.UUID(sources[0]))).status == "available"
    monkeypatch.setattr(facts, "_FACT_SCHEMA_SUFFIX", "incompatible-test-version")
    response = await client.get(
        f"{path}/moments", params={"query": "ferry", "expected_revision": 1}
    )
    assert response.json()["moments"] == []
    assert set(response.json()["unindexed_asset_ids"]) == set(assets[:2])


@pytest.mark.asyncio
async def test_visual_index_never_claims_complete_speech_coverage(collection_world):
    client, assets, sources, owner, _ = collection_world
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(MediaAssetAnalysis).where(
                MediaAssetAnalysis.user_id == owner.id,
                MediaAssetAnalysis.analyzer == facts._spec("video", "transcript")[0],
            )
        )
        await db.commit()
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    response = await client.get(
        f"{path}/moments", params={"query": "arrived", "expected_revision": 1}
    )
    assert response.json()["moments"] == []
    assert set(response.json()["incomplete_speech_asset_ids"]) == set(assets[:2])


@pytest.mark.parametrize("manual_qa", [False, True])
async def test_semantic_retrieval_is_bounded_cached_and_source_fenced(
    collection_world, monkeypatch, manual_qa
):
    from app.agents.media_moment_relevance import MomentRelevanceAgent, MomentRelevanceOutput
    from app.config import settings

    client, assets, sources, owner, _app = collection_world
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_manual_qa_reservation_approved", manual_qa)
    monkeypatch.setattr(
        settings, "ai_manual_qa_test_run_id", "camera-roll-approved-test" if manual_qa else ""
    )
    monkeypatch.setattr(settings, "ai_manual_qa_max_cost_usd", 2 if manual_qa else None)
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    calls = []

    def rank(self, input, *, ctx):
        calls.append(input)
        assert len(input.candidates) <= 100
        assert ctx.creator_id == str(owner.id)
        assert ctx.estimated_max_cost_usd == (2 if manual_qa else 0.05)
        assert ctx.reservation_approved is manual_qa
        assert ctx.test_run_id == ("camera-roll-approved-test" if manual_qa else None)
        assert any("Dark evening" in value.quality_notes for value in input.candidates)
        assert any("lens covered" in value.quality_notes for value in input.candidates)
        return MomentRelevanceOutput(moment_ids=[input.candidates[-1].id, str(uuid.uuid4())])

    monkeypatch.setattr(MomentRelevanceAgent, "run", rank)
    body = {"expected_revision": 1, "query": "arrival by boat"}
    monkeypatch.setattr(settings, "media_collection_cache_only", True)
    forced = await client.post(f"{path}/semantic-search", json=body)
    assert forced.status_code == 409 and not calls
    monkeypatch.setattr(settings, "media_collection_cache_only", False)
    cold = await client.post(f"{path}/semantic-search", json={**body, "cache_only": True})
    assert cold.status_code == 409 and not calls
    response = await client.post(f"{path}/semantic-search", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["method"] == "semantic-rerank-v1"
    assert len(result["moments"]) == 1, "Untrusted agent IDs cannot add new evidence"
    assert result["moments"][0]["uncertain"] is True
    repeated = await client.post(f"{path}/semantic-search", json={**body, "cache_only": True})
    assert repeated.json()["moments"] == result["moments"] or len(repeated.json()["moments"]) == 1
    assert len(calls) == 1, "Repeated immutable candidate/query sets reuse durable results"
    await client.put(
        path, json={**selection(assets, sources), "expected_revision": 1, "members": []}
    )
    stale = await client.post(f"{path}/semantic-search", json=body)
    assert stale.status_code == 409
    assert len(calls) == 1


async def test_targeted_inspection_reuses_exact_interval_and_checks_revision(
    collection_world, monkeypatch
):
    from app.services import media_moment_inspection

    client, assets, sources, owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    found = (
        await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    ).json()["moments"][0]
    calls = []

    def inspect(source, moment, question, *, ctx):
        calls.append((source.id, moment.start_s, moment.end_s, question))
        assert moment.end_s - moment.start_s <= 20
        assert ctx.estimated_max_cost_usd == 0.05
        return {
            "answer": "yes",
            "confidence": 0.9,
            "evidence": "Ferry crossing the water",
            "sample_sha256": "a" * 64,
        }

    monkeypatch.setattr(media_moment_inspection, "inspect_interval", inspect)
    body = {"expected_revision": 1, "moment_id": found["id"], "question": "Is a ferry visible?"}
    cold = await client.post(f"{path}/inspect", json={**body, "cache_only": True})
    assert cold.status_code == 409 and not calls
    result = await client.post(f"{path}/inspect", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["source_id"] == found["source_id"]
    assert result.json()["verification"] == "targeted_model_inspection"
    assert result.json()["uncertain"] is True, (
        "A model answer is not independent output verification"
    )
    assert result.json()["start_s"] == found["start_s"]
    again = await client.post(f"{path}/inspect", json={**body, "cache_only": True})
    assert again.status_code == 200
    assert len(calls) == 1
    wrong = await client.post(f"{path}/inspect", json={**body, "moment_id": str(uuid.uuid4())})
    assert wrong.status_code == 404
    await client.put(
        path, json={**selection(assets, sources), "expected_revision": 1, "members": []}
    )
    stale = await client.post(f"{path}/inspect", json=body)
    assert stale.status_code == 409
    assert len(calls) == 1


async def test_story_candidates_corrections_and_rejection_are_separate_from_render(
    collection_world, monkeypatch
):
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )
    from app.models import CreatorAgentApproval, Job, MediaCollectionStory

    client, assets, sources, owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    calls = []

    def ideas(self, input, *, ctx):
        calls.append(input)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title="An arrival",
                    hook="Start on the ferry",
                    payoff="Finish on the arrival",
                    rationale="The crossing leads into the recorded arrival.",
                    moment_ids=[input.candidates[0].id, input.candidates[-1].id],
                    content_plan=dict(
                        audience="Explorers",
                        angle="A crossing and arrival",
                        why_it_works="The question is resolved by the arrival.",
                        opening_text="Where does this lead?",
                        beat_reasons=["Starts the crossing", "Reveals the destination"],
                    ),
                )
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    body = {"expected_revision": 1, "query": "Tell the story of this outing"}
    cold = await client.post(f"{path}/stories", json={**body, "cache_only": True})
    assert cold.status_code == 409 and not calls
    assert cold.json()["detail"]["code"] == "collection_analysis_unavailable"
    result = await client.post(f"{path}/stories", json=body)
    assert result.status_code == 200, result.text
    first = result.json()["stories"][0]
    assert first["revision"] == 1
    assert first["requires_render_approval"] is True
    assert first["on_screen_copy"] == ["Where does this lead?"]
    exposure = {"expected_revision": 1, "expected_story_revision": 1, "client_event_id": "shown-1"}
    for _ in range(2):
        shown = await client.post(f"{path}/stories/{first['id']}/expose", json=exposure)
        assert shown.status_code == 200, shown.text
    # KRI-853: the native screen saves again before requesting stories on reopen.
    # Canonical selection returned by GET includes the server-assigned groups.
    persisted = (await client.get(path)).json()
    reopened = await client.put(
        path,
        json={
            "name": persisted["name"],
            "members": persisted["members"],
            "expected_revision": 1,
        },
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["revision"] == 1, "Unchanged resume must preserve cached story identity"
    again = await client.post(f"{path}/stories", json={**body, "cache_only": True})
    assert again.json()["stories"][0]["id"] == first["id"]
    assert len(calls) == 1
    choices = calls[0].candidates
    replacement = next(
        value.id for value in choices if value.id not in [value["id"] for value in first["moments"]]
    )
    correction = {
        "expected_revision": 1,
        "expected_story_revision": 1,
        "replace_index": 0,
        "moment_id": replacement,
        "client_event_id": "correction-1",
    }
    changed = await client.patch(f"{path}/stories/{first['id']}", json=correction)
    assert changed.status_code == 200, changed.text
    revised = changed.json()
    assert revised["revision"] == 2
    assert revised["moments"][0]["id"] == replacement
    assert revised["moments"][1] == first["moments"][1]
    assert revised["hook"] == first["hook"]
    stale = await client.patch(
        f"{path}/stories/{first['id']}", json={**correction, "client_event_id": "correction-2"}
    )
    assert stale.status_code == 409
    sound = await client.patch(
        f"{path}/stories/{first['id']}",
        json={
            "expected_revision": 1,
            "expected_story_revision": 2,
            "client_event_id": "sound-1",
            "audio_policy": "muted",
        },
    )
    assert sound.status_code == 200, sound.text
    assert sound.json()["moments"] == revised["moments"]
    assert sound.json()["audio_policy"] == "muted"
    reordered = await client.patch(
        f"{path}/stories/{first['id']}",
        json={
            "expected_revision": 1,
            "expected_story_revision": 3,
            "client_event_id": "order-1",
            "moment_order": [value["id"] for value in reversed(revised["moments"])],
        },
    )
    assert reordered.status_code == 200, reordered.text
    assert reordered.json()["moments"] == list(reversed(revised["moments"]))
    assert reordered.json()["audio_policy"] == "muted"
    assert reordered.json()["hook"] == revised["hook"]
    rejected = await client.post(
        f"{path}/stories/{first['id']}/reject",
        json={"expected_revision": 1, "expected_story_revision": 4, "client_event_id": "reject-1"},
    )
    assert rejected.status_code == 200
    async with AsyncSessionLocal() as db:
        row = await db.get(MediaCollectionStory, uuid.UUID(first["id"]))
        assert [event["type"] for event in row.events_json] == [
            "offered",
            "exposure",
            "correction",
            "correction",
            "correction",
            "rejection",
        ]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CreatorAgentApproval)
                .where(CreatorAgentApproval.creator_id == owner.id)
            )
            == 0
        )
        assert (
            await db.scalar(select(func.count()).select_from(Job).where(Job.user_id == owner.id))
            == 0
        )


@pytest.mark.parametrize("collection_world", ["native"], indirect=True)
async def test_recommended_story_compiles_exact_source_windows_without_copy(
    collection_world, monkeypatch
):
    from app.config import settings
    from app.models import MediaCollectionStory, PlanItem
    from app.pipeline.guided_story import compile_proposal_execution_plan
    from app.services.media_collection_preparation import prepare_story_snapshot
    from tests.services.test_phone_sources import receipt

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    client, assets, sources, owner, _app = collection_world

    def attached_original(index):
        value = receipt(f"clip-{index}")
        value.update(
            gcs_path=f"users/{owner.id}/media-library/analysis-proxy-{assets[index]}.mp4",
            storage_generation="42",
            duration_s=180,
            library_asset_id=assets[index],
            library_source_id=sources[index],
        )
        value["upload_contract"]["proxy"]["duration_s"] = 180
        value["upload_contract"]["proxy"]["original"]["duration_s"] = 180
        return value

    path = f"/me/media-collections/{uuid.uuid4()}"
    collection_id = uuid.UUID(path.rsplit("/", 1)[1])
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    moments = (
        await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    ).json()["moments"]
    async with AsyncSessionLocal() as db:
        row = MediaCollectionStory(
            id=uuid.uuid4(),
            collection_id=collection_id,
            user_id=owner.id,
            collection_revision=1,
            revision=1,
            story_json={
                "title": "Unconfirmed AI summary",
                "hook": "Crossing",
                "payoff": "Arrival",
                "rationale": "A journey",
            },
            moments_json=moments,
            events_json=[],
        )
        item = PlanItem(clip_assignments=[attached_original(index) for index in range(2)])
        prepared = await prepare_story_snapshot(db, owner.id, row, item)
        plan = compile_proposal_execution_plan(prepared.snapshot)
        assert plan["text_elements"] == [], "AI summaries must never silently become captions"
        windows = [
            (value["source_start_s"], value["source_end_s"]) for value in plan["story_timeline"]
        ]
        assert windows == [(value["start_s"], value["end_s"]) for value in moments]
        assert all((value["playback_rate"] or 1) == 1 for value in plan["story_timeline"])
        assert prepared.snapshot.montage_audio.preserve_source_audio is True
        item.clip_assignments = [
            {**value, "library_source_id": str(uuid.uuid4())} for value in item.clip_assignments
        ]
        with pytest.raises(Exception, match="source"):
            await prepare_story_snapshot(db, owner.id, row, item)


@pytest.mark.parametrize("localizer", [False, True])
def test_real_targeted_read_crops_bytes_before_one_bounded_model_call(
    tmp_path, monkeypatch, localizer
):
    import shutil
    import subprocess
    import time

    from google import genai
    from PIL import Image

    from app import storage
    from app.agents._runtime import RunContext
    from app.agents.clip_question import ClipQuestionAgent, ClipQuestionOutput
    from app.services.media_moment_inspection import inspect_interval

    source_file = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:d=4:r=30",
            "-vf",
            "negate=enable='gte(t,2)'",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            str(source_file),
        ],
        check=True,
        capture_output=True,
    )
    source = SimpleNamespace(
        object_path="owned-source",
        object_generation="42",
        byte_count=source_file.stat().st_size,
        uploaded_sha256=hashlib.sha256(source_file.read_bytes()).hexdigest(),
    )
    uploads, calls = [], []

    def download(path, target, *, generation, timeout_s):
        assert path == "owned-source" and generation == "42" and timeout_s <= 15
        shutil.copyfile(source_file, target)

    monkeypatch.setattr(storage, "download_generation_to_file", download)

    class Files:
        def upload(self, *, file, config):
            assert config.http_options.timeout <= 5000
            assert config.http_options.retry_options.attempts == 1
            uploads.append(Path(file).read_bytes())
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    file,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            assert abs(float(probe.stdout) - 1) <= 1 / 30
            png = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    file,
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "-",
                ],
                check=True,
                capture_output=True,
            ).stdout
            import io

            color = Image.open(io.BytesIO(png)).getpixel((40, 40))
            assert color[1] > 200 and color[2] > 200 and color[0] < 30, (
                "Only the late cyan interval reaches the provider"
            )
            return SimpleNamespace(
                uri="fixture://targeted", name="files/fixture", state=SimpleNamespace(name="ACTIVE")
            )

        def delete(self, **kwargs):
            return None

    monkeypatch.setattr(
        genai, "Client", lambda **kwargs: SimpleNamespace(files=Files(), close=lambda: None)
    )

    def answer(self, input, *, ctx):
        assert self.spec.max_attempts == 1
        assert self.spec.enable_clarification_retries is False
        assert input.file_uri == "fixture://targeted"
        calls.append(input)
        return ClipQuestionOutput(answer="yes", confidence=0.9, evidence="A cyan frame")

    monkeypatch.setattr(ClipQuestionAgent, "run", answer)
    factory = None
    if localizer:
        from app.agents.source_moment_location import (
            SourceMomentLocationAgent,
            SourceMomentLocationOutput,
        )

        def locate(self, input, *, ctx):
            answer(self, input, ctx=ctx)
            return SourceMomentLocationOutput.model_validate(
                {
                    "status": "found",
                    "matches": [
                        {"start_s": 0, "end_s": 1, "confidence": 0.9, "evidence": "A cyan frame"}
                    ],
                }
            )

        monkeypatch.setattr(SourceMomentLocationAgent, "run", locate)
        factory = SourceMomentLocationAgent
    result = inspect_interval(
        source,
        SimpleNamespace(start_s=2, end_s=3),
        "Is this cyan?",
        ctx=RunContext(deadline_monotonic=time.monotonic() + 45),
        agent_factory=factory,
    )
    assert len(uploads) == len(calls) == 1
    assert result["sample_sha256"] == hashlib.sha256(uploads[0]).hexdigest()


@pytest.mark.parametrize("operation", ["semantic-search", "stories", "inspect"])
async def test_distinct_collection_requests_share_a_source_without_cache_collisions(
    collection_world, monkeypatch, operation
):
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )
    from app.agents.media_moment_relevance import MomentRelevanceAgent, MomentRelevanceOutput
    from app.services import media_moment_inspection

    client, assets, sources, _owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    selected = selection(assets, sources)
    selected["members"] = selected["members"][:1]
    assert (await client.put(path, json=selected)).status_code == 200
    assert (await client.post(f"{path}/index", json={"expected_revision": 1})).status_code == 200
    moments = (
        await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    ).json()["moments"]
    calls = []

    def rank(self, input, *, ctx):
        calls.append(input.query)
        return MomentRelevanceOutput(moment_ids=[input.candidates[0].id])

    def stories(self, input, *, ctx):
        calls.append(input.query)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title="Outing",
                    hook="An arrival",
                    payoff="The destination",
                    rationale="The recorded journey",
                    moment_ids=[input.candidates[0].id],
                    content_plan=dict(
                        audience="Explorers",
                        angle="An arrival",
                        why_it_works="The opening question resolves at the destination.",
                        opening_text="Where does this lead?",
                        beat_reasons=["Shows the arrival"],
                    ),
                )
            ]
        )

    def inspect(source, moment, question, *, ctx):
        calls.append(question)
        return {
            "answer": "Visible",
            "confidence": 0.8,
            "evidence": "Recorded footage",
            "sample_sha256": "a" * 64,
        }

    monkeypatch.setattr(MomentRelevanceAgent, "run", rank)
    monkeypatch.setattr(CameraRollStoriesAgent, "run", stories)
    monkeypatch.setattr(media_moment_inspection, "inspect_interval", inspect)
    for query in ["first direction", "second direction", "third direction"]:
        body = {"expected_revision": 1, "query": query}
        if operation == "inspect":
            body = {"expected_revision": 1, "moment_id": moments[0]["id"], "question": query}
        result = await client.post(f"{path}/{operation}", json=body)
        assert result.status_code == 200, result.text
        cached = await client.post(f"{path}/{operation}", json={**body, "cache_only": True})
        assert cached.status_code == 200, cached.text
    assert len(calls) == 3
    if operation == "stories":
        from dataclasses import replace

        previous_id = result.json()["stories"][0]["id"]
        monkeypatch.setattr(
            CameraRollStoriesAgent,
            "spec",
            replace(CameraRollStoriesAgent.spec, prompt_version="new-creative-plan"),
        )
        refreshed = await client.post(f"{path}/stories", json=body)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["stories"][0]["id"] != previous_id
        assert len(calls) == 4
    if operation == "inspect":
        fourth = await client.post(f"{path}/inspect", json={**body, "question": "fourth direction"})
        assert fourth.status_code == 429
        assert len(calls) == 3


async def test_invalid_story_generation_returns_private_retry_response(
    collection_world, monkeypatch
):
    from app.agents._runtime import TerminalSchemaError
    from app.agents.camera_roll_stories import CameraRollStoriesAgent

    client, assets, sources, _owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    assert (await client.post(f"{path}/index", json={"expected_revision": 1})).status_code == 200

    attempts = []

    def invalid(self, input, *, ctx):
        attempts.append(ctx.request_id)
        raise TerminalSchemaError("private provider response must not leak")

    monkeypatch.setattr(CameraRollStoriesAgent, "run", invalid)
    response = await client.post(f"{path}/stories", json={"expected_revision": 1, "query": "ferry"})
    assert response.status_code == 503
    assert "private provider" not in response.text
    retry = await client.post(f"{path}/stories", json={"expected_revision": 1, "query": "ferry"})
    assert retry.status_code == 503
    assert len(attempts) == 2
    assert attempts[0] != attempts[1], "A retried lease needs a new paid-call identity"


async def test_research_ideas_keep_public_search_private_and_reuse_reviewed_evidence(
    collection_world, monkeypatch
):
    """Prerecorded provider boundaries; no live research or quality claim."""
    from app.agents.camera_roll_stories import CameraRollStoriesAgent
    from app.services import story_idea_research as research
    from app.services.researched_text import GroundedResearchAgent, ResearchEvidence

    client, assets, sources, owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    calls = []
    approved = True
    source_approved = True
    source_url = "https://si.edu/terminal"

    def search(self, value, *, ctx):
        calls.append(("search", value.model_dump()))
        assert value.topic == "History of the public ferry terminal"
        assert "private direction" not in value.model_dump_json()
        assert ctx.deadline_monotonic is not None
        return ResearchEvidence(
            facts=[
                dict(
                    id="fact-0",
                    text="The terminal was rebuilt after a fire.",
                    sources=[dict(title="Public archive", url=source_url)],
                )
            ],
            retrieved_at="2026-10-21T00:00:00+00:00",
            search_entry_html="<div>Recorded provider attribution</div>",
        )

    def ideas(self, value, *, ctx):
        calls.append(("ideas", None))
        assert value.public_research.facts[0].id == "fact-0"
        return self.parse(
            json.dumps(
                {
                    "stories": [
                        {
                            "title": "A terminal rebuilt",
                            "moment_ids": ["m1"],
                            "research_fact_ids": ["fact-0"],
                            "content_plan": {
                                "form": "factual_story",
                                "audience": "Ferry passengers",
                                "angle": "The terminal's rebuilding",
                                "why_it_works": "History gives this crossing a point.",
                                "opening_text": "This terminal was rebuilt after a fire",
                                "caption": "A piece of terminal history.",
                                "beat_reasons": [
                                    "Crossing footage supplies atmosphere, not evidence of a fire"
                                ],
                            },
                        }
                    ]
                }
            ),
            value,
        )

    def review(self, value, *, ctx):
        calls.append(
            (
                "source_review" if isinstance(self, research.StorySourceReviewAgent) else "review",
                None,
            )
        )
        return research.StoryResearchReviewOutput(
            approved=source_approved
            if isinstance(self, research.StorySourceReviewAgent)
            else approved,
            reason="Recorded review",
        )

    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    monkeypatch.setattr(research.StoryResearchReviewAgent, "run", review)
    monkeypatch.setattr(research.StorySourceReviewAgent, "run", review)
    body = dict(
        expected_revision=1,
        query="My private direction for a ferry outing",
        research_topic="History of the public ferry terminal",
    )
    cold = await client.post(f"{path}/stories", json={**body, "cache_only": True})
    assert cold.status_code == 409 and not calls
    response = await client.post(f"{path}/stories", json=body)
    assert response.status_code == 200, response.text
    story = response.json()["stories"][0]
    assert story["research"]["basis"] == "background_context"
    assert (
        story["research"]["evidence"]["facts"][0]["sources"][0]["url"] == "https://si.edu/terminal"
    )
    assert story["research"]["evidence"]["search_entry_html"]
    assert story["requires_render_approval"]
    assert [row[0] for row in calls] == ["search", "ideas", "source_review", "review"]
    cached = await client.post(f"{path}/stories", json={**body, "cache_only": True})
    assert cached.status_code == 200 and cached.json()["stories"][0] == story
    assert len(calls) == 4
    changed_plan = {**story["content_plan"], "opening_text": "My revised opening"}
    changed = await client.patch(
        f"{path}/stories/{story['id']}",
        json=dict(
            expected_revision=1,
            expected_story_revision=1,
            client_event_id="research-copy-edit",
            content_plan=changed_plan,
        ),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["research"]["review_current"] is False
    assert changed.json()["research"]["evidence"] == story["research"]["evidence"]
    approved = False
    rejected = await client.post(f"{path}/stories", json={**body, "query": "A different direction"})
    assert rejected.status_code == 200 and rejected.json()["stories"] == [], (
        "an idea the review rejects is dropped, and the creator sees no idea, not a server error"
    )
    assert len(calls) == 8
    invalid = await client.post(
        f"{path}/stories", json={**body, "research_topic": "https://private.local/x"}
    )
    assert invalid.status_code == 422 and len(calls) == 8
    source_approved = False
    unsupported = await client.post(
        f"{path}/stories", json={**body, "query": "Another source selection"}
    )
    assert unsupported.status_code == 200 and unsupported.json()["stories"] == []
    assert len(calls) == 11
    assert [row[0] for row in calls[-3:]] == ["search", "ideas", "source_review"]
    source_url = "https://travel-blog.example/terminal"
    ungrounded = await client.post(f"{path}/stories", json={**body, "query": "A third direction"})
    assert ungrounded.status_code == 503 and len(calls) == 12


async def test_one_rejected_research_idea_never_costs_the_creator_the_others(
    collection_world, monkeypatch
):
    """Failure catalogue: one flagged idea rejecting the whole researched batch, so the
    creator sees "Kria hit a problem" although other ideas passed; a reviewer timeout
    failing the request instead of dropping only the unreviewed idea."""
    from app.agents._runtime import ProviderDeadlineExceededError
    from app.agents.camera_roll_stories import CameraRollStoriesAgent
    from app.services import story_idea_research as research
    from app.services.researched_text import GroundedResearchAgent, ResearchEvidence

    client, assets, sources, _owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})

    def search(self, value, *, ctx):
        return ResearchEvidence(
            facts=[
                dict(
                    id="fact-0",
                    text="The terminal was rebuilt after a fire.",
                    sources=[dict(title="Public archive", url="https://si.edu/terminal")],
                )
            ],
            retrieved_at="2026-10-21T00:00:00+00:00",
            search_entry_html="<div>Recorded provider attribution</div>",
        )

    def idea(title, opening):
        return {
            "title": title,
            "moment_ids": ["m1"],
            "research_fact_ids": ["fact-0"],
            "content_plan": {
                "form": "factual_story",
                "audience": "Ferry passengers",
                "angle": f"{title} angle",
                "why_it_works": "History gives this crossing a point.",
                "opening_text": opening,
                "caption": f"{title} caption.",
                "beat_reasons": ["Crossing footage supplies atmosphere"],
            },
        }

    def ideas(self, value, *, ctx):
        return self.parse(
            json.dumps(
                {
                    "stories": [
                        idea("Supported", "This terminal was rebuilt after a fire"),
                        idea("Flagged", "This museum holds the fire's records"),
                        idea("Unreviewed", "The ferry crossing after the rebuild"),
                    ]
                }
            ),
            value,
        )

    def review(self, value, *, ctx):
        title = value.proposed.stories[0].title
        if title == "Unreviewed":
            raise ProviderDeadlineExceededError("review timed out")
        return research.StoryResearchReviewOutput(
            approved=title != "Flagged", reason="Recorded review"
        )

    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    monkeypatch.setattr(research.StoryResearchReviewAgent, "run", review)
    monkeypatch.setattr(research.StorySourceReviewAgent, "run", review)
    response = await client.post(
        f"{path}/stories",
        json=dict(
            expected_revision=1,
            query="A ferry outing",
            research_topic="History of the public ferry terminal",
        ),
    )
    assert response.status_code == 200, response.text
    assert [s["title"] for s in response.json()["stories"]] == ["Supported"], (
        "only the flagged and the unreviewed ideas are dropped"
    )


async def test_place_in_query_suggests_a_topic_and_only_the_confirmed_topic_reaches_search(
    collection_world, monkeypatch
):
    """The suggestion is a prefill: nothing is searched until the creator sends the topic."""
    from app.agents.camera_roll_stories import CameraRollStoriesAgent
    from app.services import story_idea_research as research
    from app.services.researched_text import GroundedResearchAgent, ResearchEvidence

    client, assets, sources, _owner, _app = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    searches = []

    def search(self, value, *, ctx):
        searches.append(value.model_dump())
        return ResearchEvidence(
            facts=[
                dict(
                    id="fact-0",
                    text="The city was excavated in the nineteenth century.",
                    sources=[dict(title="Public archive", url="https://si.edu/troy")],
                )
            ],
            retrieved_at="2026-10-21T00:00:00+00:00",
            search_entry_html="<div>Recorded provider attribution</div>",
        )

    def ideas(self, value, *, ctx):
        plan = {
            "audience": "Ferry passengers",
            "angle": "The crossing",
            "why_it_works": "A plain arrival.",
            "opening_text": "We finally arrived" + (" at Troy" if value.public_research else ""),
            "caption": "An arrival.",
            "beat_reasons": ["Crossing footage supplies atmosphere"],
        }
        story = {"title": "An arrival", "moment_ids": ["m1"], "content_plan": plan}
        if value.public_research:
            story["research_fact_ids"] = ["fact-0"]
            plan["form"] = "factual_story"
        else:
            plan["form"] = "atmosphere"
        return self.parse(json.dumps({"stories": [story]}), value)

    def approve(self, value, *, ctx):
        return research.StoryResearchReviewOutput(approved=True, reason="Recorded review")

    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    monkeypatch.setattr(research.StoryResearchReviewAgent, "run", approve)
    monkeypatch.setattr(research.StorySourceReviewAgent, "run", approve)

    async def ask(**fields):
        response = await client.post(f"{path}/stories", json={"expected_revision": 1, **fields})
        assert response.status_code == 200, response.text
        return response.json()

    plain = await ask(query="Suggest different content ideas from this outing")
    assert plain["stories"] and "suggested_research_topic" not in plain
    lowercase = await ask(query="a ferry outing with my friends on monday")
    assert "suggested_research_topic" not in lowercase

    place = await ask(query="A day trip to Troy")
    assert place["suggested_research_topic"] == "Troy"
    assert place["stories"][0].get("research") is None
    assert searches == [], "Suggesting a topic must not search"

    confirmed = await ask(
        query="A day trip to Troy", research_topic=place["suggested_research_topic"]
    )
    assert searches == [{"topic": "Troy", "request_quote": "Troy"}]
    assert "suggested_research_topic" not in confirmed
    assert confirmed["stories"][0]["research"]["topic"] == "Troy"
    assert confirmed["stories"][0]["research"]["evidence"]["facts"][0]["sources"] == [
        {"title": "Public archive", "url": "https://si.edu/troy"}
    ]


async def test_visual_end_overshoot_retains_in_bounds_footage_without_trimming_speech(
    collection_world,
):
    client, assets, sources, owner, _ = collection_world
    async with AsyncSessionLocal() as db:
        asset = await db.get(MediaAsset, uuid.UUID(assets[0]))
        asset.metadata_json = {**asset.metadata_json, "duration_s": 1.933333}
        rows = (
            await db.scalars(
                select(MediaAssetAnalysis).where(
                    MediaAssetAnalysis.user_id == owner.id,
                    MediaAssetAnalysis.source_id == uuid.UUID(sources[0]),
                )
            )
        ).all()
        for row in rows:
            if row.analyzer == "nova.media.transcript":
                row.result_json = {"words": [{"start": 0, "end": 3, "word": "Unfinished."}]}
            else:
                row.result_json = {
                    "understanding": {
                        "notable_moments": [
                            {"start_s": 0, "end_s": 3, "description": "People examine rugs"},
                            {"start_s": 2, "end_s": 4, "description": "Wrong out-of-bounds scene"},
                            {"start_s": 1, "end_s": 0.5, "description": "Wrong reversed scene"},
                        ]
                    }
                }
        await db.commit()
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources, count=1))).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["missing_analysis_asset_ids"] == []
    found = await client.get(f"{path}/moments", params={"expected_revision": 1, "query": "rugs"})
    assert [(m["start_s"], m["end_s"], m["kind"]) for m in found.json()["moments"]] == [
        (0, 1.933333, "visual")
    ]
    wrong = await client.get(f"{path}/moments", params={"expected_revision": 1, "query": "Wrong"})
    assert wrong.json()["moments"] == []
    speech = await client.get(
        f"{path}/moments", params={"expected_revision": 1, "query": "Unfinished"}
    )
    assert speech.json()["moments"] == [], "A whole word cannot be trimmed to fit a clip"


async def test_incomplete_collection_waits_before_any_story_call_and_recovers(
    collection_world, monkeypatch
):
    from app.agents.camera_roll_stories import CameraRollStoriesAgent, CameraRollStoriesOutput

    client, assets, sources, _owner, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    assert (
        await client.post(f"{path}/index", json={"expected_revision": 1, "limit": 1})
    ).status_code == 200
    calls = []
    invalidate_coverage = False

    def no_early_call(self, input, *, ctx):
        calls.append(input)
        if invalidate_coverage:
            from app.database import sync_session
            from app.models import MediaSourceMoment

            with sync_session() as db:
                db.execute(
                    delete(MediaSourceMoment).where(
                        MediaSourceMoment.asset_id == uuid.UUID(assets[1])
                    )
                )
                db.commit()
        return CameraRollStoriesOutput(stories=[])

    monkeypatch.setattr(CameraRollStoriesAgent, "run", no_early_call)
    request = {"expected_revision": 1, "query": "A useful guide to the whole outing"}
    waiting = await client.post(f"{path}/stories", json=request)
    assert waiting.status_code == 409, waiting.text
    assert "analysis" in waiting.json()["detail"].lower()
    assert calls == [], "A partial collection must not acquire a story provider call"
    partial = await client.post(f"{path}/stories", json={**request, "allow_partial_analysis": True})
    assert partial.status_code == 200, partial.text
    assert len(calls) == 1 and partial.json()["coverage"]["unindexed_asset_ids"] == [assets[1]]
    still_waiting = await client.post(f"{path}/stories", json={**request, "cache_only": True})
    assert still_waiting.status_code == 409, (
        "An explicit partial cache is not full-collection coverage"
    )
    assert len(calls) == 1
    assert (
        await client.post(f"{path}/index", json={"expected_revision": 1, "offset": 1})
    ).status_code == 200
    ready = await client.post(f"{path}/stories", json=request)
    assert ready.status_code == 200, ready.text
    assert len(calls) == 2 and len(calls[-1].candidates) >= 2
    # An untimed photo is a supported collection member, not a video awaiting
    # analysis. It must not make the video-only story adapter wait forever.
    client, assets, sources, owner, _ = collection_world
    async with AsyncSessionLocal() as db:
        photo = MediaAsset(
            user_id=owner.id,
            kind="image",
            status="ready",
            original_availability="device_only",
            metadata_json={},
        )
        db.add(photo)
        await db.flush()
        source = MediaAssetSource(
            user_id=owner.id,
            asset_id=photo.id,
            role="original",
            object_path=f"users/{owner.id}/photo.jpg",
            object_generation="42",
            uploaded_sha256="f" * 64,
            byte_count=1234,
            content_type="image/jpeg",
            status="available",
        )
        db.add(source)
        await db.commit()
        photo_id, source_id = str(photo.id), str(source.id)
    body = selection(assets, sources)
    body["expected_revision"] = 1
    body["members"].append({"asset_id": photo_id, "source_id": source_id})
    assert (await client.put(path, json=body)).status_code == 200
    mixed = await client.post(f"{path}/stories", json={**request, "expected_revision": 2})
    assert mixed.status_code == 200, mixed.text
    assert len(calls) == 3 and len(calls[-1].candidates) >= 2

    # Facts can be invalidated while the provider is working. Full coverage
    # must still hold at the final offer boundary, even when selected IDs survive.
    invalidate_coverage = True
    changed = await client.post(
        f"{path}/stories",
        json={**request, "expected_revision": 2, "query": "Another guide to this outing"},
    )
    assert changed.status_code == 409, changed.text
    assert len(calls) == 4


@pytest.mark.parametrize("copy_clone", [False, True])
async def test_distinct_ideas_can_share_footage_without_sharing_review_state(
    collection_world, monkeypatch, copy_clone
):
    """Real planner/parser/persistence, with only the provider transport replayed."""
    import copy

    from app.models import CreatorAgentApproval
    from app.services import media_collection_stories
    from tests.evals.runners.eval_runner import CassetteModelClient

    client, assets, sources, owner, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    assert (await client.post(f"{path}/index", json={"expected_revision": 1})).status_code == 200
    plans = [
        (
            "A crossing guide",
            "Follow the recorded crossing to its final view",
            "The final view from the ferry",
            "A crossing and its arrival.",
        ),
        (
            "The light at the end",
            "Notice the illuminated house against the evening scene",
            "The house is still lit",
            "The light stands out after dark.",
        ),
        (
            "An evening on the water",
            "An atmosphere edit of the evening crossing",
            "On the ferry after dark",
            "An evening crossing.",
        ),
    ]
    proposals = [
        dict(
            title=title,
            moment_ids=["m1"],
            content_plan=dict(
                form=form,
                audience="Evening travellers",
                angle=angle,
                opening_text=opening,
                caption=caption,
                why_it_works="The recorded view supports this particular point.",
                beat_reasons=["The house and ferry supply the setting"],
            ),
        )
        for (title, angle, opening, caption), form in zip(
            plans, ["review", "atmosphere", "atmosphere"], strict=True
        )
    ]
    if copy_clone:
        proposals[1] = copy.deepcopy(proposals[0])
        proposals[1]["title"] = "A new title is not a new idea"
        proposals[1]["moment_ids"] = ["m2"]
        proposals[1]["content_plan"]["why_it_works"] = "Different private reasoning."
        # Cosmetic case/spacing and different footage cannot turn identical
        # proposed copy into another content idea.
        proposals[1]["content_plan"]["opening_text"] = "  THE FINAL VIEW FROM THE FERRY  "
    transport = CassetteModelClient(json.dumps({"stories": proposals}))
    monkeypatch.setattr(media_collection_stories, "default_client", lambda: transport)
    body = {"expected_revision": 1, "query": "Suggest different content ideas for this crossing"}
    response = await client.post(f"{path}/stories", json=body)
    assert response.status_code == 200, response.text
    offered = response.json()["stories"]
    expected_titles = [plans[0][0], plans[2][0]] if copy_clone else [p[0] for p in plans]
    assert [idea["title"] for idea in offered] == expected_titles
    assert len({idea["id"] for idea in offered}) == len(expected_titles)
    assert len({tuple(m["id"] for m in idea["moments"]) for idea in offered}) == 1
    assert all(idea["requires_render_approval"] for idea in offered)
    assert transport.invocations == 1
    first = offered[0]
    changed = await client.patch(
        f"{path}/stories/{first['id']}",
        json=dict(
            expected_revision=1,
            expected_story_revision=1,
            client_event_id="change-only-one-idea",
            content_plan={**first["content_plan"], "opening_text": "My corrected opening"},
        ),
    )
    assert changed.status_code == 200, changed.text
    rejected = await client.post(
        f"{path}/stories/{first['id']}/reject",
        json=dict(
            expected_revision=1, expected_story_revision=2, client_event_id="reject-one-idea"
        ),
    )
    assert rejected.status_code == 200, rejected.text
    cached = await client.post(f"{path}/stories", json={**body, "cache_only": True})
    assert cached.status_code == 200, cached.text
    remaining = cached.json()["stories"]
    assert remaining[0]["rejected"] and remaining[0]["revision"] == 2
    assert remaining[1:] == offered[1:], "Other ideas must retain copy, source windows and state"
    assert transport.invocations == 1
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalar(select(func.count()).select_from(Job).where(Job.user_id == owner.id))
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CreatorAgentApproval)
                .where(CreatorAgentApproval.creator_id == owner.id)
            )
            == 0
        )


async def test_idea_forms_gate_evidence_and_survive_wording_edits_and_legacy_rows(
    collection_world, monkeypatch
):
    """Failure catalogue: a guide claimed over one clip, a form lost or forged by an
    older build's wording edit, and a pre-form saved story that stops loading."""
    from app.models import MediaCollectionStory
    from app.services import media_collection_stories
    from tests.evals.runners.eval_runner import CassetteModelClient

    client, assets, sources, _owner, _ = collection_world
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    assert (await client.post(f"{path}/index", json={"expected_revision": 1})).status_code == 200

    def proposal(title, form, opening):
        return dict(
            title=title,
            moment_ids=["m1"],
            content_plan=dict(
                form=form,
                audience="Evening travellers",
                angle=f"{title} from the recorded crossing",
                opening_text=opening,
                caption=opening,
                why_it_works="The recorded view supports this particular point.",
                beat_reasons=["The house and ferry supply the setting"],
            ),
        )

    proposals = [
        proposal("A guide to one clip", "guide", "Three stops on one clip"),
        proposal("Evening on the water", "atmosphere", "On the ferry after dark"),
        proposal("My take on the crossing", "review", "The crossing was worth it"),
    ]
    transport = CassetteModelClient(json.dumps({"stories": proposals}))
    monkeypatch.setattr(media_collection_stories, "default_client", lambda: transport)
    body = {"expected_revision": 1, "query": "Suggest different content ideas for this crossing"}
    response = await client.post(f"{path}/stories", json=body)
    assert response.status_code == 200, response.text
    offered = response.json()["stories"]
    assert [(s["title"], s["content_plan"]["form"]) for s in offered] == [
        ("Evening on the water", "atmosphere"),
        ("My take on the crossing", "review"),
    ]

    atmosphere, review = offered
    for story, tampered in ((atmosphere, "guide"), (review, None)):
        plan = {k: v for k, v in story["content_plan"].items() if k != "form"}
        plan["opening_text"] = f"Edited {story['id'][:4]}"
        if tampered:
            plan["form"] = tampered
        changed = await client.patch(
            f"{path}/stories/{story['id']}",
            json=dict(
                expected_revision=1,
                expected_story_revision=1,
                client_event_id=f"wording-{story['id']}",
                content_plan=plan,
            ),
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["content_plan"]["form"] == story["content_plan"]["form"]

    async with AsyncSessionLocal() as db:
        row = await db.get(MediaCollectionStory, uuid.UUID(review["id"]))
        legacy = json.loads(json.dumps(row.story_json))
        legacy["content_plan"].pop("form")
        row.story_json = legacy
        await db.commit()
    saved = await client.get(f"{path}/stories", params={"expected_revision": 1})
    assert saved.status_code == 200, saved.text
    by_id = {s["id"]: s for s in saved.json()["stories"]}
    assert "form" not in by_id[review["id"]]["content_plan"]
    assert by_id[atmosphere["id"]]["content_plan"]["form"] == "atmosphere"
    edited = await client.patch(
        f"{path}/stories/{review['id']}",
        json=dict(
            expected_revision=1,
            expected_story_revision=2,
            client_event_id="legacy-wording",
            content_plan={**by_id[review["id"]]["content_plan"], "form": "guide"},
        ),
    )
    assert edited.status_code == 200, edited.text
    assert "form" not in edited.json()["content_plan"]


# --- KRI-944: analysis that converges, and one honest per-item state --------------------


async def _add_video(
    owner,
    *,
    status="ready",
    has_audio=False,
    duration=8.0,
    facts_status="ready",
    transcript_status=None,
    metadata=None,
    kind="video",
    role="analysis_proxy",
    byte_count=None,
):
    """A catalogue asset with a proxy source and (optionally) pre-existing fact rows."""
    ready_video = {
        "duration_s": duration,
        "description": "A red ferry crossing a harbour",
        "understanding": {"summary": "Ferry crossing", "notable_moments": []},
    }
    async with AsyncSessionLocal() as db:
        asset = MediaAsset(
            user_id=owner.id,
            kind=kind,
            status=status,
            original_availability="device_only",
            original_byte_count=byte_count,
            metadata_json={"duration_s": duration, "has_audio": has_audio, **(metadata or {})},
        )
        db.add(asset)
        await db.flush()
        source = MediaAssetSource(
            user_id=owner.id,
            asset_id=asset.id,
            role=role,
            object_path=f"users/{owner.id}/kri944/{asset.id}.mp4",
            object_generation="42",
            uploaded_sha256=hashlib.sha256(str(asset.id).encode()).hexdigest(),
            byte_count=1234,
            content_type="video/mp4",
            status="available",
        )
        db.add(source)
        await db.flush()
        ref = facts._Source(
            str(asset.id),
            str(source.id),
            str(owner.id),
            kind,
            role,
            source.object_path,
            "42",
            source.uploaded_sha256,
            "available",
        )
        for analyzer, state, result in (
            (None, facts_status, ready_video),
            ("transcript", transcript_status, {"words": []}),
        ):
            if state is None:
                continue
            key = facts._key(ref, analyzer)
            db.add(
                MediaAssetAnalysis(
                    user_id=owner.id,
                    asset_id=asset.id,
                    source_id=source.id,
                    analyzer=key.analyzer,
                    model=key.model,
                    prompt_version=key.prompt_version,
                    schema_version=key.schema_version,
                    source_identity=key.source_identity,
                    status=state,
                    result_json=result if state == "ready" else None,
                )
            )
        await db.commit()
        return str(asset.id), str(source.id)


async def _items(client, path):
    response = await client.get(f"{path}/items")
    assert response.status_code == 200, response.text
    return {item["asset_id"]: item for item in response.json()["items"]}


async def test_failed_analysis_retries_itself_and_ends_ready(collection_world, monkeypatch):
    """A transient provider failure is not permanent: it is retried, then it converges."""
    from celery.exceptions import Retry

    from app.tasks import media_library as task

    client, _, _, owner, _ = collection_world
    asset_id, source_id = await _add_video(owner, status="pending", facts_status=None)
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    assert (await _items(client, path))[asset_id]["state"] == "queued"

    outcomes = [RuntimeError("provider 503"), None]

    def analyze(source, *, analyzer=None, snapshot_local_path=None):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return {
            "duration_s": 8.0,
            "description": "A red ferry crossing a harbour",
            "understanding": {"summary": "Ferry crossing", "notable_moments": []},
        }

    monkeypatch.setattr(facts, "_analyze_source", analyze)
    with pytest.raises(Retry):
        await asyncio.to_thread(task.analyze_media_asset_task.run, asset_id, str(owner.id))
    failed = (await _items(client, path))[asset_id]
    assert (failed["state"], failed["reason"], failed["attempts"], failed["retryable"]) == (
        "needs_retry",
        "provider_error",
        1,
        True,
    )

    # The worker dying before its own retry fires is covered by the reconciler, after a backoff.
    sent = []
    monkeypatch.setattr(task.analyze_media_asset_task, "delay", lambda *args: sent.append(args))
    await asyncio.to_thread(task.reconcile_media_library_analysis.run)
    assert [a for a in sent if a[0] == asset_id] == [], "A fresh failure waits out its backoff"
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MediaAsset)
            .where(MediaAsset.id == uuid.UUID(asset_id))
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await db.commit()
    await asyncio.to_thread(task.reconcile_media_library_analysis.run)
    assert (asset_id, str(owner.id)) in sent
    await asyncio.to_thread(task.analyze_media_asset_task.run, asset_id, str(owner.id))
    ready = (await _items(client, path))[asset_id]
    assert (ready["state"], ready["reason"], ready["attempts"]) == ("ready", None, 0)
    async with AsyncSessionLocal() as db:
        asset = await db.get(MediaAsset, uuid.UUID(asset_id))
        assert asset.status == "ready" and "analysis_failure" not in asset.metadata_json


async def _age(asset_id, minutes):
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MediaAsset)
            .where(MediaAsset.id == uuid.UUID(asset_id))
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=minutes))
        )
        await db.commit()


async def test_task_retries_do_not_spend_the_reconciler_budget(collection_world, monkeypatch):
    """A burst of task retries is not three strikes: revivals are counted on their own."""
    from celery.exceptions import Retry

    from app.services.media_library_ingest import retry_library_asset
    from app.tasks import media_library as task

    client, _, _, owner, _ = collection_world
    asset_id, source_id = await _add_video(owner, status="pending", facts_status=None)

    def analyze(source, *, analyzer=None, snapshot_local_path=None):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(facts, "_analyze_source", analyze)
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    resent = []
    monkeypatch.setattr(
        task.analyze_media_asset_task,
        "delay",
        lambda *a: resent.append(a) if a[0] == asset_id else None,
    )
    run = task.analyze_media_asset_task.run

    for _ in range(4):  # more failed runs than the old whole-budget of three
        with pytest.raises(Retry):
            await asyncio.to_thread(run, asset_id, str(owner.id))
    item = (await _items(client, path))[asset_id]
    assert (item["state"], item["attempts"], item["retryable"]) == ("needs_retry", 4, True)

    # The reconciler still has its whole budget: three revivals, then the row is terminal.
    for revival in range(1, 4):
        await _age(asset_id, 300)
        await asyncio.to_thread(task.reconcile_media_library_analysis.run)
        assert len(resent) == revival
        if revival < 3:
            with pytest.raises(Retry):
                await asyncio.to_thread(run, asset_id, str(owner.id))
    assert (await asyncio.to_thread(run, asset_id, str(owner.id)))["status"] == "failed"
    item = (await _items(client, path))[asset_id]
    assert (item["state"], item["reason"], item["retryable"]) == ("failed", "provider_error", False)

    await _age(asset_id, 24 * 60)
    await asyncio.to_thread(task.reconcile_media_library_analysis.run)
    assert len(resent) == 3, "A terminal row is not re-sent"
    # The creator's own retry is always allowed and starts the history over.
    async with AsyncSessionLocal() as db:
        await retry_library_asset(db, owner.id, uuid.UUID(asset_id))
    assert resent[-1] == (asset_id, str(owner.id))
    assert (await _items(client, path))[asset_id]["state"] in {"queued", "analyzing"}


async def test_budget_pause_is_left_to_the_reconciler_and_outlasts_a_provider_blip(
    collection_world, monkeypatch
):
    from app.agents._runtime import AiBudgetExceededError
    from app.tasks import media_library as task

    client, _, _, owner, _ = collection_world
    asset_id, source_id = await _add_video(owner, status="pending", facts_status=None)

    def analyze(source, *, analyzer=None, snapshot_local_path=None):
        raise AiBudgetExceededError(
            scope="environment:test", reset_at="2026-11-01", cached_behavior_available=False
        )

    monkeypatch.setattr(facts, "_analyze_source", analyze)
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    resent = []
    monkeypatch.setattr(
        task.analyze_media_asset_task,
        "delay",
        lambda *a: resent.append(a) if a[0] == asset_id else None,
    )

    # A task countdown would be gone long before a spend cap lifts, so none is scheduled.
    result = await asyncio.to_thread(task.analyze_media_asset_task.run, asset_id, str(owner.id))
    assert result["status"] == "failed"
    item = (await _items(client, path))[asset_id]
    assert (item["state"], item["reason"]) == ("needs_retry", "budget_paused")

    for revival in range(1, 7):
        await _age(asset_id, 30)
        await asyncio.to_thread(task.reconcile_media_library_analysis.run)
        assert len(resent) == revival - 1, "Waits out its backoff first"
        await _age(asset_id, 300)
        await asyncio.to_thread(task.reconcile_media_library_analysis.run)
        assert len(resent) == revival
        await asyncio.to_thread(task.analyze_media_asset_task.run, asset_id, str(owner.id))
    item = (await _items(client, path))[asset_id]
    assert (item["state"], item["reason"], item["retryable"]) == ("failed", "budget_paused", False)


@pytest.mark.parametrize("silence", ["no-audio-track", "old-asset-no-row"])
async def test_silent_video_becomes_ready_instead_of_missing_forever(collection_world, silence):
    client, _, _, owner, _ = collection_world
    asset_id, source_id = await _add_video(owner, has_audio=silence != "no-audio-track")
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["missing_analysis_asset_ids"] == []
    assert indexed.json()["context_asset_ids"] == [asset_id], "The whole short clip is offered"
    assert (await _items(client, path))[asset_id]["state"] == "ready"
    found = await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    assert [value["asset_id"] for value in found.json()["moments"]] == [asset_id]


async def test_failed_transcript_on_a_speaking_clip_is_retried_not_indexed_as_silent(
    collection_world, monkeypatch
):
    from celery.exceptions import Retry

    from app.tasks import media_library as task

    client, _, _, owner, _ = collection_world
    asset_id, source_id = await _add_video(
        owner, status="pending", has_audio=True, facts_status=None
    )
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    transcripts = [RuntimeError("provider 503"), {"words": []}]

    def analyze(source, *, analyzer=None, snapshot_local_path=None):
        if analyzer == "transcript":
            outcome = transcripts.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {
            "duration_s": 8.0,
            "description": "A red ferry crossing a harbour",
            "understanding": {"summary": "Ferry crossing", "notable_moments": []},
        }

    monkeypatch.setattr(facts, "_analyze_source", analyze)
    with pytest.raises(Retry):
        await asyncio.to_thread(task.analyze_media_asset_task.run, asset_id, str(owner.id))
    item = (await _items(client, path))[asset_id]
    assert (item["state"], item["reason"], item["retryable"]) == (
        "needs_retry",
        "provider_error",
        True,
    )
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.json()["missing_analysis_asset_ids"] == [asset_id], "Not passed off as silent"

    await asyncio.to_thread(task.analyze_media_asset_task.run, asset_id, str(owner.id))
    assert (await _items(client, path))[asset_id]["state"] == "ready"
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.json()["missing_analysis_asset_ids"] == []
    assert indexed.json()["context_asset_ids"] == [asset_id]


async def test_speaking_clip_with_spent_transcript_retries_falls_back_to_its_visuals(
    collection_world,
):
    client, _, _, owner, _ = collection_world
    spent = {
        "analysis_failure": {
            "reason": "provider_error",
            "attempts": 12,
            "revivals": 3,
            "at": "2026-10-26T00:00:00Z",
        }
    }
    asset_id, source_id = await _add_video(
        owner, status="failed", has_audio=True, transcript_status="failed", metadata=spent
    )
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.json()["missing_analysis_asset_ids"] == []
    assert indexed.json()["context_asset_ids"] == [asset_id]
    item = (await _items(client, path))[asset_id]
    assert (item["state"], item["reason"], item["retryable"]) == ("failed", "provider_error", False)


async def test_audible_video_still_waits_for_its_transcript(collection_world):
    """Only a video that will never get a transcript skips it."""
    client, _, _, owner, _ = collection_world
    asset_id, source_id = await _add_video(
        owner, status="processing", has_audio=True, transcript_status="analyzing"
    )
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection([asset_id], [source_id], 1))).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.json()["missing_analysis_asset_ids"] == [asset_id]
    assert (await _items(client, path))[asset_id]["state"] == "analyzing"


async def test_every_source_is_represented_in_a_150_source_collection(
    collection_world, monkeypatch
):
    """The cap is fair across sources: 150 sources means 150 candidates, one each."""
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )

    client, _, _, owner, _ = collection_world
    pairs = [await _add_video(owner, has_audio=False) for _ in range(150)]
    path = f"/me/media-collections/{uuid.uuid4()}"
    body = selection([a for a, _ in pairs], [s for _, s in pairs], count=150)
    assert (await client.put(path, json=body)).status_code == 200
    index = {"expected_revision": 1, "limit": 32}
    offset = 0
    while offset is not None:
        indexed = await client.post(f"{path}/index", json={**index, "offset": offset})
        assert indexed.status_code == 200, indexed.text
        assert indexed.json()["missing_analysis_asset_ids"] == []
        offset = indexed.json()["next_offset"]
    seen = []

    def ideas(self, input, *, ctx):
        seen.extend(value.source for value in input.candidates)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title="Harbour",
                    hook="Crossing",
                    payoff="Arrival",
                    rationale="Recorded arrival",
                    moment_ids=[input.candidates[0].id],
                    content_plan=dict(
                        audience="Explorers",
                        angle="An arrival",
                        why_it_works="The opening question resolves at the destination.",
                        opening_text="Where does this lead?",
                        beat_reasons=["Shows the arrival"],
                    ),
                )
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    stories = await client.post(f"{path}/stories", json={"expected_revision": 1, "query": "ferry"})
    assert stories.status_code == 200, stories.text
    assert set(seen) == {s for _, s in pairs}, "Every source contributes a candidate"
    assert stories.json()["coverage"]["unindexed_asset_ids"] == []


async def test_candidate_cap_names_the_sources_it_left_out(collection_world):
    """Past the fair cap, nothing is dropped silently: the response says what was cut."""
    from app.services.media_moments import FAIR_CANDIDATE_CAP

    client, _, _, owner, _ = collection_world
    count = FAIR_CANDIDATE_CAP + 10
    pairs = [await _add_video(owner) for _ in range(count)]
    path = f"/me/media-collections/{uuid.uuid4()}"
    body = selection([a for a, _ in pairs], [s for _, s in pairs], count=count)
    assert (await client.put(path, json=body)).status_code == 200
    offset = 0
    while offset is not None:
        indexed = await client.post(
            f"{path}/index", json={"expected_revision": 1, "limit": 32, "offset": offset}
        )
        offset = indexed.json()["next_offset"]
    from app.services import media_moments

    async with AsyncSessionLocal() as db:
        evidence = await media_moments.search_moments(
            db,
            owner.id,
            uuid.UUID(path.rsplit("/", 1)[1]),
            revision=1,
            query="",
            limit=100,
            all_candidates=True,
        )
    assert evidence.candidate_limit_reached is True
    assert len(evidence.moments) == FAIR_CANDIDATE_CAP
    represented = {value.asset_id for value in evidence.moments}
    assert {uuid.UUID(a) for a, _ in pairs} - represented == set(evidence.unoffered_asset_ids)
    assert evidence.truncated_asset_ids == [], "No source's own intervals were cut"


async def test_original_role_binding_is_rebound_to_the_proxy_and_used(
    collection_world, monkeypatch
):
    """A device-only video bound to its later-attached original is not dropped silently."""
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )

    client, _, _, owner, _ = collection_world
    asset_id, proxy_id = await _add_video(owner, has_audio=False)
    async with AsyncSessionLocal() as db:
        original = MediaAssetSource(
            user_id=owner.id,
            asset_id=uuid.UUID(asset_id),
            role="original",
            object_path=f"users/{owner.id}/kri944/{asset_id}-original.mov",
            object_generation="42",
            uploaded_sha256=hashlib.sha256(b"original" + asset_id.encode()).hexdigest(),
            byte_count=1234,
            content_type="video/quicktime",
            status="available",
        )
        db.add(original)
        await db.commit()
        original_id = str(original.id)
    path = f"/me/media-collections/{uuid.uuid4()}"
    written = await client.put(path, json=selection([asset_id], [original_id], 1))
    assert written.status_code == 200, written.text
    assert written.json()["members"][0]["source_id"] == proxy_id

    # A collection stored before the rebind existed heals on its next use.
    async with AsyncSessionLocal() as db:
        from app.models import MediaCollection

        row = await db.get(MediaCollection, uuid.UUID(path.rsplit("/", 1)[1]))
        row.selection_json = {
            **row.selection_json,
            "members": [{"asset_id": asset_id, "source_id": original_id, "group": "Event 1"}],
        }
        await db.commit()
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["indexed_sources"] == 1
    assert indexed.json()["missing_analysis_asset_ids"] == []

    offered = []

    def ideas(self, input, *, ctx):
        offered.extend(value.source for value in input.candidates)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title="Harbour",
                    hook="Crossing",
                    payoff="Arrival",
                    rationale="Recorded arrival",
                    moment_ids=[input.candidates[0].id],
                    content_plan=dict(
                        audience="Explorers",
                        angle="An arrival",
                        why_it_works="The opening question resolves at the destination.",
                        opening_text="Where does this lead?",
                        beat_reasons=["Shows the arrival"],
                    ),
                )
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    stories = await client.post(f"{path}/stories", json={"expected_revision": 1, "query": "ferry"})
    assert stories.status_code == 200, stories.text
    assert offered == [proxy_id]
    assert stories.json()["coverage"]["unsupported_asset_ids"] == []


async def test_items_route_reports_every_state_and_reason(collection_world):
    client, _, _, owner, _ = collection_world
    failure = lambda reason, attempts, revivals=0: {  # noqa: E731
        "analysis_failure": {
            "reason": reason,
            "attempts": attempts,
            "revivals": revivals,
            "at": "2026-10-26T00:00:00Z",
        }
    }
    cases = {
        "queued": await _add_video(owner, status="pending", facts_status=None),
        "analyzing": await _add_video(owner, status="processing", facts_status="analyzing"),
        "ready": await _add_video(owner),
        "needs_retry": await _add_video(
            owner, status="failed", facts_status="failed", metadata=failure("provider_error", 1)
        ),
        "budget": await _add_video(
            owner, status="failed", facts_status="failed", metadata=failure("budget_paused", 2)
        ),
        "failed": await _add_video(
            owner,
            status="failed",
            facts_status="failed",
            metadata=failure("provider_error", 9, revivals=3),
        ),
        "legacy-failed": await _add_video(owner, status="failed", facts_status="failed"),
        "legacy-old-failed": await _add_video(owner, status="failed", facts_status="failed"),
        "audio": await _add_video(owner, kind="audio"),
        "no-moment": await _add_video(
            owner, metadata={"moment_index": {"source_id": "x", "outcome": "none"}}
        ),
        "too-long": await _add_video(owner, duration=2400.0, status="pending", facts_status=None),
        "too-large": await _add_video(
            owner, kind="image", byte_count=30 * 1024 * 1024, status="pending", facts_status=None
        ),
        "original-only": await _add_video(owner, role="original"),
        "revoked": await _add_video(owner),
        "photo": await _add_video(owner, kind="image"),
    }
    path = f"/me/media-collections/{uuid.uuid4()}"
    members = [
        {"asset_id": a, "source_id": s, "group": "Day 1" if i % 2 else "Day 2"}
        for i, (a, s) in enumerate(cases.values())
    ]
    body = {"name": "States", "expected_revision": 0, "members": members}
    assert (await client.put(path, json=body)).status_code == 200
    await _age(cases["legacy-old-failed"][0], 8 * 24 * 60)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MediaAsset)
            .where(MediaAsset.id == uuid.UUID(cases["revoked"][0]))
            .values(archived_at=datetime.now(UTC))
        )
        await db.commit()
    response = await client.get(f"{path}/items")
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    items = {item["asset_id"]: item for item in response.json()["items"]}
    assert cases.pop("audio")[0] not in items, "Only footage has a tile"
    got = {
        name: (items[a]["state"], items[a]["reason"], items[a]["retryable"], items[a]["attempts"])
        for name, (a, _) in cases.items()
    }
    assert got == {
        "queued": ("queued", None, False, 0),
        "analyzing": ("analyzing", None, False, 0),
        "ready": ("ready", None, False, 0),
        "needs_retry": ("needs_retry", "provider_error", True, 1),
        "budget": ("needs_retry", "budget_paused", True, 2),
        "failed": ("failed", "provider_error", False, 9),
        "legacy-failed": ("needs_retry", "provider_error", True, 0),
        "legacy-old-failed": ("failed", "provider_error", False, 0),
        "no-moment": ("failed", "no_usable_moment", False, 0),
        "too-long": ("excluded", "too_long", False, 0),
        "too-large": ("excluded", "too_large", False, 0),
        "original-only": ("excluded", "unsupported_type", False, 0),
        "revoked": ("excluded", "access_revoked", False, 0),
        "photo": ("ready", None, False, 0),
    }
    assert {items[a]["group"] for a, _ in cases.values()} == {"Day 1", "Day 2"}
    assert items[cases["photo"][0]]["kind"] == "image"
    assert (await client.get(f"/me/media-collections/{uuid.uuid4()}/items")).status_code == 404


# --- KRI-944: ideas per event, delete and restore, another idea, delete event ----------------


async def _two_events(client, owner, per_event=2):
    """A saved, fully indexed collection holding a "Ferry" event and a "Market" event."""
    ferry = [await _add_video(owner) for _ in range(per_event)]
    market = [await _add_video(owner) for _ in range(per_event)]
    path = f"/me/media-collections/{uuid.uuid4()}"
    members = [
        {"asset_id": a, "source_id": s, "group": name}
        for name, pairs in (("Ferry", ferry), ("Market", market))
        for a, s in pairs
    ]
    body = {"name": "Trip", "expected_revision": 0, "members": members}
    assert (await client.put(path, json=body)).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200 and indexed.json()["missing_analysis_asset_ids"] == []
    return path, body, ferry, market


def _stub_ideas(monkeypatch, make):
    """Replace only the provider: `make(input)` returns (title, angle, opening, picks) tuples,
    where `picks` index into the candidates the model was actually offered."""
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )

    class Calls(list):
        pass

    calls = Calls()
    calls.request_ids = []

    def run(self, input, *, ctx):
        calls.append(input)
        calls.request_ids.append(ctx.request_id)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title=title,
                    hook=opening,
                    payoff=angle,
                    rationale="Grounded in the footage.",
                    moment_ids=[input.candidates[i].id for i in picks],
                    content_plan=dict(
                        audience="Travellers",
                        angle=angle,
                        why_it_works="A specific everyday moment.",
                        opening_text=opening,
                        beat_reasons=["Shows it"] * len(picks),
                    ),
                )
                for title, angle, opening, picks in make(input)
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", run)
    return calls


EVENT_QUERY = {"expected_revision": 1, "query": "Ideas for this outing"}


async def _saved(client, path, revision=1, group=None):
    params = {"expected_revision": revision, **({"group": group} if group else {})}
    response = await client.get(f"{path}/stories", params=params)
    assert response.status_code == 200, response.text
    return response.json()["stories"]


async def test_event_ideas_cite_only_their_own_events_moments(collection_world, monkeypatch):
    """Failure catalogue: an idea for one event citing footage from another; a model shown the
    other event's candidates; an event's ideas listed under a sibling; an unknown event
    silently falling back to the whole collection; a stale revision answering anyway."""
    client, _, _, owner, _ = collection_world
    path, _, ferry, market = await _two_events(client, owner)
    calls = _stub_ideas(
        monkeypatch,
        lambda input: [("All", "One point", "Opening", list(range(len(input.candidates))))],
    )
    for name, pairs in (("Ferry", ferry), ("Market", market)):
        response = await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": name})
        assert response.status_code == 200, response.text
        stories = response.json()["stories"]
        assert [story["group"] for story in stories] == [name]
        assert {m["source_id"] for m in stories[0]["moments"]} == {s for _, s in pairs}
        assert {m["source_id"] for m in response.json()["coverage"]["moments"]} == {
            s for _, s in pairs
        }
    assert [{c.source for c in call.candidates} for call in calls] == [
        {s for _, s in ferry},
        {s for _, s in market},
    ], "the model only ever saw its own event's footage"

    assert [story["group"] for story in await _saved(client, path, group="Ferry")] == ["Ferry"]
    assert [story["group"] for story in await _saved(client, path, group="Market")] == ["Market"]
    assert {story["group"] for story in await _saved(client, path)} == {"Ferry", "Market"}

    unknown = await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Nowhere"})
    assert unknown.status_code == 404
    stale = await client.post(
        f"{path}/stories", json={"expected_revision": 9, "query": "x", "group": "Ferry"}
    )
    assert stale.status_code == 409


async def test_deleting_an_idea_then_restoring_it_returns_the_same_idea(
    collection_world, monkeypatch
):
    """Failure catalogue: delete removing the row for good (Undo impossible); a deleted idea
    coming back from the cached generation; restore forging a new idea or changing the old
    one; restore after the window; another creator's or unknown idea."""
    from datetime import timedelta

    from app.services import media_collection_stories

    client, _, _, owner, _ = collection_world
    path, _, _, _ = await _two_events(client, owner)
    _stub_ideas(monkeypatch, lambda input: [("Crossing", "A point", "Opening", [0])])
    body = {**EVENT_QUERY, "group": "Ferry"}
    offered = (await client.post(f"{path}/stories", json=body)).json()["stories"]
    assert len(offered) == 1
    idea = offered[0]

    deleted = await client.delete(f"{path}/stories/{idea['id']}")
    assert deleted.status_code == 200, deleted.text
    assert datetime.fromisoformat(deleted.json()["restore_until"]) > datetime.now(UTC)
    assert await _saved(client, path, group="Ferry") == []
    again = await client.post(f"{path}/stories", json=body)
    assert again.status_code == 200 and again.json()["stories"] == [], "never re-offered"
    assert (await client.delete(f"{path}/stories/{idea['id']}")).status_code == 200

    restored = await client.post(f"{path}/stories/{idea['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json() == idea
    assert await _saved(client, path, group="Ferry") == [idea]
    assert (await client.post(f"{path}/stories/{idea['id']}/restore")).json() == idea

    assert (await client.delete(f"{path}/stories/{uuid.uuid4()}")).status_code == 404
    assert (await client.post(f"{path}/stories/{uuid.uuid4()}/restore")).status_code == 404
    await client.delete(f"{path}/stories/{idea['id']}")
    monkeypatch.setattr(media_collection_stories, "RESTORE_WINDOW", timedelta(seconds=-1))
    late = await client.post(f"{path}/stories/{idea['id']}/restore")
    assert late.status_code == 410
    assert late.json()["detail"]["code"] == "restore_window_closed"


async def test_a_deleted_idea_cannot_be_reviewed_corrected_rejected_or_selected(
    collection_world, monkeypatch
):
    """Failure catalogue: a stale client selecting, rejecting, exposing or correcting an idea
    the creator just deleted; the response looking like a live idea; the refusal writing
    taste feedback or events; a replay of an event recorded before the delete failing."""
    from app.config import settings
    from app.models import MediaCollectionStory

    client, _, _, owner, _ = collection_world
    path, _, _, _ = await _two_events(client, owner)
    _stub_ideas(monkeypatch, lambda input: [("Crossing", "A point", "Opening", [0])])
    idea = (await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Ferry"})).json()[
        "stories"
    ][0]
    review = {"expected_revision": 1, "expected_story_revision": 1}
    seen = await client.post(
        f"{path}/stories/{idea['id']}/expose", json={**review, "client_event_id": "seen-1"}
    )
    assert seen.status_code == 200
    assert (await client.delete(f"{path}/stories/{idea['id']}")).status_code == 200

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    url = f"{path}/stories/{idea['id']}"
    attempts = [
        client.post(f"{url}/reject", json={**review, "client_event_id": "reject-1"}),
        client.post(f"{url}/expose", json={**review, "client_event_id": "seen-2"}),
        client.patch(url, json={**review, "client_event_id": "fix-1", "audio_policy": "muted"}),
        client.post(
            f"{url}/select",
            json={
                **review,
                "client_event_id": "select-1",
                "thread_id": str(uuid.uuid4()),
                "expected_thread_revision": 0,
            },
        ),
    ]
    assert [(await attempt).status_code for attempt in attempts] == [404] * 4
    async with AsyncSessionLocal() as db:
        row = await db.get(MediaCollectionStory, uuid.UUID(idea["id"]))
        assert [event["type"] for event in row.events_json] == ["offered", "exposure", "deletion"]

    replay = await client.post(f"{url}/expose", json={**review, "client_event_id": "seen-1"})
    assert replay.status_code == 200, "a replay of an earlier event still answers"

    restored = await client.post(f"{url}/restore")
    assert restored.status_code == 200
    again = await client.post(f"{url}/reject", json={**review, "client_event_id": "reject-2"})
    assert again.status_code == 200, "a restored idea is reviewable again"


async def test_an_idea_deleted_while_generation_runs_is_not_re_offered(
    collection_world, monkeypatch
):
    """Failure catalogue: the event history read before the provider call going stale, so an
    idea deleted during the 25 to 115 s generation comes back as a new idea with the same copy."""
    client, _, _, owner, _ = collection_world
    path, _, _, _ = await _two_events(client, owner)
    loop = asyncio.get_running_loop()
    state = {"delete": None}

    def make(input):
        if state["delete"]:
            asyncio.run_coroutine_threadsafe(
                client.delete(f"{path}/stories/{state['delete']}"), loop
            ).result(timeout=30)
        return [("Crossing", "A point", "Opening", [0])]

    _stub_ideas(monkeypatch, make)
    body = {**EVENT_QUERY, "group": "Ferry"}
    idea = (await client.post(f"{path}/stories", json=body)).json()["stories"][0]

    state["delete"] = idea["id"]
    again = await client.post(f"{path}/stories", json={**body, "query": "A different angle"})
    assert again.status_code == 200, again.text
    assert again.json()["stories"] == [], "the deleted idea's copy is barred, not re-offered"
    assert await _saved(client, path, group="Ferry") == []


async def test_another_idea_adds_a_batch_and_never_repeats_or_replaces(
    collection_world, monkeypatch
):
    """Failure catalogue: another idea replacing the existing ones; repeating an existing
    idea's exact footage; the same copy under a new title or in different case; adding more
    than three; the model never being told what the event already has; every candidate filtered
    out with no second try; a request after a delete replaying the deleted idea's cached id."""
    client, _, _, owner, _ = collection_world
    path, _, ferry, _ = await _two_events(client, owner, per_event=3)
    body = {**EVENT_QUERY, "group": "Ferry"}
    repeat = ("Reworded", "Yet another point", "  OPENING ONE ", [2])

    def retried(input, fresh):
        # The retry names the dropped angle too, so it sees one more offered idea.
        return fresh if len(input.already_offered) > 4 else [repeat]

    script = {
        "first": lambda input: [("First", "Point one", "Opening one", [0])],
        "more": lambda input: [
            ("Same footage", "Entirely different point", "Fresh opening", [0]),
            ("Genuinely new", "A third point", "A third opening", [1, 2]),
            ("Also new", "A fourth point", "A fourth opening", [2]),
            ("Too many", "A fifth point", "A fifth opening", [1]),
            ("Way too many", "A sixth point", "A sixth opening", [0, 1]),
        ],
        "repeats_only": lambda input: [
            repeat,
            ("Same footage", "Other point", "Other opening", [1, 2]),
        ],
        "recovers": lambda input: retried(
            input, [("Recovered", "A brand new point", "Brand new opening", [0, 2])]
        ),
        "after_delete": lambda input: retried(
            input, [("After delete", "A point after the delete", "Opening after delete", [0, 1])]
        ),
    }
    state = {"step": "first"}
    calls = _stub_ideas(monkeypatch, lambda input: script[state["step"]](input))
    first = (await client.post(f"{path}/stories", json=body)).json()["stories"]
    assert len(first) == 1 and calls[0].already_offered == []

    state["step"] = "more"
    added = await client.post(
        f"{path}/stories", json={**body, "exclude_story_ids": [first[0]["id"]]}
    )
    assert added.status_code == 200, added.text
    new = added.json()["stories"]
    assert [story["title"] for story in new] == ["Genuinely new", "Also new", "Too many"], (
        "a batch of up to three new ideas, none repeating the offered footage"
    )
    first_moments = {m["id"] for m in first[0]["moments"]}
    assert all({m["id"] for m in n["moments"]}.isdisjoint(first_moments) for n in new)
    offered = calls[-1].already_offered
    assert [value.moment_ids for value in offered] == [[m["id"] for m in first[0]["moments"]]]
    assert offered[0].opening_text == "Opening one"

    saved = await _saved(client, path, group="Ferry")
    have = [first[0]["id"], *(n["id"] for n in new)]
    assert {story["id"] for story in saved} == set(have)
    assert next(s for s in saved if s["id"] == first[0]["id"]) == first[0], "never replaced"

    state["step"] = "repeats_only"
    before = len(calls)
    nothing = await client.post(
        f"{path}/stories",
        json={**body, "exclude_story_ids": have[:2]},
    )
    assert nothing.status_code == 200 and nothing.json()["stories"] == []
    assert len(calls) == before + 2, "an emptied request is retried once, not endlessly"
    assert len(await _saved(client, path, group="Ferry")) == 4

    state["step"] = "recovers"
    recovered = await client.post(f"{path}/stories", json={**body, "exclude_story_ids": have})
    assert [s["title"] for s in recovered.json()["stories"]] == ["Recovered"], recovered.text
    angles = [value.angle for value in calls[-1].already_offered]
    assert "Yet another point" in angles, "the retry names the angle that was dropped"

    delete = await client.delete(f"{path}/stories/{recovered.json()['stories'][0]['id']}")
    assert delete.status_code == 200
    state["step"] = "after_delete"
    again = await client.post(f"{path}/stories", json={**body, "exclude_story_ids": have})
    assert [s["title"] for s in again.json()["stories"]] == ["After delete"], (
        "asking again after a delete is a new request, not the cached one with the deleted id"
    )

    other = await client.post(
        f"{path}/stories",
        json={**body, "group": "Market", "exclude_story_ids": [first[0]["id"]]},
    )
    assert other.status_code == 422, "an event's ideas cannot be excluded from another event"
    gone = await client.post(
        f"{path}/stories", json={**body, "exclude_story_ids": [str(uuid.uuid4())]}
    )
    assert gone.status_code == 404


async def test_an_empty_another_idea_answer_is_not_cached_and_retries_use_fresh_ids(
    collection_world, monkeypatch
):
    """Failure catalogue: a model answer of "no ideas" cached under the exclude-set digest so
    every later tap replays [] with no model call; the filter-emptied retry reusing the first
    pass's request id, which the cost ledger treats as an already-settled call."""
    client, _, _, owner, _ = collection_world
    path, _, _, _ = await _two_events(client, owner, per_event=3)
    body = {**EVENT_QUERY, "group": "Ferry"}
    repeat = ("Reworded", "Yet another point", "Opening one", [2])
    script = {
        "first": lambda input: [("First", "Point one", "Opening one", [0])],
        "empty": lambda input: [],
        "fresh": lambda input: [("Fresh", "A new point", "A new opening", [1, 2])],
        "repeats": lambda input: [repeat],
    }
    state = {"step": "first"}
    calls = _stub_ideas(monkeypatch, lambda input: script[state["step"]](input))
    first = (await client.post(f"{path}/stories", json=body)).json()["stories"]
    exclude = {**body, "exclude_story_ids": [first[0]["id"]]}

    state["step"] = "empty"
    assert (await client.post(f"{path}/stories", json=exclude)).json()["stories"] == []
    state["step"] = "fresh"
    again = await client.post(f"{path}/stories", json=exclude)
    assert [s["title"] for s in again.json()["stories"]] == ["Fresh"], (
        "the earlier empty answer must not replay from the cache"
    )

    state["step"] = "empty"
    other = {**EVENT_QUERY, "group": "Market"}
    assert (await client.post(f"{path}/stories", json=other)).json()["stories"] == []
    state["step"] = "fresh"
    retried = (await client.post(f"{path}/stories", json=other)).json()["stories"]
    assert [s["title"] for s in retried] == ["Fresh"], (
        "tapping Find ideas again after 'No idea found' asks the model, not the cached []"
    )

    state["step"] = "repeats"
    before = len(calls)
    exclude = {**body, "exclude_story_ids": [first[0]["id"], again.json()["stories"][0]["id"]]}
    await client.post(f"{path}/stories", json=exclude)
    ids = calls.request_ids[before:]
    assert len(ids) == 2 and len(set(ids)) == 2, "the retry is a distinct call"


async def test_deleting_an_event_keeps_other_events_and_bumps_the_revision(
    collection_world, monkeypatch
):
    """Failure catalogue: the delete taking a sibling's members or ideas with it; the
    sibling's ideas orphaned by the revision bump; the event's ideas hard-deleted so Undo
    cannot bring them back; Undo (save the members again, restore the ideas) losing the
    sibling's ideas."""
    client, _, _, owner, _ = collection_world
    path, put_body, ferry, market = await _two_events(client, owner)
    _stub_ideas(monkeypatch, lambda input: [("Idea", "A point", "Opening", [0])])
    ferry_idea = (
        await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Ferry"})
    ).json()["stories"][0]
    market_idea = (
        await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Market"})
    ).json()["stories"][0]

    removed = await client.request(
        "DELETE", f"{path}/groups", json={"group": "Ferry", "expected_revision": 1}
    )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"revision": 2, "deleted_story_ids": [ferry_idea["id"]]}
    collection = (await client.get(path)).json()
    assert collection["revision"] == 2
    assert {m["asset_id"] for m in collection["members"]} == {a for a, _ in market}
    assert await _saved(client, path, revision=2, group="Ferry") == []
    kept = await _saved(client, path, revision=2, group="Market")
    assert [story["id"] for story in kept] == [market_idea["id"]]
    assert kept[0]["moments"] == market_idea["moments"]
    assert {i["asset_id"] for i in (await _items(client, path)).values()} == {a for a, _ in market}

    # Undo: the client saves the members again, then restores each returned idea.
    undone = await client.put(path, json={**put_body, "expected_revision": 2})
    assert undone.status_code == 200 and undone.json()["revision"] == 3
    restored = await client.post(f"{path}/stories/{ferry_idea['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["collection_revision"] == 3
    final = await _saved(client, path, revision=3)
    assert {story["id"] for story in final} == {ferry_idea["id"], market_idea["id"]}
    assert {story["collection_revision"] for story in final} == {3}


async def test_adding_footage_to_an_event_keeps_its_ideas_usable(collection_world, monkeypatch):
    """Failure catalogue: adding a clip to an event orphaning every idea it already had, so
    the phone still shows them and "Use this idea" answers 409 "Story belongs to an older
    collection"; an idea whose own footage left its event surviving the write."""
    from app.config import settings
    from app.kria.runtime import RuntimeFailure

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    client, _, _, owner, _ = collection_world
    path, put_body, _, _ = await _two_events(client, owner)
    _stub_ideas(monkeypatch, lambda input: [("Idea", "A point", "Opening", [0])])
    ferry_idea = (
        await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Ferry"})
    ).json()["stories"][0]

    added_asset, added_source = await _add_video(owner)
    grown = [
        *put_body["members"],
        {"asset_id": added_asset, "source_id": added_source, "group": "Ferry"},
    ]
    written = await client.put(path, json={**put_body, "expected_revision": 1, "members": grown})
    assert written.status_code == 200 and written.json()["revision"] == 2, written.text
    kept = await _saved(client, path, revision=2, group="Ferry")
    assert [story["id"] for story in kept] == [ferry_idea["id"]]
    assert kept[0]["collection_revision"] == 2
    selection_body = {
        "expected_revision": 2,
        "expected_story_revision": 1,
        "client_event_id": f"choose-{ferry_idea['id']}-1",
        "thread_id": str(uuid.uuid4()),
        "expected_thread_revision": 0,
    }
    with pytest.raises(RuntimeFailure, match="thread_not_found"):
        # Past every story and collection check: only the made-up chat is refused.
        await client.post(f"{path}/stories/{ferry_idea['id']}/select", json=selection_body)

    used = {moment["source_id"] for moment in ferry_idea["moments"]}
    without_its_footage = [member for member in grown if member["source_id"] not in used]
    shrunk = await client.put(
        path, json={**put_body, "expected_revision": 2, "members": without_its_footage}
    )
    assert shrunk.status_code == 200 and shrunk.json()["revision"] == 3, shrunk.text
    assert await _saved(client, path, revision=3, group="Ferry") == []
    stale = await client.post(
        f"{path}/stories/{ferry_idea['id']}/select",
        json={**selection_body, "expected_revision": 3},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "Story belongs to an older collection"


async def test_event_delete_with_a_stale_revision_is_a_409_and_changes_nothing(collection_world):
    client, _, _, owner, _ = collection_world
    path, _, _, _ = await _two_events(client, owner)
    stale = await client.request(
        "DELETE", f"{path}/groups", json={"group": "Ferry", "expected_revision": 7}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "collection_changed"
    unknown = await client.request(
        "DELETE", f"{path}/groups", json={"group": "Nowhere", "expected_revision": 1}
    )
    assert unknown.status_code == 404
    collection = (await client.get(path)).json()
    assert collection["revision"] == 1 and len(collection["members"]) == 4


# --- KRI-944: photos feed ideas -----------------------------------------------------------


def _captured(day):
    return {
        "upload_contract": {
            "proxy": {"original": {"capture": {"capture_time": f"2026-05-{day:02d}T10:00:00Z"}}}
        }
    }


async def test_event_title_and_notes_reach_the_model_and_key_the_idea_cache(
    collection_world, monkeypatch
):
    """Failure catalogue: events dropped on read-back; an older client's PUT (no `events`)
    wiping them; the model never seeing the title, notes or capture dates; edited notes
    served from the old idea cache; unchanged notes paying for a second generation; an
    empty query rejected although the event describes the outing."""
    client, _, _, owner, _ = collection_world
    ferry = [await _add_video(owner, metadata=_captured(day)) for day in (2, 4)]
    path = f"/me/media-collections/{uuid.uuid4()}"
    members = [{"asset_id": a, "source_id": s, "group": "Ferry"} for a, s in ferry]
    events = [
        {"group": "Ferry", "title": "Ferry to Bozcaada", "about": "Island day with friends"},
        {"group": "Not yet filmed", "title": "Sunset dinner"},
    ]
    body = {"name": "Trip", "expected_revision": 0, "members": members, "events": events}
    saved = await client.put(path, json=body)
    assert saved.status_code == 200, saved.text
    assert (await client.get(path)).json()["events"] == [
        events[0],
        {**events[1], "about": ""},
    ]
    assert (await client.post(f"{path}/index", json={"expected_revision": 1})).status_code == 200

    calls = _stub_ideas(monkeypatch, lambda input: [("Ferry", "A point", "Opening", [0])])
    ask = {"expected_revision": 1, "group": "Ferry"}
    assert (await client.post(f"{path}/stories", json={**ask, "query": ""})).status_code == 200
    assert (calls[0].event_title, calls[0].event_about) == (
        "Ferry to Bozcaada",
        "Island day with friends",
    )
    assert calls[0].capture_span.model_dump() == {"first": "2026-05-02", "last": "2026-05-04"}
    whole = await client.post(f"{path}/stories", json={"expected_revision": 1, "query": ""})
    assert whole.status_code == 422, "an empty query needs an event to describe the request"

    assert (await client.post(f"{path}/stories", json={**ask, "query": ""})).status_code == 200
    assert len(calls) == 1, "same notes hit the idea cache"

    older_client = {k: v for k, v in body.items() if k != "events"}
    renamed = await client.put(
        path, json={**older_client, "name": "Trip 2", "expected_revision": 1}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["events"][0]["title"] == "Ferry to Bozcaada", "omitted keeps events"

    edited = [{**events[0], "about": "Island day, rainy"}, events[1]]
    changed = await client.put(
        path, json={**body, "name": "Trip 2", "expected_revision": 2, "events": edited}
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 3, changed.text
    assert (await client.post(f"{path}/index", json={"expected_revision": 3})).status_code == 200
    ask = {"expected_revision": 3, "group": "Ferry", "query": ""}
    assert (await client.post(f"{path}/stories", json=ask)).status_code == 200
    assert len(calls) == 2 and calls[1].event_about == "Island day, rainy"
    assert (await client.post(f"{path}/stories", json=ask)).status_code == 200
    assert len(calls) == 2, "unchanged notes do not regenerate"


async def _add_photo(
    owner, *, facts_status="ready", subject="A red ferry at dusk", description="Ferry in harbour"
):
    """A library photo: an original source and (optionally) its image analysis."""
    result = {
        "subject": subject,
        "description": description,
        "on_screen_text": "",
        "kind_hint": "photo",
        "aspect": 0.75,
    }
    async with AsyncSessionLocal() as db:
        asset = MediaAsset(
            user_id=owner.id,
            kind="image",
            status="ready" if facts_status == "ready" else "pending",
            original_availability="cloud_backed",
            metadata_json={},
        )
        db.add(asset)
        await db.flush()
        source = MediaAssetSource(
            user_id=owner.id,
            asset_id=asset.id,
            role="original",
            object_path=f"users/{owner.id}/kri944/{asset.id}.jpg",
            object_generation="42",
            uploaded_sha256=hashlib.sha256(str(asset.id).encode()).hexdigest(),
            byte_count=1234,
            content_type="image/jpeg",
            status="available",
        )
        db.add(source)
        await db.flush()
        if facts_status is not None:
            ref = facts._Source(
                str(asset.id),
                str(source.id),
                str(owner.id),
                "image",
                "original",
                source.object_path,
                "42",
                source.uploaded_sha256,
                "available",
            )
            key = facts._key(ref, None)
            db.add(
                MediaAssetAnalysis(
                    user_id=owner.id,
                    asset_id=asset.id,
                    source_id=source.id,
                    analyzer=key.analyzer,
                    model=key.model,
                    prompt_version=key.prompt_version,
                    schema_version=key.schema_version,
                    source_identity=key.source_identity,
                    status=facts_status,
                    result_json=result if facts_status == "ready" else None,
                )
            )
        await db.commit()
        return str(asset.id), str(source.id)


@pytest.fixture
def stills_buildable(monkeypatch):
    """Open the gate that lets an idea cite a photo (see `media_moments.stills_buildable`)."""
    from app.config import settings
    from app.services import actual_export_verification

    monkeypatch.setattr(actual_export_verification, "VERIFIES_STILL_PICTURES", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["stillImages"])


async def _saved_event(client, owner, videos, photos, group="Day"):
    members = [await _add_video(owner) for _ in range(videos)]
    members += [await _add_photo(owner) for _ in range(photos)]
    path = f"/me/media-collections/{uuid.uuid4()}"
    body = {
        "name": "Mixed",
        "expected_revision": 0,
        "members": [{"asset_id": a, "source_id": s, "group": group} for a, s in members],
    }
    assert (await client.put(path, json=body)).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    return path, members, indexed.json()


async def test_two_photos_and_two_videos_yield_an_idea_citing_a_photo(
    collection_world, stills_buildable, monkeypatch
):
    """Failure catalogue: photos left out of indexing (stuck "untimed"); a photo indexed with the
    video spec or with source time it never had; the model not told a candidate is a photo;
    an idea built only from the videos because photos never reached the candidate list."""
    client, _, _, owner, _ = collection_world
    path, members, indexed = await _saved_event(client, owner, videos=2, photos=2)
    assert indexed["indexed_sources"] == 4
    assert indexed["untimed_asset_ids"] == [] and indexed["missing_analysis_asset_ids"] == []
    photo_assets = {a for a, _ in members[2:]}
    calls = _stub_ideas(
        monkeypatch,
        lambda input: [("All", "One point", "Opening", list(range(len(input.candidates))))],
    )
    response = await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Day"})
    assert response.status_code == 200, response.text
    offered = calls[0].candidates
    assert {c.media for c in offered if str(c.source) in {s for _, s in members[2:]}} == {"still"}
    assert {c.media for c in offered if str(c.source) in {s for _, s in members[:2]}} == {"video"}
    cited = response.json()["stories"][0]["moments"]
    photos = [m for m in cited if m["asset_id"] in photo_assets]
    assert len(photos) == 2
    for moment in photos:
        assert (moment["start_s"], moment["end_s"]) == (0.0, 3.0)
        assert moment["evidence_scope"] == "timed_moment" and moment["kind"] == "visual"
        assert moment["evidence"]["media"] == "still"
        assert moment["text"].startswith("Still photo shown for 3s")
        assert "A red ferry at dusk" in moment["text"]
    # A photo has no speech to leave incomplete.
    assert not photo_assets & set(response.json()["coverage"]["incomplete_speech_asset_ids"])


async def test_a_photo_only_event_yields_ideas(collection_world, stills_buildable, monkeypatch):
    client, _, _, owner, _ = collection_world
    path, members, indexed = await _saved_event(client, owner, videos=0, photos=3)
    assert indexed["indexed_sources"] == 3
    calls = _stub_ideas(
        monkeypatch,
        lambda input: [("Stills", "A point", "Opening", [0, 1, 2])],
    )
    response = await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Day"})
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and {c.media for c in calls[0].candidates} == {"still"}
    story = response.json()["stories"][0]
    assert story["duration_s"] == 9.0 and len(story["moments"]) == 3


async def test_photos_stay_out_of_ideas_until_a_project_with_photos_can_be_built(
    collection_world, monkeypatch
):
    """With the gate closed (the default), photos are analysed and shown but never offered, and
    never hold the event's video ideas back."""
    client, _, _, owner, _ = collection_world
    path, members, indexed = await _saved_event(client, owner, videos=2, photos=2)
    assert indexed["indexed_sources"] == 4
    calls = _stub_ideas(
        monkeypatch,
        lambda input: [("All", "One point", "Opening", list(range(len(input.candidates))))],
    )
    response = await client.post(f"{path}/stories", json={**EVENT_QUERY, "group": "Day"})
    assert response.status_code == 200, response.text
    assert {c.media for c in calls[0].candidates} == {"video"}
    assert {m["asset_id"] for m in response.json()["stories"][0]["moments"]} <= {
        a for a, _ in members[:2]
    }
    assert response.json()["coverage"]["unindexed_asset_ids"] == []


async def test_items_route_reports_photos_ready_after_analysis_and_sets_empty_ones_aside(
    collection_world, stills_buildable, monkeypatch
):
    """Failure catalogue: an analysed photo still shown as waiting; a photo whose analysis holds
    nothing usable offered a pointless retry or left to block its event forever."""
    client, _, _, owner, _ = collection_world
    waiting = await _add_photo(owner, facts_status=None)
    analysed = await _add_photo(owner)
    empty = await _add_photo(owner, subject="", description="")
    video = await _add_video(owner)
    path = f"/me/media-collections/{uuid.uuid4()}"
    pairs = [waiting, analysed, empty, video]
    body = {
        "name": "Photos",
        "expected_revision": 0,
        "members": [{"asset_id": a, "source_id": s, "group": "Day"} for a, s in pairs],
    }
    assert (await client.put(path, json=body)).status_code == 200
    before = await _items(client, path)
    assert before[analysed[0]]["state"] == "ready" and before[analysed[0]]["kind"] == "image"
    assert before[waiting[0]]["state"] == "queued"

    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.json()["missing_analysis_asset_ids"] == [waiting[0]]
    assert indexed.json()["unusable_timing_asset_ids"] == [empty[0]]
    after = await _items(client, path)
    assert after[analysed[0]]["state"] == "ready"
    assert (after[empty[0]]["state"], after[empty[0]]["reason"], after[empty[0]]["retryable"]) == (
        "excluded",
        "no_usable_moment",
        False,
    )

    calls = _stub_ideas(monkeypatch, lambda input: [("Idea", "A point", "Opening", [0])])
    ideas = await client.post(
        f"{path}/stories",
        json={**EVENT_QUERY, "group": "Day", "allow_partial_analysis": False},
    )
    # Only the still-queued photo may hold ideas back; the empty one never does.
    assert ideas.status_code == 409, ideas.text
    partial = await client.post(
        f"{path}/stories", json={**EVENT_QUERY, "group": "Day", "allow_partial_analysis": True}
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["coverage"]["unindexed_asset_ids"] == [waiting[0]]
    assert len(calls) == 1


async def test_reindexing_a_photo_replaces_its_moment_when_the_analysis_changes(
    collection_world, monkeypatch
):
    from app.models import MediaSourceMoment
    from app.pipeline import clip_cache

    # Read the stored fact itself, not this process's copy of it.
    monkeypatch.setattr(clip_cache, "get_versioned_media_cache", lambda key: None)
    client, _, _, owner, _ = collection_world
    path, members, _ = await _saved_event(client, owner, videos=0, photos=1)
    photo, source = members[0]

    async def moments():
        async with AsyncSessionLocal() as db:
            return (
                await db.scalars(
                    select(MediaSourceMoment).where(MediaSourceMoment.asset_id == uuid.UUID(photo))
                )
            ).all()

    first = await moments()
    assert len(first) == 1
    again = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert again.json()["indexed_sources"] == 1
    assert [m.id for m in await moments()] == [first[0].id], "an unchanged photo keeps its row"

    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(MediaAssetAnalysis).where(MediaAssetAnalysis.asset_id == uuid.UUID(photo))
        )
        row.result_json = {**row.result_json, "subject": "A market stall"}
        await db.commit()
    changed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert changed.json()["indexed_sources"] == 1
    replaced = await moments()
    assert len(replaced) == 1 and replaced[0].id != first[0].id
    assert "A market stall" in replaced[0].text
