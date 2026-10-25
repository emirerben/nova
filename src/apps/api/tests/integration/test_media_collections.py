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
    monkeypatch.setattr(settings, "media_collection_proposal_user_ids", [owner.id])
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
    monkeypatch.setattr(settings, "media_collection_proposal_user_ids", [])
    assert (await client.get("/creation-threads/capabilities")).json()["media_collections"] is False
    assert (await client.get(f"/me/media-collections/{uuid.uuid4()}")).status_code == 404

    monkeypatch.setattr(settings, "media_collection_proposal_user_ids", [owner.id])
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
    from app.services.media_moments import _index_spec

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
        assert len(input.candidates) <= 100
        selected_sources = {value.source for value in input.candidates}
        assert len(selected_sources) == min(collection_size, 100)
        assert selected_sources <= set(source_ids)
        if collection_size <= 100:
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
    assert len(checked) == min(collection_size, 100)
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


def test_real_targeted_read_crops_bytes_before_one_bounded_model_call(tmp_path, monkeypatch):
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
    result = inspect_interval(
        source,
        SimpleNamespace(start_s=2, end_s=3),
        "Is this cyan?",
        ctx=RunContext(deadline_monotonic=time.monotonic() + 45),
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
    failed = await client.post(f"{path}/stories", json={**body, "query": "A different direction"})
    assert failed.status_code == 503 and "stories" not in failed.json()
    assert len(calls) == 8
    invalid = await client.post(
        f"{path}/stories", json={**body, "research_topic": "https://private.local/x"}
    )
    assert invalid.status_code == 422 and len(calls) == 8
    source_approved = False
    unsupported = await client.post(
        f"{path}/stories", json={**body, "query": "Another source selection"}
    )
    assert unsupported.status_code == 503 and len(calls) == 11
    assert [row[0] for row in calls[-3:]] == ["search", "ideas", "source_review"]
    source_url = "https://travel-blog.example/terminal"
    ungrounded = await client.post(f"{path}/stories", json={**body, "query": "A third direction"})
    assert ungrounded.status_code == 503 and len(calls) == 12


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
                audience="Evening travellers",
                angle=angle,
                opening_text=opening,
                caption=caption,
                why_it_works="The recorded view supports this particular point.",
                beat_reasons=["The house and ferry supply the setting"],
            ),
        )
        for title, angle, opening, caption in plans
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
