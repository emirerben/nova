"""KRI-734 publication fence: rejection never replaces a playable artifact."""

# ruff: noqa: F811 -- pytest registers the imported shared HTTP fixture

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.routes import device_render as routes
from app.services.device_render import device_record, save_device_record
from tests.routes.test_device_render import (  # noqa: F401
    body,
    fixture,
    mock_storage,
    prepared,
    scalar,
)


@pytest.mark.parametrize("outcome", ["rejected", "partial", "unsupported"])
@pytest.mark.parametrize("replacement", [False, True])
def test_unchecked_export_cannot_publish_or_replace_last_good(
    fixture, monkeypatch, outcome, replacement
):
    mock_storage(fixture, monkeypatch)
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    attempt, _ = prepared(fixture)
    if replacement:
        fixture.job.assembly_plan["variants"][0].update(
            ok=True,
            render_status="ready",
            video_path="last-good.mp4",
            poster_path="last-good.jpg",
            output_url="last-good-url",
        )
    last_good = copy.deepcopy(fixture.job.assembly_plan["variants"][0])
    record = device_record(fixture.job, "first")
    reservation = record["attempts"][attempt]
    record["actual_export_policy"] = {"version": 1}
    save_device_record(fixture.job, "first", record)

    def verify(*args, **kwargs):
        if "verification_report" in kwargs:
            kwargs["verification_report"].update(
                status=outcome, checks=[{"kind": "picture", "status": "failed"}]
            )

    monkeypatch.setattr(routes, "_verify_export", verify)
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: type(
            "M", (), dict(size=reservation["size"], content_type="video/mp4", generation="g")
        )(),
    )
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert response.status_code == 422, response.text
    record = device_record(fixture.job, "first")
    assert record["status"]["phase"] == "needs_attention"
    assert record["attempts"][attempt]["actual_export_verification"]["status"] == outcome
    if replacement:
        assert fixture.job.assembly_plan["variants"][0] == last_good
    assert (
        fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
        ).status_code
        == 409
    )


def test_published_fast_path_requires_its_own_successful_report(fixture, monkeypatch):
    mock_storage(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    record.update(
        actual_export_policy={"version": 1},
        published_attempt="00000000-0000-0000-0000-000000000734",
    )
    record["status"]["phase"] = "published"
    record["attempts"] = {
        record["published_attempt"]: {"actual_export_verification": {"status": "rejected"}}
    }
    save_device_record(fixture.job, "first", record)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete",
        json=body(fixture, record["published_attempt"]),
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("protocol,expected", [(11, 426), (12, 200)])
def test_required_output_check_is_declared_and_old_share_ui_is_refused(
    fixture, monkeypatch, protocol, expected
):
    mock_storage(fixture, monkeypatch)
    record = device_record(fixture.job, "first")
    record["actual_export_policy"] = {"version": 1}
    save_device_record(fixture.job, "first", record)
    response = fixture.client.get(
        f"/me/jobs/{fixture.job.id}/device-render?variant_id=first",
        headers={"X-Kria-Client-Protocol": str(protocol)},
    )
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["output_verification_required"] is True
