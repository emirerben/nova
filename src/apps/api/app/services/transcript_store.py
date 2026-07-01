"""Tiny Redis-backed store for the transcript-flow async `analyze` result.

The `analyze` step (probe duration + light footage summary) runs in a Celery task;
the frontend polls a GET route. Rather than add a DB column for transient analyze
state, the task writes its result here under a short TTL and the poll route reads it.
Keyed by an opaque `analyze_id` (uuid) the POST route hands back to the client.

Degrades safely: if Redis is unavailable, `put_analyze` is a no-op and `get_analyze`
returns None (the poll route then reports "pending" until the TTL — never crashes).
"""

from __future__ import annotations

import json
import threading
from typing import Any

import redis as redis_lib
import structlog

from app.config import settings

log = structlog.get_logger()

_TTL_S = 900  # 15 min — comfortably longer than a creator sits on the Brief step.
# Keyed by (item_id, analyze_id) so the poll route — which owner-checks item_id —
# can only ever read a result produced for THAT item. Keying on the bare analyze_id
# would let any authenticated user who guessed/leaked an analyze_id read another
# user's footage summary (horizontal IDOR).
_KEY = "transcript:analyze:{}:{}"

_redis_lock = threading.Lock()
_redis_client: redis_lib.Redis | None = None


def _get_redis() -> Any:
    """Module-singleton Redis client; None on connection failure (same pattern as
    clip_router_cache)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            _redis_client = redis_lib.from_url(
                settings.redis_url, socket_connect_timeout=2, socket_timeout=2
            )
            _redis_client.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("transcript_store.redis_unavailable", error=str(exc)[:200])
            _redis_client = None
    return _redis_client


def put_analyze(item_id: str, analyze_id: str, payload: dict) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        client.set(_KEY.format(item_id, analyze_id), json.dumps(payload), ex=_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("transcript_store.put_failed", error=str(exc)[:200])


def get_analyze(item_id: str, analyze_id: str) -> dict | None:
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_KEY.format(item_id, analyze_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("transcript_store.get_failed", error=str(exc)[:200])
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None
