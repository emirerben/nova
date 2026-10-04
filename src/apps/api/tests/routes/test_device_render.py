"""HTTP owner fences, upload idempotency, and finalization races."""

import copy
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from app.pipeline.speech_cleanup_apply import cut_fingerprint, hydrate_speech_cleanup_snapshot
from app.routes import device_render as routes
from app.services.device_render import (
    device_record,
    device_status,
    pin_device_request,
    retry_device_render,
    save_device_record,
)
from tests.services.test_guided_speech_cleanup import fixture_ids as cleanup_fixture_ids
from tests.services.test_guided_speech_cleanup import load_fixture as load_cleanup_fixture


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
    signed_calls = []

    def signed_get_url(path, expiration_minutes=5):
        signed_calls.append((path, expiration_minutes))
        return "https://storage.example/video"

    monkeypatch.setattr(routes.storage, "signed_get_url", signed_get_url)
    return signed_calls


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

    def verify(*args, **kwargs):
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
    signed_calls = mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    poster = f"job-posters/{fixture.job.id}/abc.poster.jpg"
    verifier = MagicMock(return_value=poster)
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
    # KRI-231: phone renders never pass through a worker, so publication is
    # the only place their gallery poster can be minted.
    assert fixture.job.assembly_plan["variants"][0]["poster_path"] == poster
    assert verifier.call_args.kwargs["poster_job_id"] == fixture.job.id
    # A phone-rendered variant's playback URL must survive at least as long
    # as a normal viewing/scrubbing session — the bare `signed_get_url`
    # default (5 min, sized for ffprobe preflight) previously expired while
    # the URL was still on screen (prod job 9c7a1f4f-3ce6-40f5-81a3-e66f81f75963).
    assert signed_calls == [(path, routes.PLAYBACK_URL_TTL_MIN)]
    assert routes.PLAYBACK_URL_TTL_MIN > 60
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
    # The kill-switch override is response-only — the underlying pinned phase
    # is never overwritten. The poll-touch write (last_polled_at, throttled)
    # is an orthogonal bookkeeping commit and is expected here.
    assert device_record(fixture.job, "first")["status"]["phase"] == "awaiting_device"
    assert device_record(fixture.job, "first")["last_polled_at"]
    fixture.db.commit.assert_awaited_once()


def test_get_device_render_touches_last_polled_at_throttled(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    response = fixture.client.get(f"/me/jobs/{fixture.job.id}/device-render?variant_id=first")
    assert response.status_code == 200
    first_poll = device_record(fixture.job, "first")["last_polled_at"]
    assert first_poll
    fixture.db.commit.assert_awaited_once()
    # A second poll within the throttle window does not write again.
    response = fixture.client.get(f"/me/jobs/{fixture.job.id}/device-render?variant_id=first")
    assert response.status_code == 200
    assert device_record(fixture.job, "first")["last_polled_at"] == first_poll
    fixture.db.commit.assert_awaited_once()


def test_get_device_render_includes_published_attempt_generation(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    attempt = str(uuid.uuid4())
    record = device_record(fixture.job, "first")
    record["status"]["phase"] = "published"
    record["published_attempt"] = attempt
    record["base_generation"] = attempt
    fixture.job.assembly_plan["variants"][0]["render_generation_id"] = attempt
    save_device_record(fixture.job, "first", record)

    response = fixture.client.get(f"/me/jobs/{fixture.job.id}/device-render?variant_id=first")
    assert response.status_code == 200
    assert response.json()["published_generation"] == attempt


@pytest.mark.parametrize("during_verification", [False, True])
def test_held_recipe_cannot_publish_reserved_output(fixture, monkeypatch, during_verification):
    attempt, _ = prepared(fixture)
    mock_storage(fixture, monkeypatch)

    def hold(*args, **kwargs):
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


def visual_recipe(fixture, monkeypatch, visual_id=None, kind="image"):
    """Pin a recipe whose only asset is the job owner's own pool photo or video."""
    from app.kria.recipes_v2 import EditRecipeV2
    from app.models import PlanItemAsset
    from app.services.phone_sources import PhoneVisualBinding

    item_id = uuid.uuid4()
    fixture.job.content_plan_item_id = item_id
    visual_id = visual_id or str(uuid.uuid4())
    row = SimpleNamespace(
        id=visual_id,
        user_id=fixture.user.id,
        plan_item_id=item_id,
        status="ready",
        kind=kind,
        gcs_generation="42",
        gcs_path=f"users/{fixture.user.id}/plan/{item_id}/pool/{visual_id}"
        + (".mov" if kind == "video" else ".jpg"),
    )
    probe = {"duration_s": 8, "width": 1920, "height": 1080} if kind == "video" else {}
    asset = PhoneVisualBinding(
        media_id=visual_id,
        gcs_path=row.gcs_path,
        generation="42",
        sha256="a" * 64,
        byte_count=12,
        kind=kind,
        **probe,
    ).render_asset()
    value = fixture.request.recipe.model_dump(mode="json")
    value.update(
        schema_version=2,
        renderer_version="kria-ios-2",
        asset_manifest={"assets": [asset.model_dump()]},
    )
    value["assets"][0].update(
        id=asset.id,
        relative_path=asset.id,
        fingerprint={"algorithm": "sha256", "hex": "a" * 64, "byte_count": 12},
    )
    value["tracks"][0]["clips"][0]["source_asset_id"] = asset.id
    fixture.request = make_device_request(
        job_id=fixture.job.id,
        variant_id="first",
        revision=2,
        recipe=EditRecipeV2.model_validate(value),
    )
    pin_device_request(fixture.job, fixture.request, base_generation="approved")
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))

    async def get(model, key, **kwargs):
        assert model is PlanItemAsset
        return row if str(key) == str(row.id) else None

    fixture.db.get.side_effect = get
    signer = MagicMock(return_value="https://storage.example/pinned")
    monkeypatch.setattr(routes.storage, "signed_get_url_for_generation", signer)
    return asset, row, signer


