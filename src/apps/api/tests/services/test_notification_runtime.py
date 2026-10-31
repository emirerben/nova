"""Synthetic signing keys and HTTP/2 doubles. No provider or device contact."""

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils
from pydantic import SecretStr

from app.config import settings
from app.services import notification_runtime as runtime
from app.services.apns import (
    Accepted,
    ActivityNotification,
    APNsTarget,
    Rejected,
    TransportUncertain,
)
from app.services.notification_delivery import notification_policy
from app.tasks.notification_delivery import notification_sender

NOTICE = ActivityNotification(UUID(int=1), UUID(int=2))


@pytest.fixture
def configured(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    for name, value in {
        "native_notifications_enabled": True,
        "native_notifications_allowed_owner_ids": [UUID(int=9)],
        "token_encryption_key": Fernet.generate_key().decode(),
        "native_notifications_apns_environment": "sandbox",
        "native_notifications_apns_topic": "com.example.kria",
        "native_notifications_apns_team_id": "TEAM123456",
        "native_notifications_apns_key_id": "KEY1234567",
        "native_notifications_apns_private_key": SecretStr(pem),
    }.items():
        monkeypatch.setattr(settings, name, value)
    return runtime.notification_configuration()


def target(configuration, **changes):
    return replace(
        APNsTarget(configuration.environment, configuration.topic, SecretStr("ab" * 32)),
        **changes,
    )


class RecordingHTTP2(httpx.AsyncBaseTransport):
    def __init__(self, *, status=200, content=b"", close_error=False, hanging_close=False):
        self.status = status
        self.content = content
        self.close_error = close_error
        self.hanging_close = hanging_close
        self.requests = []
        self.closed = False

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(
            self.status, content=self.content, extensions={"http_version": b"HTTP/2"}
        )

    async def aclose(self):
        self.closed = True
        if self.hanging_close:
            await asyncio.Future()
        if self.close_error:
            raise RuntimeError("synthetic sensitive cleanup text")


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_enabled_configuration_connects_all_three_gates_without_opening_a_transport(
    configured, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Discovery and availability must not construct HTTP resources or sign tokens")

    monkeypatch.setattr(runtime, "APNsTransport", forbidden)
    monkeypatch.setattr(runtime, "CachedAPNsBearer", forbidden)
    assert configured is not None
    assert notification_policy().available
    assert notification_policy().allows(UUID(int=9))
    assert not notification_policy().allows(UUID(int=10))
    assert notification_sender() is not None
    assert runtime.notification_beat_schedule() == {
        "discover-native-notifications-every-minute": {
            "task": "tasks.sweep_notifications",
            "schedule": 60.0,
        }
    }


def test_disabled_flag_does_not_read_key_or_schedule(configured, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled notifications must not inspect credential material")

    monkeypatch.setattr(settings, "native_notifications_enabled", False)
    monkeypatch.setattr(runtime, "_parse_configuration", forbidden)
    assert not notification_policy().available
    assert notification_sender() is None
    assert runtime.notification_beat_schedule() == {}


@pytest.mark.parametrize(
    "name,value",
    [
        ("native_notifications_apns_environment", "invalid"),
        ("native_notifications_allowed_owner_ids", []),
        ("token_encryption_key", ""),
        ("token_encryption_key", "synthetic invalid encryption key"),
        ("native_notifications_apns_topic", None),
        ("native_notifications_apns_topic", "https://example.com"),
        ("native_notifications_apns_team_id", "short"),
        ("native_notifications_apns_key_id", "short"),
        ("native_notifications_apns_private_key", None),
        ("native_notifications_apns_private_key", SecretStr("synthetic malformed key")),
    ],
)
def test_incomplete_or_invalid_configuration_fails_closed(configured, monkeypatch, name, value):
    monkeypatch.setattr(settings, name, value)
    assert not notification_policy().available
    assert notification_sender() is None
    assert runtime.notification_beat_schedule() == {}


@pytest.mark.parametrize(
    "key", [ec.generate_private_key(ec.SECP384R1()), rsa.generate_private_key(65537, 2048)]
)
def test_wrong_signing_key_algorithm_is_unavailable(configured, monkeypatch, key):
    monkeypatch.setattr(
        settings,
        "native_notifications_apns_private_key",
        SecretStr(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
        ),
    )
    assert runtime.notification_configuration() is None


def test_real_es256_signature_and_apple_claims(configured):
    bearer = runtime.CachedAPNsBearer(configured, clock=lambda: 1_700_000_000.75)()
    header, claims, raw = bearer.get_secret_value().split(".")
    assert json.loads(decode(header)) == {"alg": "ES256", "kid": configured.key_id}
    assert json.loads(decode(claims)) == {"iss": configured.team_id, "iat": 1_700_000_000}
    signature = decode(raw)
    assert len(signature) == 64
    r, s = int.from_bytes(signature[:32]), int.from_bytes(signature[32:])
    configured.signing_key.public_key().verify(
        utils.encode_dss_signature(r, s),
        f"{header}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


def test_bearer_cache_refreshes_at_fifty_minutes_and_refuses_clock_rollback(configured):
    now = [1_700_000_000.0]
    provider = runtime.CachedAPNsBearer(configured, clock=lambda: now[0])
    first = provider()
    now[0] += 50 * 60 - 1
    assert provider() == first
    now[0] += 1
    second = provider()
    assert second != first
    now[0] -= 1
    with pytest.raises(RuntimeError, match="APNs clock unavailable"):
        provider()
    now[0] += 1
    assert provider() == second


def test_concurrent_requests_share_one_signed_bearer(configured):
    provider = runtime.CachedAPNsBearer(configured, clock=lambda: 1_700_000_000)
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: provider(), range(32)))
    assert len({token.get_secret_value() for token in tokens}) == 1


def test_bearer_scope_changes_with_process_or_key_material(configured):
    first = runtime._process_bearer(configured, 1)
    assert runtime._process_bearer(configured, 1) is first
    assert runtime._process_bearer(configured, 2) is not first
    rotated = replace(configured, signing_key=ec.generate_private_key(ec.SECP256R1()))
    assert runtime._process_bearer(rotated, 2).configuration.signing_key is rotated.signing_key


@pytest.mark.parametrize("changes", [{"topic": "com.other.app"}, {"environment": "production"}])
async def test_sender_refuses_target_outside_configured_topic_and_environment(configured, changes):
    def forbidden():
        pytest.fail("An out-of-scope target must never open a connection")

    sender = runtime.PerSendAPNsSender(configured, transport_factory=forbidden)
    assert await sender.send(target(configured, **changes), NOTICE) == Rejected(
        "request", "InvalidRequest"
    )


def test_fresh_transport_per_task_loop_with_shared_bearer(configured):
    transports = []

    def factory():
        transport = RecordingHTTP2()
        transports.append(transport)
        return transport

    sender = runtime.PerSendAPNsSender(configured, transport_factory=factory)
    assert asyncio.run(sender.send(target(configured), NOTICE)) == Accepted()
    assert asyncio.run(sender.send(target(configured), NOTICE)) == Accepted()
    assert len(transports) == 2
    assert all(t.closed and len(t.requests) == 1 for t in transports)
    assert (
        transports[0].requests[0].headers["authorization"]
        == transports[1].requests[0].headers["authorization"]
    )


@pytest.mark.parametrize(
    "status,content,expected",
    [
        (200, b"", Accepted()),
        (410, b'{"reason":"Unregistered"}', Rejected("token", "Unregistered")),
    ],
)
async def test_cleanup_failure_preserves_known_provider_outcome(
    configured, status, content, expected
):
    transport = RecordingHTTP2(status=status, content=content, close_error=True)
    sender = runtime.PerSendAPNsSender(configured, transport_factory=lambda: transport)
    assert await sender.send(target(configured), NOTICE) == expected
    assert transport.closed


async def test_hanging_cleanup_is_bounded_without_losing_acceptance(configured, monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr(
        runtime.asyncio, "timeout", lambda duration: timeout(0.01 if duration == 17 else duration)
    )
    transport = RecordingHTTP2(hanging_close=True)
    sender = runtime.PerSendAPNsSender(configured, transport_factory=lambda: transport)
    assert await sender.send(target(configured), NOTICE) == Accepted()
    assert transport.closed


async def test_unknown_transport_outcome_never_retries(configured):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("synthetic sensitive provider error")

    sender = runtime.PerSendAPNsSender(
        configured, transport_factory=lambda: httpx.MockTransport(handler)
    )
    assert await sender.send(target(configured), NOTICE) == TransportUncertain("connection")
    assert len(calls) == 1


def test_runtime_routes_delivery_and_discovery_to_maintenance_queue():
    from app.worker import celery_app

    assert "discover-native-notifications-every-minute" not in celery_app.conf.beat_schedule
    for name in ["tasks.sweep_notifications", "tasks.deliver_notification"]:
        assert celery_app.conf.task_routes[name]["queue"] == "maintenance"
