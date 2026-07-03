"""Route tests for the plan-item asset pool (auto-placement PR0, plans/005).

Mock-DB style, mirroring test_plan_item_variant_edit.py. Locks the PR0 contract:
flag gating (404 when OVERLAY_AUTOPLACE_ENABLED off), ownership, the 20-asset cap,
content-hash dedupe (never re-registers identical bytes), the pool GCS-prefix
check on register, and the silent-rollback trap (`db.commit` awaited on writes).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

SETTINGS = "app.config.settings"


@pytest.fixture(autouse=True)
def _no_real_broker_publish():
    """Review C4: register/upload routes dispatch analyze_pool_asset.apply_async.
    Without a patch these publish REAL Celery messages to the shared redis broker
    (conftest REDIS_URL) — a sibling worktree worker consumes them with garbage
    args (asset.id is an AsyncMock). Patch the dispatch so tests are isolated AND
    the dispatch contract is finally assertable."""
    with patch("app.tasks.autoplace.analyze_pool_asset.apply_async") as m:
        yield m


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    return r


def _scalars_result(values: list) -> MagicMock:
    r = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=values)
    r.scalars = MagicMock(return_value=scalars)
    return r


def _owned_item(user_id: uuid.UUID):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db(execute_results: list, plan) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    db.get = AsyncMock(return_value=plan)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _asset_row(item_id, user_id, *, content_hash="abc123") -> MagicMock:
    a = MagicMock()
    a.id = uuid.uuid4()
    a.plan_item_id = item_id
    a.user_id = user_id
    a.gcs_path = f"users/{user_id}/plan/{item_id}/pool/x.png"
    a.kind = "image"
    a.content_hash = content_hash
    a.source_filename = "x.png"
    a.duration_s = None
    a.aspect = None
    a.analysis = None
    a.status = "uploaded"
    return a


# ── flag gating ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("post", "/assets/upload-urls", {"files": []}),
        (
            "post",
            "/assets",
            {"gcs_path": "users/u/plan/i/pool/f.png", "content_type": "image/png"},
        ),
        ("get", "/assets", None),
        ("delete", f"/assets/{uuid.uuid4()}", None),
    ],
)
def test_all_pool_routes_404_when_flag_off(
    client: TestClient, method: str, path_suffix: str, body: dict | None
):
    user = _user()
    _override(user, AsyncMock())
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", False):
        resp = getattr(client, method)(
            f"/plan-items/{uuid.uuid4()}{path_suffix}",
            **({"json": body} if body is not None else {}),
        )
    assert resp.status_code == 404


# ── upload-urls ───────────────────────────────────────────────────────────────


def test_upload_urls_happy_path(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(0)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            "app.routes.plan_items.storage.presigned_put_url_for_pool_asset",
            return_value=("https://signed", f"users/{user.id}/plan/{item.id}/pool/f.png"),
        ),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={
                "files": [
                    {"filename": "f.png", "content_type": "image/png", "file_size_bytes": 100}
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["urls"][0]["upload_url"] == "https://signed"
    assert body["urls"][0]["gcs_path"].startswith(f"users/{user.id}/plan/{item.id}/pool/")


def test_upload_urls_rejects_bad_content_type(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(0)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={
                "files": [
                    {"filename": "f.pdf", "content_type": "application/pdf", "file_size_bytes": 9}
                ]
            },
        )
    assert resp.status_code == 400


def test_upload_urls_enforces_cap_counting_existing(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    # 19 existing + 2 requested > 20 → reject
    db = _db([_scalar_result(item), _scalar_result(19)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={
                "files": [
                    {"filename": "a.png", "content_type": "image/png", "file_size_bytes": 1},
                    {"filename": "b.png", "content_type": "image/png", "file_size_bytes": 1},
                ]
            },
        )
    assert resp.status_code == 400
    assert "capped" in resp.json()["detail"]


# ── register ──────────────────────────────────────────────────────────────────


def _register_body(user_id, item_id, **overrides) -> dict:
    body = {
        "gcs_path": f"users/{user_id}/plan/{item_id}/pool/f.png",
        "content_type": "image/png",
        "content_hash": "hash-1",
        "source_filename": "f.png",
    }
    body.update(overrides)
    return body


def test_register_happy_path_commits(client: TestClient, _no_real_broker_publish):
    user = _user()
    item, plan = _owned_item(user.id)
    # execute order: load item, dedupe lookup (None), count (0)
    db = _db([_scalar_result(item), _scalar_result(None), _scalar_result(0)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(f"/plan-items/{item.id}/assets", json=_register_body(user.id, item.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "image"
    assert body["status"] == "uploaded"
    assert body["deduped"] is False
    assert db.add.call_count == 1
    # Silent-rollback trap: the write must be committed (plan 005 decision 4A).
    assert db.commit.await_count >= 1
    # Review C4: analysis IS dispatched (and to a mock, not the real broker).
    assert _no_real_broker_publish.call_count == 1


def test_register_dedupes_on_content_hash(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    existing = _asset_row(item.id, user.id, content_hash="hash-1")
    db = _db([_scalar_result(item), _scalar_result(existing)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(f"/plan-items/{item.id}/assets", json=_register_body(user.id, item.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduped"] is True
    assert body["id"] == str(existing.id)
    # Dedupe path adds no row.
    assert db.add.call_count == 0


def test_register_rejects_foreign_prefix(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json=_register_body(user.id, item.id, gcs_path="users/other/plan/x/pool/f.png"),
        )
    assert resp.status_code == 422


def test_register_enforces_cap(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(None), _scalar_result(20)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/assets", json=_register_body(user.id, item.id))
    assert resp.status_code == 400
    assert "capped" in resp.json()["detail"]


# ── list ──────────────────────────────────────────────────────────────────────


def test_list_returns_assets_with_display_urls(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    rows = [_asset_row(item.id, user.id), _asset_row(item.id, user.id, content_hash="h2")]
    db = _db([_scalar_result(item), _scalars_result(rows)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_assets"] == 20
    assert len(body["assets"]) == 2
    assert body["assets"][0]["display_url"] == "https://get"


def test_list_survives_signing_failure(client: TestClient):
    """Thumbnail signing is best-effort — a storage error must not 500 the list."""
    user = _user()
    item, plan = _owned_item(user.id)
    rows = [_asset_row(item.id, user.id)]
    db = _db([_scalar_result(item), _scalars_result(rows)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            "app.routes.plan_items.storage.signed_get_url",
            side_effect=RuntimeError("gcs down"),
        ),
    ):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 200
    assert resp.json()["assets"][0]["display_url"] is None


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_asset_and_commits(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    db = _db([_scalar_result(item), _scalar_result(asset)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.delete(f"/plan-items/{item.id}/assets/{asset.id}")
    assert resp.status_code == 200
    db.delete.assert_awaited_once_with(asset)
    assert db.commit.await_count >= 1


def test_delete_404_when_asset_missing(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(None)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.delete(f"/plan-items/{item.id}/assets/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── ownership ─────────────────────────────────────────────────────────────────


def test_list_404_when_not_owner(client: TestClient):
    user = _user()
    item, plan = _owned_item(uuid.uuid4())  # plan owned by someone else
    db = _db([_scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 404