def test_visual_download_signs_exactly_the_pinned_pool_generation(fixture, monkeypatch):
    asset, row, signer = visual_recipe(fixture, monkeypatch)
    assert asset.id == f"visual-{row.id}"
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(row.gcs_path, generation="42")
    assert response.json()["asset_id"] == asset.id
    assert response.json()["download_url"] == "https://storage.example/pinned"
    # A cached identity-map row could hide a concurrent removal or replacement.
    assert fixture.db.get.await_args.kwargs == {"populate_existing": True}
    # The owner lock is released before the (network-free) signing call.
    assert fixture.db.rollback.await_count == 1


@pytest.mark.parametrize("mutation", ["owner", "other_item", "no_item", "missing", "bad_uuid"])
def test_visual_download_hides_photos_outside_the_jobs_own_item(fixture, monkeypatch, mutation):
    asset, row, signer = visual_recipe(
        fixture, monkeypatch, visual_id="not-a-uuid" if mutation == "bad_uuid" else None
    )
    if mutation == "owner":
        row.user_id = uuid.uuid4()
    elif mutation == "other_item":
        row.plan_item_id = uuid.uuid4()
    elif mutation == "no_item":
        fixture.job.content_plan_item_id = None
    elif mutation == "missing":
        row.id = uuid.uuid4()
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Visual unavailable"
    signer.assert_not_called()
    if mutation == "bad_uuid":
        fixture.db.get.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation",
    [
        "not_ready",
        "video",
        "generation",
        "no_generation",
        "other_owner_prefix",
        "other_item_prefix",
        "dot_dot",
        "empty_segment",
        "bare_prefix",
        "removed",
        "unsignable",
    ],
)
def test_visual_download_rejects_changed_pool_rows(fixture, monkeypatch, mutation):
    asset, row, signer = visual_recipe(fixture, monkeypatch)
    prefix = f"users/{fixture.user.id}/plan/{row.plan_item_id}/pool/"
    if mutation == "not_ready":
        row.status = "analyzing"
    elif mutation == "video":
        row.kind = "video"
    elif mutation == "generation":
        row.gcs_generation = "43"
    elif mutation == "no_generation":
        row.gcs_generation = None
    elif mutation == "other_owner_prefix":
        row.gcs_path = f"users/{uuid.uuid4()}/plan/{row.plan_item_id}/pool/photo.jpg"
    elif mutation == "other_item_prefix":
        row.gcs_path = f"users/{fixture.user.id}/plan/{uuid.uuid4()}/pool/photo.jpg"
    elif mutation == "dot_dot":
        row.gcs_path = f"{prefix}../../{uuid.uuid4()}/pool/photo.jpg"
    elif mutation == "empty_segment":
        row.gcs_path = f"{prefix}/photo.jpg"
    elif mutation == "bare_prefix":
        row.gcs_path = prefix
    elif mutation == "removed":
        signer.side_effect = FileNotFoundError()
    else:
        signer.side_effect = ValueError("unsigned")
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Visual changed; refresh the recipe"
    assert signer.call_count == int(mutation in {"removed", "unsignable"})


def test_visual_download_signs_a_pinned_pool_video(fixture, monkeypatch):
    asset, row, signer = visual_recipe(fixture, monkeypatch, kind="video")
    assert asset.media_kind == "video"
    assert "visualVideos" in fixture.request.recipe.required_capabilities
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(row.gcs_path, generation="42")
    assert response.json()["asset_id"] == asset.id
    assert response.json()["download_url"] == "https://storage.example/pinned"


@pytest.mark.parametrize("pinned,stored", [("video", "image"), ("image", "video")])
def test_visual_download_requires_the_row_to_be_the_pinned_kind(
    fixture, monkeypatch, pinned, stored
):
    # The device prepares photos and videos differently, so a row whose kind
    # no longer matches the recipe must not be signed as the other one.
    asset, row, signer = visual_recipe(fixture, monkeypatch, kind=pinned)
    row.kind = stored
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Visual changed; refresh the recipe"
    signer.assert_not_called()


@pytest.mark.parametrize("mutation", ["not_ready", "generation", "other_item_prefix"])
def test_visual_video_download_rejects_changed_pool_rows(fixture, monkeypatch, mutation):
    asset, row, signer = visual_recipe(fixture, monkeypatch, kind="video")
    if mutation == "not_ready":
        row.status = "analyzing"
    elif mutation == "generation":
        row.gcs_generation = "43"
    else:
        row.gcs_path = f"users/{fixture.user.id}/plan/{uuid.uuid4()}/pool/clip.mov"
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    signer.assert_not_called()


