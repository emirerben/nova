"""HTTP owner fences, upload idempotency, and finalization races."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.main import app
from app.routes import device_render as routes
from app.services.device_render import device_record, pin_device_request, save_device_record


def scalar(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.fixture
def fixture(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        status="processing",
        content_plan_item_id=None,
        assembly_plan={"variants": [{"variant_id": "first", "render_generation_id": "approved"}]},
    )
    recipe = EditRecipeV1(
        assets=[{"id": "a", "relative_path": "a"}],
        tracks=[
            {
                "id": "v",
                "kind": "video",
                "clips": [
                    {
                        "id": "c",
                        "source_asset_id": "a",
                        "source_start": 0,
                        "source_duration": 2,
                        "timeline_start": 0,
                        "rate": 1,
                    }
                ],
            }
        ],
    )
    request = make_device_request(job_id=job.id, variant_id="first", revision=1, recipe=recipe)
    pin_device_request(job, request, base_generation="approved")
    db = AsyncMock()
    db.add = MagicMock()
    db.get.return_value = job
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(routes.limiter, "enabled", False)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(user=user, job=job, request=request, db=db, client=client)
    app.dependency_overrides.clear()


def body(fixture, attempt=None):
    return {
        "identity": fixture.request.identity.model_dump(mode="json"),
        "attempt_id": attempt or str(uuid.uuid4()),
    }


def prepared(fixture):
    attempt = str(uuid.uuid4())
    record = device_record(fixture.job, "first")
    record["status"]["phase"] = "syncing"
    path = f"{fixture.user.id}/{fixture.job.id}/device/{attempt}.mp4"
    record["attempts"][attempt] = {"path": path, "size": 12, "sha256": "a" * 64}
    save_device_record(fixture.job, "first", record)
    return attempt, path


def mock_storage(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: SimpleNamespace(size=12, content_type="video/mp4", generation="42"),
    )
    monkeypatch.setattr(routes.storage, "signed_get_url", lambda _: "https://storage.example/video")


def test_foreign_job_is_filtered_before_recipe_lookup(fixture):
    fixture.db.execute.side_effect = [scalar(None), scalar(None)]
    response = fixture.client.get(f"/me/jobs/{fixture.job.id}/device-render?variant_id=first")
    assert response.status_code == 404
    query = fixture.db.execute.await_args.args[0].compile()
    assert fixture.user.id in query.params.values()
    assert fixture.job.id in query.params.values()


def test_kill_switch_blocks_new_upload_reservations(fixture, monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/uploads",
        json={**body(fixture), "file_size_bytes": 12, "sha256": "a" * 64},
    )
    assert response.status_code == 404
    fixture.db.execute.assert_not_called()


def test_reservation_persists_cleanup_before_signing_and_rejects_changed_bytes(
    fixture, monkeypatch
):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    fixture.db.execute.return_value = scalar(None)

    def sign(*args):
        fixture.db.commit.assert_awaited_once()
        assert fixture.db.add.call_args.args[0].purpose == "device_export"
        return "https://storage.example/upload"

    monkeypatch.setattr(routes.storage, "signed_put_url", sign)
    payload = {**body(fixture), "file_size_bytes": 12, "sha256": "a" * 64}
    response = fixture.client.post(f"/me/jobs/{fixture.job.id}/device-render/uploads", json=payload)
    assert response.status_code == 200
    assert response.json()["upload_headers"]["x-goog-if-generation-match"] == "0"
    payload["sha256"] = "b" * 64
    assert (
        fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/uploads", json=payload
        ).status_code
        == 409
    )


def test_editor_change_during_verification_rejects_late_publication(fixture, monkeypatch):
    attempt, _ = prepared(fixture)
    mock_storage(fixture, monkeypatch)

    def verify(*args):
        fixture.job.assembly_plan["variants"][0]["render_generation_id"] = "new-edit"

    monkeypatch.setattr(routes, "_verify_export", verify)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert response.status_code == 409
    fixture.db.commit.assert_not_awaited()
    assert fixture.job.assembly_plan["variants"][0]["render_generation_id"] == "new-edit"


def test_verified_publication_attaches_cleanup_and_reconciles_lost_response(fixture, monkeypatch):
    attempt, path = prepared(fixture)
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    verifier = MagicMock()
    monkeypatch.setattr(routes, "_verify_export", verifier)
    cleanup = SimpleNamespace(
        user_id=fixture.user.id,
        status="reserved",
        retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    fixture.db.execute.return_value = scalar(cleanup)
    payload = body(fixture, attempt)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=payload
    )
    assert response.status_code == 200
    assert cleanup.status == "attached"
    assert fixture.job.status == "variants_ready"
    assert fixture.job.assembly_plan["variants"][0]["video_path"] == path
    assert (
        fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/complete", json=payload
        ).status_code
        == 200
    )
    verifier.assert_called_once()
    assert (
        fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture)
        ).status_code
        == 409
    )


@pytest.mark.parametrize("change", ["epoch", "item"])
async def test_owner_fence_rechecks_job_after_lock(fixture, monkeypatch, change):
    item_id = uuid.uuid4()
    fixture.job.content_plan_item_id = item_id
    fixture.job.content_plan_ownership_epoch = 3
    item = SimpleNamespace(id=item_id, content_plan_id=uuid.uuid4(), current_job_id=fixture.job.id)
    plan = SimpleNamespace(user_id=fixture.user.id, ownership_epoch=3)
    fixture.db.execute.side_effect = [scalar(None), scalar(fixture.job)]
    monkeypatch.setattr(routes, "load_owned_plan_persona", AsyncMock())
    calls = 0

    async def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 or calls == 3:
            return item
        if calls == 2:
            return plan
        if change == "epoch":
            fixture.job.content_plan_ownership_epoch = 4
        else:
            fixture.job.content_plan_item_id = uuid.uuid4()
        return fixture.job

    fixture.db.get.side_effect = get
    with pytest.raises(routes.HTTPException) as failure:
        await routes._owned_job(fixture.db, fixture.user.id, fixture.job.id)
    assert failure.value.status_code == 409


def test_kill_switch_holds_unstarted_recipes_without_mutating_them(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    response = fixture.client.get(f"/me/jobs/{fixture.job.id}/device-render?variant_id=first")
    assert response.status_code == 200
    assert response.json()["phase"] == "needs_attention"
    assert device_record(fixture.job, "first")["status"]["phase"] == "awaiting_device"
    fixture.db.commit.assert_not_awaited()


@pytest.mark.parametrize("during_verification", [False, True])
def test_held_recipe_cannot_publish_reserved_output(fixture, monkeypatch, during_verification):
    attempt, _ = prepared(fixture)
    mock_storage(fixture, monkeypatch)

    def hold(*args):
        record = device_record(fixture.job, "first")
        record["status"]["phase"] = "needs_attention"
        save_device_record(fixture.job, "first", record)

    verifier = MagicMock(side_effect=hold)
    monkeypatch.setattr(routes, "_verify_export", verifier)
    if not during_verification:
        hold()
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert response.status_code == 409
    assert verifier.call_count == int(during_verification)
    fixture.db.commit.assert_not_awaited()


def library_recipe(fixture, monkeypatch):
    from app.kria.recipes_v2 import EditRecipeV2
    from app.kria.render_assets import LibraryRenderAsset

    asset = LibraryRenderAsset(
        id="a",
        catalog="music",
        catalog_id="track",
        generation="42",
        fingerprint={"sha256": "a" * 64, "byte_count": 12},
    )
    value = fixture.request.recipe.model_dump(mode="json")
    value.update(
        schema_version=2,
        renderer_version="kria-ios-2",
        asset_manifest={"assets": [asset.model_dump()]},
    )
    value["assets"][0]["fingerprint"] = {
        "algorithm": "sha256",
        "hex": "a" * 64,
        "byte_count": 12,
    }
    fixture.request = make_device_request(
        job_id=fixture.job.id,
        variant_id="first",
        revision=2,
        recipe=EditRecipeV2.model_validate(value),
    )
    pin_device_request(fixture.job, fixture.request, base_generation="approved")
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    monkeypatch.setattr(routes, "catalog_path", AsyncMock(return_value="music/track/audio.mp3"))
    monkeypatch.setattr(routes, "inspect_library_asset", lambda *a, **kw: asset)
    signer = MagicMock(return_value="https://storage.example/pinned")
    monkeypatch.setattr(routes.storage, "signed_get_url_for_generation", signer)
    return asset, signer


def download_asset(fixture, **changes):
    return fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/assets",
        json={
            "identity": fixture.request.identity.model_dump(mode="json"),
            "asset_id": "a",
            **changes,
        },
    )


def test_library_download_pins_generation(fixture, monkeypatch):
    _, signer = library_recipe(fixture, monkeypatch)
    response = download_asset(fixture)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with("music/track/audio.mp3", generation="42")
    assert response.json()["asset_id"] == "a"
    assert routes._owned_job.await_count == 2
    fixture.db.rollback.assert_awaited_once()


@pytest.mark.parametrize(
    "mutation", ["generation", "fingerprint", "withdrawn", "owner", "revision"]
)
def test_library_download_rejects_changes(fixture, monkeypatch, mutation):
    from fastapi import HTTPException

    asset, signer = library_recipe(fixture, monkeypatch)
    if mutation == "generation":
        monkeypatch.setattr(
            routes,
            "inspect_library_asset",
            lambda *a, **kw: asset.model_copy(update={"generation": "43"}),
        )
    elif mutation == "fingerprint":
        monkeypatch.setattr(
            routes,
            "inspect_library_asset",
            lambda *a, **kw: asset.model_copy(
                update={"fingerprint": asset.fingerprint.model_copy(update={"sha256": "b" * 64})}
            ),
        )
    elif mutation == "withdrawn":
        routes.catalog_path.side_effect = ["music/track/audio.mp3", FileNotFoundError()]
    elif mutation == "owner":
        routes._owned_job.side_effect = [fixture.job, HTTPException(404, "Job not found")]
    else:
        record = device_record(fixture.job, "first")
        record["base_generation"] = "different"
        save_device_record(fixture.job, "first", record)
    response = download_asset(fixture)
    assert response.status_code in {404, 409}, response.text
    signer.assert_not_called()


def test_library_download_rejects_missing_asset(fixture, monkeypatch):
    _, signer = library_recipe(fixture, monkeypatch)
    assert download_asset(fixture, asset_id="other").status_code == 404
    signer.assert_not_called()


def test_library_download_rejects_legacy_recipe(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    assert download_asset(fixture).status_code == 404


def test_published_phone_export_edits_pin_next_device_revision(fixture, monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status
    from tests.routes.test_phone_editor_commit import phone_job, save

    fixture.job = phone_job(monkeypatch)
    fixture.user.id = fixture.job.user_id
    fixture.request = device_status(fixture.job, "guided_story").request
    attempt = str(uuid.uuid4())
    record = device_record(fixture.job, "guided_story")
    record["status"]["phase"] = "syncing"
    record["attempts"][attempt] = {
        "path": f"{fixture.user.id}/{fixture.job.id}/device/{attempt}.mp4",
        "size": 12,
        "sha256": "a" * 64,
    }
    save_device_record(fixture.job, "guided_story", record)
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(routes, "_verify_export", MagicMock())
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert response.status_code == 200, response.text
    prep = save(fixture.job, generation=attempt)
    assert device_status(fixture.job, "guided_story").request.identity.recipe_revision == 2
    assert fixture.job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    cloud = MagicMock()
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant.apply_async", cloud
    )
    gj.enqueue_editor_commit_render(str(fixture.job.id), "guided_story", prep)
    cloud.assert_not_called()
