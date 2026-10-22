"""KRI-822: a device-only library video gains its verified full original for Visuals.

Failure modes this file guards (written before the fix):
- F1  owned device-only video (proxy only) can never become a Visuals source;
- F3  an analysis proxy is promoted to, or registered as, the original;
- F4  bytes that differ from the declared original (size or sha) change the asset;
- F5  another account upgrades or projects the owner's asset;
- F6  an archived asset is upgraded;
- F9  with both sources present, a source-less Visuals pick selects the proxy;
- F10 a lost finalize response duplicates the source or the catalogue entry;
- F15 a real iPhone .mov original is rejected by the mp4-only / 512 MB library limits;
- F16 a multi-GB original is staged whole on the API machine's RAM-backed /tmp;
- F17 the fixed upload identity dead-ends once its target is cleaned up, or once the
      verified cloud original is lost (every later upgrade replays the stale row);
- F18 the upgrade flips the asset's contract, so its proxy can no longer be repaired.
- F19 attaching the original resets a ready asset to pending and re-runs paid library
      analysis on the multi-GB original (whose failure would then mark Footage failed);
- F20 an attached original is listed first, so pre-KRI-822 clients bind it for Footage;
- F21 the real generation-pinned stream / probe input yields wrong or partial bytes.

Runs the real routes and the real byte verification (sha256 + ffprobe) against
PostgreSQL; only GCS is replaced by a local byte store.
"""

import hashlib
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select

from app import storage
from app.auth import get_current_user, is_native_client
from app.database import AsyncSessionLocal, engine
from app.kria.media_sources import MediaUploadContract
from app.models import (
    ContentPlan,
    MediaAsset,
    MediaAssetSource,
    Persona,
    PlanItem,
    PlanItemAsset,
    TemporaryMediaUpload,
    User,
)
from app.routes.media_assets import router
from app.services import media_library_ingest as ingest
from app.services import media_library_projections as projections
from tests.services.test_phone_sources import receipt

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Library upgrade requires a disposable test database", allow_module_level=True)
if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    pytest.skip("Library upgrade verification needs ffmpeg/ffprobe", allow_module_level=True)


@pytest.fixture(autouse=True)
async def _dispose_async_pool():
    await engine.dispose(close=False)
    yield
    await engine.dispose()


@pytest.fixture(scope="module")
def original_mov(tmp_path_factory) -> bytes:
    """A real QuickTime original, like the one an iPhone keeps in Photos."""
    path = tmp_path_factory.mktemp("original") / "horse.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


async def _user(db) -> uuid.UUID:
    owner = uuid.uuid4()
    db.add(User(id=owner, email=f"{owner}@library-upgrade.test"))
    await db.flush()
    return owner


