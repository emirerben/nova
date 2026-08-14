"""Route tests for the plan-item asset pool (auto-placement PR0, plans/005).

Mock-DB style, mirroring test_plan_item_variant_edit.py. Locks the PR0 contract:
flag gating (404 when OVERLAY_AUTOPLACE_ENABLED off), ownership, the 20-asset cap,
content-hash dedupe (never re-registers identical bytes), the pool GCS-prefix
check on register, and the silent-rollback trap (`db.commit` awaited on writes).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
        patch("app.routes.plan_items.storage.signed_put_url_legacy", return_value="https://signed"),
        patch("app.routes.plan_items.storage.delete_object_generation"),
        patch(
            "app.routes.plan_items.storage.delete_object_generation_best_effort",
            return_value=True,
        ),
        patch("app.routes.plan_items.storage.delete_object_best_effort", return_value=True),
        patch(f"{SETTINGS}.pool_asset_queued_status_enabled", True),
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


def _assert_capacity_query_expires_abandoned_reservations(db: AsyncMock) -> None:
    count_sql = next(
        str(call.args[0])
        for call in db.execute.await_args_list
        if "count(*)" in str(call.args[0]).lower()
    )
    assert "plan_item_assets.status NOT IN" in count_sql
    assert "plan_item_assets.upload_expires_at" in count_sql
    assert "plan_item_assets.created_at" in count_sql


def _assert_dedupe_query_reuses_only_finalized_assets(db: AsyncMock) -> None:
    dedupe_sql = next(
        str(call.args[0])
        for call in db.execute.await_args_list
        if "AND plan_item_assets.content_hash =" in str(call.args[0])
    )
    assert "plan_item_assets.status IN" in dedupe_sql


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
    a.gcs_generation = None
    a.correlation_id = None
    return a


# ── flag gating ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        (
            "post",
            "/assets/upload-urls",
            {"files": [{"filename": "x.png", "content_type": "image/png", "file_size_bytes": 1}]},
        ),
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
    assert body["urls"][0]["gcs_path"].startswith(
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/"
    )


def test_upload_urls_preserves_deployed_content_type_only_client(client: TestClient):
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
        patch("app.routes.plan_items.storage.signed_put_url") as strict,
        patch(
            "app.routes.plan_items.storage.signed_put_url_legacy",
            return_value="https://legacy-signed",
        ) as legacy,
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
    assert resp.json()["urls"][0]["upload_headers"] == {}
    assert resp.json()["urls"][0]["gcs_path"].startswith("dev-user/")
    legacy.assert_called_once()
    strict.assert_not_called()


def test_upload_urls_stable_client_id_uses_strict_persistent_target(client: TestClient):
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
        patch(
            "app.routes.plan_items.storage.signed_put_url", return_value="https://strict"
        ) as strict,
        patch("app.routes.plan_items.storage.signed_put_url_legacy") as legacy,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={
                "files": [
                    {
                        "filename": "f.png",
                        "content_type": "image/png",
                        "file_size_bytes": 100,
                        "client_upload_id": "stable-file",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    target = resp.json()["urls"][0]
    assert target["gcs_path"].startswith(f"dev-user/{user.id}/plan-pool-reservations/{item.id}/")
    assert target["upload_headers"] == {"x-goog-if-generation-match": "0"}
    strict.assert_called_once()
    legacy.assert_not_called()


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


@pytest.mark.parametrize(
    ("files", "status_code"),
    [
        ([], 422),
        (
            [
                {
                    "filename": "a.png",
                    "content_type": "image/png",
                    "file_size_bytes": 1,
                    "client_upload_id": "same",
                },
                {
                    "filename": "b.png",
                    "content_type": "image/png",
                    "file_size_bytes": 1,
                    "client_upload_id": "same",
                },
            ],
            422,
        ),
        ([{"filename": "a.png", "content_type": "image/png", "file_size_bytes": 0}], 422),
        (
            [
                {
                    "filename": "a.png",
                    "content_type": "image/png",
                    "file_size_bytes": 25 * 1024 * 1024 + 1,
                }
            ],
            422,
        ),
        (
            [
                {
                    "filename": "a.mp4",
                    "content_type": "video/mp4",
                    "file_size_bytes": 512 * 1024 * 1024 + 1,
                }
            ],
            422,
        ),
    ],
)
def test_upload_urls_rejects_invalid_batch_before_storage_changes(
    client: TestClient,
    files: list[dict],
    status_code: int,
):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_best_effort") as cleanup,
        patch("app.routes.plan_items.storage.signed_put_url") as signed,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={"files": files},
        )

    assert resp.status_code == status_code
    cleanup.assert_not_called()
    signed.assert_not_called()
    db.commit.assert_not_awaited()


def test_upload_urls_rejects_more_than_capacity_before_route_work(client: TestClient):
    user = _user()
    load_owned = AsyncMock()
    _override(user, AsyncMock())
    files = [
        {
            "filename": f"{index}.png",
            "content_type": "image/png",
            "file_size_bytes": 1,
            "client_upload_id": f"file-{index}",
        }
        for index in range(21)
    ]
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items._load_owned_item", new=load_owned),
    ):
        resp = client.post(
            f"/plan-items/{uuid.uuid4()}/assets/upload-urls",
            json={"files": files},
        )

    assert resp.status_code == 422
    load_owned.assert_not_awaited()


@pytest.mark.parametrize(
    ("content_type", "size"),
    [("image/png", 25 * 1024 * 1024), ("video/mp4", 512 * 1024 * 1024)],
)
def test_upload_urls_accepts_exact_size_limits(
    client: TestClient,
    content_type: str,
    size: int,
):
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
                    {
                        "filename": "asset",
                        "content_type": content_type,
                        "file_size_bytes": size,
                    }
                ]
            },
        )

    assert resp.status_code == 200


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


def test_upload_urls_reuses_reservation_target_without_orphaning_old_url(client: TestClient):
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
    assert target["gcs_path"] == old_path
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


def test_upload_urls_cleans_expired_promotion_before_counting_capacity(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "promoting"
    reservation.client_upload_id = "abandoned-file"
    reservation.upload_expires_at = datetime.now(UTC) - timedelta(hours=1)
    source_path = f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/old.png"
    destination_path = f"users/{user.id}/plan/{item.id}/pool/{reservation.id}-old.png"
    reservation.gcs_path = source_path
    reservation.gcs_generation = None
    reservation.analysis = {
        "_upload_promotion": {
            "source_path": source_path,
            "source_generation": "42",
            "destination_path": destination_path,
        }
    }
    db = _db(
        [
            _scalar_result(item),
            _scalars_result([reservation]),
            _scalars_result([]),
            _scalar_result(0),
        ],
        plan,
    )
    _override(user, db)

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            "app.routes.plan_items.storage.delete_object_generation_best_effort",
            return_value=True,
        ) as exact_cleanup,
        patch(
            "app.routes.plan_items.storage.delete_object_best_effort",
            return_value=True,
        ) as latest_cleanup,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload-urls",
            json={
                "files": [
                    {
                        "filename": "new.png",
                        "content_type": "image/png",
                        "file_size_bytes": 100,
                        "client_upload_id": "new-file",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    exact_cleanup.assert_called_once_with(source_path, generation="42")
    latest_cleanup.assert_called_once_with(destination_path)
    db.delete.assert_awaited_once_with(reservation)
    _assert_capacity_query_expires_abandoned_reservations(db)


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
    # execute order: load item, path owner, dedupe lookup, count.
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(None), _scalar_result(0)],
        plan,
    )
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
    publish = _no_real_broker_publish.call_args
    assert publish.kwargs["args"] == [body["id"], False]
    assert publish.kwargs["headers"]["pool_asset_attempt_token"]
    _assert_capacity_query_expires_abandoned_reservations(db)
    _assert_dedupe_query_reuses_only_finalized_assets(db)


def test_queued_response_maps_to_uploaded_until_frontend_activation(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(None), _scalar_result(0)],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(f"{SETTINGS}.pool_asset_queued_status_enabled", False),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json=_register_body(user.id, item.id),
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "uploaded"
    queued_asset = db.add.call_args.args[0]
    assert queued_asset.status == "queued"


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


def test_reserved_registration_recovers_uploaded_crash_gap(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "uploaded"
    reservation.analysis_attempt_count = 0
    reservation.gcs_generation = "84"
    staging_path = (
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/original.png"
    )
    db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
    _override(user, db)

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
        patch(
            "app.routes.plan_items.storage.delete_object_best_effort",
            return_value=True,
        ) as cleanup,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "reservation_id": str(reservation.id),
                "gcs_path": staging_path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 200
    assert reservation.status == "queued"
    assert reservation.analysis_attempt_token
    _no_real_broker_publish.assert_called_once()
    cleanup.assert_called_once_with(staging_path)


def test_reserved_registration_rejects_expired_reservation(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.upload_expires_at = datetime.now(UTC) - timedelta(hours=1)
    reservation.gcs_path = (
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/expired.png"
    )
    db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
    _override(user, db)

    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "reservation_id": str(reservation.id),
                "gcs_path": reservation.gcs_path,
                "content_type": "image/png",
            },
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "message": "This upload link expired. Retry the upload to get a new link.",
        "code": "upload_reservation_expired",
        "retryable": True,
        "stage": "transfer",
    }
    db.delete.assert_awaited_once_with(reservation)
    _no_real_broker_publish.assert_not_called()


def test_dedupe_against_uploaded_asset_dispatches_analysis(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    existing = _asset_row(item.id, user.id, content_hash="hash-1")
    existing.status = "uploaded"
    existing.analysis_attempt_count = 0
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(existing)],
        plan,
    )
    _override(user, db)

    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json=_register_body(user.id, item.id),
        )

    assert resp.status_code == 200
    assert resp.json()["deduped"] is True
    assert existing.status == "queued"
    _no_real_broker_publish.assert_called_once()
    _assert_dedupe_query_reuses_only_finalized_assets(db)


def test_path_based_client_adopts_preparing_reservation(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    reservation.analysis_attempt_count = 0
    db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    _override(user, db)
    metadata = ObjectMetadata(
        path=reservation.gcs_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=metadata),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "gcs_path": reservation.gcs_path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(reservation.id)
    assert reservation.status == "queued"
    assert reservation.gcs_generation == "42"
    db.add.assert_not_called()
    _no_real_broker_publish.assert_called_once()


def test_legacy_staging_registration_promotes_verified_generation(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.gcs_path = (
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/legacy.png"
    )
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    reservation.analysis_attempt_count = 0
    db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    _override(user, db)
    source = ObjectMetadata(
        path=reservation.gcs_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )

    def _promote(source_path, destination_path, *, source_generation):  # noqa: ANN001
        assert source_path == source.path
        assert source_generation == "42"
        return ObjectMetadata(
            path=destination_path,
            generation="84",
            etag="promoted",
            size=100,
            content_type="image/png",
        )

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=source),
        patch("app.routes.plan_items.storage.copy_object_generation", side_effect=_promote) as copy,
        patch("app.routes.plan_items.storage.delete_object_generation") as cleanup,
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        first = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "gcs_path": source.path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

        retry_db = _db(
            [_scalar_result(item), _scalar_result(None), _scalar_result(reservation)],
            plan,
        )
        _override(user, retry_db)
        retry = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "gcs_path": source.path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"] == str(reservation.id)
    assert reservation.gcs_path.startswith(f"users/{user.id}/plan/{item.id}/pool/")
    assert reservation.gcs_generation == "84"
    copy.assert_called_once()
    cleanup.assert_called_once_with(source.path, generation="42")


def test_staging_promotion_commit_failure_keeps_durable_cleanup_claim(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.gcs_path = (
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/legacy.png"
    )
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    db.commit.side_effect = [None, RuntimeError("private database detail")]
    _override(user, db)
    source = ObjectMetadata(
        path=reservation.gcs_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )

    def _promote(_source_path, destination_path, *, source_generation):  # noqa: ANN001
        assert source_generation == "42"
        return ObjectMetadata(
            path=destination_path,
            generation="84",
            etag="promoted",
            size=100,
            content_type="image/png",
        )

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=source),
        patch("app.routes.plan_items.storage.copy_object_generation", side_effect=_promote),
        patch("app.routes.plan_items._pool_promotion_is_durable", return_value=False),
        patch("app.routes.plan_items.storage.delete_object_generation_best_effort") as cleanup,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "gcs_path": source.path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "registration_temporarily_unavailable"
    cleanup.assert_not_called()
    _no_real_broker_publish.assert_not_called()


def test_staging_copy_failure_leaves_durable_promotion_cleanup_claim(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.gcs_path = (
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/legacy.png"
    )
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    _override(user, db)
    source = ObjectMetadata(
        path=reservation.gcs_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=source),
        patch(
            "app.routes.plan_items.storage.copy_object_generation",
            side_effect=ConnectionError("copy response lost"),
        ),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "gcs_path": source.path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 503
    assert reservation.status == "promoting"
    assert reservation.analysis["_upload_promotion"] == {
        "source_path": source.path,
        "source_generation": "42",
        "destination_path": (f"users/{user.id}/plan/{item.id}/pool/{reservation.id}-x.png"),
    }
    assert db.commit.await_count == 1
    _no_real_broker_publish.assert_not_called()


def test_staging_registration_retry_resumes_durable_promotion_claim(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    source_path = f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/legacy.png"
    destination_path = f"users/{user.id}/plan/{item.id}/pool/{reservation.id}-x.png"
    reservation.status = "promoting"
    reservation.gcs_path = source_path
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    reservation.analysis_attempt_count = 0
    reservation.analysis = {
        "_upload_promotion": {
            "source_path": source_path,
            "source_generation": "42",
            "destination_path": destination_path,
        }
    }
    db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    _override(user, db)
    source = ObjectMetadata(
        path=source_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )
    destination = ObjectMetadata(
        path=destination_path,
        generation="84",
        etag="promoted",
        size=100,
        content_type="image/png",
    )

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=source),
        patch(
            "app.routes.plan_items.storage.copy_object_generation",
            return_value=destination,
        ) as copy,
        patch("app.routes.plan_items.storage.delete_object_generation"),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "reservation_id": str(reservation.id),
                "gcs_path": source_path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 200
    assert reservation.gcs_path == destination_path
    assert reservation.gcs_generation == "84"
    assert reservation.analysis is None
    copy.assert_called_once_with(source_path, destination_path, source_generation="42")
    _no_real_broker_publish.assert_called_once()


def test_staging_promotion_ambiguous_commit_preserves_durable_generation(
    client: TestClient,
    _no_real_broker_publish,
):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    reservation.gcs_path = (
        f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation.id}/legacy.png"
    )
    reservation.upload_content_type = "image/png"
    reservation.upload_size_bytes = 100
    db = _db(
        [_scalar_result(item), _scalar_result(reservation), _scalar_result(None)],
        plan,
    )
    db.commit.side_effect = [None, ConnectionError("ack lost after commit")]
    _override(user, db)
    source = ObjectMetadata(
        path=reservation.gcs_path,
        generation="42",
        etag="etag",
        size=100,
        content_type="image/png",
    )

    def _promote(_source_path, destination_path, *, source_generation):  # noqa: ANN001
        assert source_generation == "42"
        return ObjectMetadata(
            path=destination_path,
            generation="84",
            etag="promoted",
            size=100,
            content_type="image/png",
        )

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.object_metadata", return_value=source),
        patch("app.routes.plan_items.storage.copy_object_generation", side_effect=_promote),
        patch("app.routes.plan_items._pool_promotion_is_durable", return_value=True),
        patch("app.routes.plan_items.storage.delete_object_generation_best_effort") as cleanup,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "gcs_path": source.path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 503
    cleanup.assert_not_called()
    db.rollback.assert_awaited_once()
    _no_real_broker_publish.assert_not_called()


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


def test_register_rejects_cleanup_claimed_reservation(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "cleanup_pending"
    db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json={
                "reservation_id": str(reservation.id),
                "gcs_path": reservation.gcs_path,
                "content_type": "image/png",
                "content_hash": "hash",
            },
        )

    assert resp.status_code == 409
    assert "no longer available" in resp.json()["detail"]


def test_register_dispatch_failure_is_actionable(client: TestClient, _no_real_broker_publish):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(None), _scalar_result(0)],
        plan,
    )
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
    db = _db([_scalar_result(item), _scalar_result(None), _scalar_result(existing)], plan)
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


def test_legacy_registration_retry_never_deletes_retained_generation(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    existing = _asset_row(item.id, user.id, content_hash="hash-1")
    existing.gcs_path = f"users/{user.id}/plan/{item.id}/pool/f.png"
    existing.gcs_generation = "7"
    db = _db([_scalar_result(item), _scalar_result(existing)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
        patch("app.routes.plan_items.storage.delete_object_generation") as delete_generation,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets",
            json=_register_body(user.id, item.id),
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(existing.id)
    delete_generation.assert_not_called()


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
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(None), _scalar_result(20)],
        plan,
    )
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/assets", json=_register_body(user.id, item.id))
    assert resp.status_code == 400
    assert "capped" in resp.json()["detail"]


def test_multipart_upload_uses_staging_and_shared_registration(client: TestClient):
    from app.routes import plan_items as routes  # noqa: PLC0415

    user = _user()
    item, plan = _owned_item(user.id)
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(0)],
        plan,
    )
    _override(user, db)

    async def _register(_item_id, body, _request, _user, _db):  # noqa: ANN001
        reservation = db.add.call_args.args[0]
        assert reservation.status == "preparing"
        assert body.reservation_id == str(reservation.id)
        reservation.status = "queued"
        return routes._asset_out(reservation)

    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.upload_local_file") as upload,
        patch("app.routes.plan_items.register_pool_asset", side_effect=_register) as register,
        patch("app.routes.plan_items.storage.signed_get_url", return_value="https://get"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload",
            files={"file": ("x.png", b"png-bytes", "image/png")},
        )

    assert resp.status_code == 200
    reservation = db.add.call_args.args[0]
    assert reservation.gcs_path.startswith(f"dev-user/{user.id}/plan-pool-reservations/{item.id}/")
    assert not reservation.gcs_path.startswith("users/")
    upload.assert_called_once()
    assert upload.call_args.args[1] == reservation.gcs_path
    register.assert_awaited_once()
    _assert_capacity_query_expires_abandoned_reservations(db)
    _assert_dedupe_query_reuses_only_finalized_assets(db)


def test_multipart_provider_failure_leaves_lifecycle_covered_reservation(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db(
        [_scalar_result(item), _scalar_result(None), _scalar_result(0)],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            "app.routes.plan_items.storage.upload_local_file",
            side_effect=ConnectionError("provider detail"),
        ),
        patch("app.routes.plan_items.register_pool_asset") as register,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/assets/upload",
            files={"file": ("x.png", b"png-bytes", "image/png")},
        )

    assert resp.status_code == 500
    reservation = db.add.call_args.args[0]
    assert reservation.status == "preparing"
    assert reservation.gcs_path.startswith("dev-user/")
    assert db.commit.await_count == 1
    register.assert_not_awaited()


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
    publish = _no_real_broker_publish.call_args.kwargs
    args = publish["args"]
    assert args[:2] == [str(asset.id), False]
    assert publish["headers"] == {"pool_asset_attempt_token": asset.analysis_attempt_token}


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


def test_reanalyze_rejects_unverified_reservation(client: TestClient, _no_real_broker_publish):
    user = _user()
    item, _plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.status = "preparing"
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(asset))
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items._load_owned_item", new=AsyncMock(return_value=item)),
    ):
        resp = client.post(f"/plan-items/{item.id}/assets/{asset.id}/reanalyze")

    assert resp.status_code == 409
    assert asset.status == "preparing"
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
    asset.gcs_generation = "42"
    db = _db(
        [
            _scalar_result(item),
            _scalar_result(asset),
            _scalar_result(0),
            _scalar_result(asset),
            _scalar_result(0),
        ],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            "app.routes.plan_items.storage.delete_object_generation_best_effort",
            return_value=True,
        ) as cleanup,
    ):
        resp = client.delete(f"/plan-items/{item.id}/assets/{asset.id}")
    assert resp.status_code == 200
    cleanup.assert_called_once_with(asset.gcs_path, generation="42")
    db.delete.assert_awaited_once_with(asset)
    assert db.commit.await_count == 2


def test_delete_retains_quota_claim_when_persistent_cleanup_fails(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.gcs_generation = "42"
    db = _db(
        [
            _scalar_result(item),
            _scalar_result(asset),
            _scalar_result(0),
            _scalar_result(asset),
            _scalar_result(0),
        ],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            "app.routes.plan_items.storage.delete_object_generation_best_effort",
            return_value=False,
        ),
    ):
        resp = client.delete(f"/plan-items/{item.id}/assets/{asset.id}")

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "asset_cleanup_temporarily_unavailable"
    assert asset.status == "cleanup_pending"
    db.delete.assert_not_awaited()
    assert db.commit.await_count == 1


def test_delete_rechecks_references_after_cleanup_claim_commit(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    item.clip_gcs_paths = []
    item.clip_assignments = []
    asset = _asset_row(item.id, user.id)
    asset.gcs_generation = "42"
    db = _db(
        [
            _scalar_result(item),
            _scalar_result(asset),
            _scalar_result(0),
            _scalar_result(asset),
        ],
        plan,
    )

    async def _commit():
        if db.commit.await_count == 1:
            item.clip_gcs_paths = [asset.gcs_path]
            item.clip_assignments = [{"gcs_path": asset.gcs_path, "shot_id": None}]

    db.commit.side_effect = _commit
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_generation_best_effort") as cleanup,
    ):
        resp = client.delete(f"/plan-items/{item.id}/assets/{asset.id}")

    assert resp.status_code == 409
    assert asset.status == "uploaded"
    assert "_pool_cleanup_previous_status" not in (asset.analysis or {})
    cleanup.assert_not_called()
    db.delete.assert_not_awaited()
    assert db.commit.await_count == 2


def test_delete_shared_generation_keeps_bytes(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.gcs_generation = "42"
    db = _db([_scalar_result(item), _scalar_result(asset), _scalar_result(1)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_generation_best_effort") as cleanup,
    ):
        resp = client.delete(f"/plan-items/{item.id}/assets/{asset.id}")

    assert resp.status_code == 200
    cleanup.assert_not_called()
    db.delete.assert_awaited_once_with(asset)
    assert db.commit.await_count == 1


@pytest.mark.parametrize("reference_kind", ["clip_assignment", "accepted_overlay"])
def test_delete_rejects_durable_edit_references(client: TestClient, reference_kind: str):
    user = _user()
    item, plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.gcs_generation = "42"
    if reference_kind == "clip_assignment":
        item.clip_gcs_paths = [asset.gcs_path]
        item.clip_assignments = [{"gcs_path": asset.gcs_path, "shot_id": None}]
    else:
        job = MagicMock()
        job.status = "variants_ready"
        job.raw_storage_path = "users/u/other.mp4"
        job.assembly_plan = {
            "variants": [
                {
                    "media_overlays": [
                        {"src_gcs_path": asset.gcs_path, "source": "overlay_suggestion"}
                    ]
                }
            ]
        }
        item.current_job = job
        item.current_job_id = job.id
    db = _db([_scalar_result(item), _scalar_result(asset)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch("app.routes.plan_items.storage.delete_object_generation_best_effort") as cleanup,
    ):
        resp = client.delete(f"/plan-items/{item.id}/assets/{asset.id}")

    assert resp.status_code == 409
    cleanup.assert_not_called()
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlay_writes_reject_cleanup_pending_pool_path():
    from fastapi import HTTPException  # noqa: PLC0415

    from app.routes.plan_items import _require_ready_pool_paths  # noqa: PLC0415

    user = _user()
    item, _plan = _owned_item(user.id)
    asset = _asset_row(item.id, user.id)
    asset.status = "cleanup_pending"
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalars_result([asset]))

    with pytest.raises(HTTPException) as exc_info:
        await _require_ready_pool_paths(
            item_id=str(item.id),
            user_id=user.id,
            payload={"media_overlays": [{"src_gcs_path": asset.gcs_path}]},
            db=db,
        )

    assert exc_info.value.status_code == 409


def test_asset_delete_fences_inflight_matcher_before_it_can_persist_stale_path():
    from app.routes.plan_items import _clear_suggestions_for_asset  # noqa: PLC0415

    job = MagicMock()
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "v1",
                "overlay_suggest_status": "matching",
                "overlay_suggest_attempt_token": "attempt-before-delete",
                "overlay_suggestions": None,
            }
        ]
    }

    with patch("sqlalchemy.orm.attributes.flag_modified") as dirty:
        assert _clear_suggestions_for_asset(job, str(uuid.uuid4())) == 0

    variant = job.assembly_plan["variants"][0]
    assert variant["overlay_suggest_status"] is None
    assert "overlay_suggest_attempt_token" not in variant
    dirty.assert_called_once_with(job, "assembly_plan")


def test_delete_404_when_asset_missing(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db([_scalar_result(item), _scalar_result(None)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.delete(f"/plan-items/{item.id}/assets/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_rejects_live_reservation_without_freeing_capacity(client: TestClient):
    user = _user()
    item, plan = _owned_item(user.id)
    reservation = _asset_row(item.id, user.id)
    reservation.status = "preparing"
    db = _db([_scalar_result(item), _scalar_result(reservation)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.delete(f"/plan-items/{item.id}/assets/{reservation.id}")

    assert resp.status_code == 409
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── ownership ─────────────────────────────────────────────────────────────────


def test_list_404_when_not_owner(client: TestClient):
    user = _user()
    item, plan = _owned_item(uuid.uuid4())  # plan owned by someone else
    db = _db([_scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.get(f"/plan-items/{item.id}/assets")
    assert resp.status_code == 404
