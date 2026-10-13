"""Authenticated library API: expiry, pagination, quota, archive and stable retry."""

import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.auth import get_current_user, is_native_client
from app.database import AsyncSessionLocal, engine
from app.models import User
from app.routes.media_assets import router
from app.services import media_library_ingest as ingest

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Library API requires a disposable test database", allow_module_level=True)


@pytest.fixture(autouse=True)
async def _dispose_async_pool():
    # Other route modules can leave connections attached to their closed loop.
    await engine.dispose(close=False)
    yield
    await engine.dispose()


@pytest.fixture
async def client(monkeypatch):
    from sqlalchemy import delete

    owner = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=owner, email=f"{owner}@library-api.test"))
        await db.commit()
    monkeypatch.setattr(ingest.settings, "media_library_enabled", True)
    monkeypatch.setattr(
        ingest.storage, "signed_put_url", lambda *args, **kwargs: "https://upload.test/signed"
    )
    monkeypatch.setattr(
        ingest.storage, "object_metadata", lambda *args: SimpleNamespace(generation="42", size=12)
    )
    monkeypatch.setattr(
        ingest,
        "verified_source_metadata",
        lambda *args: dict(
            uploaded_sha256="a" * 64,
            byte_count=12,
            generation="42",
            role="original",
            content_type="image/png",
            metadata={},
        ),
    )
    monkeypatch.setattr(ingest, "dispatch_library_analysis", lambda *args: None)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner)
    app.dependency_overrides[is_native_client] = lambda: True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == owner))
        await db.commit()


@pytest.mark.asyncio
async def test_finalize_then_reserve_never_issues_new_put(client):
    body = dict(
        client_upload_id="photo",
        filename="photo.png",
        kind="image",
        content_type="image/png",
        file_size_bytes=12,
    )
    reserved = await client.post("/me/media-assets/uploads", json=body)
    assert reserved.status_code == 200
    identifier = reserved.json()["reservation_id"]
    finalized = await client.post(
        f"/me/media-assets/uploads/{identifier}/finalize", json={"client_event_id": "first"}
    )
    assert finalized.status_code == 200
    again = await client.post("/me/media-assets/uploads", json=body)
    assert again.json()["asset_id"] == finalized.json()["id"]
    assert again.json()["upload_url"] is None
    assert len((await client.get("/me/media-assets")).json()["assets"]) == 1


@pytest.mark.asyncio
async def test_archive_hides_selection_and_wrong_owner_is_missing(client):
    body = dict(
        client_upload_id="photo",
        filename="photo.png",
        kind="image",
        content_type="image/png",
        file_size_bytes=12,
    )
    reserved = (await client.post("/me/media-assets/uploads", json=body)).json()
    asset = (
        await client.post(
            f"/me/media-assets/uploads/{reserved['reservation_id']}/finalize",
            json={"client_event_id": "first"},
        )
    ).json()
    assert (await client.post(f"/me/media-assets/{asset['id']}/archive")).status_code == 200
    assert (await client.get("/me/media-assets")).json()["assets"] == []
    assert (await client.get(f"/me/media-assets/{uuid.uuid4()}")).status_code == 404
    assert (await client.post(f"/me/media-assets/{asset['id']}/retry")).status_code == 409


@pytest.mark.asyncio
async def test_disabled_and_invalid_cursor_fail_closed(client, monkeypatch):
    assert (await client.get("/me/media-assets?cursor=invalid")).status_code == 422
    monkeypatch.setattr(ingest.settings, "media_library_enabled", False)
    assert (await client.get("/me/media-assets")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, expected, availability", [("gone", 409, "missing"), ("retry", 503, "available")]
)
async def test_access_url_distinguishes_missing_copy_from_transient_failure(
    client, monkeypatch, failure, expected, availability
):
    body = dict(
        client_upload_id="photo",
        filename="photo.png",
        kind="image",
        content_type="image/png",
        file_size_bytes=12,
    )
    reservation = (await client.post("/me/media-assets/uploads", json=body)).json()
    asset = (
        await client.post(
            f"/me/media-assets/uploads/{reservation['reservation_id']}/finalize",
            json={"client_event_id": "first"},
        )
    ).json()

    def failed_metadata(path):
        if failure == "gone":
            raise FileNotFoundError(path)
        raise TimeoutError("Storage request timed out")

    monkeypatch.setattr(ingest.storage, "object_metadata", failed_metadata)
    source_id = asset["sources"][0]["id"]
    access = await client.get(f"/me/media-assets/{asset['id']}/sources/{source_id}/access-url")
    assert access.status_code == expected
    detail = (await client.get(f"/me/media-assets/{asset['id']}")).json()
    assert detail["sources"][0]["status"] == availability


