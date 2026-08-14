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
from app.models import ContentPlan, Job, Persona, PlanItem
from app.storage import ObjectMetadata

SETTINGS = "app.config.settings"


@pytest.fixture(autouse=True)
def _no_real_broker_publish():
    """Review C4: register/upload routes dispatch analyze_pool_asset.apply_async.
    Without a patch these publish REAL Celery messages to the shared redis broker
    (conftest REDIS_URL) — a sibling worktree worker consumes them with garbage
    args (asset.id is an AsyncMock). Patch the dispatch so tests are isolated AND
    the dispatch contract is finally assertable."""
    with (
        patch("app.tasks.autoplace.analyze_pool_asset.apply_async") as m,
        patch(
            "app.routes.plan_items.storage.object_metadata",
            side_effect=lambda path: ObjectMetadata(
                path=path,
                generation="7",
                etag="etag",
                size=1,
                content_type="image/png",
            ),
        ),
        patch("app.routes.plan_items.storage.signed_put_url", return_value="https://signed"),
        patch("app.routes.plan_items.storage.delete_object_generation"),
    ):
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
    item.current_job = None
    item.current_job_id = None
    plan = MagicMock()
    plan.id = item.content_plan_id
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.ownership_epoch = 0
    plan.ownership_quarantined_at = None
    persona = MagicMock()
    persona.id = plan.persona_id
    persona.user_id = user_id
    plan._test_persona = persona
    return item, plan


