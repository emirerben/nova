"""KRI-823: the project drawer reads the CURRENT device delivery, not a stale "Rendering".

A device-delivery failure never changes ``job.status`` (it stays ``awaiting_device``), so
the drawer -- which reads the thread list projection -- used to keep saying "Rendering"
while the detail card said "needs attention". Both thread projections now carry each
variant's ``delivery_phase``, fenced to the variant's current render generation.

Each case drives the real device-render route that writes the failure, then reads the
list projection (``list_threads``) and the full projection (``_job_projection``).

Failure modes, written before the code:

* a refused publication on an EDIT (the approved sources changed under the export: one
  of the refusals that still withholds a finished video) marks only the device record;
  the variant stays ``awaiting_device`` and the drawer keeps saying Rendering
* a phone-reported failure marks the variant, but ``job.status`` stays awaiting_device
* an expired repair permission marks only the record (same record-only class)
* a late failure from an OLD generation outranks a newer published export
* a retry does not move the delivery back to a live phase; a publish does not read ready
* the previous accepted artifact (video/poster/URL) drops out while a replacement fails
* a cloud job (no device record) gains a phase or changes shape
* a malformed device record 500s the whole project list
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routes import creation_threads as threads_routes
from app.routes import generative_jobs as gj
from app.services import device_render as svc
from app.services.device_render import (
    DEVICE_RENDER_FIELD,
    device_record,
    device_status,
    save_device_record,
)
from tests.routes import test_device_render as _device_routes
from tests.routes.test_background_export_verification import checked_completion
from tests.routes.test_device_render import body, failure_body, routes
from tests.routes.test_device_render_last_good import VARIANT, _first_render_syncing, _syncing

fixture = _device_routes.fixture


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)


async def _drawer_row(job) -> dict:
    """The list projection the drawer reads, for one thread pointing at ``job``."""
    for name in ("current_phase", "failure_reason", "error_detail"):
        if not hasattr(job, name):
            setattr(job, name, None)
    now = datetime.now(UTC)
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        runtime_version=1,
        title="Project",
        status="active",
        revision=7,
        state={},
        content_plan_id=None,
        active_plan_item_id=None,
        active_creator_agent_session_id=None,
        active_job_id=job.id,
        created_at=now,
        updated_at=now,
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_Rows([thread]), _Rows([job])])
    rows = await threads_routes.list_threads(
        SimpleNamespace(id=job.user_id), db, include_archived=False, limit=20, cursor=None
    )
    assert len(rows) == 1
    return rows[0].job


def _full_projection(job, monkeypatch) -> dict:
    monkeypatch.setattr(
        gj.storage, "signed_get_url", lambda path, **_: f"https://signed.example/{path}"
    )
    return threads_routes._job_projection(job)


def _refuse_on_changed_sources(job) -> None:
    """Pin a source digest the job no longer has: that refusal still withholds the export.

    (Output checks and a changed approved contract are advisory and publish.)"""
    if not hasattr(job, "content_plan_item_id"):
        job.content_plan_item_id = None  # no plan item: no automatic-repair grant to bind
    record = device_record(job, VARIANT)
    record["actual_export_policy"] = {"version": 1, "source_receipts_digest": "stale-sources"}
    save_device_record(job, VARIANT, record)


def _phase(projection: dict, variant_id: str = VARIANT) -> str | None:
    return next(v for v in projection["variants"] if v["variant_id"] == variant_id)[
        "delivery_phase"
    ]


@pytest.mark.asyncio
async def test_a_refused_edit_publication_reads_needs_attention_and_keeps_the_last_artifact(
    fixture, monkeypatch
):
    attempt = _syncing(fixture, monkeypatch)
    job = fixture.job
    _refuse_on_changed_sources(job)
    job.status = "awaiting_device"

    response = checked_completion(fixture, body(fixture, attempt))
    assert response.status_code == 422, response.text

    row = await _drawer_row(job)
    # The job and the variant never moved: only the device record knows.
    assert row["status"] == "awaiting_device"
    assert row["variants"][0]["render_status"] == "awaiting_device"
    assert _phase(row) == "needs_attention"

    full = _full_projection(job, monkeypatch)
    assert _phase(full) == "needs_attention"
    variant = full["variants"][0]
    # The last accepted output stays playable while the replacement failed.
    assert variant["output_url"]
    assert variant["video_path"]
    # Response copies only: the stored variant never gains the projected field.
    assert "delivery_phase" not in job.assembly_plan["variants"][0]


@pytest.mark.asyncio
async def test_a_phone_reported_failure_then_retry_tracks_the_drawer(fixture, monkeypatch):
    job = fixture.job
    job.status = "awaiting_device"
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=job))

    assert _phase(await _drawer_row(job), "first") == "awaiting_device"

    failed = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/failures", json=failure_body(fixture)
    )
    assert failed.status_code == 200, failed.text
    row = await _drawer_row(job)
    assert row["status"] == "awaiting_device"
    assert row["variants"][0]["render_status"] == "needs_attention"
    assert _phase(row, "first") == "needs_attention"

    fixture.db.execute.return_value = _device_routes.scalar(None)
    retried = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/retry",
        json={"identity": fixture.request.identity.model_dump(mode="json")},
    )
    assert retried.status_code == 200, retried.text
    assert _phase(await _drawer_row(job), "first") == "awaiting_device"


@pytest.mark.asyncio
async def test_an_expired_repair_permission_reads_needs_attention(fixture):
    job = fixture.job
    job.status = "awaiting_device"
    record = device_record(job, "first")
    record["repair_authority"] = {
        "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    }
    save_device_record(job, "first", record)
    status = device_status(job, "first")
    # An automatic-repair delivery (the only kind whose permission can expire).
    status = status.model_copy(
        update={"request": status.request.model_copy(update={"recovery": object()})}
    )

    assert await routes._expire_repair_permission(fixture.db, job, record, status) is True

    row = await _drawer_row(job)
    assert row["variants"][0]["render_status"] != "needs_attention"  # record-only writer
    assert _phase(row, "first") == "needs_attention"


@pytest.mark.asyncio
async def test_a_late_failure_from_an_old_generation_never_beats_a_newer_publish(
    fixture, monkeypatch
):
    attempt = _syncing(fixture, monkeypatch)
    job = fixture.job
    published = fixture.client.post(
        f"/me/jobs/{job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert published.status_code == 200, published.text
    assert _phase(await _drawer_row(job)) == "published"

    # A failure lands on a record whose generation is no longer the variant's.
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "needs_attention"
    record["base_generation"] = "an-older-generation"
    save_device_record(job, VARIANT, record)

    assert _phase(await _drawer_row(job)) is None
    assert _phase(_full_projection(job, monkeypatch)) is None


@pytest.mark.asyncio
async def test_a_refused_first_render_reads_needs_attention_with_no_artifact(fixture, monkeypatch):
    attempt = _first_render_syncing(fixture, monkeypatch)
    job = fixture.job
    _refuse_on_changed_sources(job)
    response = checked_completion(fixture, body(fixture, attempt))
    assert response.status_code == 422, response.text

    row = await _drawer_row(job)
    assert row["variants"][0]["render_status"] == "needs_attention"
    assert _phase(row) == "needs_attention"


@pytest.mark.asyncio
async def test_a_cloud_job_and_a_malformed_record_project_no_phase(monkeypatch):
    cloud = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
        current_phase=None,
        failure_reason=None,
        error_detail=None,
        assembly_plan={"variants": [{"variant_id": "v1", "render_status": "ready"}]},
    )
    row = await _drawer_row(cloud)
    assert row["variants"] == [
        {"variant_id": "v1", "render_status": "ready", "delivery_phase": None}
    ]

    cloud.assembly_plan[DEVICE_RENDER_FIELD] = {"v1": {"status": "garbage", "base_generation": 4}}
    cloud.assembly_plan["variants"][0]["render_generation_id"] = "4"
    assert _phase(await _drawer_row(cloud), "v1") is None
    assert svc.variant_delivery_phase(cloud, None) is None
