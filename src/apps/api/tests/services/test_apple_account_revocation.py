"""Direct tests for deletion-only Apple credential exchange."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import settings
from app.services import apple_account_revocation as service
from app.services.mobile_auth import ProviderClaims


def _claims(*, subject: str = "apple-sub", client_id: str = "com.kria.ios") -> ProviderClaims:
    return ProviderClaims(
        provider="apple",
        subject=subject,
        issuer="https://appleid.apple.com",
        email="creator@example.com",
        name=None,
        email_verified=True,
        client_id=client_id,
    )


def _configure(monkeypatch) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setattr(settings, "mobile_apple_client_ids", ["com.kria.ios"])
    monkeypatch.setattr(settings, "apple_team_id", "TEAM123")
    monkeypatch.setattr(settings, "apple_key_id", "KEY123")
    monkeypatch.setattr(settings, "apple_private_key", pem.decode())
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    from app.services import token_crypto

    token_crypto._fernet.cache_clear()
    return pem


def _b64(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def test_client_secret_is_es256_and_configuration_fails_closed(monkeypatch) -> None:
    pem = _configure(monkeypatch)
    secret = service.apple_client_secret("com.kria.ios")
    header, payload, signature = secret.split(".")
    assert _b64(header) == {"alg": "ES256", "kid": "KEY123"}
    assert _b64(payload)["sub"] == "com.kria.ios"
    public = serialization.load_pem_private_key(pem, password=None).public_key()
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    public.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{header}.{payload}".encode(),
        ec.ECDSA(service.hashes.SHA256()),
    )
    monkeypatch.setattr(settings, "apple_private_key", "")
    with pytest.raises(service.AppleRevocationError, match="not_configured"):
        service.apple_client_secret("com.kria.ios")


class _Response:
    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self._payload, self._error = payload or {}, error

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_exchange_binds_returned_subject_client_nonce_and_preserves_timeout(
    monkeypatch,
) -> None:
    _configure(monkeypatch)
    returned = _claims()
    verify = AsyncMock(return_value=returned)
    monkeypatch.setattr(service, "verify_provider_id_token", verify)
    client = _Client(_Response({"id_token": "returned", "refresh_token": "refresh"}))
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **_: client)
    credential = await service.exchange_deletion_authorization(
        authorization_code="one-use", nonce="nonce", supplied_claims=_claims()
    )
    assert (
        credential.client_id == "com.kria.ios" and credential.encrypted_refresh_token != b"refresh"
    )
    verify.assert_awaited_once_with("returned", "apple", "nonce", require_email=False)
    assert client.calls[0][1]["data"]["code"] == "one-use"

    verify.return_value = _claims(subject="other")
    with pytest.raises(service.AppleRevocationError, match="binding"):
        await service.exchange_deletion_authorization(
            authorization_code="two", nonce="nonce", supplied_claims=_claims()
        )
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **_: _Client(_Response(error=httpx.ReadTimeout("timeout"))),
    )
    with pytest.raises(service.AppleRevocationError, match="exchange_failed"):
        await service.exchange_deletion_authorization(
            authorization_code="three", nonce="nonce", supplied_claims=_claims()
        )


@pytest.mark.asyncio
async def test_invalid_config_is_checked_before_code_exchange(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "apple_private_key", "bad")
    called = False

    def client(**_):
        nonlocal called
        called = True
        return _Client(_Response())

    monkeypatch.setattr(service.httpx, "AsyncClient", client)
    with pytest.raises(service.AppleRevocationError, match="client_secret_invalid"):
        await service.exchange_deletion_authorization(
            authorization_code="one-use", nonce="nonce", supplied_claims=_claims()
        )
    assert not called
