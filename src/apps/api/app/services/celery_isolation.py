"""Local-development isolation for the shared Redis Celery broker.

The Redis database used by local worktrees is commonly shared.  Kombu's
``global_keyprefix`` prefixes every broker key (including the Redis lists used
for Celery queues), so it also covers dispatches that pass an explicit
``queue=`` option.  Keeping this at the transport layer means task producers
and workers use the same contract without changing every dispatch call site.

Production leaves ``NOVA_CELERY_QUEUE_NAMESPACE`` unset.  In that mode these
helpers return the historical, unprefixed configuration byte-for-byte.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

NAMESPACE_ENV_VAR = "NOVA_CELERY_QUEUE_NAMESPACE"

# Redis key prefixes are deliberately readable in redis-cli and end in ':'.
# The separator also prevents a namespace such as ``foo`` from colliding with
# a future key whose name happens to start with ``bar``.
_PREFIX_ROOT = "nova-dev:"
_VALID_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def normalize_namespace(value: str | None) -> str | None:
    """Return a safe namespace, or ``None`` for the production default.

    The launcher derives a valid value.  Validation here also protects manual
    invocations from accidentally putting whitespace or Redis command
    separators into a key prefix.
    """

    if value is None:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if not _VALID_NAMESPACE.fullmatch(value):
        raise ValueError(
            f"{NAMESPACE_ENV_VAR} must contain only lowercase letters, digits, "
            "dots, underscores, and hyphens"
        )
    return value


def redis_key_prefix(namespace: str | None) -> str | None:
    """Return the Kombu/Celery Redis global key prefix for ``namespace``."""

    namespace = normalize_namespace(namespace)
    return f"{_PREFIX_ROOT}{namespace}:" if namespace else None


def broker_transport_options(
    namespace: str | None,
    *,
    visibility_timeout: int = 1900,
    polling_interval: int = 10,
) -> dict[str, Any]:
    """Build broker options while preserving the production option set."""

    options: dict[str, Any] = {
        "visibility_timeout": visibility_timeout,
        "polling_interval": polling_interval,
    }
    prefix = redis_key_prefix(namespace)
    if prefix:
        options["global_keyprefix"] = prefix
    return options


def result_backend_transport_options(namespace: str | None) -> dict[str, str]:
    """Build result-backend options matching the broker namespace."""

    prefix = redis_key_prefix(namespace)
    return {"global_keyprefix": prefix} if prefix else {}


def namespaced_queue_names(namespace: str | None, queues: Mapping[str, str]) -> dict[str, str]:
    """Return display/diagnostic queue names for a broker namespace.

    Queue names are not rewritten by this feature: Kombu applies the global
    prefix to the Redis keys while Celery continues to report canonical queue
    names (``celery``, ``plan-jobs``, ...).  This helper is for launcher logs
    and tests that need to show the physical Redis key.
    """

    prefix = redis_key_prefix(namespace)
    if not prefix:
        return dict(queues)
    return {name: f"{prefix}{physical}" for name, physical in queues.items()}