@pytest.fixture
async def world(monkeypatch, original_mov, tmp_path_factory):
    monkeypatch.setattr(ingest.settings, "media_library_enabled", True)
    monkeypatch.setattr(ingest, "dispatch_library_analysis", lambda *args: None)
    monkeypatch.setattr(projections, "get_media_asset_analysis", lambda **kwargs: None)
    contract = receipt()["upload_contract"]
    declared_sha = hashlib.sha256(original_mov).hexdigest()
    contract["proxy"]["original"]["sha256"] = declared_sha
    contract["proxy"]["original"]["byte_count"] = len(original_mov)
    # Stored exactly as the upload route stores it (pydantic-normalized).
    contract = MediaUploadContract.model_validate(contract).model_dump(mode="json")
    async with AsyncSessionLocal() as db:
        owner = await _user(db)
        stranger = await _user(db)
        persona, plan, item = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        db.add(Persona(id=persona, user_id=owner, persona={}))
        await db.flush()
        db.add(ContentPlan(id=plan, persona_id=persona, user_id=owner))
        await db.flush()
        db.add(PlanItem(id=item, content_plan_id=plan, position=0, idea="Horse slider"))
        asset, proxy = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="video",
            object_path=f"users/{owner}/media-library/p/analysis-proxy-horse.mp4",
            generation="10",
            uploaded_sha256="e" * 64,
            byte_count=12,
            content_type="video/mp4",
            role="analysis_proxy",
            original_declared_sha256=declared_sha,
            original_byte_count=len(original_mov),
            metadata={"filename": "horse.mov", "upload_contract": contract},
        )
        asset_id, proxy_id = asset.id, proxy.id
        await db.commit()

    # Local stand-in for GCS: the PUT lands here; generations are stable per path.
    objects: dict[str, bytes] = {}

    def object_metadata(path):
        if path.endswith("analysis-proxy-horse.mp4"):
            return SimpleNamespace(generation="10", size=12)
        if path not in objects:
            raise FileNotFoundError(path)
        return SimpleNamespace(generation="77", size=len(objects[path]))

    def download(path, local, **kwargs):
        # F16: an original is streamed, never downloaded whole.
        raise AssertionError(f"whole-object download of {path}")

    def stream(path, *, generation, size, chunk_size=8 * 1024 * 1024):
        data = objects[path]
        assert generation == "77" and size == len(data)
        for start in range(0, len(data), 4096):
            yield data[start : start + 4096]

    probes = tmp_path_factory.mktemp("probe")

    def probe_input(path, *, generation):
        local = probes / f"{uuid.uuid4()}.mov"
        local.write_bytes(objects[path])
        return str(local)

    monkeypatch.setattr(storage, "object_metadata", object_metadata)
    monkeypatch.setattr(storage, "download_generation_to_file", download)
    monkeypatch.setattr(storage, "iter_object_generation", stream)
    monkeypatch.setattr(storage, "probe_input_for_generation", probe_input)
    monkeypatch.setattr(storage, "signed_put_url", lambda *a, **k: "https://upload.test/signed")

    current = {"user": owner}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=current["user"])
    app.dependency_overrides[is_native_client] = lambda: True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client,
            current=current,
            owner=owner,
            stranger=stranger,
            item=item,
            asset=asset_id,
            proxy=proxy_id,
            contract=contract,
            objects=objects,
            original=original_mov,
            declared_sha=declared_sha,
        )
    async with AsyncSessionLocal() as db:
        await db.execute(delete(PlanItemAsset).where(PlanItemAsset.user_id == owner))
        await db.execute(delete(User).where(User.id.in_([owner, stranger])))
        await db.commit()


def _upgrade_body(w, **overrides):
    body = dict(
        client_upload_id=f"ios-original-{w.asset}",
        filename="horse.mov",
        kind="video",
        content_type="video/quicktime",
        file_size_bytes=len(w.original),
        upload_contract={"purpose": "cloud_render_source", "proxy": None},
        repair_asset_id=str(w.asset),
        attach_original=True,
    )
    return {**body, **overrides}


async def _upload(w, body, data: bytes):
    reserved = await w.client.post("/me/media-assets/uploads", json=body)
    if reserved.status_code != 200:
        return reserved
    reservation = reserved.json()
    if reservation["upload_url"]:
        w.objects[reservation["object_path"]] = data
    return await w.client.post(
        f"/me/media-assets/uploads/{reservation['reservation_id']}/finalize",
        json={"client_event_id": body["client_upload_id"]},
    )


async def _sources(asset_id):
    async with AsyncSessionLocal() as db:
        asset = await db.get(MediaAsset, asset_id)
        rows = (
            await db.scalars(
                select(MediaAssetSource)
                .where(MediaAssetSource.asset_id == asset_id)
                .order_by(MediaAssetSource.created_at)
            )
        ).all()
        return asset.original_availability, [
            (row.id, row.role, row.uploaded_sha256, row.status) for row in rows
        ]


async def _project(w, owner=None, source_id=None):
    async with AsyncSessionLocal() as db:
        row = await projections.project_library_visual(
            db,
            owner_id=owner or w.owner,
            item_id=w.item,
            asset_id=w.asset,
            source_id=source_id,
        )
        await db.commit()
        return row.library_source_id, row.gcs_path, row.upload_size_bytes


