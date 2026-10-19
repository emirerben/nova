"""Completion acknowledges a durable check; only a fenced worker may publish."""

# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.kria.device_render import DeviceExportCompleteBody
from app.routes import device_render as routes
from app.services.device_render import device_record, save_device_record
from tests.routes.test_device_render import body, fixture, mock_storage, prepared  # noqa: F401


def checked_completion(fixture, payload):
    """HTTP ack followed by a worker delivery; DB/storage transports may be doubles."""
    from app.tasks import export_verification

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(export_verification, "enqueue", lambda *args: None)
        ack = fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/complete",
            json=payload,
            headers={"X-Kria-Client-Protocol": "13"},
        )
    if ack.status_code != 202:
        return ack
    parsed = DeviceExportCompleteBody.model_validate(payload)
    record = device_record(fixture.job, parsed.identity.variant_id)
    record["attempts"][str(parsed.attempt_id)]["verification_task"].update(
        state="running",
        token="worker-token",
        lease_until=(datetime.now(UTC) + timedelta(minutes=6)).isoformat(),
    )
    save_device_record(fixture.job, parsed.identity.variant_id, record)

    async def deliver():
        try:
            result = await routes._complete_device_export(
                fixture.job.id,
                parsed,
                fixture.user.id,
                fixture.db,
                verification_token="worker-token",
            )
            return SimpleNamespace(status_code=200, text=result.model_dump_json())
        except HTTPException as exc:
            return SimpleNamespace(status_code=exc.status_code, text=str(exc.detail))

    return fixture.client.portal.call(deliver)


def test_completion_does_no_media_work_and_replays_the_same_outbox(fixture, monkeypatch):
    mock_storage(fixture, monkeypatch)
    attempt, _ = prepared(fixture)
    record = device_record(fixture.job, "first")
    record["actual_export_policy"] = {"version": 1}
    save_device_record(fixture.job, "first", record)
    decode = MagicMock(side_effect=AssertionError("API must not decode media"))
    metadata = MagicMock(side_effect=AssertionError("API must not contact storage"))
    monkeypatch.setattr(routes, "_verify_export", decode)
    monkeypatch.setattr(routes.storage, "object_metadata", metadata)
    from app.tasks import export_verification

    dispatch = MagicMock(side_effect=ConnectionError("broker unavailable"))
    monkeypatch.setattr(export_verification.verify_device_export, "apply_async", dispatch)
    for _ in range(2):
        response = fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/complete",
            json=body(fixture, attempt),
            headers={"X-Kria-Client-Protocol": "13"},
        )
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "checking"
    pending = device_record(fixture.job, "first")["attempts"][attempt]["verification_task"]
    assert pending["state"] == "queued"
    assert pending["runs"] == 0
    assert "output_url" not in fixture.job.assembly_plan["variants"][0]
    decode.assert_not_called()
    metadata.assert_not_called()
    assert fixture.db.commit.await_count >= 1


@pytest.mark.parametrize("protocol", [None, "12"])
def test_older_completion_cannot_start_background_check(fixture, monkeypatch, protocol):
    mock_storage(fixture, monkeypatch)
    attempt, _ = prepared(fixture)
    record = device_record(fixture.job, "first")
    record["actual_export_policy"] = {"version": 1}
    save_device_record(fixture.job, "first", record)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete",
        json=body(fixture, attempt),
        headers={"X-Kria-Client-Protocol": protocol} if protocol else {},
    )
    assert response.status_code == 426
