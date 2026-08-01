"""Shared Fernet encryption for persisted third-party OAuth credentials."""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class TokenCryptoError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise TokenCryptoError("OAuth token encryption is not configured")
    try:
        return Fernet(settings.token_encryption_key.encode())
    except (TypeError, ValueError) as exc:
        raise TokenCryptoError("OAuth token encryption key is invalid") from exc


def encrypt_token(value: str) -> bytes:
    if not value:
        raise TokenCryptoError("Refusing to encrypt an empty OAuth token")
    return _fernet().encrypt(value.encode())


def decrypt_token(value: bytes | None) -> str:
    if not value:
        raise TokenCryptoError("OAuth token is unavailable")
    try:
        return _fernet().decrypt(value).decode()
    except InvalidToken as exc:
        raise TokenCryptoError("OAuth token could not be decrypted") from exc