@pytest.mark.asyncio
async def test_device_only_video_reaches_visuals_through_its_verified_original(world):
    w = world
    # Before: the device-only asset only has its proxy, which Visuals must refuse (F1/F3).
    with pytest.raises(HTTPException) as refused:
        await _project(w)
    assert refused.value.status_code == 422

    finalized = await _upload(w, _upgrade_body(w), w.original)
    assert finalized.status_code == 200, finalized.text
    out = finalized.json()
    assert out["id"] == str(w.asset)
    # F18: the asset keeps its device-only proxy contract; the original row is additive.
    assert out["original_availability"] == "device_only"
    roles = {source["role"]: source for source in out["sources"]}
    assert roles["original"]["uploaded_sha256"] == w.declared_sha
    assert roles["original"]["byte_count"] == len(w.original)
    assert roles["original"]["content_type"] == "video/quicktime"
    assert roles["analysis_proxy"]["id"] == str(w.proxy)
    original_id = uuid.UUID(roles["original"]["id"])

    # F10: a lost finalize response is retried with the same client id.
    retry = await _upload(w, _upgrade_body(w), w.original)
    assert retry.status_code == 200
    availability, sources = await _sources(w.asset)
    assert availability == "device_only"
    assert [(role, status) for _, role, _, status in sources] == [
        ("analysis_proxy", "available"),
        ("original", "available"),
    ]
    # The proxy row is untouched (F3).
    assert sources[0][0] == w.proxy and sources[0][2] == "e" * 64
    async with AsyncSessionLocal() as db:
        count = len(
            (await db.scalars(select(MediaAsset).where(MediaAsset.user_id == w.owner))).all()
        )
    assert count == 1

    # The Visuals projection binds the full original, by id and when unspecified (F9).
    projected = await _project(w, source_id=original_id)
    assert projected[0] == original_id
    assert projected[1].endswith(".mov") and "analysis-proxy" not in projected[1]
    assert projected[2] == len(w.original)
    assert (await _project(w))[0] == original_id

    # F5: the stranger cannot project the owner's upgraded asset.
    with pytest.raises(HTTPException) as foreign:
        await _project(w, owner=w.stranger, source_id=original_id)
    assert foreign.value.status_code == 404


@pytest.mark.asyncio
async def test_upgrade_requires_the_exact_original_from_the_owner(world):
    w = world
    # F4: different size is refused before a PUT URL is signed.
    short = await _upload(w, _upgrade_body(w, file_size_bytes=len(w.original) - 1), b"x")
    assert short.status_code == 422
    # F4: same size, different bytes (another file picked in Files) is refused at finalize.
    impostor = bytearray(w.original)
    impostor[-1] ^= 0xFF
    wrong = await _upload(w, _upgrade_body(w, client_upload_id="wrong-file"), bytes(impostor))
    assert wrong.status_code == 422, wrong.text
    assert "not the original" in wrong.json()["detail"]
    # F3: the upgrade contract never accepts a proxy as the original.
    proxied = await _upload(
        w, _upgrade_body(w, client_upload_id="proxy", upload_contract=w.contract), w.original
    )
    assert proxied.status_code == 422
    # F3: an image or a missing target is not an original upgrade.
    no_target = await _upload(
        w, _upgrade_body(w, client_upload_id="none", repair_asset_id=None), w.original
    )
    assert no_target.status_code == 422
    # F5: a stranger cannot upgrade the owner's asset with the right bytes.
    w.current["user"] = w.stranger
    foreign = await _upload(w, _upgrade_body(w, client_upload_id="foreign"), w.original)
    assert foreign.status_code == 404
    w.current["user"] = w.owner
    availability, sources = await _sources(w.asset)
    assert availability == "device_only"
    assert [role for _, role, _, _ in sources] == ["analysis_proxy"]

    # F6: archived assets are never upgraded.
    async with AsyncSessionLocal() as db:
        await ingest.archive_library_asset(db, w.owner, w.asset)
    archived = await _upload(w, _upgrade_body(w, client_upload_id="archived"), w.original)
    assert archived.status_code == 409
    assert (await _sources(w.asset))[0] == "device_only"


async def _claim_receipts(owner):
    async with AsyncSessionLocal() as db:
        for receipt_row in (
            await db.scalars(
                select(TemporaryMediaUpload).where(TemporaryMediaUpload.user_id == owner)
            )
        ).all():
            receipt_row.cleanup_claimed_at = datetime.now(UTC)
        await db.commit()


