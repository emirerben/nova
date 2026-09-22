"""Revision/byte fences for phone rendering; no model calls or remote storage."""

import hashlib
import json
import shutil
import subprocess
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.kria.device_render import (
    BRAND_TAIL_SECONDS,
    DeviceRenderRequest,
    DeviceRenderStatus,
    make_device_request,
)
from app.kria.recipes import EditRecipeV1
from app.routes.device_render import _record, _verify_export
from app.services.device_render import (
    device_record,
    device_status,
    mark_device_failed,
    pin_device_request,
    retry_device_render,
    save_device_record,
    touch_device_poll,
)
from app.services.public_assembly_plan import project_public_assembly_plan


def _fixture():
    recipe = EditRecipeV1.model_validate_json(
        (Path(__file__).parent / "fixtures/kria_edit_recipe_v1.json").read_text()
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [{"variant_id": "original_text", "render_generation_id": "approved"}]
        },
    )
    request = make_device_request(
        job_id=job.id, variant_id="original_text", revision=1, recipe=recipe
    )
    pin_device_request(job, request, base_generation="approved")
    return job, request


def test_planning_redelivery_cannot_change_approved_recipe():
    job, request = _fixture()
    before = deepcopy(job.assembly_plan)
    assert pin_device_request(job, request, base_generation="approved") is False
    assert job.assembly_plan == before
    modified = request.recipe.model_copy(deep=True)
    modified.frame_rate = 24
    changed = make_device_request(
        job_id=job.id, variant_id="original_text", revision=1, recipe=modified
    )
    with pytest.raises(ValueError, match="already pinned"):
        pin_device_request(job, changed, base_generation="approved")
    assert device_status(job, "original_text").request == request


def test_device_receipts_never_escape_public_assembly_projection():
    job, _ = _fixture()
    public = project_public_assembly_plan(job.assembly_plan)
    assert "_device_render_v1" not in public
    assert "variants" in public


def test_publication_requires_exact_job_recipe_and_editor_generation():
    job, request = _fixture()
    assert _record(job, request.identity)[1].request == request
    changed = request.identity.model_copy(update={"recipe_revision": 2})
    with pytest.raises(HTTPException) as error:
        _record(job, changed)
    assert error.value.status_code == 409
    job.assembly_plan["variants"][0]["render_generation_id"] = "new-editor-save"
    with pytest.raises(HTTPException) as error:
        _record(job, request.identity)
    assert error.value.status_code == 409


def test_device_status_exposes_only_the_published_attempt_generation():
    job, _ = _fixture()
    record = device_record(job, "original_text")
    assert device_status(job, "original_text").published_generation is None

    record["status"]["phase"] = "published"
    record["published_attempt"] = str(uuid.uuid4())
    save_device_record(job, "original_text", record)
    status = device_status(job, "original_text")
    assert status.published_generation == record["published_attempt"]

    record["status"]["phase"] = "awaiting_device"
    save_device_record(job, "original_text", record)
    assert device_status(job, "original_text").published_generation is None


def test_retry_clears_published_generation_until_a_new_attempt_publishes():
    job, _ = _fixture()
    record = device_record(job, "original_text")
    record["status"]["phase"] = "needs_attention"
    record["published_attempt"] = str(uuid.uuid4())
    save_device_record(job, "original_text", record)

    retried = retry_device_render(job, "original_text")
    assert retried.published_generation is None
    assert "published_attempt" not in device_record(job, "original_text")


def test_wire_digest_rejects_modified_recipe_and_nonfinite_values():
    _, request = _fixture()
    wire = request.model_dump(mode="json")
    wire["recipe"]["frame_rate"] = 24
    with pytest.raises(ValueError, match="digest"):
        DeviceRenderRequest.model_validate(wire)
    wire["recipe"]["frame_rate"] = float("nan")
    with pytest.raises(ValueError):
        DeviceRenderRequest.model_validate(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("codec_name", "hevc"),
        ("width", 720),
        ("height", 1080),
        ("pix_fmt", "yuv444p"),
        ("avg_frame_rate", "24/1"),
        ("avg_frame_rate", "1/0"),
        ("tags", {"rotate": "90"}),
        ("side_data_list", [{"rotation": -90}]),
    ],
)
def test_uploaded_format_must_match_recipe(field, value):
    _, request = _fixture()
    status = DeviceRenderStatus(phase="syncing", request=request)
    data = b"fixture mp4"
    stream = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1080,
        "height": 1920,
        "pix_fmt": "yuv420p",
        "avg_frame_rate": "30/1",
        field: value,
    }
    probe = {"streams": [stream], "format": {"duration": 2}}

    def download(_path, local, *, generation):
        assert generation == "42"
        Path(local).write_bytes(data)

    with (
        patch("app.routes.device_render.storage.download_generation_to_file", side_effect=download),
        patch(
            "app.routes.device_render.subprocess.run",
            return_value=SimpleNamespace(stdout=json.dumps(probe)),
        ),
    ):
        with pytest.raises(ValueError, match="format mismatch"):
            _verify_export("owned/path", "42", len(data), hashlib.sha256(data).hexdigest(), status)


