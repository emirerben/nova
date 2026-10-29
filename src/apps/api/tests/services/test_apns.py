"""Offline provider-boundary evidence. All credentials are fabricated."""

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.services.apns import (
    Accepted,
    ActivityNotification,
    APNsTarget,
    APNsTransport,
    Rejected,
    TransportUncertain,
)

TOKEN = "ab" * 32
BEARER = "fakeheader.fakeclaims.fakesignature"
NOTICE = ActivityNotification(UUID(int=1), UUID(int=2))


def target(environment="sandbox"):
    return APNsTarget(environment, "com.example.kria", SecretStr(TOKEN))


def response(status, body=b"", **kwargs):
    return httpx.Response(status, content=body, extensions={"http_version": b"HTTP/2"}, **kwargs)


@pytest.mark.parametrize(
    "environment,host",
    [("sandbox", "api.sandbox.push.apple.com"), ("production", "api.push.apple.com")],
)
async def test_exact_private_request_and_stable_identifiers(environment, host):
    requests = []

    def handler(request):
        requests.append(request)
        return response(200)

    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(handler)
    ) as apns:
        assert await apns.send(target(environment), NOTICE) == Accepted()
        assert await apns.send(target(environment), NOTICE) == Accepted()
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == f"https://{host}/3/device/{TOKEN}"
        assert request.method == "POST"
        assert dict(request.headers) == {
            "host": host,
            "authorization": f"bearer {BEARER}",
            "apns-topic": "com.example.kria",
            "apns-id": str(NOTICE.notification_id),
            "apns-collapse-id": str(NOTICE.notification_id),
            "apns-expiration": "0",
            "apns-priority": "10",
            "apns-push-type": "alert",
            "content-type": "application/json",
            "content-length": str(len(request.content)),
        }
        assert json.loads(request.content) == {
            "aps": {"alert": "Open Kria to see an update."},
            "activity_id": str(NOTICE.activity_id),
        }
        assert request.extensions["timeout"] == dict(connect=5.0, read=5.0, write=5.0, pool=5.0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("token", "secret/../../private"),
        ("token", "abc"),
        ("token", ""),
        ("token", "aa" * 257),
        ("token", "ab\n"),
        ("token", "zz"),
        ("topic", "com.app\r\nsecret"),
        ("topic", "com/app"),
        ("topic", ""),
        ("topic", "com..app"),
        ("topic", "é.app"),
        ("topic", "x" * 256),
        ("environment", "https://evil.example"),
    ],
)
async def test_invalid_input_never_reaches_http_or_leaks(field, value, caplog):
    calls = []
    params = dict(environment="sandbox", topic="com.example.kria", token=SecretStr(TOKEN))
    params[field] = SecretStr(value) if field == "token" else value
    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(calls.append)
    ):
        with pytest.raises(ValueError) as caught:
            APNsTarget(**params)
    assert calls == []
    assert str(caught.value) == "Invalid APNs target"
    assert TOKEN not in repr(caught.value)
    assert BEARER not in caplog.text


@pytest.mark.parametrize(
    "bearer", ["", "token\r\nInjected: yes", "a.b", "a.b.c d", "a.b.é", "a" * 4097]
)
async def test_invalid_bearer_has_zero_calls(bearer):
    calls = []
    async with APNsTransport(
        lambda: SecretStr(bearer), transport=httpx.MockTransport(calls.append)
    ) as apns:
        assert await apns.send(target(), NOTICE) == Rejected("provider", "InvalidBearer")
    assert calls == []