@pytest.mark.asyncio
async def test_upgrade_identity_recovers_after_cleanup_and_after_the_cloud_copy_is_lost(world):
    w = world
    body = _upgrade_body(w, client_upload_id=f"ios-original-{w.asset}-{w.declared_sha}")
    # The PUT never finished (app killed) and cleanup later claimed the target (F17).
    first = await w.client.post("/me/media-assets/uploads", json=body)
    assert first.status_code == 200 and first.json()["upload_url"]
    await _claim_receipts(w.owner)
    finalized = await _upload(w, body, w.original)
    assert finalized.status_code == 200, finalized.text
    first_original = next(s for s in finalized.json()["sources"] if s["role"] == "original")
    assert first_original["status"] == "available"

    # The verified original later vanishes from storage (lifecycle or drift) (F17).
    w.objects.pop(next(path for path in w.objects if path.endswith(".mov")))
    with pytest.raises(HTTPException) as lost:
        await _project(w)
    assert lost.value.status_code == 409
    again = await _upload(w, body, w.original)
    assert again.status_code == 200, again.text
    availability, sources = await _sources(w.asset)
    assert availability == "device_only"
    by_status = sorted((role, status) for _, role, _, status in sources)
    assert by_status == [
        ("analysis_proxy", "available"),
        ("original", "available"),
        ("original", "missing"),
    ]
    fresh = next(
        sid for sid, role, _, status in sources if role == "original" and status == "available"
    )
    assert fresh != uuid.UUID(first_original["id"])
    assert (await _project(w))[0] == fresh


@pytest.mark.asyncio
async def test_proxy_repair_still_works_after_the_upgrade(world):
    w = world
    assert (await _upload(w, _upgrade_body(w), w.original)).status_code == 200
    # F18: the iOS proxy repair sends the asset's stored proxy contract; it must stay valid.
    async with AsyncSessionLocal() as db:
        asset = await db.get(MediaAsset, w.asset)
        assert asset.metadata_json["upload_contract"] == w.contract
        assert asset.original_availability == "device_only"
    reserved = await w.client.post(
        "/me/media-assets/uploads",
        json=dict(
            client_upload_id=f"ios-repair-{w.asset}",
            filename="horse.mov",
            kind="video",
            content_type="video/mp4",
            file_size_bytes=12,
            upload_contract=w.contract,
            repair_asset_id=str(w.asset),
        ),
    )
    assert reserved.status_code == 200, reserved.text


@pytest.mark.asyncio
async def test_attached_original_keeps_analysis_and_footage_on_the_proxy(world, monkeypatch):
    from app.services.media_library_analysis import _requested_source

    w = world
    dispatched = []
    monkeypatch.setattr(ingest, "dispatch_library_analysis", lambda *a: dispatched.append(a))
    async with AsyncSessionLocal() as db:
        asset = await db.get(MediaAsset, w.asset)
        asset.status = "ready"
        await db.commit()

    finalized = await _upload(w, _upgrade_body(w), w.original)
    assert finalized.status_code == 200, finalized.text
    # F19: no new library analysis; the asset stays ready on its proxy facts.
    assert dispatched == []
    async with AsyncSessionLocal() as db:
        assert (await db.get(MediaAsset, w.asset)).status == "ready"
    chosen = _requested_source(str(w.asset), str(w.owner), None)
    assert chosen is not None and chosen.source_id == str(w.proxy)
    assert chosen.role == "analysis_proxy"
    # F20: older clients take the first available source for phone Footage.
    listed = (await w.client.get("/me/media-assets")).json()["assets"]
    mine = next(row for row in listed if row["id"] == str(w.asset))
    assert [source["role"] for source in mine["sources"]] == ["analysis_proxy", "original"]


def test_generation_stream_and_probe_input_read_the_pinned_bytes(
    monkeypatch, tmp_path, original_mov
):
    """F21: the real local-storage branches, no monkeypatched storage."""
    monkeypatch.setattr(storage.settings, "storage_provider", "local")
    monkeypatch.setattr(storage.settings, "e2e_fixtures", True)
    monkeypatch.setattr(storage.settings, "local_storage_root", str(tmp_path))
    name = "users/u/media-library/x/source-x.mov"
    storage.local_object_path(name).parent.mkdir(parents=True, exist_ok=True)
    storage.local_object_path(name).write_bytes(original_mov)
    generation = storage.object_metadata(name).generation
    streamed = b"".join(
        storage.iter_object_generation(
            name, generation=generation, size=len(original_mov), chunk_size=1000
        )
    )
    assert hashlib.sha256(streamed).hexdigest() == hashlib.sha256(original_mov).hexdigest()
    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            storage.probe_input_for_generation(name, generation=generation),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "video" in probed.stdout
    with pytest.raises(FileNotFoundError):
        list(storage.iter_object_generation(name, generation="stale", size=len(original_mov)))
    with pytest.raises(FileNotFoundError):
        storage.probe_input_for_generation(name, generation="stale")
