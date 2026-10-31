"""Default-disabled APNs configuration, signing and per-send transport ownership."""

import asyncio
import base64
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import UUID

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from pydantic import SecretStr

from app.config import settings
from app.services.apns import (
    ActivityNotification,
    APNsTarget,
    APNsTransport,
    Environment,
    Rejected,
    Result,
    TransportUncertain,
)


@dataclass(frozen=True)
class _ProviderInput:
    allowed_owner_ids: frozenset[UUID]
    environment: str
    topic: str | None
    team_id: str | None
    key_id: str | None
    private_key: SecretStr | None = field(repr=False)


@dataclass(frozen=True)
class APNsProviderConfiguration:
    allowed_owner_ids: frozenset[UUID]
    environment: Environment
    topic: str
    team_id: str
    key_id: str
    signing_key: ec.EllipticCurvePrivateKey = field(repr=False)


@lru_cache(maxsize=1)
def _parse_configuration(raw: _ProviderInput) -> APNsProviderConfiguration:
    if (
        not raw.allowed_owner_ids
        or raw.environment not in {"sandbox", "production"}
        or not raw.topic
        or len(raw.topic) > 255
        or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", raw.topic)
        or not raw.team_id
        or not re.fullmatch(r"[A-Z0-9]{10}", raw.team_id)
        or not raw.key_id
        or not re.fullmatch(r"[A-Z0-9]{10}", raw.key_id)
        or not isinstance(raw.private_key, SecretStr)
    ):
        raise ValueError("Notification configuration unavailable")
    key = serialization.load_pem_private_key(
        raw.private_key.get_secret_value().encode(), password=None
    )
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Notification configuration unavailable")
    return APNsProviderConfiguration(
        raw.allowed_owner_ids, raw.environment, raw.topic, raw.team_id, raw.key_id, key
    )


def notification_configuration() -> APNsProviderConfiguration | None:
    if not settings.native_notifications_enabled:
        return None
    try:
        Fernet(settings.token_encryption_key.encode())
        return _parse_configuration(
            _ProviderInput(
                frozenset(settings.native_notifications_allowed_owner_ids),
                settings.native_notifications_apns_environment,
                settings.native_notifications_apns_topic,
                settings.native_notifications_apns_team_id,
                settings.native_notifications_apns_key_id,
                settings.native_notifications_apns_private_key,
            )
        )
    except Exception:
        # Cryptographic parser errors may include private material.
        return None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class CachedAPNsBearer:
    def __init__(
        self,
        configuration: APNsProviderConfiguration,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.configuration = configuration
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[int, SecretStr] | None = None

    def __call__(self) -> SecretStr:
        with self._lock:
            now = self.clock()
            if not math.isfinite(now) or now < 0:
                raise RuntimeError("APNs clock unavailable")
            if self._cached is not None:
                issued_at, token = self._cached
                if now < issued_at:
                    raise RuntimeError("APNs clock unavailable")
                if now - issued_at < 50 * 60:
                    return token
            issued_at = int(now)
            header = _encode(
                json.dumps(
                    {"alg": "ES256", "kid": self.configuration.key_id},
                    separators=(",", ":"),
                ).encode()
            )
            claims = _encode(
                json.dumps(
                    {"iss": self.configuration.team_id, "iat": issued_at},
                    separators=(",", ":"),
                ).encode()
            )
            message = f"{header}.{claims}"
            signature = self.configuration.signing_key.sign(
                message.encode(), ec.ECDSA(hashes.SHA256())
            )
            r, s = utils.decode_dss_signature(signature)
            token = SecretStr(f"{message}.{_encode(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}")
            self._cached = issued_at, token
            return token


@lru_cache(maxsize=1)
def _process_bearer(configuration: APNsProviderConfiguration, _process_id: int) -> CachedAPNsBearer:
    return CachedAPNsBearer(configuration)


class PerSendAPNsSender:
    def __init__(
        self,
        configuration: APNsProviderConfiguration,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self.configuration = configuration
        self.transport_factory = transport_factory

    async def send(self, target: APNsTarget, notification: ActivityNotification) -> Result:
        if (
            target.environment != self.configuration.environment
            or target.topic != self.configuration.topic
        ):
            return Rejected("request", "InvalidRequest")
        result = None
        try:
            async with asyncio.timeout(17):
                async with APNsTransport(
                    _process_bearer(self.configuration, os.getpid()),
                    transport=(self.transport_factory() if self.transport_factory else None),
                ) as transport:
                    result = await transport.send(target, notification)
        except Exception:
            # Cleanup cannot erase an already classified provider response.
            pass
        return result if result is not None else TransportUncertain("protocol")


def configured_notification_sender() -> PerSendAPNsSender | None:
    configuration = notification_configuration()
    return PerSendAPNsSender(configuration) if configuration is not None else None


def notification_beat_schedule() -> dict:
    if notification_configuration() is None:
        return {}
    return {
        "discover-native-notifications-every-minute": {
            "task": "tasks.sweep_notifications",
            "schedule": 60.0,
        }
    }
