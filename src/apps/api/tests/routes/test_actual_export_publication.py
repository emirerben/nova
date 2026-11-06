"""Publication fence: output checks are advisory; only mismatched authority withholds."""

# ruff: noqa: F811 -- pytest registers the imported shared HTTP fixture

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
def test_failed_checks_never_withhold_a_finished_export(fixture, monkeypatch, outcome, replacement):
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
                status=outcome,
                checks=[
                    {"kind": "picture", "status": "passed"},
                    {"kind": "text", "status": "failed", "reason": "copy_not_read"},
                ],
            )
        return "new-poster.jpg"

    monkeypatch.setattr(routes, "_verify_export", verify)
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: type(
            "M", (), dict(size=reservation["size"], content_type="video/mp4", generation="g")
        )(),
    )
    response = checked_completion(fixture, body(fixture, attempt))
    assert response.status_code == 200, response.text
    record = device_record(fixture.job, "first")
    assert record["status"]["phase"] == "published"
    assert record["published_attempt"] == attempt
    stored = record["attempts"][attempt]["actual_export_verification"]
    assert stored["status"] == outcome, "the verdict is still recorded"
    assert any(c["kind"] == "text" and c["status"] == "failed" for c in stored["checks"])
    variant = fixture.job.assembly_plan["variants"][0]
    assert variant["ok"] is True and variant["render_status"] == "ready"
    assert variant["video_path"] == reservation["path"]
    assert variant["poster_path"] == "new-poster.jpg"
    assert variant["output_url"], "the signed playback URL is minted"
    assert variant["actual_export_verification"]["status"] == outcome
    # A redelivered completion is idempotent and never needs a passing report.
    assert (
        fixture.client.post(
            f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
        ).status_code
        == 200
    )


def test_checks_that_could_not_run_publish_the_finished_export_unverified(fixture, monkeypatch):
    """The checker being down for the whole recheck window never withholds the video."""
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
    reservation = record["attempts"][attempt]
    record["actual_export_policy"] = {
        "version": 1,
        "source_receipts_digest": export_source_digest(
            approved_export_sources(fixture.job, fixture.request)
        ),
    }
    save_device_record(fixture.job, "first", record)

    def never_measured(*args, **kwargs):
        raise AssertionError("an unverified publish must not run the checks")

    monkeypatch.setattr(routes, "_verify_export", never_measured)
    monkeypatch.setattr(routes, "_poster_of_unmeasured_export", lambda *a: "unmeasured.jpg")
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: type(
            "M", (), dict(size=reservation["size"], content_type="video/mp4", generation="g")
        )(),
    )
    response = checked_completion(fixture, body(fixture, attempt), unverified=True)
    assert response.status_code == 200, response.text
    record = device_record(fixture.job, "first")
    assert record["status"]["phase"] == "published"
    stored = record["attempts"][attempt]["actual_export_verification"]
    assert stored["status"] == "partial"
    assert any(c["kind"] == "verification" for c in stored["checks"])
    variant = fixture.job.assembly_plan["variants"][0]
    assert variant["ok"] is True and variant["video_path"] == reservation["path"]
    assert variant["poster_path"] == "unmeasured.jpg"


def test_bytes_that_are_not_the_reserved_upload_are_still_withheld(fixture, monkeypatch):
    mock_storage(fixture, monkeypatch)
    attempt, _ = prepared(fixture)
    record = device_record(fixture.job, "first")
    record["actual_export_policy"] = {
        "version": 1,
        "source_receipts_digest": export_source_digest(
            approved_export_sources(fixture.job, fixture.request)
        ),
    }
    save_device_record(fixture.job, "first", record)

    def verify(*args, **kwargs):
        raise routes._ExportIntegrityError("export checksum mismatch")

    monkeypatch.setattr(routes, "_verify_export", verify)
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: type(
            "M",
            (),
            dict(
                size=record["attempts"][attempt]["size"], content_type="video/mp4", generation="g"
            ),
        )(),
    )
    response = checked_completion(fixture, body(fixture, attempt))
    assert response.status_code == 422, response.text
    stored = device_record(fixture.job, "first")
    assert stored["status"]["phase"] == "needs_attention"
    assert stored["attempts"][attempt]["actual_export_verification"]["status"] == "rejected"


def test_published_fast_path_does_not_need_a_passing_report(fixture, monkeypatch):
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
    assert response.status_code == 200, response.text
    other = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete",
        json=body(fixture, "00000000-0000-0000-0000-000000000735"),
    )
    assert other.status_code == 409, "another attempt never rides on the published one"


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
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
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
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: SimpleNamespace(
            size=reservation["size"], content_type="video/mp4", generation="g"
        ),
    )
    response = checked_completion(fixture, body(fixture, attempt))
    stored = device_record(fixture.job, "first")
    assert stored["attempts"][attempt]["actual_export_verification"]["status"] == "rejected"
    if stale == "source_receipts":
        # A source/policy digest mismatch is the one authority drift that still withholds:
        # it needs re-approval, never an exact retry loop.
        assert response.status_code == 422, response.text
        assert stored["status"]["reason_code"] == "contract_changed"
    else:
        # A mislabeled receipt is advisory: the video publishes, never as a checked receipt.
        assert response.status_code == 200, response.text
        assert stored["status"]["phase"] == "published"