def test_only_uuid_notification_fields_and_secret_repr():
    assert TOKEN not in repr(target())
    with pytest.raises(TypeError):
        ActivityNotification(UUID(int=1), UUID(int=2), title="Private title")
    with pytest.raises(ValueError, match="Invalid activity notification"):
        ActivityNotification("private-title", UUID(int=2))
    with pytest.raises(ValueError, match="Invalid APNs target"):
        APNsTarget("sandbox", "com.example.kria", TOKEN)


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b"", Accepted()),
        (200, b"private", TransportUncertain("protocol")),
        (
            410,
            b'{"reason":"Unregistered","timestamp":1700000000000}',
            Rejected("token", "Unregistered", 1700000000000),
        ),
        (410, b'{"reason":"Unregistered","timestamp":true}', Rejected("token", "Unregistered")),
        (410, b'{"reason":"Unregistered","timestamp":-1}', Rejected("token", "Unregistered")),
        (
            410,
            b'{"reason":"Unregistered","timestamp":"private"}',
            Rejected("token", "Unregistered"),
        ),
        (410, b'{"reason":"private"}', Rejected("unknown", "UnknownResponse")),
        (400, b'{"reason":"BadDeviceToken"}', Rejected("token", "BadDeviceToken")),
        (400, b'{"reason":"DeviceTokenNotForTopic"}', Rejected("token", "DeviceTokenNotForTopic")),
        (400, b'{"reason":"BadTopic"}', Rejected("request", "BadTopic")),
        (403, b'{"reason":"ExpiredProviderToken"}', Rejected("provider", "ExpiredProviderToken")),
        (403, b'{"reason":"private"}', Rejected("provider", "ProviderRejected")),
        (429, b'{"reason":"TooManyRequests"}', Rejected("retryable", "RateLimited")),
        (500, b"private", Rejected("retryable", "ServerError")),
        (503, b'{"reason":"Shutdown"}', Rejected("retryable", "ServerError")),
        (400, b'{"reason":"private"}', Rejected("request", "InvalidRequest")),
        (400, b'{"reason":[]}', Rejected("request", "InvalidRequest")),
        (418, b"private", Rejected("unknown", "UnknownResponse")),
        (302, b"", Rejected("unknown", "UnknownResponse")),
        (410, b"[]", Rejected("unknown", "UnknownResponse")),
        (410, b"not-json", Rejected("unknown", "UnknownResponse")),
        (410, b'{"reason":"Unregistered"}', Rejected("token", "Unregistered")),
        (410, b"x" * 4097, TransportUncertain("protocol")),
    ],
)
async def test_classifies_without_retry_or_raw_data(status, body, expected, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return response(
            status, body, headers={"location": "https://evil.example", "private": TOKEN}
        )

    caplog.set_level("DEBUG")
    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(handler)
    ) as apns:
        result = await apns.send(target(), NOTICE)
    assert result == expected
    assert len(calls) == 1
    assert TOKEN not in repr(result) + caplog.text
    assert BEARER not in repr(result) + caplog.text
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "error,category",
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectError, "connection"),
        (httpx.RemoteProtocolError, "protocol"),
        (httpx.ReadError, "connection"),
    ],
)
async def test_transport_exceptions_are_uncertain_and_redacted(error, category, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        raise error(f"{TOKEN} {BEARER}", request=request)

    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(handler)
    ) as apns:
        result = await apns.send(target(), NOTICE)
    assert result == TransportUncertain(category)
    assert len(calls) == 1
    assert TOKEN not in repr(result) + caplog.text
    assert BEARER not in repr(result) + caplog.text


async def test_signer_failure_is_sanitized_before_http():
    calls = []

    def signer():
        raise RuntimeError(TOKEN + BEARER)

    async with APNsTransport(signer, transport=httpx.MockTransport(calls.append)) as apns:
        assert await apns.send(target(), NOTICE) == Rejected("provider", "ProviderUnavailable")
    assert calls == []


async def test_http1_success_is_uncertain():
    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as apns:
        assert await apns.send(target(), NOTICE) == TransportUncertain("protocol")


async def test_cancellation_propagates():
    async def handler(request):
        raise asyncio.CancelledError()

    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(handler)
    ) as apns:
        with pytest.raises(asyncio.CancelledError):
            await apns.send(target(), NOTICE)


def test_factory_disables_http1_proxy_environment_and_retries(monkeypatch):
    from app.services.apns import create_apns_http_transport

    options = {}
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: options.update(kwargs))
    create_apns_http_transport()
    assert options == {"http1": False, "http2": True, "trust_env": False, "retries": 0}


async def test_partial_response_failure_is_uncertain_and_closed():
    class InterruptedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'{"reason":'
            raise httpx.ReadError(TOKEN)

        async def aclose(self):
            self.closed = True

    stream = InterruptedStream()
    async with APNsTransport(
        lambda: SecretStr(BEARER),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(410, stream=stream, extensions={"http_version": b"HTTP/2"})
        ),
    ) as apns:
        assert await apns.send(target(), NOTICE) == TransportUncertain("connection")
    assert stream.closed