def _db(execute_results: list, plan) -> AsyncMock:
    item = execute_results[0].scalar_one_or_none()
    remaining = list(execute_results[1:])

    async def _execute(stmt):  # noqa: ANN001
        descriptions = getattr(stmt, "column_descriptions", None) or []
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is Persona:
            return _scalar_result(plan._test_persona)
        if entity is PlanItem:
            return _scalar_result(item)
        if remaining:
            return remaining.pop(0)
        empty = _scalars_result([])
        empty.scalar_one_or_none = MagicMock(return_value=None)
        empty.scalar_one = MagicMock(return_value=0)
        return empty

    async def _get(model, _row_id, **_kwargs):  # noqa: ANN001
        if model is ContentPlan:
            return plan
        if model is Job:
            return item.current_job
        return None

    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock(side_effect=_get)
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
    a.user_context = None
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
        (
            "post",
            "/assets/00000000-0000-0000-0000-0000000000aa/reanalyze",
            None,
        ),
        ("patch", "/assets/00000000-0000-0000-0000-0000000000aa/context", {"user_context": "x"}),
        # FIXED uuid, NOT uuid.uuid4(): a fresh uuid per import makes the
        # parametrize id differ across pytest-xdist workers → "Different tests
        # were collected between gw1 and gw3" collection error on CI (-n auto).
        ("delete", "/assets/00000000-0000-0000-0000-0000000000aa", None),
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
    db = _db(
        [
            _scalar_result(item),
            _scalars_result([]),
            _scalars_result([]),
            _scalar_result(0),
        ],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_best_effort"),
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
    db = _db(
        [
            _scalar_result(item),
            _scalars_result([]),
            _scalars_result([]),
            _scalar_result(0),
        ],
        plan,
    )
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
    db = _db(
        [
            _scalar_result(item),
            _scalars_result([]),
            _scalars_result([]),
            _scalar_result(19),
        ],
        plan,
    )
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
    assert resp.json()["detail"] == "Your visuals pool has room for 1 more. Select up to 1."


def test_upload_urls_reuses_reservation_and_rotates_interrupted_target(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.client_upload_id = "file-stable"
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    reservation.upload_expires_at = None
    old_path = reservation.gcs_path
    db = _db(
        [
            _scalar_result(item),
            _scalars_result([]),
            _scalars_result([reservation]),
            _scalar_result(1),
        ],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_best_effort") as cleanup,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            headers={"X-Correlation-Id": "batch-stable"},
            json={
                "files": [
                    {
                        "filename": "f.png",
                        "content_type": "image/png",
                        "file_size_bytes": 100,
                        "client_upload_id": "file-stable",
                    }
                ]
            },
        )
    assert resp.status_code == 200
    target = resp.json()["urls"][0]
    assert target["reservation_id"] == str(reservation.id)
    assert target["client_upload_id"] == "file-stable"
    assert target["gcs_path"] != old_path
    assert reservation.correlation_id == "batch-stable"
    cleanup.assert_called_once_with(old_path)
    db.add.assert_not_called()


def test_upload_urls_keeps_old_target_when_cleanup_must_retry(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.client_upload_id = "file-stable"
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    old_path = reservation.gcs_path
    db = _db(
        [
            _scalar_result(item),
            _scalars_result([]),
            _scalars_result([reservation]),
            _scalar_result(1),
        ],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_best_effort", return_value=False),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={
                "files": [
                    {
                        "filename": "f.png",
                        "content_type": "image/png",
                        "file_size_bytes": 100,
                        "client_upload_id": "file-stable",
                    }
                ]
            },
        )

    assert resp.status_code == 503
    assert resp.json()["detail"]["retryable"] is True
    assert resp.json()["detail"]["stage"] == "reservation_cleanup"
    assert reservation.gcs_path == old_path
    db.commit.assert_not_awaited()


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
    assert body["status"] == "queued"
    assert body["error_code"] is None
    assert body["retryable"] is False
    assert body["deduped"] is False
    assert db.add.call_count == 1
    # Silent-rollback trap: the write must be committed (plan 005 decision 4A).
    assert db.commit.await_count >= 1
    # Review C4: analysis IS dispatched (and to a mock, not the real broker).
    assert _no_real_broker_publish.call_count == 1


def test_reserved_registration_finalizes_once_and_repeat_is_idempotent(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.client_upload_id = "file-stable"
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    reservation.correlation_id = "batch-original"
    reservation.analysis_attempt_count = 0
    first_db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    _override(user, first_db)
    metadata = ObjectMetadata(
        path=reservation.gcs_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )
    payload = {
        "reservation_id": str(reservation.id),
        "gcs_path": reservation.gcs_path,
        "content_type": "image/png",
        "content_hash": "hash",
    }
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=metadata),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        first = client.post(
            f"/plan-items/{item.id}/assets",
            headers={"X-Correlation-Id": "batch-retry"},
            json=payload,
        )

        second_db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
        _override(user, second_db)
        second = client.post(f"/plan-items/{item.id}/assets", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"] == str(reservation.id)
    assert reservation.status == "queued"
    assert reservation.gcs_generation == "42"
    assert reservation.correlation_id == "batch-retry"
    assert reservation.analysis_attempt_token
    assert reservation.analysis_last_dispatched_at is not None
    _no_real_broker_publish.assert_called_once()


def test_register_rejects_and_deletes_reservation_metadata_mismatch(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
    _override(user, db)
    mismatched = ObjectMetadata(
        path=reservation.gcs_path,
        generation="11",
        etag="etag",
        size=99,
        content_type="image/png",
    )
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=mismatched),
        patch("app.routes.plan_items.storage.delete_object_generation") as delete_generation,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "reservation_id": str(reservation.id),
                "gcs_path": reservation.gcs_path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )
    assert resp.status_code == 422
    assert "did not match" in resp.json()["detail"]
    delete_generation.assert_called_once_with(reservation.gcs_path, generation="11")
    db.delete.assert_awaited_once_with(reservation)


def test_register_cleanup_failure_keeps_reservation_retryable(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
    _override(user, db)
    mismatched = ObjectMetadata(
        path=reservation.gcs_path,
        generation="11",
        etag="etag",
        size=99,
        content_type="image/png",
    )
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=mismatched),
        patch(
            "app.routes.plan_items.storage.delete_object_generation",
            side_effect=RuntimeError("private storage detail"),
        ),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "reservation_id": str(reservation.id),
                "gcs_path": reservation.gcs_path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 503
    assert resp.json()["detail"] == {
        "message": "Kria couldn't finish cleaning up this upload. Retry in a moment.",
        "code": "upload_cleanup_temporarily_unavailable",
        "retryable": True,
        "stage": "registration_cleanup",
    }
    db.delete.assert_not_awaited()


def test_register_dispatch_failure_is_actionable(client: TestClient, _no_real_broker_publish):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(None), _scalar_result(0)], plan)
    _override(user, db)
    _no_real_broker_publish.side_effect = RuntimeError("redis unavailable")
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(f"/plan-items/{item.id}/assets", json=_register_body(user.id, item.id))
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert resp.json()["error_code"] == "analysis_temporarily_unavailable"
    assert resp.json()["retryable"] is True
    assert "redis" not in resp.json()["error_detail"]


