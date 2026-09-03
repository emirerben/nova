"""Guards for per-worktree Celery Redis keyspaces."""

from __future__ import annotations

from kombu.transport.redis import PrefixedStrictRedis

from app.services.celery_isolation import (
    broker_transport_options,
    redis_key_prefix,
    result_backend_transport_options,
)


def test_worktree_namespaces_prefix_default_and_explicit_queue_keys() -> None:
    """Kombu prefixes Redis lists regardless of Celery's logical queue name."""
    prefix_a = redis_key_prefix("wt-a")
    prefix_b = redis_key_prefix("wt-b")
    assert prefix_a and prefix_b and prefix_a != prefix_b

    client_a = PrefixedStrictRedis(global_keyprefix=prefix_a)
    client_b = PrefixedStrictRedis(global_keyprefix=prefix_b)
    for queue in ("celery", "plan-jobs", "overlay-jobs"):
        key_a = client_a._prefix_args(["LPUSH", queue, "message"])[1]
        key_b = client_b._prefix_args(["LPUSH", queue, "message"])[1]
        assert key_a == f"{prefix_a}{queue}"
        assert key_b == f"{prefix_b}{queue}"
        assert key_a != key_b


def test_production_namespace_is_byte_compatible() -> None:
    """Unset namespace keeps the historical options and canonical queues."""
    assert broker_transport_options(None) == {
        "visibility_timeout": 1900,
        "polling_interval": 10,
    }
    assert result_backend_transport_options(None) == {}
    assert redis_key_prefix(None) is None


def test_namespace_validation_rejects_key_separators() -> None:
    import pytest

    with pytest.raises(ValueError, match="lowercase letters"):
        redis_key_prefix("worktree/a")
