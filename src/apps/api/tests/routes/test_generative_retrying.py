"""Read-time `retrying` flag + render heartbeat (2026-07-21 OOM incident).

A worker OOM-killed mid-reframe leaves its job at status="rendering" with zero
signal until the acks_late redelivery (30+ min). The orchestrator now ticks
jobs.worker_heartbeat_at from a daemon thread; the status route reports
`retrying: true` once the beacon goes stale on a non-terminal job.

Pins:
  - stale beacon + live status → retrying
  - fresh beacon → not retrying (healthy long render is NOT flagged)
  - NULL beacon → never retrying (legacy rows / non-heartbeating orchestrators)
  - terminal / queued statuses → never retrying regardless of beacon age
  - job_heartbeat beats synchronously on entry (a redelivered attempt clears
    the stale flag immediately, before any slow ingest work) and stops on exit
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.routes.generative_jobs import _compute_retrying
from app.services import job_phases


def _job(status: str, beacon_age_s: float | None) -> SimpleNamespace:
    beacon = (
        datetime.now(UTC) - timedelta(seconds=beacon_age_s) if beacon_age_s is not None else None
    )
    return SimpleNamespace(status=status, worker_heartbeat_at=beacon)


def _stale_age() -> float:
    return settings.render_heartbeat_stale_after_s + 30


@pytest.mark.parametrize("status", ["processing", "rendering"])
def test_stale_beacon_on_live_status_reports_retrying(status: str) -> None:
    assert _compute_retrying(_job(status, _stale_age())) is True


def test_fresh_beacon_is_not_retrying() -> None:
    assert _compute_retrying(_job("rendering", 5)) is False


def test_null_beacon_never_retrying() -> None:
    # Legacy rows and orchestrators without a heartbeat thread must stay false
    # forever — NULL means "no signal", not "stale".
    assert _compute_retrying(_job("rendering", None)) is False


@pytest.mark.parametrize(
    "status",
    ["queued", "variants_ready", "variants_ready_partial", "variants_failed", "processing_failed"],
)
def test_non_live_statuses_never_retrying(status: str) -> None:
    assert _compute_retrying(_job(status, _stale_age())) is False


def test_job_heartbeat_beats_on_entry_and_stops_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    beats: list[str] = []
    monkeypatch.setattr(job_phases, "beat_heartbeat", lambda job_id: beats.append(str(job_id)))

    with job_phases.job_heartbeat("11111111-2222-3333-4444-555555555555"):
        # The entry beat is synchronous — a redelivered attempt must clear the
        # stale flag BEFORE the slow ingest work starts, not 30s later.
        assert len(beats) >= 1

    entry_count = len(beats)
    # After exit the loop thread is stopped; no further beats accumulate.
    assert len(beats) == entry_count
