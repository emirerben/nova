"""What the sentence meaning check answered for a render, kept for the render's own retries.

The paid-call ledger refuses to pay twice for one logical call, and a Celery retry of the render
task is the same delivery: same task id, same job, same prompt. The retry would therefore be
refused ("already settled") and, with no stored answer, would place the clips by keyword where
the first attempt placed them by meaning. So the answer is stored the moment it is known, under
(job, digest of the whole request), and a retry takes it instead of calling again.

Fail-open Redis, like `app.services.turn_checkpoint`: an unreachable store is a miss, and a miss
is always safe (the retry pays again under its own retry-scoped key). It is not a source of
truth for anything.
"""

from __future__ import annotations

import json

import structlog

from app.config import settings

log = structlog.get_logger()

TTL_S = 3600
_FORMAT = 1


def _redis():  # noqa: ANN202
    import redis  # noqa: PLC0415

    return redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)


def _key(job_id: str, digest: str) -> str:
    return f"nova:insert-sentence-picks:{job_id}:{digest}"


def load(job_id: str, digest: str) -> list[dict] | None:
    """The stored answer rows (``{"media_id", "first", "last", "confidence"}``), or ``None``."""
    try:
        raw = _redis().get(_key(job_id, digest))
        if raw is None:
            return None
        envelope = json.loads(raw)
        if envelope.get("v") != _FORMAT or not isinstance(envelope.get("picks"), list):
            return None
        return [row for row in envelope["picks"] if isinstance(row, dict)]
    except (ValueError, TypeError, AttributeError):
        return None
    except Exception:  # noqa: BLE001 - an unreachable store is a miss
        log.warning("insert_pick_checkpoint_unreadable", exc_info=True)
        return None


def store(job_id: str, digest: str, rows: list[dict]) -> None:
    try:
        _redis().setex(_key(job_id, digest), TTL_S, json.dumps({"v": _FORMAT, "picks": rows}))
    except Exception:  # noqa: BLE001 - a skipped write costs one repeated call, nothing else
        log.warning("insert_pick_checkpoint_store_failed", exc_info=True)
