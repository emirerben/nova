"""Route tests for account deletion + data export (privacy policy §9).

Mock-DB style, mirroring test_me_jobs.py. Covers: the confirmation-code round
trip (request → confirm), the IDOR guard (a code minted for user A must not
delete user B even if B is somehow signed in with it), the FK-safe deletion
order dispatching the async GCS purge, and the export bundle's shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

_KEY = Fernet.generate_key().decode()


def _user(*, user_id: uuid.UUID | None = None, email: str = "creator@example.com") -> MagicMock:
    u = MagicMock()
    u.id = user_id or uuid.uuid4()
    u.email = email
    u.name = "Creator"
    u.auth_provider = "google"
    u.onboarding_status = "complete"
    u.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return u


def _job(*, user_id: uuid.UUID, raw_storage_path: str = "users/x/job/raw.mp4") -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user_id
    job.raw_storage_path = raw_storage_path
    job.mode = "generative"
    job.job_type = "default"
    job.status = "variants_ready"
    job.created_at = datetime(2026, 5, 1, tzinfo=UTC)
    job.transcript = None
    job.selected_platforms = None
    job.assembly_plan = {}
    return job


def _scalars(rows: list) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return r


def _db(execute_results: list) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── POST /me/account/delete-request ────────────────────────────────────────────


def test_delete_request_dispatches_email_with_valid_token() -> None:
    user = _user()
    _override(user, _db([]))
    with (
        patch("app.routes.me.settings.token_encryption_key", _KEY),
        patch("app.tasks.account_lifecycle.send_account_deletion_email.delay") as mock_delay,
    ):
        resp = client.post("/me/account/delete-request")
    assert resp.status_code == 202
    assert resp.json() == {"requested": True}
    mock_delay.assert_called_once()
    email_arg, token_arg = mock_delay.call_args.args
    assert email_arg == user.email
    # The token decrypts back to the caller's own id.
    assert Fernet(_KEY.encode()).decrypt(token_arg.encode()).decode() == str(user.id)


def test_delete_request_503s_when_encryption_key_unset() -> None:
    user = _user()
    _override(user, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", ""):
        resp = client.post("/me/account/delete-request")
    assert resp.status_code == 503


# ── POST /me/account/delete-confirm ─────────────────────────────────────────────


def test_delete_confirm_rejects_garbage_token() -> None:
    user = _user()
    _override(user, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", _KEY):
        resp = client.post("/me/account/delete-confirm", json={"token": "not-a-real-token"})
    assert resp.status_code == 400


def test_delete_confirm_rejects_token_minted_for_a_different_user() -> None:
    caller = _user()
    other_user_id = uuid.uuid4()
    other_token = Fernet(_KEY.encode()).encrypt(str(other_user_id).encode()).decode()
    _override(caller, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", _KEY):
        resp = client.post("/me/account/delete-confirm", json={"token": other_token})
    # 404, not 403 — matches this file's IDOR convention (never confirm the
    # token was well-formed for an account that isn't the caller's).
    assert resp.status_code == 404


def test_delete_confirm_deletes_in_fk_safe_order_and_dispatches_purge() -> None:
    user = _user()
    job = _job(user_id=user.id)
    token = Fernet(_KEY.encode()).encrypt(str(user.id).encode()).decode()

    # execute() call order in confirm_account_deletion:
    #   1. select jobs (for job_ids/raw_paths)
    #   2. update Job.content_plan_item_id -> NULL
    #   3. select tiktok OAuthToken rows
    #   4. delete TikTokPublication
    #   5. delete OAuthToken
    #   6. delete Job
    #   7. delete User
    db = _db(
        [
            _scalars([job]),  # 1
            MagicMock(),  # 2
            _scalars([]),  # 3 — no tiktok token, nothing to revoke
            MagicMock(),  # 4
            MagicMock(),  # 5
            MagicMock(),  # 6
            MagicMock(),  # 7
        ]
    )
    _override(user, db)
    with (
        patch("app.routes.me.settings.token_encryption_key", _KEY),
        patch("app.tasks.account_lifecycle.purge_user_storage.delay") as mock_purge,
    ):
        resp = client.post("/me/account/delete-confirm", json={"token": token})

    assert resp.status_code == 204
    assert db.execute.await_count == 7
    db.commit.assert_awaited_once()
    mock_purge.assert_called_once_with(str(user.id), [str(job.id)], [job.raw_storage_path])


def test_delete_confirm_revokes_tiktok_token_before_deleting(monkeypatch) -> None:
    user = _user()
    token = Fernet(_KEY.encode()).encrypt(str(user.id).encode()).decode()

    tiktok_row = MagicMock()
    tiktok_row.access_token = b"encrypted-blob"

    db = _db(
        [
            _scalars([]),  # jobs
            MagicMock(),  # null content_plan_item_id
            _scalars([tiktok_row]),  # tiktok OAuthToken rows
            MagicMock(),  # delete TikTokPublication
            MagicMock(),  # delete OAuthToken
            MagicMock(),  # delete Job
            MagicMock(),  # delete User
        ]
    )
    _override(user, db)
    with (
        patch("app.routes.me.settings.token_encryption_key", _KEY),
        patch("app.routes.me.decrypt_token", return_value="raw-access-token") as mock_decrypt,
        patch("app.routes.me.tiktok_client.revoke_access") as mock_revoke,
        patch("app.tasks.account_lifecycle.purge_user_storage.delay"),
    ):
        resp = client.post("/me/account/delete-confirm", json={"token": token})

    assert resp.status_code == 204
    mock_decrypt.assert_called_once_with(b"encrypted-blob")
    mock_revoke.assert_called_once_with("raw-access-token")


# ── GET /me/export ───────────────────────────────────────────────────────────


def test_export_returns_full_bundle() -> None:
    user = _user()
    job = _job(user_id=user.id)

    persona = MagicMock()
    persona.questionnaire = {"work": "barista"}
    persona.persona = {"summary": "..."}
    persona.tiktok_profile = None
    persona.style = None
    persona.idea_seeds = []
    persona.persona_status = "ready"
    persona.created_at = datetime(2026, 1, 2, tzinfo=UTC)

    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=persona)),  # persona
            _scalars([]),  # content_plans
            _scalars([job]),  # jobs
            _scalars([]),  # feedback
            _scalars([]),  # tiktok publications
        ]
    )
    _override(user, db)
    with patch("app.routes.me.signed_get_url", return_value="https://signed.example/raw.mp4"):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == user.email
    assert body["persona"]["questionnaire"] == {"work": "barista"}
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["source_media_url"] == "https://signed.example/raw.mp4"
    assert "All jobs include a re-signed source-media link" in body["note"]


def test_export_survives_a_signing_failure_without_500ing() -> None:
    user = _user()
    job = _job(user_id=user.id)

    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # no persona
            _scalars([]),  # content_plans
            _scalars([job]),  # jobs
            _scalars([]),  # feedback
            _scalars([]),  # tiktok publications
        ]
    )
    _override(user, db)
    with patch("app.routes.me.signed_get_url", side_effect=RuntimeError("gcs down")):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"][0]["source_media_url"] is None
    assert body["persona"] is None


def test_export_rejects_quarantined_plan_before_loading_children() -> None:
    user = _user()
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    plan.persona_id = uuid.uuid4()
    plan.ownership_quarantined_at = datetime(2026, 8, 11, tzinfo=UTC)
    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            _scalars([plan]),
        ]
    )
    _override(user, db)

    resp = client.get("/me/export")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Content plan is unavailable"
    assert db.execute.await_count == 2