def test_visual_download_rejects_a_superseded_recipe(fixture, monkeypatch):
    asset, _, signer = visual_recipe(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    record["base_generation"] = "different"
    save_device_record(fixture.job, "first", record)
    assert download_asset(fixture, asset_id=asset.id).status_code == 409
    signer.assert_not_called()
    fixture.db.get.assert_not_awaited()


@pytest.mark.parametrize("gate", ["cohort", "kill_switch"])
def test_visual_download_requires_phone_rendering_for_the_user(fixture, monkeypatch, gate):
    asset, _, signer = visual_recipe(fixture, monkeypatch)
    if gate == "cohort":
        monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])
    else:
        monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 404
    assert response.json()["detail"] == "Phone rendering is unavailable"
    routes._owned_job.assert_not_awaited()
    fixture.db.get.assert_not_awaited()
    signer.assert_not_called()


def voiceover_recipe(fixture, monkeypatch, *, item_id=None):
    """Pin a recipe whose only asset is the job owner's own plan-item voiceover."""
    from app.kria.recipes_v2 import EditRecipeV2
    from app.kria.render_assets import VoiceoverRenderAsset
    from app.models import PlanItem

    item_id = item_id or uuid.uuid4()
    fixture.job.content_plan_item_id = item_id
    voiceover_path = f"voiceover-uploads/direct/{fixture.user.id}/{uuid.uuid4()}/voice.mp3"
    item = SimpleNamespace(
        id=item_id,
        audio_mode="voiceover",
        voiceover_gcs_path=voiceover_path,
        voiceover_generation="42",
    )
    asset = VoiceoverRenderAsset(
        id=f"voiceover-{item_id}",
        plan_item_id=str(item_id),
        generation="42",
        fingerprint={"sha256": "a" * 64, "byte_count": 12},
    )
    value = fixture.request.recipe.model_dump(mode="json")
    value.update(
        schema_version=2,
        renderer_version="kria-ios-2",
        asset_manifest={"assets": [asset.model_dump()]},
    )
    value["assets"][0].update(
        id=asset.id,
        relative_path=asset.id,
        fingerprint={"algorithm": "sha256", "hex": "a" * 64, "byte_count": 12},
    )
    value["tracks"][0]["clips"][0]["source_asset_id"] = asset.id
    fixture.request = make_device_request(
        job_id=fixture.job.id,
        variant_id="first",
        revision=2,
        recipe=EditRecipeV2.model_validate(value),
    )
    pin_device_request(fixture.job, fixture.request, base_generation="approved")
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))

    async def get(model, key, **kwargs):
        assert model is PlanItem
        return item if key == item.id else None

    fixture.db.get.side_effect = get
    signer = MagicMock(return_value="https://storage.example/pinned")
    monkeypatch.setattr(routes.storage, "signed_get_url_for_generation", signer)
    return asset, item, signer


def test_voiceover_download_signs_exactly_the_pinned_generation(fixture, monkeypatch):
    asset, item, signer = voiceover_recipe(fixture, monkeypatch)
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(item.voiceover_gcs_path, generation="42")
    assert response.json()["asset_id"] == asset.id
    assert response.json()["download_url"] == "https://storage.example/pinned"
    assert fixture.db.rollback.await_count == 2


@pytest.mark.parametrize("mutation", ["other_item", "no_item"])
def test_voiceover_download_hides_voiceovers_outside_the_jobs_own_item(
    fixture, monkeypatch, mutation
):
    """Unlike a Visuals-pool asset (whose id the device sends and the route
    parses as a UUID), a voiceover asset is looked up by the JOB's OWN
    `content_plan_item_id`, never by parsing the recipe-carried
    `plan_item_id` string -- so there is no "bad id fails to parse" case to
    cover here, only "the job's own item no longer matches"."""
    asset, _item, signer = voiceover_recipe(fixture, monkeypatch)
    if mutation == "other_item":
        fixture.job.content_plan_item_id = uuid.uuid4()
    else:
        fixture.job.content_plan_item_id = None
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Voiceover unavailable"
    signer.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "row_missing",
        "audio_mode",
        "generation",
        "no_path",
        "no_generation",
        "removed",
        "unsignable",
    ],
)
def test_voiceover_download_rejects_changed_voiceovers(fixture, monkeypatch, mutation):
    asset, item, signer = voiceover_recipe(fixture, monkeypatch)
    if mutation == "row_missing":
        # The job's content_plan_item_id no longer resolves to a PlanItem row
        # at all (e.g. deleted) -- a conflict (refresh the recipe), the same
        # as any other post-pin drift, not a bare 404.
        item.id = uuid.uuid4()
    elif mutation == "audio_mode":
        item.audio_mode = "kria"
    elif mutation == "generation":
        item.voiceover_generation = "43"
    elif mutation == "no_path":
        item.voiceover_gcs_path = None
    elif mutation == "no_generation":
        item.voiceover_generation = None
    elif mutation == "removed":
        signer.side_effect = FileNotFoundError()
    else:
        signer.side_effect = ValueError("unsigned")
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Voiceover changed; refresh the recipe"
    assert signer.call_count == int(mutation in {"removed", "unsignable"})


