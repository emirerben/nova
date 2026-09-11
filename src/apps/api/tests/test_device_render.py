"""Revision/byte fences for phone rendering; no model calls or remote storage."""

import hashlib
import json
import shutil
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.kria.device_render import DeviceRenderRequest, DeviceRenderStatus, make_device_request
from app.kria.recipes import EditRecipeV1
from app.routes.device_render import _record, _verify_export
from app.services.device_render import device_status, pin_device_request
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
