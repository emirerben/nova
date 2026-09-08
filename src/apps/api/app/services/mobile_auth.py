"""Provider verification and token/session primitives for native clients.

The API never trusts an email or provider subject supplied by the app.  The app
only supplies an ID token; this module validates its issuer, audience, nonce,
signature, and verified-email claim before it can be linked to a user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import settings

Provider = Literal["google", "apple"]
JWT_ISSUER = "kria-api"
JWT_AUDIENCE = "kria-mobile"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
APPLE_ISSUER = "https://appleid.apple.com"
JWKS_URLS = {
    "google": "https://www.googleapis.com/oauth2/v3/certs",
    "apple": "https://appleid.apple.com/auth/keys",
}


class MobileAuthError(ValueError):
    def __init__(self, code: str, message: str = "Authentication failed") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderClaims:
    provider: Provider
    subject: str
    issuer: str
    email: str
    name: str | None
    email_verified: bool


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    token_id: uuid.UUID
    token_version: int
    issued_at: int
    expires_at: int


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_b64decode(value))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise MobileAuthError("invalid_id_token", "Malformed identity token") from exc
    if not isinstance(parsed, dict):
        raise MobileAuthError("invalid_id_token", "Malformed identity token")
    return parsed


def _audiences(provider: Provider) -> set[str]:
    values = (
        settings.mobile_google_client_ids
        if provider == "google"
        else settings.mobile_apple_client_ids
    )
    return {str(v).strip() for v in values if str(v).strip()}


def _issuer_ok(provider: Provider, issuer: object) -> bool:
    if not isinstance(issuer, str):
        return False
    return issuer in (GOOGLE_ISSUERS if provider == "google" else {APPLE_ISSUER})


def _verified(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


async def _verify_signature(
    token_parts: list[str], provider: Provider, header: dict[str, Any]
) -> None:
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise MobileAuthError("invalid_id_token", "Unsupported identity token signature")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(JWKS_URLS[provider])
            response.raise_for_status()
            payload = response.json()
            keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(keys, list):
                raise ValueError("JWKS response has no key list")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise MobileAuthError("provider_unavailable", "Identity provider unavailable") from exc
    jwk = next(
        (key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]),
        None,
    )
    if not jwk or jwk.get("kty") != "RSA":
        raise MobileAuthError("invalid_id_token", "Unknown identity token key")
    try:
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(_b64decode(jwk["e"]), "big"),
            int.from_bytes(_b64decode(jwk["n"]), "big"),
        ).public_key()
        public_key.verify(
            _b64decode(token_parts[2]),
            f"{token_parts[0]}.{token_parts[1]}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (KeyError, ValueError, TypeError, OverflowError, InvalidSignature) as exc:
        raise MobileAuthError("invalid_id_token", "Invalid identity token signature") from exc


async def verify_provider_id_token(token: str, provider: Provider, nonce: str) -> ProviderClaims:
    """Verify a Google/Apple ID token against the server's configured clients."""
    if provider not in ("google", "apple") or not token or not nonce:
        raise MobileAuthError("invalid_id_token")
    parts = token.split(".")
    if len(parts) != 3:
        raise MobileAuthError("invalid_id_token", "Malformed identity token")
    header, claims = _b64json(parts[0]), _b64json(parts[1])
    await _verify_signature(parts, provider, header)
    issuer = claims.get("iss")
    audience = claims.get("aud")
    allowed = _audiences(provider)
    if isinstance(audience, str):
        aud_ok = audience in allowed
    elif isinstance(audience, list) and all(isinstance(value, str) for value in audience):
        # OIDC permits an audience array only when ``azp`` identifies the
        # authorized party.  Requiring it for multi-audience tokens prevents a
        # token minted for another client from being accepted merely because
        # one unrelated audience happens to be configured here.
        aud_ok = bool(set(audience) & allowed) and (
            len(audience) == 1 or (isinstance(claims.get("azp"), str) and claims["azp"] in allowed)
        )
    else:
        aud_ok = False
    if not _issuer_ok(provider, issuer) or not allowed or not aud_ok:
        raise MobileAuthError("invalid_id_token", "Identity token issuer or audience is invalid")
    token_nonce = str(claims.get("nonce", ""))
    valid_nonce = hmac.compare_digest(token_nonce, nonce) or (
        provider == "apple"
        and hmac.compare_digest(token_nonce, hashlib.sha256(nonce.encode()).hexdigest())
    )
    if not valid_nonce:
        raise MobileAuthError("invalid_nonce", "Identity token nonce is invalid")
    now = int(time.time())
    try:
        if int(claims["exp"]) <= now or int(claims.get("iat", now)) > now + 60:
            raise MobileAuthError("invalid_id_token", "Identity token has expired")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise MobileAuthError("invalid_id_token", "Identity token lifetime is invalid") from exc
    if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
        raise MobileAuthError("invalid_id_token", "Identity token subject is missing")
    if not isinstance(claims.get("email"), str) or "@" not in claims["email"]:
        raise MobileAuthError("email_required", "A verified email is required")
    if not _verified(claims.get("email_verified")):
        raise MobileAuthError("email_unverified", "A verified email is required")
    return ProviderClaims(
        provider=provider,
        subject=claims["sub"],
        issuer=str(issuer),
        email=claims["email"].strip().casefold(),
        name=claims.get("name") if isinstance(claims.get("name"), str) else None,
        email_verified=True,
    )