def test_register_dedupes_on_content_hash(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    existing = _asset_row(item.id, user.id, content_hash="hash-1")
    db = _db([_scalar_result(item), _scalar_result(existing)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
        patch("app.routes.plan_items.storage.delete_object_generation") as delete_generation,
    ):
        resp = client.post(f"/plan-items/{item.id}/assets", json=_register_body(user.id, item.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduped"] is True
    assert body["id"] == str(existing.id)
    # Dedupe path adds no row.
    assert db.add.call_count == 0
    delete_generation.assert_called_once_with(
        f"users/{user.id}/plan/{item.id}/pool/f.png",
        generation="7",
    )


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


def test_reanalyze_failed_asset_queues_fenced_attempt(client: TestClient, _no_real_broker_publish):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.status = "failed"
    asset.error_code = "analysis_failed"
    asset.error_detail = "Try again"
    asset.error_retryable = True
    asset.analysis_attempt_count = 3
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(asset))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items._load_owned_item", new=AsyncMock(return_value=item)),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(f"/plan-items/{item.id}/assets/{asset.id}/reanalyze")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["error_code"] is None
    assert asset.analysis_attempt_count == 1
    args = _no_real_broker_publish.call_args.kwargs["args"]
    assert args[:2] == [str(asset.id), False]
    assert args[2] == asset.analysis_attempt_token


def test_reanalyze_active_asset_is_idempotent(client: TestClient, _no_real_broker_publish):
    user = _user()
    item, _plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.status = "analyzing"
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(asset))
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items._load_owned_item", new=AsyncMock(return_value=item)),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(f"/plan-items/{item.id}/assets/{asset.id}/reanalyze")
    assert resp.status_code == 200
    assert resp.json()["status"] == "analyzing"
    _no_real_broker_publish.assert_not_called()


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


def test_list_serializes_brands_from_analysis(client: TestClient):
    """PoolAssetOut carries `brands` from the analysis JSONB (ANALYSIS_VERSION 5,
    brand-aware matching): list passthrough when analyzed, [] when nothing was
    detected, None on legacy analyses, and non-list garbage degrades to None —
    never a response-validation 500."""
    user = _user()
    item, plan = _owned_item(user.id)
    analyzed = _asset_row(item.id, user.id, content_hash="h1")
    analyzed.analysis = {"subject": "checkout screen", "brands": ["Acme", "Duolingo"]}
    none_found = _asset_row(item.id, user.id, content_hash="h2")
    none_found.analysis = {"subject": "settings", "brands": []}
    legacy = _asset_row(item.id, user.id, content_hash="h3")  # analysis None (pre-v5)
    garbage = _asset_row(item.id, user.id, content_hash="h4")
    garbage.analysis = {"brands": "Acme"}  # corrupt non-list shape
    rows = [analyzed, none_found, legacy, garbage]
    db = _db([_scalar_result(item), _scalars_result(rows)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 200
    out = resp.json()["assets"]
    assert out[0]["brands"] == ["Acme", "Duolingo"]
    assert out[1]["brands"] == []
    assert out[2]["brands"] is None
    assert out[3]["brands"] is None


def test_list_brands_filters_falsy_and_coerces_non_strings(client: TestClient):
    """A within-list corrupt element must not 500 the response: falsy entries
    (None, "") are dropped by the `if b` filter and truthy non-strings are
    str()-coerced, so `list[str]` validation always holds."""
    user = _user()
    item, plan = _owned_item(user.id)
    row = _asset_row(item.id, user.id, content_hash="h1")
    row.analysis = {"subject": "x", "brands": ["Acme", None, "", 123]}
    db = _db([_scalar_result(item), _scalars_result([row])], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 200
    assert resp.json()["assets"][0]["brands"] == ["Acme", "123"]


def test_list_serializes_user_context_and_source_labeled_nova_fields(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    row = _asset_row(item.id, user.id, content_hash="h1")
    row.user_context = "This is the competitor pricing table"
    row.analysis = {
        "subject": "spreadsheet",
        "description": "A table with three pricing tiers",
        "on_screen_text": "$9 $19 $49",
    }
    db = _db([_scalar_result(item), _scalars_result([row])], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 200
    asset = resp.json()["assets"][0]
    assert asset["user_context"] == "This is the competitor pricing table"
    assert asset["nova_description"] == "A table with three pricing tiers"
    assert asset["nova_on_screen_text"] == "$9 $19 $49"


# ── context ──────────────────────────────────────────────────────────────────


def test_update_context_trims_saves_and_leaves_analysis_unchanged(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.analysis = {"description": "Nova's original read"}
    db = _db([_scalar_result(item), _scalar_result(asset)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.patch(
            f"/plan-items/{item.id}/assets/{asset.id}/context",
            json={"user_context": "  use this when I mention churn  "},
        )
    assert resp.status_code == 200
    assert asset.user_context == "use this when I mention churn"
    assert asset.analysis == {"description": "Nova's original read"}
    assert resp.json()["user_context"] == "use this when I mention churn"
    assert resp.json()["nova_description"] == "Nova's original read"
    assert db.commit.await_count >= 1


def test_update_context_empty_string_clears(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.user_context = "old"
    db = _db([_scalar_result(item), _scalar_result(asset)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.patch(
            f"/plan-items/{item.id}/assets/{asset.id}/context",
            json={"user_context": "   "},
        )
    assert resp.status_code == 200
    assert asset.user_context is None
    assert resp.json()["user_context"] == ""


def test_update_context_rejects_over_500_chars_after_trim(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    db = _db([_scalar_result(item), _scalar_result(asset)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.patch(
            f"/plan-items/{item.id}/assets/{asset.id}/context",
            json={"user_context": f"  {'x' * 501}  "},
        )
    assert resp.status_code == 422
    assert asset.user_context is None
    assert db.commit.await_count == 0


def test_update_context_bad_uuid_rejects(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.patch(
            f"/plan-items/{item.id}/assets/not-a-uuid/context",
            json={"user_context": "x"},
        )
    assert resp.status_code == 400


def test_update_context_404_when_asset_missing(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(None)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.patch(
            f"/plan-items/{item.id}/assets/{uuid.uuid4()}/context",
            json={"user_context": "x"},
        )
    assert resp.status_code == 404


def test_update_context_clears_only_pending_suggestions(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    job = MagicMock()
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "v1",
                "overlay_suggestions": [{"id": "s1"}],
                "overlay_suggest_status": "ready",
                "overlay_suggest_hash": "old",
                "overlay_suggest_wishlist": ["add x"],
                "media_overlays": [{"id": "manual", "start_s": 1.0, "end_s": 2.0}],
                "sound_effects": [{"id": "sfx"}],
                "visual_blocks": [{"id": "block"}],
                "render_status": "ready",
            }
        ]
    }
    item.current_job = job
    item.current_job_id = job.id
    db = _db([_scalar_result(item), _scalar_result(asset)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
        patch("app.routes.plan_items.flag_modified") as flag_modified,
    ):
        resp = client.patch(
            f"/plan-items/{item.id}/assets/{asset.id}/context",
            json={"user_context": "this means launch week"},
        )
    assert resp.status_code == 200
    variant = job.assembly_plan["variants"][0]
    assert variant["overlay_suggestions"] is None
    assert variant["overlay_suggest_status"] is None
    assert variant["overlay_suggest_hash"] is None
    assert variant["overlay_suggest_wishlist"] is None
    assert variant["media_overlays"] == [{"id": "manual", "start_s": 1.0, "end_s": 2.0}]
    assert variant["sound_effects"] == [{"id": "sfx"}]
    assert variant["visual_blocks"] == [{"id": "block"}]
    assert variant["render_status"] == "ready"
    flag_modified.assert_called_once_with(job, "assembly_plan")


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
