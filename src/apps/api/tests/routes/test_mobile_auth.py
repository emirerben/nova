"""Focused route contracts for native identity and session lifecycle."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from app.auth import get_current_user
from app.config import settings
from app.routes import auth
from app.services.mobile_auth import AccessClaims, ProviderClaims


def _scalar(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _scalars(values: list[str]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _db(*results: object) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    remaining = iter(results)

    async def execute(statement: object) -> object:
        # Route unit tests do not need PostgreSQL to exercise the lock itself;
        # lock statements must not consume the ordered query fixtures below.
        if "pg_advisory_xact_lock" in str(statement):
            return MagicMock()
        return next(remaining)

    db.execute = AsyncMock(side_effect=execute)
    return db


def _user(*, provider: str = "google") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="creator@example.com",
        name=None,
        auth_provider=provider,
        onboarding_status="complete",
    )


def _claims(provider: str = "google", subject: str = "provider-subject") -> ProviderClaims:
    return ProviderClaims(
        provider=provider,
        subject=subject,
        issuer="https://accounts.google.com",
        email="creator@example.com",
        name="Creator",
        email_verified=True,
    )


@pytest.mark.asyncio
async def test_exchange_migrates_only_exact_legacy_google_identity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    db = _db(_scalar(None), _scalar(user), _scalars(["google"]))
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=_claims()))
    issued = auth.MobileSessionOut(
        access_token="access",
        refresh_token="refresh",
        expires_in=900,
        user=auth._mobile_user(user),
    )
    issue = AsyncMock(return_value=issued)
    monkeypatch.setattr(auth, "_issue_mobile_session", issue)

    result = await auth.mobile_exchange(
        auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"),
        db,
    )

    assert result.user.id == str(user.id)
    identity = db.add.call_args.args[0]
    assert identity.user_id == user.id
    assert identity.provider == "google"
    assert identity.subject == "provider-subject"
    issue.assert_awaited_once_with(user, "google", db)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_exchange_locks_identity_and_normalized_email_before_lookup(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    claims = _claims(subject="same-subject")
    claims = ProviderClaims(
        provider=claims.provider,
        subject=claims.subject,
        issuer=claims.issuer,
        email="Creator@Example.com",
        name=claims.name,
        email_verified=claims.email_verified,
    )
    identity = SimpleNamespace(user_id=user.id, last_seen_at=None)
    db = _db(_scalar(identity), _scalar(user), _scalars(["google"]))
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=claims))
    monkeypatch.setattr(
        auth,
        "_issue_mobile_session",
        AsyncMock(
            return_value=auth.MobileSessionOut(
                access_token="access",
                refresh_token="refresh",
                expires_in=900,
                user=auth._mobile_user(user),
            )
        ),
    )

    await auth.mobile_exchange(
        auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"), db
    )

    first_two = [call.args[0] for call in db.execute.await_args_list[:2]]
    assert all("pg_advisory_xact_lock" in str(statement) for statement in first_two)
    lock_params = [
        next(
            value
            for value in statement.compile().params.values()
            if isinstance(value, str) and value.startswith("native-auth:")
        )
        for statement in first_two
    ]
    assert lock_params == sorted(
        [
            "native-auth:email:creator@example.com",
            "native-auth:identity:google:same-subject",
        ]
    )
    assert "mobile_identities" in str(db.execute.await_args_list[2].args[0])


@pytest.mark.asyncio
async def test_exchange_requires_explicit_link_for_apple_email_collision(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    db = _db(_scalar(None), _scalar(user))
    monkeypatch.setattr(
        auth,
        "verify_provider_id_token",
        AsyncMock(return_value=_claims(provider="apple")),
    )

    with pytest.raises(HTTPException) as raised:
        await auth.mobile_exchange(
            auth.MobileExchangeRequest(provider="apple", id_token="token", nonce="nonce"),
            db,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "provider_link_required"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_rotates_once_and_links_replacement(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    old = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        family_id=uuid.uuid4(),
        provider="google",
        token_version=1,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        used_at=None,
        revoked_at=None,
        last_used_at=None,
        replaced_by=None,
    )
    replacement = SimpleNamespace(id=uuid.uuid4())
    db = _db(_scalar(old), _scalar(user), _scalar(replacement), _scalars(["google"]))
    issued = auth.MobileSessionOut(
        access_token="next-access",
        refresh_token="next-refresh",
        expires_in=900,
        user=auth._mobile_user(user),
    )
    issue = AsyncMock(return_value=issued)
    monkeypatch.setattr(auth, "_issue_mobile_session", issue)

    result = await auth.mobile_refresh(auth.MobileRefreshRequest(refresh_token="old"), db)

    assert result.refresh_token == "next-refresh"
    assert old.used_at is not None
    assert old.replaced_by == replacement.id
    issue.assert_awaited_once_with(user, "google", db, family_id=old.family_id)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_token_reuse_revokes_entire_family(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    reused = SimpleNamespace(
        family_id=uuid.uuid4(),
        used_at=datetime.now(UTC),
    )
    db = _db(_scalar(reused), MagicMock())

    with pytest.raises(HTTPException) as raised:
        await auth.mobile_refresh(auth.MobileRefreshRequest(refresh_token="reused"), db)

    assert raised.value.status_code == 401
    assert raised.value.detail == {"code": "refresh_reuse_detected"}
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_invalidates_the_complete_refresh_family(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    session = SimpleNamespace(family_id=uuid.uuid4())
    db = _db(_scalar(session), MagicMock())

    response = await auth.mobile_revoke(
        auth.MobileRefreshRequest(refresh_token="current-refresh"),
        db,
    )

    assert response.revoked is True
    assert db.execute.await_count == 2
    update_statement = db.execute.await_args_list[1].args[0]
    assert session.family_id in update_statement.compile().params.values()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_is_idempotent_for_an_unknown_refresh_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    db = _db(_scalar(None))

    response = await auth.mobile_revoke(
        auth.MobileRefreshRequest(refresh_token="already-gone"),
        db,
    )

    assert response.revoked is True
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_rejects_access_older_than_recent_auth_window(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "mobile_link_max_auth_age_seconds", 300)
    user = _user()
    access = AccessClaims(
        user_id=user.id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()) - 301,
        expires_at=int(time.time()) + 500,
    )
    monkeypatch.setattr(auth, "decode_access_token", MagicMock(return_value=access))
    verify = AsyncMock()
    monkeypatch.setattr(auth, "verify_provider_id_token", verify)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/mobile/link",
            "headers": [(b"authorization", b"Bearer access")],
        }
    )

    with pytest.raises(HTTPException) as raised:
        await auth.mobile_link(
            auth.MobileExchangeRequest(provider="apple", id_token="token", nonce="nonce"),
            request,
            user,
            _db(),
        )

    assert raised.value.status_code == 401
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_canonical_identity_replay_skips_email_conflict(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    access = AccessClaims(
        user_id=user.id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    identity = SimpleNamespace(user_id=user.id, last_seen_at=None)
    db = _db(_scalar(identity), _scalars(["google"]))
    monkeypatch.setattr(auth, "decode_access_token", MagicMock(return_value=access))
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=_claims()))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/mobile/link",
            "headers": [(b"authorization", b"Bearer access")],
        }
    )

    response = await auth.mobile_link(
        auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"),
        request,
        user,
        db,
    )

    non_lock_statements = [
        call.args[0]
        for call in db.execute.await_args_list
        if "pg_advisory_xact_lock" not in str(call.args[0])
    ]
    assert len(non_lock_statements) == 2
    assert all("users.email" not in str(statement) for statement in non_lock_statements)
    assert identity.last_seen_at is not None
    assert response.linked_providers == ["google"]
    db.add.assert_not_called()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_link_missing_identity_preserves_email_conflict(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    other_user = _user()
    access = AccessClaims(
        user_id=user.id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    db = _db(_scalar(None), _scalar(other_user))
    monkeypatch.setattr(auth, "decode_access_token", MagicMock(return_value=access))
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=_claims()))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/mobile/link",
            "headers": [(b"authorization", b"Bearer access")],
        }
    )

    with pytest.raises(HTTPException) as raised:
        await auth.mobile_link(
            auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"),
            request,
            user,
            db,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == {"code": "provider_link_email_conflict"}
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_bearer_cannot_cross_tenant_session(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    owner_id = uuid.uuid4()
    claims = AccessClaims(
        user_id=owner_id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    monkeypatch.setattr("app.auth.decode_access_token", MagicMock(return_value=claims))
    db = _db(_scalar(None))

    with pytest.raises(HTTPException) as raised:
        await get_current_user(
            x_user_id=None,
            authorization="Bearer mobile-access",
            db=db,
        )

    assert raised.value.status_code == 401
    assert raised.value.detail == "invalid_access_token"
    statement = db.execute.await_args.args[0]
    compiled = statement.compile()
    assert owner_id in compiled.params.values()
    assert claims.session_id in compiled.params.values()


@pytest.mark.asyncio
async def test_mobile_bearer_loads_user_for_matching_live_session(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    claims = AccessClaims(
        user_id=user.id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=2,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    session = SimpleNamespace(token_version=2)
    monkeypatch.setattr("app.auth.decode_access_token", MagicMock(return_value=claims))
    db = _db(_scalar(session), _scalar(user))

    result = await get_current_user(
        x_user_id=None,
        authorization="Bearer mobile-access",
        db=db,
    )

    assert result is user
    assert db.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["missing_session", "version_mismatch", "missing_user"])
async def test_mobile_bearer_rejects_invalid_session_or_missing_user(monkeypatch, scenario) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user_id = uuid.uuid4()
    claims = AccessClaims(
        user_id=user_id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    monkeypatch.setattr("app.auth.decode_access_token", MagicMock(return_value=claims))
    if scenario == "missing_session":
        db = _db(_scalar(None))
    elif scenario == "version_mismatch":
        db = _db(_scalar(SimpleNamespace(token_version=2)))
    else:
        db = _db(_scalar(SimpleNamespace(token_version=1)), _scalar(None))

    with pytest.raises(HTTPException) as raised:
        await get_current_user(
            x_user_id=None,
            authorization="Bearer mobile-access",
            db=db,
        )
    assert raised.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("user_exists", [True, False])
async def test_exchange_existing_identity_updates_last_seen_or_rejects_dangling_user(
    monkeypatch, user_exists
) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    identity = SimpleNamespace(user_id=user.id, last_seen_at=datetime(2020, 1, 1, tzinfo=UTC))
    db = _db(_scalar(identity), _scalar(user if user_exists else None), _scalars(["google"]))
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=_claims()))
    issue = AsyncMock(
        return_value=auth.MobileSessionOut(
            access_token="access",
            refresh_token="refresh",
            expires_in=900,
            user=auth._mobile_user(user),
        )
    )
    monkeypatch.setattr(auth, "_issue_mobile_session", issue)

    if not user_exists:
        with pytest.raises(HTTPException) as raised:
            await auth.mobile_exchange(
                auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"),
                db,
            )
        assert raised.value.status_code == 401
        issue.assert_not_awaited()
        return

    result = await auth.mobile_exchange(
        auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"),
        db,
    )
    assert result.user.linked_providers == ["google"]
    assert identity.last_seen_at > datetime(2020, 1, 1, tzinfo=UTC)
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_exchange_creates_new_user_and_identity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    db = _db(_scalar(None), _scalar(None), _scalars(["apple"]))
    claims = _claims(provider="apple")
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=claims))

    async def issue(user, provider, _db):
        return auth.MobileSessionOut(
            access_token="access",
            refresh_token="refresh",
            expires_in=900,
            user=auth._mobile_user(user),
        )

    monkeypatch.setattr(auth, "_issue_mobile_session", issue)

    result = await auth.mobile_exchange(
        auth.MobileExchangeRequest(provider="apple", id_token="token", nonce="nonce"),
        db,
    )

    added = [call.args[0] for call in db.add.call_args_list]
    created_user = next(item for item in added if item.__class__.__name__ == "User")
    identity = next(item for item in added if item.__class__.__name__ == "MobileIdentity")
    assert created_user.auth_provider == "apple"
    assert created_user.name == "Creator"
    assert identity.user_id == created_user.id
    assert result.user.linked_providers == ["apple"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["expired", "revoked", "missing_user", "naive_expiry"])
async def test_refresh_rejects_invalid_session_states(monkeypatch, scenario) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    expiry = datetime.now(UTC) + timedelta(days=1)
    if scenario == "expired":
        expiry = datetime.now(UTC) - timedelta(seconds=1)
    elif scenario == "naive_expiry":
        expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    session = SimpleNamespace(
        user_id=user.id,
        family_id=uuid.uuid4(),
        provider="google",
        expires_at=expiry,
        used_at=None,
        revoked_at=datetime.now(UTC) if scenario == "revoked" else None,
    )
    db = _db(_scalar(session), _scalar(None))

    with pytest.raises(HTTPException) as raised:
        await auth.mobile_refresh(auth.MobileRefreshRequest(refresh_token="refresh"), db)
    assert raised.value.status_code == 401
    if scenario == "missing_user":
        assert db.execute.await_count == 2
    else:
        assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_link_creates_identity_for_recent_matching_mobile_user(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    access = AccessClaims(
        user_id=user.id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    monkeypatch.setattr(auth, "decode_access_token", MagicMock(return_value=access))
    monkeypatch.setattr(
        auth, "verify_provider_id_token", AsyncMock(return_value=_claims(provider="apple"))
    )
    db = _db(_scalar(None), _scalar(None), _scalars(["google", "apple"]))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/mobile/link",
            "headers": [(b"authorization", b"Bearer access")],
        }
    )

    result = await auth.mobile_link(
        auth.MobileExchangeRequest(provider="apple", id_token="token", nonce="nonce"),
        request,
        user,
        db,
    )

    identity = db.add.call_args.args[0]
    assert identity.user_id == user.id
    assert identity.provider == "apple"
    assert result.linked_providers == ["google", "apple"]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["provider_owner", "email_owner"])
async def test_link_rejects_provider_or_email_owned_by_another_user(monkeypatch, scenario) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "test-secret")
    user = _user()
    other = _user()
    access = AccessClaims(
        user_id=user.id,
        session_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_version=1,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 900,
    )
    monkeypatch.setattr(auth, "decode_access_token", MagicMock(return_value=access))
    monkeypatch.setattr(auth, "verify_provider_id_token", AsyncMock(return_value=_claims()))
    if scenario == "provider_owner":
        db = _db(_scalar(SimpleNamespace(user_id=other.id)))
        expected = "provider_already_linked"
    else:
        db = _db(_scalar(None), _scalar(other))
        expected = "provider_link_email_conflict"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/mobile/link",
            "headers": [(b"authorization", b"Bearer access")],
        }
    )

    with pytest.raises(HTTPException) as raised:
        await auth.mobile_link(
            auth.MobileExchangeRequest(provider="google", id_token="token", nonce="nonce"),
            request,
            user,
            db,
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == expected


@pytest.mark.asyncio
async def test_mobile_me_filters_unknown_identity_providers() -> None:
    user = _user()
    db = _db(_scalars(["google", "legacy", "apple"]))

    result = await auth.mobile_me(user, db)

    assert result.linked_providers == ["google", "apple"]
