"""HTTP fences for strict whole-file export limits."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.routes import device_render as routes
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.device_render import device_record, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.routes.test_device_render import (
    body,
    mock_storage,
    prepared,
    scalar,
)
from tests.routes.test_device_render import (
    fixture as _device_render_fixture_source,
)
from tests.services.test_strict_export_duration import assembly_for, contract, recipe


@pytest.fixture
def device_render_fixture(monkeypatch):
    yield from _device_render_fixture_source.__wrapped__(monkeypatch)


def _install_strict_device_export(fixture) -> None:
    value = contract("Keep it under 40 seconds")
    fixture.job.assembly_plan = {
        **assembly_for(value, "Keep it under 40 seconds"),
        "variants": [{"variant_id": "first", "render_generation_id": "t1"}],
        PHONE_SOURCES_FIELD: [_binding(duration_s=37.976494594).model_dump(mode="json")],
    }
    fixture.request = make_device_request(
        job_id=fixture.job.id, variant_id="first", revision=1, recipe=recipe()
    )
    assert pin_device_request(fixture.job, fixture.request, base_generation="t1")


def _live_upload_reservation(fixture) -> None:
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )


def test_status_exposes_strict_maximum_without_changing_the_persisted_request(
    device_render_fixture, monkeypatch
):
    fixture = device_render_fixture
    _install_strict_device_export(fixture)
    monkeypatch.setattr(routes, "_owned_job", AsyncMock(return_value=fixture.job))

    response = fixture.client.get(f"/me/jobs/{fixture.job.id}/device-render?variant_id=first")

    assert response.status_code == 200, response.text
    assert response.json()["request"]["max_export_duration_s"] == 40
    persisted = DeviceRenderStatus.model_validate(device_record(fixture.job, "first")["status"])
    assert "max_export_duration_s" not in persisted.request.model_dump()


def test_strict_completion_persists_the_measured_valid_file_duration(
    device_render_fixture, monkeypatch
):
    fixture = device_render_fixture
    _install_strict_device_export(fixture)
    attempt, _ = prepared(fixture)
    mock_storage(fixture, monkeypatch)
    _live_upload_reservation(fixture)

    def verify(*_args, export_measurements, max_export_duration_s, **_kwargs):
        assert max_export_duration_s == 40
        export_measurements["measured_export_duration_s"] = 39.576667
        return None

    monkeypatch.setattr(routes, "_verify_export", verify)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )

    assert response.status_code == 200, response.text
    variant = fixture.job.assembly_plan["variants"][0]
    assert variant["duration_s"] == 39.576667
    assert variant["measured_export_generation_id"] == attempt


def test_strict_completion_refusal_does_not_publish_a_video(device_render_fixture, monkeypatch):
    fixture = device_render_fixture
    _install_strict_device_export(fixture)
    attempt, _ = prepared(fixture)
    mock_storage(fixture, monkeypatch)
    _live_upload_reservation(fixture)
    monkeypatch.setattr(
        routes,
        "_verify_export",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CreatorRenderContractError("The export must stay under 40 seconds.")
        ),
    )

    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )

    assert response.status_code == 422
    variant = fixture.job.assembly_plan["variants"][0]
    assert "video_path" not in variant
    assert device_record(fixture.job, "first")["contract_decline"]["stage"] == "actual_export"


def test_later_edit_during_strict_measurement_cannot_publish_stale_output(
    device_render_fixture, monkeypatch
):
    fixture = device_render_fixture
    _install_strict_device_export(fixture)
    attempt, _ = prepared(fixture)
    mock_storage(fixture, monkeypatch)
    _live_upload_reservation(fixture)

    def verify(*_args, export_measurements, **_kwargs):
        export_measurements["measured_export_duration_s"] = 39.576667
        fixture.job.assembly_plan["variants"][0]["render_generation_id"] = "later-edit"
        return None

    monkeypatch.setattr(routes, "_verify_export", verify)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )

    assert response.status_code == 409
    variant = fixture.job.assembly_plan["variants"][0]
    assert "video_path" not in variant
    assert "measured_export_duration_s" not in variant
    retry = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert retry.status_code == 409
    assert "measured_export_duration_s" not in fixture.job.assembly_plan["variants"][0]