async def test_overall_deadline_covers_transport_that_ignores_io_timeout(monkeypatch):
    real_timeout = asyncio.timeout
    deadlines = []
    calls = []

    def fast_timeout(delay):
        deadlines.append(delay)
        return real_timeout(0.01)

    async def handler(request):
        calls.append(request)
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "timeout", fast_timeout)
    async with APNsTransport(
        lambda: SecretStr(BEARER), transport=httpx.MockTransport(handler)
    ) as apns:
        assert await apns.send(target(), NOTICE) == TransportUncertain("timeout")
    assert deadlines == [15]
    assert len(calls) == 1


async def test_close_exception_does_not_expose_credentials():
    class BrokenClose(httpx.MockTransport):
        async def aclose(self):
            raise RuntimeError(TOKEN + BEARER)

    with pytest.raises(RuntimeError, match="APNs transport close failed") as caught:
        async with APNsTransport(
            lambda: SecretStr(BEARER), transport=BrokenClose(lambda _: response(200))
        ):
            pass
    assert TOKEN not in str(caught.value)
    assert BEARER not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_content_encoding_is_not_decompressed():
    import gzip

    encoded = gzip.compress(b'{"reason":"Unregistered","padding":"' + b"x" * 100_000 + b'"}')
    assert len(encoded) < 4096
    async with APNsTransport(
        lambda: SecretStr(BEARER),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                410,
                stream=httpx.ByteStream(encoded),
                headers={"content-encoding": "gzip"},
                extensions={"http_version": b"HTTP/2"},
            )
        ),
    ) as apns:
        assert await apns.send(target(), NOTICE) == Rejected("unknown", "UnknownResponse")


@pytest.mark.parametrize("close_failure", [False, True])
async def test_cancelled_stream_preserves_cancellation_despite_cleanup(monkeypatch, close_failure):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: real_timeout(1 if delay == 15 else 0.01))
    reading = asyncio.Event()
    closing = asyncio.Event()

    class CancelledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closing.set()
            if close_failure:
                raise RuntimeError(TOKEN + BEARER)
            await asyncio.Event().wait()

    async with APNsTransport(
        lambda: SecretStr(BEARER),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=CancelledStream(), extensions={"http_version": b"HTTP/2"}
            )
        ),
    ) as apns:
        task = asyncio.create_task(apns.send(target(), NOTICE))
        await reading.wait()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        if not done:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done, "external cancellation remained stuck in response cleanup"
    assert closing.is_set()


@pytest.mark.parametrize("stall_read", [False, True])
async def test_stalled_close_preserves_read_outcome(monkeypatch, stall_read):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: real_timeout(0.01))
    closing = asyncio.Event()

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"reason":'
            if stall_read:
                await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await asyncio.Event().wait()

    async with APNsTransport(
        lambda: SecretStr(BEARER),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                410, stream=StalledStream(), extensions={"http_version": b"HTTP/2"}
            )
        ),
    ) as apns:
        task = asyncio.create_task(apns.send(target(), NOTICE))
        done, _ = await asyncio.wait({task}, timeout=0.2)
        if not done:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert done, "request deadline left send stuck in response cleanup"
        expected = (
            TransportUncertain("timeout") if stall_read else Rejected("unknown", "UnknownResponse")
        )
        assert task.result() == expected
    assert closing.is_set()


@pytest.mark.parametrize("close_failure", [False, True])
async def test_accepted_response_survives_cleanup_failure(monkeypatch, close_failure):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: real_timeout(1 if delay == 15 else 0.01))
    closing = asyncio.Event()

    class AcceptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

        async def aclose(self):
            closing.set()
            if close_failure:
                raise RuntimeError(TOKEN + BEARER)
            await asyncio.Event().wait()

    async with APNsTransport(
        lambda: SecretStr(BEARER),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=AcceptedStream(), extensions={"http_version": b"HTTP/2"}
            )
        ),
    ) as apns:
        assert await apns.send(target(), NOTICE) == Accepted()
    assert closing.is_set()


async def test_external_cancellation_during_accepted_response_cleanup_propagates():
    closing = asyncio.Event()

    class AcceptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

        async def aclose(self):
            closing.set()
            await asyncio.Event().wait()

    async with APNsTransport(
        lambda: SecretStr(BEARER),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=AcceptedStream(), extensions={"http_version": b"HTTP/2"}
            )
        ),
    ) as apns:
        task = asyncio.create_task(apns.send(target(), NOTICE))
        await closing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