@pytest.mark.asyncio
async def test_access_url_is_fresh_pinned_and_denies_other_source(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    body = dict(
        client_upload_id="photo",
        filename="photo.png",
        kind="image",
        content_type="image/png",
        file_size_bytes=12,
    )
    reservation = (await client.post("/me/media-assets/uploads", json=body)).json()
    asset = (
        await client.post(
            f"/me/media-assets/uploads/{reservation['reservation_id']}/finalize",
            json={"client_event_id": "first"},
        )
    ).json()
    signatures = []
    monkeypatch.setattr(
        ingest.storage,
        "signed_get_url_for_generation",
        lambda path, **kwargs: signatures.append((path, kwargs)) or "https://source.test/fresh",
    )
    url = f"/me/media-assets/{asset['id']}/sources/{asset['sources'][0]['id']}/access-url"
    response = await client.get(url)
    assert response.status_code == 200
    assert signatures[0][1] == {"generation": "42"}
    expires = datetime.fromisoformat(response.json()["expires_at"])
    assert (
        datetime.now(UTC) + timedelta(minutes=4)
        < expires
        < datetime.now(UTC) + timedelta(minutes=6)
    )
    assert (
        await client.get(f"/me/media-assets/{asset['id']}/sources/{uuid.uuid4()}/access-url")
    ).status_code == 404


@pytest.mark.asyncio
async def test_catalogue_pagination_escaping_filters_and_collection_denial(client, monkeypatch):
    # Value: protects=complete ordered owner catalogue and literal search;
    # fails_when=cursor skips assets or search treats creator text as SQL patterns;
    # why_new=API tests covered only an empty archive page;
    # seam=none; no_claim=large catalogue latency (three synthetic assets).
    import hashlib

    monkeypatch.setattr(
        ingest,
        "verified_source_metadata",
        lambda path, generation, kind, payload: dict(
            uploaded_sha256=hashlib.sha256(path.encode()).hexdigest(),
            byte_count=12,
            generation="42",
            role="original",
            content_type=payload["content_type"],
            metadata={},
        ),
    )
    identifiers = set()
    for index, name in enumerate(["100% real.png", "ordinary.png", "third.png"]):
        body = dict(
            client_upload_id=f"catalogue-{index}",
            filename=name,
            kind="image",
            content_type="image/png",
            file_size_bytes=12,
        )
        reserved = (await client.post("/me/media-assets/uploads", json=body)).json()
        result = await client.post(
            f"/me/media-assets/uploads/{reserved['reservation_id']}/finalize",
            json={"client_event_id": name},
        )
        assert result.status_code == 200
        identifiers.add(result.json()["id"])
    seen, cursor = set(), None
    for _ in range(3):
        params = {"limit": 1, "kind": "image"}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/me/media-assets", params=params)).json()
        assert len(page["assets"]) == 1 and page["active_count"] == 3
        seen.add(page["assets"][0]["id"])
        cursor = page["next_cursor"]
    assert seen == identifiers and cursor is None
    literal = (await client.get("/me/media-assets", params={"query": "%"})).json()
    assert (
        len(literal["assets"]) == 1
        and literal["assets"][0]["metadata_json"]["filename"] == "100% real.png"
    )
    assert (await client.get("/me/media-assets", params={"kind": "audio"})).json()["assets"] == []
    assert (
        await client.get("/me/media-assets", params={"collection_id": str(uuid.uuid4())})
    ).status_code == 404