def song_recipe(fixture, monkeypatch, *, item_id=None):
    """Pin a recipe whose only asset is the job owner's own plan-item song (KRI-374)."""
    from app.kria.recipes_v2 import EditRecipeV2
    from app.kria.render_assets import SongRenderAsset
    from app.models import PlanItem

    item_id = item_id or uuid.uuid4()
    fixture.job.content_plan_item_id = item_id
    song_path = f"users/{fixture.user.id}/creation-threads/{uuid.uuid4()}/song.m4a"
    item = SimpleNamespace(
        id=item_id,
        audio_mode="song",
        song_gcs_path=song_path,
        song_generation=42,
        # The voiceover columns stay empty: a song never rides them.
        voiceover_gcs_path=None,
        voiceover_generation=None,
    )
    asset = SongRenderAsset(
        id=f"song-{item_id}",
        plan_item_id=str(item_id),
        generation="42",
        fingerprint={"sha256": "a" * 64, "byte_count": 12},
    )
    value = fixture.request.recipe.model_dump(mode="json")
    value.update(
        schema_version=2,
        renderer_version="kria-ios-2",
        asset_manifest={"assets": [asset.model_dump()]},
    )
    value["assets"][0].update(
        id=asset.id,
        relative_path=asset.id,
        fingerprint={"algorithm": "sha256", "hex": "a" * 64, "byte_count": 12},
    )
    value["tracks"][0]["clips"][0]["source_asset_id"] = asset.id
    fixture.request = make_device_request(
        job_id=fixture.job.id,
        variant_id="first",
        revision=2,
        recipe=EditRecipeV2.model_validate(value),
    )
    pin_device_request(fixture.job, fixture.request, base_generation="approved")
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))

    async def get(model, key, **kwargs):
        assert model is PlanItem
        return item if key == item.id else None

    fixture.db.get.side_effect = get
    signer = MagicMock(return_value="https://storage.example/pinned-song")
    monkeypatch.setattr(routes.storage, "signed_get_url_for_generation", signer)
    return asset, item, signer


def test_song_download_signs_exactly_the_pinned_generation(fixture, monkeypatch):
    asset, item, signer = song_recipe(fixture, monkeypatch)
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(item.song_gcs_path, generation="42")
    assert response.json()["asset_id"] == asset.id
    assert response.json()["download_url"] == "https://storage.example/pinned-song"


@pytest.mark.parametrize("mutation", ["other_item", "no_item"])
def test_song_download_hides_songs_outside_the_jobs_own_item(fixture, monkeypatch, mutation):
    asset, _item, signer = song_recipe(fixture, monkeypatch)
    fixture.job.content_plan_item_id = uuid.uuid4() if mutation == "other_item" else None
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Song unavailable"
    signer.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "row_missing",
        "audio_mode",
        "generation",
        "no_path",
        "no_generation",
        "removed",
        "unsignable",
    ],
)
def test_song_download_rejects_replaced_or_removed_songs(fixture, monkeypatch, mutation):
    asset, item, signer = song_recipe(fixture, monkeypatch)
    if mutation == "row_missing":
        item.id = uuid.uuid4()
    elif mutation == "audio_mode":
        item.audio_mode = "kria"  # the creator removed the song
    elif mutation == "generation":
        item.song_generation = 43  # replaced by another upload
    elif mutation == "no_path":
        item.song_gcs_path = None
    elif mutation == "no_generation":
        item.song_generation = None
    elif mutation == "removed":
        signer.side_effect = FileNotFoundError()
    else:
        signer.side_effect = ValueError("unsigned")
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Song changed; refresh the recipe"
    assert signer.call_count == int(mutation in {"removed", "unsignable"})


def test_a_voiceover_item_cannot_be_used_to_fetch_a_song_asset(fixture, monkeypatch):
    """The two private-audio kinds do not share a grant: a song asset needs the
    item to be in ``song`` mode even when a voiceover with the same generation exists."""
    asset, item, signer = song_recipe(fixture, monkeypatch)
    item.audio_mode = "voiceover"
    item.voiceover_gcs_path = item.song_gcs_path
    item.voiceover_generation = "42"
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    signer.assert_not_called()


# --- "Clean up speech": a guided Job's cleaned derivative, never raw bytes ---
#
# The synthetic preflight snapshot's raw source is re-pinned at the recipe's
# asset generation, so the raw-voiceover grant WOULD sign the raw recording
# for these Jobs: every rejection below proves there is no fallback to it.


def cleanup_snapshot() -> dict:
    snapshot = copy.deepcopy(load_cleanup_fixture()["snapshot"])
    snapshot["source"]["generation"] = "42"
    return snapshot


def cleanup_job_plan(narration: dict, *, contract: str = "required_v1") -> dict:
    """The immutable Job fields a guided "Clean up speech" dispatch pins."""
    return {
        "creator_generation_id": "creator-generation",
        "guided_edit": {"approved_proposal": {"narration": narration}},
        "speech_cleanup_contract": contract,
        "speech_cleanup_preflight_contract": "snapshot_v1",
        "_speech_cleanup_internal": {"preflight_snapshot": cleanup_snapshot()},
    }


def cleaned_narration(*, owner_id=None, analysis_id=None, **provenance) -> dict:
    fixture_owner, item_id, fixture_analysis = cleanup_fixture_ids(load_cleanup_fixture())
    analysis_id = analysis_id or fixture_analysis
    snapshot = cleanup_snapshot()
    return {
        "gcs_path": (
            f"users/{owner_id or fixture_owner}/plan/{item_id}/speech-cleanup/"
            f"{analysis_id}/{'0a' * 16}.wav"
        ),
        "generation": "42",
        "duration_s": 41.59,
        "speech_cleanup": {
            "analysis_id": analysis_id,
            "source_gcs_path": snapshot["source"]["storage_path"],
            "source_generation": "42",
            "source_duration_s": snapshot["source"]["window_end_s"],
            "cut_sha256": cut_fingerprint(hydrate_speech_cleanup_snapshot(snapshot)),
            **provenance,
        },
    }


