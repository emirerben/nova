"""Apple refresh-token exchange/revocation used exclusively during erasure."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import settings
from app.services.mobile_auth import MobileAuthError, ProviderClaims, verify_provider_id_token
from app.services.token_crypto import TokenCryptoError, encrypt_token

APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"


class AppleRevocationError(RuntimeError):
    """Safe error code only; tokens and provider payloads are never retained."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_apple_revocation_configuration() -> None:
    """Fail before sending a confirmation email that cannot be completed."""
    if not settings.mobile_apple_client_ids:
        raise AppleRevocationError("apple_revocation_not_configured")
    apple_client_secret(settings.mobile_apple_client_ids[0])
    try:
        encrypt_token("preflight")
    except TokenCryptoError as exc:
        raise AppleRevocationError("token_encryption_unavailable") from exc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def apple_client_secret(client_id: str) -> str:
    """Build Apple's short lived ES256 client assertion without a JWT package."""
    if client_id not in set(settings.mobile_apple_client_ids):
        raise AppleRevocationError("apple_client_not_allowed")
    if not settings.apple_team_id or not settings.apple_key_id or not settings.apple_private_key:
        raise AppleRevocationError("apple_revocation_not_configured")
    try:
        key = serialization.load_pem_private_key(settings.apple_private_key.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("not EC")
        now = int(time.time())
        header = _b64(
            json.dumps(
                {"alg": "ES256", "kid": settings.apple_key_id}, separators=(",", ":")
            ).encode()
        )
        payload = _b64(
            json.dumps(
                {
                    "iss": settings.apple_team_id,
                    "iat": now,
                    "exp": now + 300,
                    "aud": "https://appleid.apple.com",
                    "sub": client_id,
                },
                separators=(",", ":"),
            ).encode()
        )
        der = key.sign(f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
        # Apple/JWS ES256 uses fixed-width raw r || s, not ASN.1 DER.
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{header}.{payload}.{_b64(signature)}"
    except AppleRevocationError:
        raise
    except Exception as exc:  # configuration is deliberately opaque
        raise AppleRevocationError("apple_client_secret_invalid") from exc


@dataclass(frozen=True)
class AppleRefreshCredential:
    encrypted_refresh_token: bytes
    client_id: str


async def exchange_deletion_authorization(
    *, authorization_code: str, nonce: str, supplied_claims: ProviderClaims
) -> AppleRefreshCredential:
    """Consume a proof only after all non-consuming validation has completed."""
    client_id = supplied_claims.client_id
    if not client_id:
        raise AppleRevocationError("apple_client_missing")
    secret = apple_client_secret(client_id)  # validates config before code use
    try:
        # Validate encryption before consuming Apple's single-use code.
        encrypt_token("preflight")
    except TokenCryptoError as exc:
        raise AppleRevocationError("token_encryption_unavailable") from exc
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                APPLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": secret,
                    "code": authorization_code,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            payload = response.json()
        returned_id_token = payload.get("id_token") if isinstance(payload, dict) else None
        refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
        if (
            not isinstance(returned_id_token, str)
            or not isinstance(refresh_token, str)
            or not refresh_token
        ):
            raise AppleRevocationError("apple_token_response_invalid")
        returned = await verify_provider_id_token(
            returned_id_token, "apple", nonce, require_email=False
        )
    except AppleRevocationError:
        raise
    except (httpx.HTTPError, ValueError, MobileAuthError) as exc:
        raise AppleRevocationError("apple_token_exchange_failed") from exc
    if returned.subject != supplied_claims.subject or returned.client_id != client_id:
        raise AppleRevocationError("apple_token_binding_failed")
    try:
        return AppleRefreshCredential(encrypt_token(refresh_token), client_id)
    except TokenCryptoError as exc:
        raise AppleRevocationError("token_encryption_unavailable") from exc


def revoke_refresh_token(encrypted_refresh_token: bytes, client_id: str) -> bool:
    """Return true only for Apple's confirmed revocation response."""
    from app.services.token_crypto import decrypt_token

    try:
        secret = apple_client_secret(client_id)
        token = decrypt_token(encrypted_refresh_token)
        response = httpx.post(
            APPLE_REVOKE_URL,
            data={
                "client_id": client_id,
                "client_secret": secret,
                "token": token,
                "token_type_hint": "refresh_token",
            },
            timeout=8.0,
        )
        return response.status_code == 200
    except Exception:
        return False
