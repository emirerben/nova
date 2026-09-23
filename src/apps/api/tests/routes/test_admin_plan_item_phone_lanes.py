"""Tests for the admin phone-subtitled-lanes write surface (KRI-174 Phase
1.5): PUT/GET/DELETE /admin/plan-items/{item_id}/phone-lanes in
routes/admin_plan_items.py.

Covers:
  - auth gate (X-Admin-Token), mirroring tests/routes/test_admin_plan_items.py
  - 404 for both an unknown id and a malformed (non-UUID) id, on all three
    verbs
  - PUT happy path: an overlay image + a muted ending video + two sound
    effects resolve to a fully-pinned `PhoneSubtitledLaneRequest` (gcs_path/
    generation filled from the item's own pool rows), persisted once
  - PUT 422s: media outside the item, wrong kind, not ready, missing
    generation, unknown/unpublished/unplayable sound effect, duplicate
    overlay ids, and an invalid overlay window (end_s <= start_s)
  - GET returns the stored request (or None) and never writes
  - DELETE clears the stored request and commits
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _plan_item_row(**overrides) -> SimpleNamespace:
    base = dict(id=uuid.uuid4(), phone_lane_request=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _asset_row(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        plan_item_id=None,
        status="ready",
        kind="image",
        gcs_path="users/u1/plan/p1/pool/asset.jpg",
        gcs_generation="12345",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _sfx_row(**overrides) -> SimpleNamespace:
    base = dict(
        id="catalog-1",
        audio_gcs_path="sfx/click.m4a",
        status="ready",
        published_at=datetime.now(UTC),
        archived_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _overlay_card(**overrides) -> dict:
    base = dict(id="ov-1", media_id=str(uuid.uuid4()), start_s=1.0, end_s=3.0)
    base.update(overrides)
    return base


def _sfx_card(**overrides) -> dict:
    base = dict(id="sfx-1", catalog_id="catalog-1", at_s=2.0)
    base.update(overrides)
    return base


def _ending_clip(**overrides) -> dict:
    base = dict(media_id=str(uuid.uuid4()))
    base.update(overrides)
    return base


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _db_gen(item, *, assets: list | None = None, sfx_side_effect: list | None = None):
    """Build a get_db override + a holder dict exposing the AsyncMock db.

    The first `db.execute()` result is always the PlanItem lookup. A second
    result (the PlanItemAsset lookup) is only queued when `assets` is given
    — several 422 paths (duplicate ids, an unknown/unpublished sound effect
    with no overlay/ending media referenced) never reach that query.
    `sfx_side_effect` becomes `db.get`'s side_effect (SoundEffect lookups),
    in the order `body.sound_effects` lists them.
    """
    holder: dict[str, AsyncMock] = {}

    async def _gen():
        db = AsyncMock()
        item_res = MagicMock()
        item_res.scalar_one_or_none.return_value = item
        execute_results = [item_res]
        if assets is not None:
            assets_res = MagicMock()
            assets_res.scalars.return_value.all.return_value = assets
            execute_results.append(assets_res)
        db.execute = AsyncMock(side_effect=execute_results)
        if sfx_side_effect is not None:
            db.get = AsyncMock(side_effect=sfx_side_effect)
        holder["db"] = db
        yield db

    return _gen, holder


def _request(client, method: str, item_id, *, json: dict | None = None, lane_flag: bool = False):
    with (
        patch("app.routes.admin.settings") as auth_settings,
        patch("app.routes.admin_plan_items.settings") as lane_settings,
    ):
        auth_settings.admin_api_key = VALID_TOKEN
        lane_settings.phone_subtitled_media_lanes_enabled = lane_flag
        kwargs = {"headers": {"X-Admin-Token": VALID_TOKEN}}
        if json is not None:
            kwargs["json"] = json
        return client.request(method, f"/admin/plan-items/{item_id}/phone-lanes", **kwargs)


def _call(
    client, method: str, item, *, json: dict | None = None, assets=None, sfx=None, lane_flag=False
):
    gen, holder = _db_gen(item, assets=assets, sfx_side_effect=sfx)
    app.dependency_overrides[get_db] = gen
    try:
        res = _request(client, method, item.id, json=json, lane_flag=lane_flag)
    finally:
        app.dependency_overrides.pop(get_db, None)
    return res, holder


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestPhoneLanesAuth:
    def test_put_missing_token_unauthorized(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.put(f"/admin/plan-items/{uuid.uuid4()}/phone-lanes", json={})
        assert res.status_code in (401, 422)

    def test_put_wrong_token_401(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.put(
                f"/admin/plan-items/{uuid.uuid4()}/phone-lanes",
                json={},
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401

    def test_get_missing_token_unauthorized(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get(f"/admin/plan-items/{uuid.uuid4()}/phone-lanes")
        assert res.status_code in (401, 422)

    def test_get_wrong_token_401(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get(
                f"/admin/plan-items/{uuid.uuid4()}/phone-lanes",
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401

    def test_delete_wrong_token_401(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.delete(
                f"/admin/plan-items/{uuid.uuid4()}/phone-lanes",
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401


# ── 404 ──────────────────────────────────────────────────────────────────────


class TestPhoneLanesNotFound:
    def _unknown_item_gen(self):
        async def _gen():
            db = AsyncMock()
            item_res = MagicMock()
            item_res.scalar_one_or_none.return_value = None
            db.execute = AsyncMock(return_value=item_res)
            yield db

        return _gen

    def _malformed_gen(self):
        async def _gen():
            yield AsyncMock()

        return _gen

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_unknown_id_returns_404(self, client, method):
        app.dependency_overrides[get_db] = self._unknown_item_gen()
        try:
            res = _request(client, method, uuid.uuid4(), json={} if method == "PUT" else None)
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_malformed_id_returns_404(self, client, method):
        app.dependency_overrides[get_db] = self._malformed_gen()
        try:
            res = _request(client, method, "not-a-uuid", json={} if method == "PUT" else None)
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404


# ── PUT happy path ───────────────────────────────────────────────────────────


class TestPutPhoneLanesHappyPath:
    def test_overlay_ending_and_sfx_resolve_and_persist(self, client):
        item = _plan_item_row()
        image_asset = _asset_row(
            plan_item_id=item.id,
            kind="image",
            status="ready",
            gcs_path="users/u1/plan/p1/pool/photo.jpg",
            gcs_generation="111",
        )
        video_asset = _asset_row(
            plan_item_id=item.id,
            kind="video",
            status="ready",
            gcs_path="users/u1/plan/p1/pool/ending.mp4",
            gcs_generation="222",
        )
        sfx_a = _sfx_row(id="catalog-a", audio_gcs_path="sfx/a.m4a")
        sfx_b = _sfx_row(id="catalog-b", audio_gcs_path="sfx/b.wav")

        body = {
            "overlays": [
                _overlay_card(
                    id="ov-1",
                    media_id=str(image_asset.id),
                    start_s=1.0,
                    end_s=3.5,
                    x_frac=0.6,
                    y_frac=0.3,
                    scale=0.4,
                    fade=True,
                    z=2,
                )
            ],
            "sound_effects": [
                _sfx_card(id="sfx-1", catalog_id="catalog-a", at_s=0.5),
                _sfx_card(id="sfx-2", catalog_id="catalog-b", at_s=4.0, volume=0.8),
            ],
            "ending_clip": _ending_clip(
                media_id=str(video_asset.id), trim_start_s=0.2, max_duration_s=3.0
            ),
        }

        res, holder = _call(
            client,
            "PUT",
            item,
            json=body,
            assets=[image_asset, video_asset],
            sfx=[sfx_a, sfx_b],
            lane_flag=True,
        )

        assert res.status_code == 200
        payload = res.json()
        assert payload["item_id"] == str(item.id)
        assert payload["flag_enabled"] is True
        assert isinstance(payload["note"], str) and payload["note"]

        stored = payload["phone_lane_request"]
        assert len(stored["overlays"]) == 1
        overlay = stored["overlays"][0]
        assert overlay["id"] == "ov-1"
        assert overlay["media_id"] == str(image_asset.id)
        assert overlay["gcs_path"] == image_asset.gcs_path
        assert overlay["generation"] == "111"
        assert overlay["x_frac"] == 0.6
        assert overlay["scale"] == 0.4
        assert overlay["fade"] is True
        assert overlay["z"] == 2

        assert stored["ending_clip"]["media_id"] == str(video_asset.id)
        assert stored["ending_clip"]["gcs_path"] == video_asset.gcs_path
        assert stored["ending_clip"]["generation"] == "222"
        assert stored["ending_clip"]["trim_start_s"] == 0.2
        assert stored["ending_clip"]["max_duration_s"] == 3.0

        sound_effects = {sfx["id"]: sfx for sfx in stored["sound_effects"]}
        assert sound_effects["sfx-1"]["catalog_id"] == "catalog-a"
        assert sound_effects["sfx-1"]["at_s"] == 0.5
        assert sound_effects["sfx-2"]["catalog_id"] == "catalog-b"
        assert sound_effects["sfx-2"]["volume"] == 0.8

        # Persisted on the item + committed exactly once.
        assert item.phone_lane_request == stored
        holder["db"].commit.assert_awaited_once()


# ── PUT 422s ─────────────────────────────────────────────────────────────────


class TestPutPhoneLanes422:
    def test_media_not_in_item_returns_422(self, client):
        item = _plan_item_row()
        other_item_id = uuid.uuid4()
        asset = _asset_row(plan_item_id=other_item_id, kind="image", status="ready")
        body = {"overlays": [_overlay_card(media_id=str(asset.id))]}
        res, holder = _call(client, "PUT", item, json=body, assets=[asset])
        assert res.status_code == 422
        assert str(asset.id) in res.json()["detail"]
        holder["db"].commit.assert_not_awaited()

    def test_media_wrong_kind_returns_422(self, client):
        item = _plan_item_row()
        asset = _asset_row(plan_item_id=item.id, kind="video", status="ready")
        body = {"overlays": [_overlay_card(media_id=str(asset.id))]}
        res, _holder = _call(client, "PUT", item, json=body, assets=[asset])
        assert res.status_code == 422
        assert "not a ready image" in res.json()["detail"]

    def test_media_not_ready_returns_422(self, client):
        item = _plan_item_row()
        asset = _asset_row(plan_item_id=item.id, kind="image", status="analyzing")
        body = {"overlays": [_overlay_card(media_id=str(asset.id))]}
        res, _holder = _call(client, "PUT", item, json=body, assets=[asset])
        assert res.status_code == 422

    def test_missing_generation_returns_422(self, client):
        item = _plan_item_row()
        asset = _asset_row(plan_item_id=item.id, kind="image", status="ready", gcs_generation=None)
        body = {"overlays": [_overlay_card(media_id=str(asset.id))]}
        res, _holder = _call(client, "PUT", item, json=body, assets=[asset])
        assert res.status_code == 422

    def test_unknown_sfx_returns_422(self, client):
        item = _plan_item_row()
        body = {"sound_effects": [_sfx_card(catalog_id="does-not-exist")]}
        res, _holder = _call(client, "PUT", item, json=body, sfx=[None])
        assert res.status_code == 422
        assert "does-not-exist" in res.json()["detail"]

    def test_unpublished_sfx_returns_422(self, client):
        item = _plan_item_row()
        unpublished = _sfx_row(published_at=None)
        body = {"sound_effects": [_sfx_card(catalog_id=unpublished.id)]}
        res, _holder = _call(client, "PUT", item, json=body, sfx=[unpublished])
        assert res.status_code == 422

    def test_unplayable_sfx_extension_returns_422(self, client):
        item = _plan_item_row()
        unplayable = _sfx_row(audio_gcs_path="sfx/whoosh.ogg")
        body = {"sound_effects": [_sfx_card(catalog_id=unplayable.id)]}
        res, _holder = _call(client, "PUT", item, json=body, sfx=[unplayable])
        assert res.status_code == 422

    def test_duplicate_overlay_ids_returns_422(self, client):
        item = _plan_item_row()
        body = {
            "overlays": [
                _overlay_card(id="ov-1", media_id=str(uuid.uuid4())),
                _overlay_card(id="ov-1", media_id=str(uuid.uuid4())),
            ]
        }
        res, holder = _call(client, "PUT", item, json=body)
        assert res.status_code == 422
        assert "unique" in res.json()["detail"]
        holder["db"].commit.assert_not_awaited()

    def test_duplicate_sfx_ids_returns_422(self, client):
        item = _plan_item_row()
        body = {
            "sound_effects": [
                _sfx_card(id="sfx-1", catalog_id="catalog-a"),
                _sfx_card(id="sfx-1", catalog_id="catalog-b"),
            ]
        }
        res, _holder = _call(client, "PUT", item, json=body)
        assert res.status_code == 422

    def test_invalid_overlay_window_returns_422(self, client):
        item = _plan_item_row()
        asset = _asset_row(plan_item_id=item.id, kind="image", status="ready")
        body = {"overlays": [_overlay_card(media_id=str(asset.id), start_s=5.0, end_s=5.0)]}
        res, _holder = _call(client, "PUT", item, json=body, assets=[asset])
        assert res.status_code == 422


# ── GET ──────────────────────────────────────────────────────────────────────


class TestGetPhoneLanes:
    def test_returns_stored_request(self, client):
        stored = {"overlays": [], "sound_effects": [], "ending_clip": None}
        item = _plan_item_row(phone_lane_request=stored)
        res, holder = _call(client, "GET", item, lane_flag=True)
        assert res.status_code == 200
        body = res.json()
        assert body["item_id"] == str(item.id)
        assert body["phone_lane_request"] == stored
        assert body["flag_enabled"] is True
        assert "note" not in body
        holder["db"].commit.assert_not_awaited()

    def test_returns_none_when_unset(self, client):
        item = _plan_item_row(phone_lane_request=None)
        res, holder = _call(client, "GET", item)
        assert res.status_code == 200
        body = res.json()
        assert body["phone_lane_request"] is None
        holder["db"].commit.assert_not_awaited()


# ── DELETE ───────────────────────────────────────────────────────────────────


class TestDeletePhoneLanes:
    def test_clears_stored_request(self, client):
        item = _plan_item_row(
            phone_lane_request={"overlays": [], "sound_effects": [], "ending_clip": None}
        )
        res, holder = _call(client, "DELETE", item)
        assert res.status_code == 200
        body = res.json()
        assert body["item_id"] == str(item.id)
        assert body["phone_lane_request"] is None
        assert item.phone_lane_request is None
        holder["db"].commit.assert_awaited_once()

    def test_idempotent_when_already_unset(self, client):
        item = _plan_item_row(phone_lane_request=None)
        res, holder = _call(client, "DELETE", item)
        assert res.status_code == 200
        assert res.json()["phone_lane_request"] is None
        holder["db"].commit.assert_awaited_once()
