"""Historical owned bytes -> catalogue -> second-post reuse, on real Postgres."""

import asyncio
import copy
import io
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import delete, func, select

from app import storage
from app.database import AsyncSessionLocal
from app.models import (
    ContentPlan,
    CreationThread,
    MediaAsset,
    MediaAssetSource,
    Persona,
    PlanItem,
    PlanItemAsset,
    User,
)
from app.routes import media_assets
from app.services import media_library_ingest as ingest
from app.services.media_library_projections import project_library_visual
from scripts import backfill_media_library as backfill

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Backfill requires a disposable database", allow_module_level=True)


@pytest.fixture
async def world(monkeypatch, tmp_path):
    owner, stranger, persona, plan = (uuid.uuid4() for _ in range(4))
    first, second, thread = (uuid.uuid4() for _ in range(3))
    prefix = f"users/{owner}/creation-threads/{thread}/"
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    video = tmp_path / "proxy.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=32x32:r=15:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    contract = {
        "purpose": "analysis_proxy",
        "proxy": {
            "original": {
                "sha256": "a" * 64,
                "byte_count": 1000,
                "duration_s": 1,
                "width": 320,
                "height": 320,
                "has_audio": False,
            },
            "duration_s": 1,
            "width": 32,
            "height": 32,
            "frame_rate": 15,
        },
    }
    clip = {"gcs_path": prefix + "analysis-proxy-old", "media_id": "analysis-proxy-old"}
    state = {
        "media": [
            {
                "media_id": clip["media_id"],
                "kind": "video",
                "filename": "old.mov",
                "upload_contract": contract,
            }
        ],
        "media_count": 1,
    }
    paths = {
        clip["gcs_path"]: (video.read_bytes(), "video/mp4", "42"),
        prefix + "photo.png": (image.getvalue(), "image/png", "43"),
    }
    reads = []

    def metadata(path):
        data, content_type, generation = paths[path]
        return SimpleNamespace(size=len(data), generation=generation, content_type=content_type)

    def download(path, local, *, generation):
        data, _, actual = paths[path]
        assert actual == generation
        reads.append(path)
        Path(local).write_bytes(data)

    def forbidden(*args, **kwargs):
        raise AssertionError("Backfill must not upload or call providers")

    monkeypatch.setattr(storage, "object_metadata", metadata)
    monkeypatch.setattr(storage, "download_generation_to_file", download)
    monkeypatch.setattr(storage, "upload_public_read", forbidden)
    monkeypatch.setattr(ingest, "dispatch_library_analysis", forbidden)
    monkeypatch.setattr(ingest.settings, "media_library_enabled", True)
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                User(id=owner, email=f"{owner}@backfill.test"),
                User(id=stranger, email=f"{stranger}@backfill.test"),
            ]
        )
        await db.flush()
        db.add(Persona(id=persona, user_id=owner, persona={}))
        await db.flush()
        db.add(ContentPlan(id=plan, persona_id=persona, user_id=owner))
        await db.flush()
        db.add_all(
            [
                PlanItem(
                    id=first,
                    content_plan_id=plan,
                    position=0,
                    idea="First",
                    clip_assignments=[clip],
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                PlanItem(id=second, content_plan_id=plan, position=1, idea="Second"),
            ]
        )
        await db.flush()
        db.add(
            CreationThread(
                id=thread, creator_id=owner, active_plan_item_id=first, state=state, revision=7
            )
        )
        db.add(
            PlanItemAsset(
                user_id=owner,
                plan_item_id=first,
                kind="image",
                gcs_path=prefix + "photo.png",
                gcs_generation="43",
                source_filename="photo.png",
                status="ready",
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        # Ownership must be proven by both the row and its owning plan.
        db.add(
            PlanItemAsset(
                user_id=stranger,
                plan_item_id=first,
                kind="image",
                gcs_path=f"users/{stranger}/stolen.png",
                status="ready",
            )
        )
        await db.commit()
    yield SimpleNamespace(
        owner=owner,
        stranger=stranger,
        first=first,
        second=second,
        thread=thread,
        paths=paths,
        clip=clip,
        state=state,
        reads=reads,
    )
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ContentPlan).where(ContentPlan.id == plan))
        await db.execute(delete(User).where(User.id.in_([owner, stranger])))
        await db.commit()


async def test_dry_run_import_list_reuse_and_idempotence(world):
    w = world
    dry = await backfill.run(w.owner)
    assert dry["counts"]["eligible"] == 2
    assert w.reads == []
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(MediaAsset).where(MediaAsset.user_id == w.owner)
            )
            == 0
        )
    results = await asyncio.gather(
        backfill.run(w.owner, apply=True), backfill.run(w.owner, apply=True)
    )
    assert sum(r["counts"].get("created", 0) for r in results) == 2
    await backfill.run(w.owner, apply=True)
    async with AsyncSessionLocal() as db:
        page = await media_assets.list_assets(
            SimpleNamespace(id=w.owner),
            db,
            limit=100,
            cursor=None,
            query=None,
            kind=None,
            status=None,
            collection_id=None,
        )
        assert page.active_count == 2
        assert all(a.status == "ready" and a.analysis is None for a in page.assets)
        video = next(a for a in page.assets if a.kind == "video")
        assert video.original_availability == "device_only"
        assert video.original_declared_sha256 == "a" * 64
        assert video.sources[0].role == "analysis_proxy"
        assert any(u.creation_thread_id == w.thread for u in video.usage)
        photo = next(a for a in page.assets if a.kind == "image")
        # The regular reuse path attaches the verified source to a new post without PUT.
        projected = await project_library_visual(
            db, owner_id=w.owner, item_id=w.second, asset_id=photo.id
        )
        assert projected.gcs_path.endswith("photo.png")
        await db.commit()
        original = await db.get(PlanItem, w.first)
        old_thread = await db.get(CreationThread, w.thread)
        assert original.clip_assignments == [w.clip]
        assert old_thread.state == w.state and old_thread.revision == 7
        assert (
            await db.scalar(
                select(func.count())
                .select_from(MediaAssetSource)
                .where(MediaAssetSource.user_id == w.owner)
            )
            == 2
        )
    assert set(w.reads) == set(w.paths)


