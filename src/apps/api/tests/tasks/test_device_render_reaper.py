"""Tests for app.tasks.device_render_reaper — the stale device-render sweep.

Mocks `sync_session` (mirrors tests/tasks/test_reaper.py's discipline) so the
sweep's discovery + per-record staleness logic runs offline against a single
fake `Job`-shaped object per test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.services.device_render import device_record, pin_device_request, save_device_record
from app.tasks import device_render_reaper as reaper

_RECIPE = EditRecipeV1.model_validate_json(
    (Path(__file__).resolve().parents[1] / "fixtures/kria_edit_recipe_v1.json").read_text()
)


def _pinned_job(*, pinned_minutes_ago: float = 0.0) -> SimpleNamespace:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status="awaiting_device",
        updated_at=datetime.now(UTC) - timedelta(minutes=pinned_minutes_ago),
        assembly_plan={
            "variants": [{"variant_id": "original_text", "render_generation_id": "approved"}]
        },
    )
    request = make_device_request(
        job_id=job.id, variant_id="original_text", revision=1, recipe=_RECIPE
    )
    pin_device_request(job, request, base_generation="approved")
    record = device_record(job, "original_text")
    record["pinned_at"] = (datetime.now(UTC) - timedelta(minutes=pinned_minutes_ago)).isoformat()
    save_device_record(job, "original_text", record)
    return job


def _run_reaper(job: SimpleNamespace, *, stale_after_s: int) -> dict[str, int]:
    session = MagicMock()
    ids_result = MagicMock()
    ids_result.scalars.return_value.all.return_value = [job.id]
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = job
    session.execute.side_effect = [ids_result, job_result]
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    with patch("app.tasks.device_render_reaper.sync_session", return_value=ctx):
        return reaper.reap_stale_device_renders.run(stale_after_s=stale_after_s)


def test_reaps_a_record_stale_past_pinned_at():
    job = _pinned_job(pinned_minutes_ago=120)
    summary = _run_reaper(job, stale_after_s=3600)
    assert summary == {"reaped_jobs": 1, "reaped_records": 1, "scanned_jobs": 1}
    record = device_record(job, "original_text")
    assert record["status"]["phase"] == "needs_attention"
    assert record["status"]["reason_code"] == "unknown"
    assert record["reaped_at"]
    assert job.assembly_plan["variants"][0]["render_status"] == "needs_attention"
    assert job.assembly_plan["variants"][0]["ok"] is False
    assert job.failure_reason == "device_render_stale"
    # job.status is left untouched — the recipe stays pinned, same contract
    # as the client-driven failure-report endpoint.
    assert job.status == "awaiting_device"


def test_skips_a_fresh_unpolled_record():
    job = _pinned_job(pinned_minutes_ago=1)
    summary = _run_reaper(job, stale_after_s=3600)
    assert summary == {"reaped_jobs": 0, "reaped_records": 0, "scanned_jobs": 1}
    assert device_record(job, "original_text")["status"]["phase"] == "awaiting_device"


def test_recent_poll_overrides_a_stale_pin():
    job = _pinned_job(pinned_minutes_ago=120)
    record = device_record(job, "original_text")
    record["last_polled_at"] = datetime.now(UTC).isoformat()
    save_device_record(job, "original_text", record)
    summary = _run_reaper(job, stale_after_s=3600)
    assert summary["reaped_records"] == 0
    assert device_record(job, "original_text")["status"]["phase"] == "awaiting_device"


def test_syncing_phase_with_recent_poll_is_not_reaped():
    job = _pinned_job(pinned_minutes_ago=120)
    record = device_record(job, "original_text")
    record["status"]["phase"] = "syncing"
    record["last_polled_at"] = datetime.now(UTC).isoformat()
    save_device_record(job, "original_text", record)
    summary = _run_reaper(job, stale_after_s=3600)
    assert summary["reaped_records"] == 0
    assert device_record(job, "original_text")["status"]["phase"] == "syncing"


def test_published_record_is_never_reaped():
    job = _pinned_job(pinned_minutes_ago=120)
    record = device_record(job, "original_text")
    record["status"]["phase"] = "published"
    save_device_record(job, "original_text", record)
    summary = _run_reaper(job, stale_after_s=3600)
    assert summary["reaped_records"] == 0
    assert device_record(job, "original_text")["status"]["phase"] == "published"


def test_already_reaped_record_is_not_reaped_twice():
    job = _pinned_job(pinned_minutes_ago=120)
    first = _run_reaper(job, stale_after_s=3600)
    assert first["reaped_records"] == 1
    reaped_at = device_record(job, "original_text")["reaped_at"]
    job.assembly_plan["variants"][0]["render_status"] = "resurrected_by_test"
    second = _run_reaper(job, stale_after_s=3600)
    assert second["reaped_records"] == 0
    assert device_record(job, "original_text")["reaped_at"] == reaped_at
    # The second sweep must not have touched the variant a second time.
    assert job.assembly_plan["variants"][0]["render_status"] == "resurrected_by_test"


def test_uses_settings_default_when_stale_after_s_omitted(monkeypatch):
    job = _pinned_job(pinned_minutes_ago=120)
    monkeypatch.setattr(reaper.settings, "device_render_stale_after_s", 60)
    session = MagicMock()
    ids_result = MagicMock()
    ids_result.scalars.return_value.all.return_value = [job.id]
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = job
    session.execute.side_effect = [ids_result, job_result]
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    with patch("app.tasks.device_render_reaper.sync_session", return_value=ctx):
        summary = reaper.reap_stale_device_renders.run()
    assert summary["reaped_records"] == 1
