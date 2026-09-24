from fastapi import Request
from slowapi import Limiter


def get_real_ip(request: Request) -> str:
    """Rate limit by real IP; proxy-aware.

    Fly's edge sets `Fly-Client-IP` to the true client address and clients
    cannot forge it, so prefer it: the first `X-Forwarded-For` entry is
    client-controlled and would let a caller pick its own limiter bucket.
    The XFF/socket fallback keeps local dev and tests working unchanged.
    """
    # Real ASGI requests always carry a "headers" key; some unit tests build a
    # minimal http scope without one (previously harmless, since
    # get_remote_address never touched headers) -- tolerate that rather than
    # hunting down every such scope across the suite.
    if "headers" in request.scope:
        fly_ip = request.headers.get("Fly-Client-IP")
        if fly_ip:
            return fly_ip.strip()
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client and request.client.host else "127.0.0.1"


# Default key for every `@limiter.limit(...)` call site that doesn't pass its
# own `key_func`. Behind Fly's proxy, the raw socket address (slowapi's
# `get_remote_address` default) is the proxy-to-machine hop, not the caller —
# every request landing on a given machine shows the same address there, so
# the plain socket key put every user on that machine in one shared bucket
# (KRI-195). `get_real_ip` already falls back to the socket address when no
# Fly/XFF header is present, so this is a no-op for local dev and tests.
limiter = Limiter(key_func=get_real_ip)