async def test_stale_missing_proxy_receipt_and_transfer_budget(world):
    w = world
    async with AsyncSessionLocal() as db:
        photo = await db.scalar(select(PlanItemAsset).where(PlanItemAsset.user_id == w.owner))
        photo.gcs_generation = "changed"
        old = await db.get(CreationThread, w.thread)
        old.state = {"media": []}
        await db.commit()
    report = await backfill.run(w.owner, apply=True)
    assert report["counts"].get("created", 0) == 0
    assert report["counts"]["generation_changed"] == 1
    assert report["counts"]["proxy_receipt_missing"] == 1
    async with AsyncSessionLocal() as db:
        old = await db.get(CreationThread, w.thread)
        old.state = copy.deepcopy(w.state)
        await db.commit()
    report = await backfill.run(w.owner, apply=True, max_bytes=1)
    assert report["counts"]["byte_budget"] == 1
    assert not w.reads


async def test_quota_and_archived_sources_are_not_bypassed(world, monkeypatch):
    w = world
    monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 1)
    report = await backfill.run(w.owner, apply=True)
    assert report["counts"]["created"] == 1
    assert report["counts"]["library_quota"] == 1
    async with AsyncSessionLocal() as db:
        asset = await db.scalar(select(MediaAsset).where(MediaAsset.user_id == w.owner))
        assert asset.kind == "image"  # newest upload wins when capacity is one
        asset.archived_at = datetime.now(UTC)
        await db.commit()
    again = await backfill.run(w.owner, apply=True)
    assert again["counts"]["archived"] == 1
    assert again["counts"]["created"] == 1
    # A row claiming another user, attached to this owner's plan, grants no access.
    foreign = await backfill.run(w.stranger, apply=True)
    assert foreign["counts"].get("created", 0) == 0
