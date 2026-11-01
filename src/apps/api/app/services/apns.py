"""Isolated APNs transport. Acceptance is not device receipt. No runtime wiring."""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import httpx
from pydantic import SecretStr

Environment = Literal["sandbox", "production"]
Category = Literal["token", "request", "provider", "retryable", "unknown"]
Code = Literal[
    "Unregistered",
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "BadTopic",
    "ExpiredProviderToken",
    "InvalidProviderToken",
    "MissingProviderToken",
    "Forbidden",
    "ProviderRejected",
    "ProviderUnavailable",
    "InvalidBearer",
    "RateLimited",
    "ServerError",
    "InvalidRequest",
    "UnknownResponse",
]
_HOSTS = {"sandbox": "api.sandbox.push.apple.com", "production": "api.push.apple.com"}


@dataclass(frozen=True)
class APNsTarget:
    environment: Environment
    topic: str
    token: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.environment, str)
            or self.environment not in _HOSTS
            or not isinstance(self.topic, str)
            or len(self.topic) > 255
            or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", self.topic)
            or not isinstance(self.token, SecretStr)
            or not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,256}", self.token.get_secret_value())
        ):
            raise ValueError("Invalid APNs target")


@dataclass(frozen=True)
class ActivityNotification:
    activity_id: UUID
    notification_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.activity_id, UUID) or not isinstance(self.notification_id, UUID):
            raise ValueError("Invalid activity notification")


@dataclass(frozen=True)
class Accepted:
    """APNs accepted the notification. Device receipt remains unknown."""


@dataclass(frozen=True)
class Rejected:
    category: Category
    code: Code
    invalidated_at_ms: int | None = None


@dataclass(frozen=True)
class TransportUncertain:
    category: Literal["timeout", "connection", "protocol"]


Result = Accepted | Rejected | TransportUncertain


def create_apns_http_transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(http1=False, http2=True, trust_env=False, retries=0)


class APNsTransport:
    """Owns a reusable transport; close it after use. Disable wire-level tracing."""

    def __init__(
        self,
        bearer_provider: Callable[[], SecretStr],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bearer_provider = bearer_provider
        self._transport = transport if transport is not None else create_apns_http_transport()

    async def __aenter__(self) -> "APNsTransport":
        return self

    async def __aexit__(self, *args: object) -> None:
        try:
            await self._transport.aclose()
        except Exception:
            raise RuntimeError("APNs transport close failed") from None

    async def send(self, target: APNsTarget, notification: ActivityNotification) -> Result:
        if not isinstance(target, APNsTarget) or not isinstance(notification, ActivityNotification):
            raise ValueError("Invalid APNs input")
        try:
            bearer = self._bearer_provider()
        except Exception:
            return Rejected("provider", "ProviderUnavailable")
        if (
            not isinstance(bearer, SecretStr)
            or len(bearer.get_secret_value()) > 4096
            or not re.fullmatch(
                r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", bearer.get_secret_value()
            )
        ):
            return Rejected("provider", "InvalidBearer")
        request = httpx.Request(
            "POST",
            f"https://{_HOSTS[target.environment]}/3/device/{target.token.get_secret_value()}",
            headers={
                "authorization": f"bearer {bearer.get_secret_value()}",
                "apns-topic": target.topic,
                "apns-id": str(notification.notification_id),
                "apns-collapse-id": str(notification.notification_id),
                "apns-expiration": "0",
                "apns-priority": "10",
                "apns-push-type": "alert",
                "content-type": "application/json",
            },
            json={
                "aps": {"alert": "Open Kria to see an update."},
                "activity_id": str(notification.activity_id),
            },
            extensions={"timeout": dict(connect=5.0, read=5.0, write=5.0, pool=5.0)},
        )
        response = None
        result = None
        failed = False
        try:
            try:
                async with asyncio.timeout(15):
                    response = await self._transport.handle_async_request(request)
                    if response.http_version != "HTTP/2":
                        return TransportUncertain("protocol")
                    body = bytearray()
                    async for chunk in response.stream:
                        if len(body) + len(chunk) > 4096:
                            return TransportUncertain("protocol")
                        body.extend(chunk)
                    result = _classify(response.status_code, body)
                    return result
            except BaseException:
                failed = True
                raise
            finally:
                if response is not None:
                    try:
                        async with asyncio.timeout(1):
                            await response.aclose()
                    except Exception:
                        if not failed and result is None:
                            raise
        except (TimeoutError, httpx.TimeoutException):
            return TransportUncertain("timeout")
        except httpx.NetworkError:
            return TransportUncertain("connection")
        except Exception:
            # Provider and transport exception messages can contain credentials.
            return TransportUncertain("protocol")


def _classify(status: int, body: bytearray) -> Result:
    if status == 200:
        return TransportUncertain("protocol") if body else Accepted()
    if status == 429:
        return Rejected("retryable", "RateLimited")
    if 500 <= status <= 599:
        return Rejected("retryable", "ServerError")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        payload = None
    reason = payload.get("reason") if isinstance(payload, dict) else None
    if status == 410 and reason == "Unregistered":
        timestamp = payload.get("timestamp")
        if type(timestamp) is not int or not 0 <= timestamp <= 253402300799999:
            timestamp = None
        return Rejected("token", "Unregistered", timestamp)
    if status == 400:
        if reason == "BadDeviceToken":
            return Rejected("token", "BadDeviceToken")
        if reason == "DeviceTokenNotForTopic":
            return Rejected("token", "DeviceTokenNotForTopic")
        if reason == "BadTopic":
            return Rejected("request", "BadTopic")
        return Rejected("request", "InvalidRequest")
    if status == 403:
        codes: dict[str, Code] = {
            "ExpiredProviderToken": "ExpiredProviderToken",
            "InvalidProviderToken": "InvalidProviderToken",
            "MissingProviderToken": "MissingProviderToken",
            "Forbidden": "Forbidden",
        }
        code = (
            codes.get(reason, "ProviderRejected") if isinstance(reason, str) else "ProviderRejected"
        )
        return Rejected("provider", code)
    return Rejected("unknown", "UnknownResponse")
