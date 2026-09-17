"""Route contract tests for POST /auth/mobile/reviewer-login (KRI-111).

TestClient + a mocked AsyncSession (mirrors test_auth_regression.py /
test_admin_kria.py) so the limiter decorator sees a real ASGI request. Each
test uses its own X-Forwarded-For IP (mirrors tests/test_waitlist.py) so the
shared in-memory rate-limiter state never bleeds across tests.
"""

from __future__ import annotations

import itertools
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.services.mobile_auth import decode_access_token
from app.services.reviewer_login import hash_password

_ip_counter = itertools.count(1)

REVIEWER_EMAIL = "reviewer@usekria.com"
REVIEWER_PASSWORD = "correct horse battery staple"


def _headers() -> dict[str, str]:
    ip = f"10.{next(_ip_counter) % 256}.1.1"
    return {"X-Forwarded-For": ip}


def _scalar(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars(values: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _add_assigns_id(obj: object) -> None:
    # Mirrors what a real flush against Postgres would do for MobileSession's
    # client-side `default=uuid.uuid4` primary key — there's no real engine
    # here to apply it, so a mocked db.add() must assign one itself, or the
    # route's real crypto/decode path downstream sees a None session id.
    if getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()


def _db(*results: object) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock(side_effect=_add_assigns_id)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    remaining = iter(results)

    async def execute(statement: object) -> object:
        if "pg_advisory_xact_lock" in str(statement):
            return MagicMock()
        return next(remaining)

    db.execute = AsyncMock(side_effect=execute)
    return db


def _configure_reviewer_login(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reviewer_login_enabled", True)
    monkeypatch.setattr(settings, "reviewer_login_email", REVIEWER_EMAIL)
    monkeypatch.setattr(settings, "reviewer_login_password_hash", hash_password(REVIEWER_PASSWORD))
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_db, None)


def _post(client: TestClient, db, email: str, password: str):
    app.dependency_overrides[get_db] = lambda: db
    return client.post(
        "/auth/mobile/reviewer-login",
        json={"email": email, "password": password},
        headers=_headers(),
    )


def test_feature_off_returns_404(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reviewer_login_enabled", False)
    response = _post(client, _db(), REVIEWER_EMAIL, REVIEWER_PASSWORD)
    assert response.status_code == 404


def test_configured_but_no_mobile_jwt_secret_returns_404(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    monkeypatch.setattr(settings, "mobile_jwt_secret", "")
    response = _post(client, _db(), REVIEWER_EMAIL, REVIEWER_PASSWORD)
    assert response.status_code == 404


def test_wrong_password_returns_401_invalid_credentials(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    response = _post(client, _db(), REVIEWER_EMAIL, "not the password")
    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "invalid_credentials",
        "message": "Invalid email or password",
    }


def test_wrong_email_returns_same_401_body(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    response = _post(client, _db(), "not-the-reviewer@example.com", REVIEWER_PASSWORD)
    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "invalid_credentials",
        "message": "Invalid email or password",
    }


def test_success_issues_reviewer_session_for_new_user(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    db = _db(_scalar(None), _scalars([]))
    response = _post(client, db, REVIEWER_EMAIL, REVIEWER_PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["email"] == REVIEWER_EMAIL

    claims = decode_access_token(body["access_token"])
    assert claims.user_id is not None

    # The created User + the MobileSession must both have been added.
    added = [call.args[0] for call in db.add.call_args_list]
    assert len(added) == 2
    user_row = next(row for row in added if type(row).__name__ == "User")
    session_row = next(row for row in added if type(row).__name__ == "MobileSession")
    assert user_row.auth_provider == "reviewer"
    assert user_row.email == REVIEWER_EMAIL
    assert session_row.provider == "reviewer"
    db.commit.assert_awaited()


def test_success_reuses_existing_reviewer_user_no_duplicate(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    existing_user = MagicMock()
    existing_user.id = uuid.uuid4()
    existing_user.email = REVIEWER_EMAIL
    existing_user.name = "Kria Reviewer"
    existing_user.onboarding_status = "pending"

    db = _db(_scalar(existing_user), _scalars([]))
    response = _post(client, db, REVIEWER_EMAIL, REVIEWER_PASSWORD)
    assert response.status_code == 200

    added = [call.args[0] for call in db.add.call_args_list]
    # Only the new MobileSession is added — no second User row.
    assert len(added) == 1
    assert type(added[0]).__name__ == "MobileSession"
    assert added[0].user_id == existing_user.id