def cleaned_voiceover_recipe(fixture, monkeypatch, narration=None, **plan):
    """Pin a guided recipe whose voiceover asset is the item's cleaned derivative."""
    owner_id, item_id, _analysis_id = cleanup_fixture_ids(load_cleanup_fixture())
    fixture.user.id = owner_id
    fixture.job.user_id = owner_id
    asset, item, signer = voiceover_recipe(fixture, monkeypatch, item_id=item_id)
    source = cleanup_snapshot()["source"]
    item.voiceover_gcs_path = source["storage_path"]
    item.voiceover_duration_s = source["window_end_s"]
    assert item.voiceover_generation == source["generation"] == asset.generation
    narration = narration or cleaned_narration()
    fixture.job.assembly_plan.update(cleanup_job_plan(narration, **plan))
    return asset, item, signer, narration


def test_cleaned_voiceover_download_signs_only_the_derivative(fixture, monkeypatch):
    asset, item, signer, narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(narration["gcs_path"], generation="42")
    assert item.voiceover_gcs_path != narration["gcs_path"]
    assert fixture.db.rollback.await_count == 2


def test_narrated_cleaned_binding_grants_derivative_without_guided_proposal(fixture, monkeypatch):
    """KRI-277: narrated jobs retain their clean receipt next to the recipe."""
    from app.schemas.edit_proposal import NarrationTrack
    from app.services.device_narration_binding import make_device_narration_binding

    asset, _item, signer, narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    record["narration_binding"] = make_device_narration_binding(
        NarrationTrack.model_validate(narration), asset
    ).model_dump(mode="json")
    save_device_record(fixture.job, "first", record)
    fixture.job.assembly_plan.pop("guided_edit")

    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(narration["gcs_path"], generation="42")


def test_narrated_binding_fingerprint_mismatch_fails_closed(fixture, monkeypatch):
    from app.schemas.edit_proposal import NarrationTrack
    from app.services.device_narration_binding import make_device_narration_binding

    asset, _item, signer, narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    binding = make_device_narration_binding(
        NarrationTrack.model_validate(narration), asset
    ).model_dump(mode="json")
    binding["sha256"] = "b" * 64
    record["narration_binding"] = binding
    save_device_record(fixture.job, "first", record)
    fixture.job.assembly_plan.pop("guided_edit")

    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    signer.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    ["generation", "size", "hash", "raw_source"],
)
def test_legacy_narrated_cleanup_recovery_validates_snapshot_and_derivative(
    fixture, monkeypatch, mutation
):
    """Exercise legacy recovery from the immutable preflight snapshot, not a stub."""
    from app.kria.render_assets import VoiceoverRenderAsset
    from app.services.device_narration_binding import _legacy_cleaned_narration

    owner_id, item_id, _analysis_id = cleanup_fixture_ids(load_cleanup_fixture())
    payload = b"legacy-cleaned-wav"
    digest = hashlib.sha256(payload).hexdigest()
    asset = VoiceoverRenderAsset(
        id=f"voiceover-{item_id}",
        plan_item_id=str(item_id),
        generation="42",
        fingerprint={"sha256": digest, "byte_count": len(payload)},
    )
    item = SimpleNamespace(
        id=item_id,
        audio_mode="voiceover",
        voiceover_gcs_path=cleanup_snapshot()["source"]["storage_path"],
        voiceover_generation="42",
        voiceover_duration_s=cleanup_snapshot()["source"]["window_end_s"],
    )
    if mutation == "raw_source":
        item.voiceover_generation = "43"
    metadata = SimpleNamespace(
        generation="43" if mutation == "generation" else "42",
        size=len(payload) + 1 if mutation == "size" else len(payload),
    )
    monkeypatch.setattr(routes.storage, "object_metadata", lambda _path: metadata)
    monkeypatch.setattr(
        routes.storage,
        "download_generation_to_file",
        lambda _path, local, *, generation: Path(local).write_bytes(
            b"wrong" if mutation == "hash" else payload
        ),
    )
    plan = {
        "speech_cleanup_contract": "required_v1",
        "speech_cleanup_preflight_contract": "snapshot_v1",
        "_speech_cleanup_internal": {"preflight_snapshot": cleanup_snapshot()},
    }
    if mutation == "raw_source":
        with pytest.raises(ValueError, match="source generation"):
            _legacy_cleaned_narration(plan, owner_id=owner_id, item=item, asset=asset)
    elif mutation in {"generation", "size"}:
        with pytest.raises(ValueError, match="metadata"):
            _legacy_cleaned_narration(plan, owner_id=owner_id, item=item, asset=asset)
    elif mutation == "hash":
        with pytest.raises(ValueError, match="fingerprint"):
            _legacy_cleaned_narration(plan, owner_id=owner_id, item=item, asset=asset)


