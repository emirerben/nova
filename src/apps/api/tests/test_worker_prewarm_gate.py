"""Tests for the worker_ready CLIP-prewarm queue gate in app/worker.py.

The 2026-08-02 incident these pin: worker_ready signal handlers fire in EVERY
celery worker process, so the CLIP font-matcher prewarm (torch + ViT-B/32,
~450-600MB peak per clip_font_matcher.py) also ran on the 512MB `light`
maintenance machine, OOM crash-looping it until flyd's on-failure restart
budget ran out. The machine parked `stopped` — a state every subsequent
deploy preserves — silently killing the stale-job sweeper, the daily digest,
and the TikTok polls for two days. The gate keeps the prewarm on workers
that actually consume a render queue, and fails CLOSED (skip) when the
consumer shape can't be read.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch


def _sender_consuming(*queue_names: str) -> MagicMock:
    """Fake worker_ready sender (celery Consumer) consuming the given queues.

    Mirrors the real shape: sender.app.amqp.queues.consume_from is a
    dict-like keyed by queue name.
    """
    sender = MagicMock()
    sender.app.amqp.queues.consume_from = {name: object() for name in queue_names}
    return sender


# ── _worker_consumes_render_queues (the gate itself) ────────────────────────


def test_maintenance_only_worker_is_not_a_render_worker():
    """The light machine (`-Q maintenance`) must never prewarm CLIP."""
    from app.worker import _worker_consumes_render_queues

    assert _worker_consumes_render_queues(_sender_consuming("maintenance")) is False


def test_render_worker_queues_pass_the_gate():
    from app.worker import _worker_consumes_render_queues

    sender = _sender_consuming("celery", "plan-jobs", "overlay-jobs")
    assert _worker_consumes_render_queues(sender) is True


def test_default_local_dev_worker_passes_the_gate():
    """A bare `celery worker` (no -Q) consumes the default `celery` queue —
    local dev keeps the prewarm."""
    from app.worker import _worker_consumes_render_queues

    assert _worker_consumes_render_queues(_sender_consuming("celery")) is True


def test_unreadable_sender_fails_closed():
    """Introspection failure → skip the prewarm. Worst case on a render
    worker is one ~3-5s lazy load; the other direction is the OOM loop."""
    from app.worker import _worker_consumes_render_queues

    assert _worker_consumes_render_queues(object()) is False


def test_gate_matches_every_render_worker_queue_individually():
    """Any single render queue is enough — the gate must stay in sync with
    RENDER_WORKER_QUEUES, not hardcode `celery`."""
    from app.services.queue_state import RENDER_WORKER_QUEUES
    from app.worker import _worker_consumes_render_queues

    for queue in RENDER_WORKER_QUEUES:
        assert _worker_consumes_render_queues(_sender_consuming(queue)) is True


def _fly_toml_queues(process_name: str) -> set[str]:
    """Parse a fly.toml process line's `-Q` list. The capture runs to the
    next whitespace or closing quote — NOT a char-class allowlist — so any
    legal queue name (dots included) is captured whole; a truncating match
    would make the drift guards below pass on exactly the renames they
    exist to catch."""
    fly_toml = (Path(__file__).parents[4] / "fly.toml").read_text()
    match = re.search(rf'^\s*{process_name}\s*=\s*"[^"]*-Q\s+([^"\s]+)', fly_toml, re.MULTILINE)
    assert match, f"could not find the {process_name} process's -Q list in fly.toml"
    return set(match.group(1).split(","))


def test_render_worker_queues_match_fly_toml_worker_command():
    """RENDER_WORKER_QUEUES is hand-synced with fly.toml's worker `-Q` list
    (queue_state.py says so). A queue rename in fly.toml alone would silently
    fail-close the prewarm on the REAL render worker — this pins the two
    together so the drift fails CI instead."""
    from app.services.queue_state import RENDER_WORKER_QUEUES

    assert _fly_toml_queues("worker") == set(RENDER_WORKER_QUEUES)


def test_light_queues_stay_disjoint_from_render_queues():
    """The inverse invariant: the light machine's `-Q` list must never
    intersect RENDER_WORKER_QUEUES. A fly.toml edit like
    `light = "... -Q maintenance,celery"` would pass the prewarm gate on the
    small VM and re-open the 2026-08-02 OOM incident with no code change —
    this makes that edit fail CI instead."""
    from app.services.queue_state import RENDER_WORKER_QUEUES

    assert _fly_toml_queues("light").isdisjoint(RENDER_WORKER_QUEUES)


def test_autoplace_worker_uses_only_the_dedicated_queue():
    """Pool analysis must neither wait behind nor prewarm render work."""
    from app.services.queue_state import RENDER_WORKER_QUEUES

    assert _fly_toml_queues("autoplace") == {"autoplace-jobs"}
    assert _fly_toml_queues("autoplace").isdisjoint(RENDER_WORKER_QUEUES)


def test_real_celery_app_exposes_the_introspection_surface():
    """The gate reads celery's `app.amqp.queues.consume_from` — an internal
    surface the MagicMock senders above can't defend. Pin it against the
    REAL celery app so a celery upgrade that renames/moves the attribute
    fails CI instead of silently fail-closing the prewarm on the render
    worker (whose only symptom would be one stdout line + a 3-5s lazy load)."""
    from app.worker import celery_app

    consume_from = celery_app.amqp.queues.consume_from
    assert hasattr(consume_from, "keys")
    # Default app (no -Q select) must include the default render queue.
    assert "celery" in set(consume_from.keys())


# ── _prewarm_clip_font_matcher (hook wiring: gate short-circuits the thread) ─


@patch("app.worker.threading.Thread")
def test_maintenance_worker_never_spawns_prewarm_thread(mock_thread: MagicMock):
    from app.worker import _prewarm_clip_font_matcher

    _prewarm_clip_font_matcher(sender=_sender_consuming("maintenance"))

    mock_thread.assert_not_called()


@patch("app.worker.threading.Thread")
def test_render_worker_spawns_prewarm_thread(mock_thread: MagicMock):
    from app.worker import _prewarm_clip_font_matcher

    _prewarm_clip_font_matcher(sender=_sender_consuming("celery", "plan-jobs", "overlay-jobs"))

    mock_thread.assert_called_once()
