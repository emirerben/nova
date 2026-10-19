"""KRI-729/730 HTTP → real PostgreSQL, before the new collection implementation.

Failure cases are preregistered in docs/reviews/camera-roll-recommendations/acceptance.md.
No provider, storage download, upload, render or original backup is permitted here.
"""

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select

from app.auth import get_current_user, is_native_client
from app.database import AsyncSessionLocal, get_db
from app.models import MediaAsset, MediaAssetAnalysis, MediaAssetSource, User
from app.services import media_library_analysis as facts

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Requires disposable Postgres", allow_module_level=True)


@pytest.fixture
async def collection_world(monkeypatch):
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

    def no_download(*args, **kwargs):
        raise AssertionError("Collection indexing must not read media bytes")

    monkeypatch.setattr(storage, "download_generation_to_file", no_download)
    monkeypatch.setattr(storage, "download_to_file", no_download)
    monkeypatch.setattr(facts, "_analyze_source", no_download)
    owner = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@collection.test")
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
                object_path=f"users/{asset.user_id}/media-library/{asset.id}.mp4",
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
                            ]
                        },
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
        await db.execute(delete(User).where(User.id.in_([owner.id, other.id])))
        await db.commit()


def selection(assets, sources, count=2):
    return {
        "name": "Ferry outing",
        "expected_revision": 0,
        "members": [{"asset_id": assets[i], "source_id": sources[i]} for i in range(count)],
    }


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