@pytest.mark.parametrize("no_removal", [False, True])
def test_legacy_narrated_cleanup_recovery_reconstructs_matching_derivative(
    fixture, monkeypatch, no_removal
):
    from app.kria.render_assets import VoiceoverRenderAsset
    from app.services.device_narration_binding import _legacy_cleaned_narration

    owner_id, item_id, _analysis_id = cleanup_fixture_ids(load_cleanup_fixture())
    payload = b"legacy-cleaned-wav"
    asset = VoiceoverRenderAsset(
        id=f"voiceover-{item_id}",
        plan_item_id=str(item_id),
        generation="42",
        fingerprint={"sha256": hashlib.sha256(payload).hexdigest(), "byte_count": len(payload)},
    )
    snapshot = cleanup_snapshot()
    if no_removal:
        duration = snapshot["source"]["window_end_s"]
        snapshot["analysis"]["cut_plan"].update(
            keep_segments=[{"start_s": 0.0, "end_s": duration}],
            removed=[],
            time_saved_s=0.0,
        )
        snapshot["analysis"]["findings"] = []
        snapshot["analysis"]["public_receipt"].update(
            candidate_count=0,
            category_counts={"filler_sounds": 0, "long_pauses": 0, "retakes": 0},
            estimated_removed_ms=0,
            result_duration_ms=round(duration * 1000),
        )
    item = SimpleNamespace(
        id=item_id,
        audio_mode="voiceover",
        voiceover_gcs_path=snapshot["source"]["storage_path"],
        voiceover_generation="42",
        voiceover_duration_s=snapshot["source"]["window_end_s"],
    )
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(generation="42", size=len(payload)),
    )
    monkeypatch.setattr(
        routes.storage,
        "download_generation_to_file",
        lambda _path, local, *, generation: Path(local).write_bytes(payload),
    )
    narration = _legacy_cleaned_narration(
        {
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "_speech_cleanup_internal": {"preflight_snapshot": snapshot},
        },
        owner_id=owner_id,
        item=item,
        asset=asset,
    )
    assert narration is not None
    assert narration.generation == "42"
    assert narration.speech_cleanup is not None
    assert narration.speech_cleanup.source_gcs_path == item.voiceover_gcs_path


def test_retry_preserves_matching_narrated_cleaned_binding(fixture, monkeypatch):
    from app.schemas.edit_proposal import NarrationTrack
    from app.services.device_narration_binding import make_device_narration_binding

    asset, _item, _signer, narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    record["status"]["phase"] = "needs_attention"
    binding = make_device_narration_binding(
        NarrationTrack.model_validate(narration), asset
    ).model_dump(mode="json")
    record["narration_binding"] = binding
    save_device_record(fixture.job, "first", record)

    retry_device_render(fixture.job, "first")
    assert device_record(fixture.job, "first")["narration_binding"] == binding


@pytest.mark.parametrize("change", [None, "generation", "item"])
def test_editor_repin_keeps_only_an_identical_narration_binding(fixture, monkeypatch, change):
    from app.schemas.edit_proposal import NarrationTrack
    from app.services.device_narration_binding import make_device_narration_binding

    asset, _item, _signer, narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    binding = make_device_narration_binding(
        NarrationTrack.model_validate(narration), asset
    ).model_dump(mode="json")
    record["narration_binding"] = binding
    save_device_record(fixture.job, "first", record)
    recipe = fixture.request.recipe
    assets = list(recipe.asset_manifest.assets)
    if change is not None:
        update = {"plan_item_id" if change == "item" else "generation": "changed"}
        assets = [
            asset.model_copy(update=update) if entry.id == asset.id else entry for entry in assets
        ]
    recipe = recipe.model_copy(
        update={
            "asset_manifest": recipe.asset_manifest.model_copy(update={"assets": tuple(assets)})
        }
    )
    request = make_device_request(
        job_id=fixture.job.id, variant_id="first", revision=3, recipe=recipe
    )
    pin_device_request(fixture.job, request, base_generation="approved")
    persisted = device_record(fixture.job, "first")
    assert ("narration_binding" in persisted) is (change is None)


def test_narrated_binding_rechecks_the_source_after_external_grant_work(fixture, monkeypatch):
    from app.schemas.edit_proposal import NarrationTrack
    from app.services.device_narration_binding import make_device_narration_binding

    asset, item, signer, narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    record["narration_binding"] = make_device_narration_binding(
        NarrationTrack.model_validate(narration), asset
    ).model_dump(mode="json")
    save_device_record(fixture.job, "first", record)
    original = routes._authorized_cleaned_narration

    def change_source(*args, **kwargs):
        result = original(*args, **kwargs)
        item.voiceover_generation = "43"
        return result

    monkeypatch.setattr(routes, "_authorized_cleaned_narration", change_source)
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    signer.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), routes.NotFound("missing")])
def test_legacy_cleaned_derivative_missing_fails_closed_as_recipe_conflict(
    fixture, monkeypatch, error
):
    asset, _item, signer, _narration = cleaned_voiceover_recipe(fixture, monkeypatch)
    monkeypatch.setattr(
        routes, "_authorized_cleaned_narration", lambda *_args: (_ for _ in ()).throw(error)
    )

    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Voiceover changed; refresh the recipe"
    signer.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "replaced_raw",
        "cleared_voiceover",
        "pinned_generation",
        "foreign_owner_prefix",
        "analysis_mismatch",
        "cut_mismatch",
        "not_required_contract",
        "malformed_provenance",
    ],
)
def test_cleaned_voiceover_download_never_falls_back_to_the_raw_recording(
    fixture, monkeypatch, mutation
):
    narration, plan = cleaned_narration(), {}
    if mutation == "pinned_generation":
        narration["generation"] = "43"
    elif mutation == "foreign_owner_prefix":
        narration = cleaned_narration(owner_id=uuid.uuid4())
    elif mutation == "analysis_mismatch":
        narration = cleaned_narration(analysis_id=str(uuid.uuid4()))
    elif mutation == "cut_mismatch":
        narration = cleaned_narration(cut_sha256="e" * 64)
    elif mutation == "not_required_contract":
        plan = {"contract": "off_v1"}
    elif mutation == "malformed_provenance":
        narration["speech_cleanup"] = {"analysis_id": "not-a-uuid"}
    asset, item, signer, _ = cleaned_voiceover_recipe(fixture, monkeypatch, narration, **plan)
    if mutation == "replaced_raw":
        item.voiceover_generation = "43"
    elif mutation == "cleared_voiceover":
        item.audio_mode = "kria"
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Voiceover changed; refresh the recipe"
    signer.assert_not_called()


