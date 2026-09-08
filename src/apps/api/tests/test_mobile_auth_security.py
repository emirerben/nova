"""Adversarial claim-shape checks for native provider authentication."""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from app.config import settings
from app.services import mobile_auth


def _part(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def _bytes_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _integer_part(value: int) -> str:
    return _bytes_part(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _token(claims: dict) -> str:
    return ".".join(
        [
            _part({"alg": "RS256", "kid": "test"}),
            _part(claims),
            "signature",
        ]
    )


def _claims(**overrides: object) -> dict:
    value: dict = {
        "iss": "https://accounts.google.com",
        "aud": "ios-client",
        "sub": "google-sub",
        "email": "creator@example.com",
        "email_verified": True,
        "nonce": "nonce-1",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_provider_rejects_multi_audience_without_authorized_party(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_google_client_ids", ["ios-client"])

    async def no_signature(*_args: object) -> None:
        return None

    monkeypatch.setattr(mobile_auth, "_verify_signature", no_signature)

    with pytest.raises(mobile_auth.MobileAuthError, match="audience"):
        await mobile_auth.verify_provider_id_token(
            _token(_claims(aud="another-client")), "google", "nonce-1"
        )

    with pytest.raises(mobile_auth.MobileAuthError, match="audience"):
        await mobile_auth.verify_provider_id_token(
            _token(_claims(aud=["ios-client", "other-client"])),
            "google",
            "nonce-1",
        )

    accepted = await mobile_auth.verify_provider_id_token(
        _token(_claims(aud=["ios-client", "other-client"], azp="ios-client")),
        "google",
        "nonce-1",
    )
    assert accepted.subject == "google-sub"


@pytest.mark.asyncio
async def test_provider_rejects_non_string_audience_entries_without_leaking_type_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "mobile_google_client_ids", ["ios-client"])

    async def no_signature(*_args: object) -> None:
        return None

    monkeypatch.setattr(mobile_auth, "_verify_signature", no_signature)

    with pytest.raises(mobile_auth.MobileAuthError, match="audience"):
        await mobile_auth.verify_provider_id_token(
            _token(_claims(aud=["ios-client", {"not": "a-string"}])),
            "google",
            "nonce-1",
        )

    with pytest.raises(mobile_auth.MobileAuthError, match="audience"):
        await mobile_auth.verify_provider_id_token(
            _token(_claims(aud=["ios-client", "other-client"], azp={"not": "a-string"})),
            "google",
            "nonce-1",
        )


@pytest.mark.asyncio
async def test_provider_malformed_jwks_is_classified_as_unavailable(monkeypatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str) -> _Response:
            return _Response()

    monkeypatch.setattr(mobile_auth.httpx, "AsyncClient", lambda **_kwargs: _Client())

    with pytest.raises(mobile_auth.MobileAuthError) as raised:
        await mobile_auth.verify_provider_id_token(_token(_claims()), "google", "nonce-1")
    assert raised.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_provider_verifies_real_rsa_signature_and_rejects_tampering(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_google_client_ids", ["ios-client"])
    private_key = mobile_auth.rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kid": "real-key",
                "kty": "RSA",
                "e": _integer_part(numbers.e),
                "n": _integer_part(numbers.n),
            }
        ]
    }

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return jwks

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str) -> _Response:
            return _Response()

    monkeypatch.setattr(mobile_auth.httpx, "AsyncClient", lambda **_kwargs: _Client())
    header = _part({"alg": "RS256", "kid": "real-key"})
    payload = _part(_claims())
    signature = private_key.sign(
        f"{header}.{payload}".encode(),
        mobile_auth.padding.PKCS1v15(),
        mobile_auth.hashes.SHA256(),
    )
    token = f"{header}.{payload}.{_bytes_part(signature)}"

    verified = await mobile_auth.verify_provider_id_token(token, "google", "nonce-1")
    assert verified.subject == "google-sub"

    tampered = f"{header}.{_part(_claims(sub='attacker'))}.{_bytes_part(signature)}"
    with pytest.raises(mobile_auth.MobileAuthError) as raised:
        await mobile_auth.verify_provider_id_token(tampered, "google", "nonce-1")
    assert raised.value.code == "invalid_id_token"


@pytest.mark.asyncio
async def test_provider_rejects_unsupported_header_and_unknown_key(monkeypatch) -> None:
    with pytest.raises(mobile_auth.MobileAuthError) as raised:
        await mobile_auth._verify_signature(
            ["header", "payload", "signature"],
            "google",
            {"alg": "HS256", "kid": "key"},
        )
    assert raised.value.code == "invalid_id_token"

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return {"keys": [{"kid": "different", "kty": "RSA"}]}

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str) -> _Response:
            return _Response()

    monkeypatch.setattr(mobile_auth.httpx, "AsyncClient", lambda **_kwargs: _Client())
    with pytest.raises(mobile_auth.MobileAuthError) as raised:
        await mobile_auth._verify_signature(
            ["header", "payload", "signature"],
            "google",
            {"alg": "RS256", "kid": "missing"},
        )
    assert raised.value.code == "invalid_id_token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "client_ids", "expected_code"),
    [
        ({"iss": "https://attacker.example"}, ["ios-client"], "invalid_id_token"),
        ({}, [], "invalid_id_token"),
        ({"exp": 0}, ["ios-client"], "invalid_id_token"),
        ({"iat": "future"}, ["ios-client"], "invalid_id_token"),
        ({"sub": ""}, ["ios-client"], "invalid_id_token"),
        ({"email": "not-an-email"}, ["ios-client"], "email_required"),
        ({"email_verified": False}, ["ios-client"], "email_unverified"),
    ],
)
async def test_provider_rejects_invalid_security_claim_boundaries(
    monkeypatch, overrides, client_ids, expected_code
) -> None:
    monkeypatch.setattr(settings, "mobile_google_client_ids", client_ids)

    async def no_signature(*_args: object) -> None:
        return None

    monkeypatch.setattr(mobile_auth, "_verify_signature", no_signature)
    overrides = dict(overrides)
    if overrides.get("iat") == "future":
        overrides["iat"] = int(time.time()) + 61
    with pytest.raises(mobile_auth.MobileAuthError) as raised:
        await mobile_auth.verify_provider_id_token(
            _token(_claims(**overrides)), "google", "nonce-1"
        )
    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_apple_accepts_hashed_nonce_and_string_verified_email(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mobile_apple_client_ids", ["apple-client"])

    async def no_signature(*_args: object) -> None:
        return None

    monkeypatch.setattr(mobile_auth, "_verify_signature", no_signature)
    nonce = "native-raw-nonce"
    claims = _claims(
        iss=mobile_auth.APPLE_ISSUER,
        aud="apple-client",
        sub="apple-sub",
        nonce=hashlib.sha256(nonce.encode()).hexdigest(),
        email_verified="TRUE",
        name={"not": "a string"},
    )
    verified = await mobile_auth.verify_provider_id_token(_token(claims), "apple", nonce)
    assert verified.provider == "apple"
    assert verified.name is None
