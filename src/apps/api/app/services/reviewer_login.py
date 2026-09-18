"""Password hashing for the Apple Beta App Review demo account (KRI-111).

Stdlib-only (no passlib/bcrypt dependency) — `hashlib.scrypt` is available on
every CPython build. There is exactly one credential this ever guards (the
fixed reviewer email/password Apple's App Review team signs in with), so a
single hash format with generous scrypt cost parameters is sufficient; this
is deliberately not a general-purpose password-hashing service.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from base64 import b64decode, b64encode

_ALGORITHM = "scrypt"
_N = 2**15
_R = 8
_P = 1
_MAXMEM = 64 * 1024 * 1024
_DKLEN = 32
_SALT_BYTES = 16

# Bounds on parsed cost parameters. These exist only to keep a malformed or
# tampered stored hash from turning a login attempt into an unbounded CPU/
# memory burn — verify_password() never raises, but it must not hang either.
_MAX_N = 2**20
_MAX_R = 64
_MAX_P = 16


def hash_password(password: str) -> str:
    """Hash `password` into the durable `scrypt$n$r$p$salt$hash` format."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        maxmem=_MAXMEM,
        dklen=_DKLEN,
    )
    return "$".join(
        [
            _ALGORITHM,
            str(_N),
            str(_R),
            str(_P),
            b64encode(salt).decode("ascii"),
            b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verify. Any malformed `encoded` value returns False.

    Never raises — callers (the reviewer-login route) run this unconditionally,
    including against `DUMMY_HASH` when the supplied email doesn't match, so a
    parse failure must degrade to "wrong password", never a 500.
    """
    try:
        algorithm, n_raw, r_raw, p_raw, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if not (1 < n <= _MAX_N and 0 < r <= _MAX_R and 0 < p <= _MAX_P):
            return False
        salt = b64decode(salt_b64, validate=True)
        expected = b64decode(hash_b64, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_MAXMEM,
            dklen=len(expected) or _DKLEN,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, expected)


def is_configured() -> bool:
    """Whether the reviewer-login route is enabled and has a usable account."""
    # Imported lazily so `python -m app.cli.reviewer_login hash` works outside
    # the API environment (Settings() requires STORAGE_BUCKET/DATABASE_URL).
    from app.config import settings  # noqa: PLC0415

    return bool(
        settings.reviewer_login_enabled
        and settings.reviewer_login_email
        and settings.reviewer_login_password_hash
    )


# A valid hash of a random (never-used) string. The route runs `verify_password`
# against this whenever the supplied email doesn't match the configured one, so
# a wrong-email attempt takes the same code path and roughly the same wall time
# as a wrong-password attempt — the response never reveals which check failed.
DUMMY_HASH = hash_password(b64encode(os.urandom(32)).decode("ascii"))

__all__ = ["DUMMY_HASH", "hash_password", "is_configured", "verify_password"]