@pytest.mark.parametrize(
    "pinned,reported,verified",
    [
        # KRI-810: a request pinned before the verifier repair (Troya job
        # 57602026 was pinned to v2) publishes bytes the repaired verifier checks.
        ("phone-simple-cut-v2", "phone-simple-cut-v3", True),
        ("phone-simple-cut-v3", "phone-simple-cut-v3", True),
        # KRI-1042: a request pinned to v3 (Lisbon job 3daccc8c) publishes bytes
        # the v4 text proof checks.
        ("phone-simple-cut-v3", "phone-simple-cut-v4", True),
        ("phone-simple-cut-v4", "phone-simple-cut-v4", True),
        # KRI-1042 frame profile: the Lisbon request (pinned to v3) and v4 pins
        # publish bytes the v5 picture rule checks.
        ("phone-simple-cut-v3", "phone-simple-cut-v5", True),
        ("phone-simple-cut-v4", "phone-simple-cut-v5", True),
        ("phone-simple-cut-v5", "phone-simple-cut-v5", True),
        # An older verifier never satisfies a pin: the video still publishes, unverified.
        ("phone-simple-cut-v2", "phone-simple-cut-v1", False),
        ("phone-simple-cut-v3", "phone-simple-cut-v2", False),
        ("phone-simple-cut-v4", "phone-simple-cut-v3", False),
        ("phone-simple-cut-v5", "phone-simple-cut-v4", False),
    ],
)
def test_pinned_verifier_counts_as_verified_only_for_its_own_or_a_later_verifier(
    fixture, monkeypatch, pinned, reported, verified
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
    assert response.status_code == 200, response.text
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
    assert counts["verified_device_exports"] == int(verified)
    stored = device_record(fixture.job, "first")
    assert stored["status"]["phase"] == "published"
    assert stored["published_attempt"] == attempt


@pytest.mark.parametrize("failure", ["checksum", "size", "unreadable"])
@pytest.mark.parametrize("unverified", [False, True])
def test_unverified_publication_still_requires_reserved_bytes(
    fixture, monkeypatch, failure, unverified
):
    from pathlib import Path

    mock_storage(fixture, monkeypatch)
    attempt, _ = prepared(fixture)
    record = device_record(fixture.job, "first")
    record["actual_export_policy"] = {
        "version": 1,
        "source_receipts_digest": export_source_digest(
            approved_export_sources(fixture.job, fixture.request)
        ),
    }
    save_device_record(fixture.job, "first", record)
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )

    def download(_path, target, **_kwargs):
        if failure == "unreadable":
            raise ValueError("storage read unavailable")
        Path(target).write_bytes(b"x" * (12 if failure == "checksum" else 13))

    monkeypatch.setattr(routes.storage, "download_generation_to_file", download)
    monkeypatch.setattr(routes, "upload_video_poster", lambda *a, **kw: None)
    response = checked_completion(fixture, body(fixture, attempt), unverified=unverified)
    assert response.status_code == (409 if failure == "unreadable" else 422), response.text
    record = device_record(fixture.job, "first")
    assert record["status"]["phase"] != "published"
    assert "publication_proof" not in record["attempts"][attempt]
    assert "video_path" not in fixture.job.assembly_plan["variants"][0]


@pytest.mark.parametrize("changed", ["path", "size", "sha256", "policy"])
def test_publication_revalidates_upload_and_policy_after_download(fixture, monkeypatch, changed):
    import copy

    mock_storage(fixture, monkeypatch)
    attempt, _ = prepared(fixture)
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )

    def verify(*args, **kwargs):
        record = copy.deepcopy(device_record(fixture.job, "first"))
        if changed == "policy":
            record["actual_export_policy"] = {"version": 1, "source_receipts_digest": "new"}
        else:
            record["attempts"][attempt][changed] = 13 if changed == "size" else "changed"
        save_device_record(fixture.job, "first", record)

    monkeypatch.setattr(routes, "_verify_export", verify)
    response = fixture.client.post(
        f"/me/jobs/{fixture.job.id}/device-render/complete", json=body(fixture, attempt)
    )
    assert response.status_code == 409, response.text
    record = device_record(fixture.job, "first")
    assert record["status"]["phase"] == "syncing"
    assert "publication_proof" not in record["attempts"][attempt]