def test_checksum_failure_precedes_decoder_and_never_publishes():
    _, request = _fixture()

    def download(_path, local, *, generation):
        Path(local).write_bytes(b"replaced bytes")

    with (
        patch("app.routes.device_render.storage.download_generation_to_file", side_effect=download),
        patch("app.routes.device_render.subprocess.run") as decoder,
    ):
        with pytest.raises(ValueError, match="checksum"):
            _verify_export(
                "owned/path",
                "42",
                len(b"replaced bytes"),
                "0" * 64,
                DeviceRenderStatus(phase="syncing", request=request),
            )
        decoder.assert_not_called()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_real_h264_aac_export_passes_verification(tmp_path):
    _, request = _fixture()
    recipe = request.recipe
    output = tmp_path / "native-contract.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={recipe.canvas.width}x{recipe.canvas.height}:r={recipe.frame_rate}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(recipe.duration),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    data = output.read_bytes()

    def download(_path, local, *, generation):
        assert generation == "42"
        Path(local).write_bytes(data)

    with patch(
        "app.routes.device_render.storage.download_generation_to_file", side_effect=download
    ):
        _verify_export(
            "owned/path",
            "42",
            len(data),
            hashlib.sha256(data).hexdigest(),
            DeviceRenderStatus(phase="syncing", request=request),
        )


@pytest.mark.parametrize("phase", ["awaiting_device", "syncing"])
def test_mark_device_failed_transitions_from_failable_phases(phase):
    job, _ = _fixture()
    record = device_record(job, "original_text")
    record["status"]["phase"] = phase

    save_device_record(job, "original_text", record)
    status = mark_device_failed(
        job, "original_text", reason_code="export_failed", detail="custom detail"
    )
    assert status.phase == "needs_attention"
    assert status.reason == "custom detail"
    assert status.reason_code == "export_failed"
    persisted = device_record(job, "original_text")
    assert persisted["status"]["phase"] == "needs_attention"
    assert persisted["failed_at"]
    assert persisted["failure"] == {
        "reason_code": "export_failed",
        "detail": "custom detail",
        "failed_at": persisted["failed_at"],
    }


def test_mark_device_failed_uses_default_detail_when_empty():
    job, _ = _fixture()
    status = mark_device_failed(job, "original_text", reason_code="thermal", detail="")
    assert status.reason
    assert "device" in status.reason.lower()


@pytest.mark.parametrize("phase", ["published", "needs_attention"])
def test_mark_device_failed_rejects_non_failable_phases(phase):
    job, _ = _fixture()
    record = device_record(job, "original_text")
    record["status"]["phase"] = phase

    save_device_record(job, "original_text", record)
    with pytest.raises(ValueError, match="cannot fail"):
        mark_device_failed(job, "original_text", reason_code="unknown", detail="")


def test_touch_device_poll_writes_iso_timestamp():
    job, _ = _fixture()
    now = datetime.now(UTC)
    touch_device_poll(job, "original_text", now)
    assert device_record(job, "original_text")["last_polled_at"] == now.isoformat()


def test_retry_device_render_reissues_revision_and_preserves_recipe_and_base_generation():
    job, request = _fixture()
    mark_device_failed(job, "original_text", reason_code="export_failed", detail="")
    new_status = retry_device_render(job, "original_text")
    assert new_status.phase == "awaiting_device"
    assert new_status.request.identity.recipe_revision == request.identity.recipe_revision + 1
    assert new_status.request.recipe == request.recipe
    assert new_status.reason is None
    assert new_status.reason_code is None
    assert device_record(job, "original_text")["base_generation"] == "approved"
    assert device_record(job, "original_text")["attempts"] == {}


def test_retry_device_render_requires_needs_attention_phase():
    job, _ = _fixture()
    with pytest.raises(ValueError, match="not awaiting a retry"):
        retry_device_render(job, "original_text")


# --- brand tail -------------------------------------------------------------
# The phone appends brand furniture (watermark for the whole edit, then the
# outro) after the recipe's own timeline, so a branded upload is legitimately
# longer than `recipe.duration`. It declares WHICH tail it used at reservation
# time; the length of that tail is the server's, so a tampered client cannot
# pad an upload with arbitrary trailing footage.
#
# Nothing exercised the duration branch before this: every existing probe
# happened to equal recipe.duration exactly, so the gate passed incidentally.


def _probe_with_duration(duration: float) -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
            }
        ],
        "format": {"duration": duration},
    }


def _run_verify(probe: dict, brand_tail: str) -> None:
    _, request = _fixture()
    status = DeviceRenderStatus(phase="syncing", request=request)
    data = b"fixture mp4"

    def download(_path, local, *, generation):
        Path(local).write_bytes(data)

    with (
        patch("app.routes.device_render.storage.download_generation_to_file", side_effect=download),
        patch(
            "app.routes.device_render.subprocess.run",
            return_value=SimpleNamespace(stdout=json.dumps(probe)),
        ),
    ):
        _verify_export(
            "owned/path", "42", len(data), hashlib.sha256(data).hexdigest(), status, brand_tail
        )


@pytest.mark.parametrize("brand_tail", sorted(BRAND_TAIL_SECONDS))
def test_declared_brand_tail_is_added_to_the_expected_duration(brand_tail):
    _, request = _fixture()
    expected = request.recipe.duration + BRAND_TAIL_SECONDS[brand_tail]
    _run_verify(_probe_with_duration(expected), brand_tail)


def test_branded_export_is_rejected_when_the_tail_is_not_declared():
    """The regression this contract exists for.

    Before the tail was declared, a branded phone render was 1.6s longer than
    the recipe and every publish failed with "export duration mismatch".
    """
    _, request = _fixture()
    branded = request.recipe.duration + BRAND_TAIL_SECONDS["standard"]
    with pytest.raises(ValueError, match="duration mismatch"):
        _run_verify(_probe_with_duration(branded), "none")


def test_declaring_a_tail_that_was_not_appended_is_rejected():
    """A client cannot buy 1.6s of slack by claiming a tail it did not add."""
    _, request = _fixture()
    with pytest.raises(ValueError, match="duration mismatch"):
        _run_verify(_probe_with_duration(request.recipe.duration), "standard")
