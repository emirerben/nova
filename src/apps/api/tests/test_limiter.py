"""Unit tests for app.limiter (KRI-195).

Behind Fly's proxy, the raw socket address seen by the app (slowapi's
`get_remote_address` default) is the proxy-to-machine hop, not the caller --
prod logs show every request landing on a given machine with the identical
`request.client.host`, regardless of which real user sent it. Any
`@limiter.limit(...)` call site that doesn't override `key_func` falls back to
the shared `limiter`'s default, so that default must be the Fly-aware
`get_real_ip`, not `get_remote_address`.
"""

from starlette.requests import Request

from app.limiter import get_real_ip, limiter


def _request(headers: list[tuple[bytes, bytes]], client_host: str = "172.16.4.74") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (client_host, 0),
        }
    )


def test_default_limiter_key_func_is_proxy_aware() -> None:
    """Guards KRI-195: every bare `@limiter.limit(...)` call site (no
    explicit key_func) must key on the Fly-aware real IP. Reverting this to
    `get_remote_address` puts every user hitting one Fly machine back in a
    single shared rate-limit bucket."""
    assert limiter._key_func is get_real_ip


def test_get_real_ip_prefers_fly_client_ip_header() -> None:
    request = _request(
        headers=[
            (b"fly-client-ip", b"203.0.113.10"),
            (b"x-forwarded-for", b"198.51.100.20"),
        ]
    )
    assert get_real_ip(request) == "203.0.113.10"


def test_get_real_ip_falls_back_to_x_forwarded_for_first_entry() -> None:
    request = _request(headers=[(b"x-forwarded-for", b"198.51.100.20, 10.0.0.1")])
    assert get_real_ip(request) == "198.51.100.20"


def test_get_real_ip_falls_back_to_socket_address_with_no_proxy_headers() -> None:
    request = _request(headers=[], client_host="10.9.8.7")
    assert get_real_ip(request) == "10.9.8.7"
