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

import threading
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


def test_age_at_threshold_is_not_retrying() -> None:
    # Strict > contract: age at (just under, for wall-clock drift) the stale
    # threshold is still healthy — retrying starts only PAST it.
    assert (
        _compute_retrying(_job("rendering", settings.render_heartbeat_stale_after_s - 5)) is False
    )


def test_beacon_older_than_redelivery_window_stops_claiming_retry() -> None:
    # Past visibility_timeout + slack no acks_late redelivery can be pending
    # (a hard time_limit SIGKILL ACKS the message) — "retrying automatically"
    # would be a false promise forever; the stale-job reaper owns it from here.
    from app.worker import celery_app

    visibility_timeout = int(celery_app.conf.broker_transport_options["visibility_timeout"])
    beyond_window = visibility_timeout + settings.render_heartbeat_stale_after_s + 300 + 60
    assert _compute_retrying(_job("rendering", beyond_window)) is False


async def _status_response_for(monkeypatch: pytest.MonkeyPatch, *, beacon_age_s: float | None):
    """Drive the REAL status route with a stale/fresh beacon (mirrors the
    archetype_fallback route-test pattern). Pins the response wiring — the
    Pydantic field defaults to False, so dropping the retrying=... kwarg from
    the route would pass every helper-only test while killing the feature."""
    import types
    import uuid as _uuid
    from datetime import datetime as _dt

    import app.routes.generative_jobs as gj
    import app.services.phase_baselines as pb

    beacon = _dt.now(UTC) - timedelta(seconds=beacon_age_s) if beacon_age_s is not None else None
    job = types.SimpleNamespace(
        id=_uuid.uuid4(),
        status="rendering",
        mode="generative",
        assembly_plan={"variants": []},
        error_detail=None,
        created_at=_dt.now(UTC),
        updated_at=_dt.now(UTC),
        all_candidates={},
        current_phase="assemble",
        phase_log=None,
        started_at=_dt.now(UTC),
        finished_at=None,
        worker_heartbeat_at=beacon,
    )

    async def _load(job_id, db, user, allowed_modes=None):
        return job

    monkeypatch.setattr(gj, "_load_generative_job", _load)
    monkeypatch.setattr(pb, "get_baselines", lambda mode: None)
    return await gj.get_generative_job_status(str(job.id), current_user=object(), db=object())


async def test_status_route_reports_retrying_for_stale_beacon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = await _status_response_for(monkeypatch, beacon_age_s=_stale_age())
    assert resp.retrying is True


async def test_status_route_fresh_beacon_not_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _status_response_for(monkeypatch, beacon_age_s=5)
    assert resp.retrying is False


async def test_status_route_null_beacon_not_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _status_response_for(monkeypatch, beacon_age_s=None)
    assert resp.retrying is False


def test_interval_misconfig_floors_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator raising the beat interval above the stale threshold must not
    # flap `retrying` between beats of a healthy render: the effective
    # threshold floors at two missed beats.
    monkeypatch.setattr(settings, "render_heartbeat_interval_s", 300)
    monkeypatch.setattr(settings, "render_heartbeat_stale_after_s", 150)
    assert _compute_retrying(_job("rendering", 400)) is False  # one beat gap — healthy
    assert _compute_retrying(_job("rendering", 700)) is True  # two+ missed beats — dead


def _heartbeat_threads() -> list[threading.Thread]:
    return [
        t for t in threading.enumerate() if t.name.startswith("job-heartbeat-") and t.is_alive()
    ]


def test_job_heartbeat_beats_on_entry_and_stops_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    beats: list[str] = []
    monkeypatch.setattr(job_phases, "beat_heartbeat", lambda job_id: beats.append(str(job_id)))

    with job_phases.job_heartbeat("11111111-2222-3333-4444-555555555555"):
        # The entry beat is synchronous — a redelivered attempt must clear the
        # stale flag BEFORE the slow ingest work starts, not 30s later.
        assert len(beats) >= 1
        # The loop thread is genuinely running while the body executes.
        assert _heartbeat_threads()

    # Thread-liveness (not beat-count — the ≥30s interval makes a count
    # assertion vacuous): exiting the context must terminate the loop thread.
    assert _heartbeat_threads() == []