def test_raw_guided_narration_keeps_the_raw_voiceover_grant(fixture, monkeypatch):
    asset, item, signer = voiceover_recipe(fixture, monkeypatch)
    raw = {"gcs_path": item.voiceover_gcs_path, "generation": "42", "duration_s": 12.0}
    fixture.job.assembly_plan.update(cleanup_job_plan(raw, contract="off_v1"))
    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(item.voiceover_gcs_path, generation="42")


def test_markerless_required_raw_narration_keeps_legacy_voiceover_grant(fixture, monkeypatch):
    asset, item, signer = voiceover_recipe(fixture, monkeypatch)
    raw = {"gcs_path": item.voiceover_gcs_path, "generation": "42", "duration_s": 12.0}
    plan = cleanup_job_plan(raw)
    plan.pop("_speech_cleanup_internal")
    plan.pop("speech_cleanup_preflight_contract")
    fixture.job.assembly_plan.update(plan)
    fixture.job.assembly_plan.pop("guided_edit")

    response = download_asset(fixture, asset_id=asset.id)
    assert response.status_code == 200, response.text
    signer.assert_called_once_with(item.voiceover_gcs_path, generation="42")


def complete_device_export(fixture, monkeypatch, plan: dict | None):
    attempt, _ = prepared(fixture)
    if plan is not None:
        fixture.job.assembly_plan.update(plan)
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(routes, "_verify_export", MagicMock(return_value=None))
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
    assert fixture.job.status == "variants_ready"
    return fixture.job.assembly_plan


def test_cleaned_guided_completion_records_the_applied_cleanup(fixture, monkeypatch):
    cleanup = load_cleanup_fixture()
    plan = complete_device_export(fixture, monkeypatch, cleanup_job_plan(cleaned_narration()))
    assert plan["speech_cleanup_outcome"] == {
        "job_id": str(fixture.job.id),
        "render_generation_id": "creator-generation",
        "status": "applied",
        "removal_count": cleanup["expected"]["removal_count"],
        "removed_ms": cleanup["snapshot"]["analysis"]["public_receipt"]["estimated_removed_ms"],
    }


def test_mismatched_cleaned_guided_completion_records_a_failed_cleanup(fixture, monkeypatch):
    narration = cleaned_narration(cut_sha256="e" * 64)
    plan = complete_device_export(fixture, monkeypatch, cleanup_job_plan(narration))
    assert plan["speech_cleanup_outcome"]["status"] == "failed"
    assert plan["speech_cleanup_outcome"]["error"] == {"code": "internal_error", "retryable": True}


def test_raw_required_guided_completion_writes_no_false_failed_cleanup(fixture, monkeypatch):
    """A guided Job pinned before cleaned narration existed (raw audio under a
    required_v1 contract) keeps no receipt when its native edit re-exports."""
    raw = {"gcs_path": "users/u/plan/i/voiceover.m4a", "generation": "7", "duration_s": 48.6}
    plan = complete_device_export(fixture, monkeypatch, cleanup_job_plan(raw))
    assert "speech_cleanup_outcome" not in plan


@pytest.mark.parametrize("plan", [None, "off_v1"])
def test_completion_without_a_required_guided_cleanup_writes_no_receipt(fixture, monkeypatch, plan):
    raw = {"gcs_path": "users/u/plan/i/voiceover.m4a", "generation": "7", "duration_s": 48.6}
    extra = cleanup_job_plan(raw, contract=plan) if plan else None
    assert "speech_cleanup_outcome" not in complete_device_export(fixture, monkeypatch, extra)


