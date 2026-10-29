"""KRI-734 publication fence: rejection never replaces a playable artifact."""

# ruff: noqa: F811 -- pytest registers the imported shared HTTP fixture

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.routes import device_render as routes
from app.services.device_render import (
    approved_export_sources,
    device_record,
    export_source_digest,
    save_device_record,
)
from tests.routes.test_background_export_verification import checked_completion
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
    record["actual_export_policy"] = {
        "version": 1,
        "source_receipts_digest": export_source_digest(
            approved_export_sources(fixture.job, fixture.request)
        ),
    }
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
    response = checked_completion(fixture, body(fixture, attempt))
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


@pytest.mark.parametrize("protocol,expected", [(11, 426), (12, 426), (13, 200)])
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


@pytest.mark.parametrize("stale", ["source_receipts", "identity"])
def test_measured_output_with_stale_authority_never_keeps_a_checked_receipt(
    fixture, monkeypatch, stale
):
    mock_storage(fixture, monkeypatch)
    attempt, _ = prepared(fixture)
    record = device_record(fixture.job, "first")
    digest = export_source_digest(approved_export_sources(fixture.job, fixture.request))
    record["actual_export_policy"] = {
        "version": 1,
        "source_receipts_digest": "a" * 64 if stale == "source_receipts" else digest,
    }
    reservation = record["attempts"][attempt]
    save_device_record(fixture.job, "first", record)
    identity = fixture.request.identity.model_dump(mode="json")
    if stale == "identity":
        identity["recipe_revision"] += 1

    def verify(*args, **kwargs):
        kwargs["verification_report"].update(
            status="checked",
            identity=identity,
            verifier_version="phone-simple-cut-v1",
            sha256=reservation["sha256"],
            file_size_bytes=reservation["size"],
            checks=[{"kind": "duration", "status": "passed"}],
        )

    monkeypatch.setattr(routes, "_verify_export", verify)
    response = checked_completion(fixture, body(fixture, attempt))
    assert response.status_code == 422, response.text
    stored = device_record(fixture.job, "first")
    assert stored["attempts"][attempt]["actual_export_verification"]["status"] == "rejected"
    if stale == "source_receipts":
        # An authority drift is not "this iPhone can't render it" (S3-j): it is its
        # own code, and that code still asks for a change, never an exact retry loop.
        assert stored["status"]["reason_code"] == "contract_changed", (
            "Changed authority needs re-approval, not an exact retry loop"
        )


@pytest.mark.parametrize(
    "pinned,reported,expected",
    [
        # KRI-810: a request pinned before the verifier repair (Troya job
        # 57602026 was pinned to v2) publishes bytes the repaired verifier checks.
        ("phone-simple-cut-v2", "phone-simple-cut-v3", 200),
        ("phone-simple-cut-v3", "phone-simple-cut-v3", 200),
        # KRI-1042: a request pinned to v3 (Lisbon job 3daccc8c) publishes bytes
        # the v4 text proof checks.
        ("phone-simple-cut-v3", "phone-simple-cut-v4", 200),
        ("phone-simple-cut-v4", "phone-simple-cut-v4", 200),
        # An older verifier never satisfies a pin, nor a newer pin.
        ("phone-simple-cut-v2", "phone-simple-cut-v1", 422),
        ("phone-simple-cut-v3", "phone-simple-cut-v2", 422),
        ("phone-simple-cut-v4", "phone-simple-cut-v3", 422),
    ],
)
def test_pinned_verifier_accepts_its_own_or_the_current_verifier_only(
    fixture, monkeypatch, pinned, reported, expected
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
    record = device_record(fixture.job, "first")
    record["actual_export_policy"] = {
        "version": 1,
        "verifier_version": pinned,
        "source_receipts_digest": export_source_digest(
            approved_export_sources(fixture.job, fixture.request)
        ),
    }
    reservation = record["attempts"][attempt]
    save_device_record(fixture.job, "first", record)

    def verify(*args, **kwargs):
        kwargs["verification_report"].update(
            status="checked",
            identity=fixture.request.identity.model_dump(mode="json"),
            verifier_version=reported,
            sha256=reservation["sha256"],
            file_size_bytes=reservation["size"],
            checks=[{"kind": "duration", "status": "passed"}],
        )

    monkeypatch.setattr(routes, "_verify_export", verify)
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: SimpleNamespace(
            size=reservation["size"], content_type="video/mp4", generation="g"
        ),
    )
    response = checked_completion(fixture, body(fixture, attempt))
    assert response.status_code == expected, response.text
    from app.services.request_following_outcomes import delivery_counts

    capture = {
        "sections": {
            f"job-{fixture.job.id}": {
                "id": str(fixture.job.id),
                "status": fixture.job.status,
                "assembly_plan": fixture.job.assembly_plan,
            }
        }
    }
    counts = delivery_counts(capture)
    assert counts["verified_device_exports"] == int(expected == 200)
    stored = device_record(fixture.job, "first")
    if expected == 200:
        assert stored["status"]["phase"] == "published"
        assert stored["published_attempt"] == attempt
    else:
        assert stored["status"]["phase"] != "published"
