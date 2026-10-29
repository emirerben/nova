"""Actual MP4 -> authenticated completion -> artifact and receipt publication.

Object storage and database transport are local doubles; media decoding, source
receipt resolution, contract fences, gate and publication handler are real.
"""

# ruff: noqa: F811 -- imported pytest fixtures

import copy
import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.device_render import make_device_request
from app.routes import device_render as routes
from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract
from app.services.device_render import (
    approved_export_sources,
    device_record,
    device_status,
    export_source_digest,
    pin_device_request,
    retry_device_render,
    save_device_record,
)
from tests.integration.test_actual_export_verification import encode, exports  # noqa: F401
from tests.routes.test_background_export_verification import checked_completion
from tests.routes.test_device_render import (  # noqa: F401
    body,
    fixture,
    mock_storage,
    prepared,
    scalar,
)

pytestmark = pytest.mark.timeout(240)  # two real candidate decodes plus codec fixture creation


@pytest.mark.parametrize("options", [{}, {"order": ("b", "a")}, {"title": False}, {"gain": 2}])
def test_actual_bytes_are_checked_before_publication_and_retry(
    fixture, exports, monkeypatch, options
):
    root, sources, recipe = exports
    fixture.request = make_device_request(
        job_id=fixture.job.id, variant_id="first", revision=2, recipe=recipe
    )
    fixture.job.assembly_plan["_phone_sources_v1"] = [
        {
            "media_id": key,
            "proxy_path": f"analysis-proxy-{key}.mp4",
            "generation": f"source-{key}",
            "original": {
                "sha256": key * 64,
                "byte_count": 1,
                "duration_s": 4,
                "width": 640,
                "height": 360,
                "orientation_degrees": 0,
                "has_audio": True,
            },
        }
        for key in sources
    ]
    monkeypatch.setattr(settings, "phone_actual_export_verification_enabled", True)
    monkeypatch.setattr(settings, "phone_actual_export_verification_user_ids", [fixture.user.id])
    fixture.job.assembly_plan[CONTRACT_FIELD] = (
        CreatorRenderContract(generation_id="approved")
        .rebind(original_audio="require", audio_source_ids=("a", "b"))
        .model_dump(mode="json")
    )
    pin_device_request(fixture.job, fixture.request, base_generation="approved")
    bindings = approved_export_sources(fixture.job, fixture.request)
    record = device_record(fixture.job, "first")
    assert record["actual_export_policy"] == {
        "version": 1,
        "verifier_version": "phone-simple-cut-v5",
        "source_receipts_digest": export_source_digest(bindings),
    }
    save_device_record(fixture.job, "first", record)
    # A replacement is staged beside the accepted artifact.
    fixture.job.assembly_plan["variants"][0].update(
        ok=True,
        render_status="ready",
        video_path="previous.mp4",
        poster_path="previous.jpg",
        output_url="previous-url",
    )
    previous = copy.deepcopy(fixture.job.assembly_plan["variants"][0])
    mock_storage(fixture, monkeypatch)
    monkeypatch.setattr(routes, "upload_video_poster", lambda *args, **kwargs: "new-poster.jpg")
    fixture.db.execute.return_value = scalar(
        SimpleNamespace(
            user_id=fixture.user.id,
            status="reserved",
            retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )

    def complete(candidate):
        attempt, path = prepared(fixture)
        record = device_record(fixture.job, "first")
        reservation = record["attempts"][attempt]
        reservation.update(
            size=candidate.stat().st_size, sha256=hashlib.sha256(candidate.read_bytes()).hexdigest()
        )
        save_device_record(fixture.job, "first", record)
        monkeypatch.setattr(
            routes.storage,
            "object_metadata",
            lambda _: SimpleNamespace(
                size=candidate.stat().st_size, content_type="video/mp4", generation="42"
            ),
        )

        def download(remote, local, *, generation):
            if remote == path:
                assert generation == "42"
                source = candidate
            else:
                key = next(key for key in sources if remote == f"analysis-proxy-{key}.mp4")
                assert generation == f"source-{key}"
                source = sources[key]
            shutil.copyfile(source, local)

        monkeypatch.setattr(routes.storage, "download_generation_to_file", download)
        response = checked_completion(fixture, body(fixture, attempt))
        report = device_record(fixture.job, "first")["attempts"][attempt][
            "actual_export_verification"
        ]
        assert report["sha256"] == reservation["sha256"]
        assert report["storage_generation"] == "42"
        return response, report

    response, report = complete(encode(root, sources, **options))
    if options:
        assert response.status_code == 422, response.text
        assert report["status"] == "rejected"
        assert fixture.job.assembly_plan["variants"][0] == previous
        recipe_digest = fixture.request.identity.recipe_digest
        monkeypatch.setattr(settings, "phone_actual_export_verification_enabled", False)
        fixture.request = retry_device_render(fixture.job, "first").request
        assert fixture.request.identity.recipe_digest == recipe_digest
        assert "actual_export_policy" in device_record(fixture.job, "first")
        response, report = complete(encode(root, sources))
    assert response.status_code == 200, (response.text, report)
    assert report["status"] == "checked"
    assert device_status(fixture.job, "first").phase == "published"
    assert fixture.job.assembly_plan["variants"][0]["actual_export_verification"] == report
    assert fixture.job.assembly_plan["variants"][0]["poster_path"] == "new-poster.jpg"


def test_prepared_story_cannot_drop_actual_output_checks_when_enrollment_is_withdrawn(
    fixture, exports, monkeypatch
):
    _root, sources, recipe = exports
    fixture.request = make_device_request(
        job_id=fixture.job.id, variant_id="first", revision=2, recipe=recipe
    )
    fixture.job.assembly_plan.update(
        {
            "creator_generation_id": "approved",
            "prepared_collection_story": {"story_id": "server-owned"},
        }
    )
    fixture.job.assembly_plan["_phone_sources_v1"] = [
        {
            "media_id": key,
            "proxy_path": f"analysis-proxy-{key}.mp4",
            "generation": f"source-{key}",
            "original": {
                "sha256": key * 64,
                "byte_count": 1,
                "duration_s": 4,
                "width": 640,
                "height": 360,
                "orientation_degrees": 0,
                "has_audio": True,
            },
        }
        for key in sources
    ]
    fixture.job.assembly_plan[CONTRACT_FIELD] = (
        CreatorRenderContract(generation_id="approved")
        .rebind(original_audio="require", audio_source_ids=("a", "b"))
        .model_dump(mode="json")
    )
    monkeypatch.setattr(settings, "phone_actual_export_verification_enabled", False)
    monkeypatch.setattr(settings, "phone_actual_export_verification_user_ids", [])
    pin_device_request(fixture.job, fixture.request, base_generation="approved")
    policy = device_record(fixture.job, "first")["actual_export_policy"]
    assert policy["verifier_version"] == "phone-simple-cut-v5"
    assert policy["source_receipts_digest"] == export_source_digest(
        approved_export_sources(fixture.job, fixture.request)
    )