def test_subtitled_completion_records_the_applied_cleanup_from_the_variant_context(
    fixture, monkeypatch
):
    """A non-guided phone render (`_run_phone_subtitled_job`'s "subtitled"/
    self-narrated shape, or `_run_phone_narrated_job`'s narrated-with-
    voiceover shape) persists its OWN `_speech_cleanup_outcome_context` on
    the completed variant at plan time, mirroring the cloud renderer's
    `base["_speech_cleanup_outcome_context"]` -- completion publishes the
    SAME public receipt shape the guided-story path does, straight from that
    context, with no separate re-binding proof needed."""
    from app.pipeline.speech_cleanup_apply import (
        PREFLIGHT_JOB_CONTRACT_FIELD,
        PREFLIGHT_JOB_CONTRACT_VALUE,
    )

    fixture.job.assembly_plan["variants"][0]["_speech_cleanup_outcome_context"] = {
        "analysis_attempt_id": str(uuid.uuid4()),
        "analysis_view": "full_clip",
        "detector_version": "v1",
        "source_tag": "a" * 16,
        "selected_plan": "candidate",
        "candidate_status": "ready",
        "output_removal_count": 2,
        "output_removed_ms": 2440,
    }
    plan = complete_device_export(
        fixture,
        monkeypatch,
        {
            "speech_cleanup_contract": "required_v1",
            "creator_generation_id": "creator-generation",
            PREFLIGHT_JOB_CONTRACT_FIELD: PREFLIGHT_JOB_CONTRACT_VALUE,
        },
    )
    assert plan["speech_cleanup_outcome"] == {
        "job_id": str(fixture.job.id),
        "render_generation_id": "creator-generation",
        "status": "applied",
        "removal_count": 2,
        "removed_ms": 2440,
    }


def test_subtitled_completion_with_no_persisted_context_writes_no_receipt(fixture, monkeypatch):
    """A non-guided phone render under `off_v1`/`legacy_auto` never persists
    `_speech_cleanup_outcome_context` on its variant -- completion must not
    fabricate a receipt from nothing."""
    from app.pipeline.speech_cleanup_apply import (
        PREFLIGHT_JOB_CONTRACT_FIELD,
        PREFLIGHT_JOB_CONTRACT_VALUE,
    )

    plan = complete_device_export(
        fixture,
        monkeypatch,
        {
            "speech_cleanup_contract": "required_v1",
            "creator_generation_id": "creator-generation",
            PREFLIGHT_JOB_CONTRACT_FIELD: PREFLIGHT_JOB_CONTRACT_VALUE,
        },
    )
    assert "speech_cleanup_outcome" not in plan


def test_published_phone_export_edits_pin_next_device_revision(fixture, monkeypatch):
    from app.routes import generative_jobs as gj
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
    monkeypatch.setattr(routes, "_verify_export", MagicMock(return_value=None))
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


def failure_body(fixture, **overrides):
    return {
        "identity": fixture.request.identity.model_dump(mode="json"),
        "reason_code": "export_failed",
        "detail": "",
        **overrides,
    }


def test_report_failure_transitions_and_persists_reason(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/failures", json=failure_body(fixture)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "identity": fixture.request.identity.model_dump(mode="json"),
        "phase": "needs_attention",
        "reason_code": "export_failed",
    }
    record = device_record(fixture.job, "first")
    assert record["status"]["phase"] == "needs_attention"
    assert record["status"]["reason_code"] == "export_failed"
    assert record["failed_at"]
    assert record["failure"]["reason_code"] == "export_failed"
    assert fixture.job.assembly_plan["variants"][0]["render_status"] == "needs_attention"
    assert fixture.job.assembly_plan["variants"][0]["ok"] is False
    assert fixture.job.failure_reason == "export_failed"
    assert fixture.job.error_detail is None
    fixture.db.commit.assert_awaited_once()


def test_report_failure_rejects_when_published(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    record = device_record(fixture.job, "first")
    record["status"]["phase"] = "published"
    save_device_record(fixture.job, "first", record)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/failures", json=failure_body(fixture)
    )
    assert response.status_code == 409
    fixture.db.commit.assert_not_awaited()


def test_report_failure_idempotent_repeat(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    first = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/failures", json=failure_body(fixture)
    )
    assert first.status_code == 200
    fixture.db.commit.reset_mock()
    second = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/failures",
        json=failure_body(fixture, reason_code="thermal"),
    )
    assert second.status_code == 200
    assert second.json()["reason_code"] == "export_failed"  # original reason preserved
    fixture.db.commit.assert_not_awaited()


def test_retry_issues_new_revision_with_identical_recipe_and_clears_failure(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    failed = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/failures", json=failure_body(fixture)
    )
    assert failed.status_code == 200
    assert fixture.job.failure_reason == "export_failed"
    old_identity = fixture.request.identity
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/retry",
        json={"identity": old_identity.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["phase"] == "awaiting_device"
    assert payload["identity"]["recipe_revision"] == old_identity.recipe_revision + 1
    new_status = device_status(fixture.job, "first")
    assert new_status.phase == "awaiting_device"
    assert new_status.request.recipe == fixture.request.recipe
    assert new_status.reason is None
    assert new_status.reason_code is None
    assert fixture.job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert fixture.job.assembly_plan["variants"][0]["ok"] is False
    assert fixture.job.failure_reason is None
    assert fixture.job.error_detail is None


def test_retry_rejected_unless_needs_attention(fixture, monkeypatch):
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/retry",
        json={"identity": fixture.request.identity.model_dump(mode="json")},
    )
    assert response.status_code == 409
    fixture.db.commit.assert_not_awaited()


def test_publication_replaces_stale_cloud_poster_when_poster_fails(fixture, monkeypatch):
    """A failed phone poster must not leave an earlier cloud render's poster."""
    attempt, path = prepared(fixture)
    fixture.job.assembly_plan["variants"][0]["poster_path"] = "old/cloud.poster.jpg"
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(routes, "_verify_export", MagicMock(return_value=None))
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
    variant = fixture.job.assembly_plan["variants"][0]
    assert variant["video_path"] == path
    assert variant["poster_path"] is None