def _sign(payload: dict[str, Any]) -> str:
    secret = settings.mobile_jwt_secret.encode()
    if not secret:
        raise MobileAuthError("auth_unavailable", "Mobile authentication is not configured")
    header = {"alg": "HS256", "typ": "JWT"}
    parts = [
        base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
        for part in (header, payload)
    ]
    signing_input = ".".join(parts).encode()
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    parts.append(base64.urlsafe_b64encode(signature).rstrip(b"=").decode())
    return ".".join(parts)


def issue_access_token(
    user_id: uuid.UUID, session_id: uuid.UUID, token_version: int = 1
) -> tuple[str, int]:
    now = int(time.time())
    ttl = settings.mobile_access_token_ttl_seconds
    token_id = uuid.uuid4()
    return _sign(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "jti": str(token_id),
            "token_version": token_version,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "typ": "access",
            "iat": now,
            "exp": now + ttl,
        }
    ), ttl


def decode_access_token(token: str) -> AccessClaims:
    if not settings.mobile_jwt_secret or not token:
        raise MobileAuthError("invalid_access_token")
    parts = token.split(".")
    if len(parts) != 3:
        raise MobileAuthError("invalid_access_token")
    try:
        signing_input = ".".join(parts[:2]).encode()
        expected = hmac.new(
            settings.mobile_jwt_secret.encode(), signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(parts[2])):
            raise MobileAuthError("invalid_access_token")
        header, payload = _b64json(parts[0]), _b64json(parts[1])
        now = int(time.time())
        if header.get("alg") != "HS256" or payload.get("typ") != "access":
            raise MobileAuthError("invalid_access_token")
        if payload.get("iss") != JWT_ISSUER or payload.get("aud") != JWT_AUDIENCE:
            raise MobileAuthError("invalid_access_token")
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        if (
            expires_at <= now
            or issued_at > now + 60
            or expires_at <= issued_at
            or not payload.get("sub")
        ):
            raise MobileAuthError("invalid_access_token")
        user_id = uuid.UUID(str(payload["sub"]))
        session_id = uuid.UUID(str(payload["sid"]))
        token_id = uuid.UUID(str(payload["jti"]))
        if int(payload["token_version"]) < 1:
            raise MobileAuthError("invalid_access_token")
        return AccessClaims(
            user_id=user_id,
            session_id=session_id,
            token_id=token_id,
            token_version=int(payload["token_version"]),
            issued_at=issued_at,
            expires_at=expires_at,
        )
    except (ValueError, TypeError, KeyError, OverflowError, MobileAuthError) as exc:
        if isinstance(exc, MobileAuthError):
            raise
        raise MobileAuthError("invalid_access_token") from exc


def verify_access_token(token: str) -> uuid.UUID:
    """Compatibility helper returning only the subject for pure callers."""
    return decode_access_token(token).user_id


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def refresh_token_hash(token: str) -> str:
    return hmac.new(settings.mobile_jwt_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def refresh_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(days=settings.mobile_refresh_token_ttl_days)
