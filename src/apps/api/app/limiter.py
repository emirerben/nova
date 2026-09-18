from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


def get_real_ip(request: Request) -> str:
    """Rate limit by real IP; proxy-aware.

    Fly's edge sets `Fly-Client-IP` to the true client address and clients
    cannot forge it, so prefer it: the first `X-Forwarded-For` entry is
    client-controlled and would let a caller pick its own limiter bucket.
    The XFF/socket fallback keeps local dev and tests working unchanged.
    """
    fly_ip = request.headers.get("Fly-Client-IP")
    if fly_ip:
        return fly_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host or "127.0.0.1"
