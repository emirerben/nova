"""Real local storage and decoder controls; no provider, UI, or production media.

Failure matrix: unowned path/missing proof => no read; replaced generation or
wrong bytes/size => unavailable; corrupt media/deadline/frame budget => unavailable;
valid export => actual pinned-byte receipt and bounded timestamped JPEG bytes.
"""

import hashlib
import io
import json
import subprocess
import time
import uuid

import pytest
from PIL import Image

from app import storage
from app.config import settings
from app.services import render_review_evidence as evidence


@pytest.fixture
def local_video(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path / "storage"))
    owner, job, attempt, execution = [str(uuid.uuid4()) for _ in range(4)]
    path = f"{owner}/{job}/device/{attempt}.mp4"
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x120:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=160x120:r=10:d=1",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        timeout=15,
    )
    storage.upload_public_read(str(video), path)
    metadata = storage.object_metadata_once(path, timeout_s=2)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    identity = dict(job_id=job, variant_id="v", recipe_revision=1, recipe_digest="a" * 64)
    proof = dict(
        version=1,
        artifact_path=path,
        storage_generation=metadata.generation,
        file_size_bytes=metadata.size,
        sha256=digest,
        identity=identity,
        source_receipts_digest=None,
    )
    basis = evidence.ReviewBasis(
        execution_id=execution,
        owner_id=owner,
        job_id=job,
        variant_id="v",
        published_attempt=attempt,
        requirement_generation="approved",
        recipe_digest="a" * 64,
        recipe_identity_json=json.dumps(identity),
        object_path=path,
        publication_proof_json=json.dumps(proof),
    )
    return basis, video, proof


def test_real_bytes_and_decoded_pixels_have_exact_provenance(local_video):
    basis, video, proof = local_video
    result = evidence.obtain_render_review_evidence(
        basis, limits=evidence.EvidenceLimits(frame_count=2)
    )
    assert result.status == "ready", result.reason
    bundle = result.bundle
    assert bundle.verified_artifact.sha256 == hashlib.sha256(video.read_bytes()).hexdigest()
    assert bundle.verified_artifact.object_version == proof["storage_generation"]
    assert bundle.verified_artifact.byte_count == video.stat().st_size
    assert len(bundle.frames) == 2
    for frame, expected in zip(bundle.frames, [(255, 0, 0), (0, 0, 255)], strict=True):
        image = Image.open(io.BytesIO(frame.image_bytes)).convert("RGB")
        assert max(abs(a - b) for a, b in zip(image.getpixel((20, 20)), expected)) < 15
        assert frame.image_sha256 == hashlib.sha256(frame.image_bytes).hexdigest()
        assert frame.artifact_sha256 == bundle.verified_artifact.sha256
        assert frame.width <= 640 and frame.height <= 640
        assert frame.decoder_version.startswith("ffmpeg version")
    assert [f.timestamp_s for f in bundle.frames] == pytest.approx([0.5, 1.5], abs=0.01)
    assert bundle.audio_inspected is False
    assert bundle.review_model_received_visual_input is False
    serialized = bundle.model_dump_json()
    assert len(serialized.encode()) < 2_000_000
    assert evidence.EvidenceBundle.model_validate_json(serialized) == bundle


@pytest.mark.parametrize(
    "change", ["path", "identity", "generation", "sha", "size", "missing_proof"]
)
def test_publication_or_storage_mismatch_is_unavailable(local_video, change):
    basis, video, proof = local_video
    if change == "path":
        basis = basis.model_copy(update={"object_path": "other-owner/arbitrary.mp4"})
    elif change == "identity":
        proof["identity"]["variant_id"] = "other"
    elif change == "generation":
        proof["storage_generation"] = "older-generation"
    elif change == "sha":
        proof["sha256"] = "b" * 64
    elif change == "size":
        proof["file_size_bytes"] += 1
    else:
        proof = {}
    basis = basis.model_copy(update={"publication_proof_json": json.dumps(proof)})
    assert evidence.obtain_render_review_evidence(basis).status == "unavailable"


def test_changed_local_generation_between_lookup_and_download_fails(local_video, monkeypatch):
    basis, _, _ = local_video
    original = storage.download_generation_to_file

    def replaced(path, local_path, **kwargs):
        storage.local_object_path(path).write_bytes(b"changed after metadata")
        return original(path, local_path, **kwargs)

    monkeypatch.setattr(storage, "download_generation_to_file", replaced)
    assert evidence.obtain_render_review_evidence(basis).status == "unavailable"


def test_corrupt_media_never_returns_frames_or_receipt(local_video):
    basis, _, proof = local_video
    path = storage.local_object_path(basis.object_path)
    path.write_bytes(b"not an mp4")
    metadata = storage.object_metadata_once(basis.object_path, timeout_s=2)
    proof.update(
        storage_generation=metadata.generation,
        file_size_bytes=metadata.size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    basis = basis.model_copy(update={"publication_proof_json": json.dumps(proof)})
    result = evidence.obtain_render_review_evidence(basis)
    assert result.status == "unavailable"
    assert result.bundle is None


def test_size_deadline_and_frame_cap_are_bounded(local_video, monkeypatch):
    basis, _, _ = local_video
    result = evidence.obtain_render_review_evidence(
        basis, limits=evidence.EvidenceLimits(max_artifact_bytes=1)
    )
    assert result.status == "unavailable"

    def no_read(*args, **kwargs):
        pytest.fail("expired deadline must not read storage")

    monkeypatch.setattr(storage, "object_metadata_once", no_read)
    assert (
        evidence.obtain_render_review_evidence(
            basis, deadline_monotonic=time.monotonic() - 1
        ).reason
        == "deadline_exhausted"
    )
    with pytest.raises(ValueError):
        evidence.EvidenceLimits(frame_count=7)


def test_unowned_path_never_reads_storage(local_video, monkeypatch):
    basis, _, _ = local_video

    def no_read(*args, **kwargs):
        pytest.fail("unowned path must fail before storage")

    monkeypatch.setattr(storage, "object_metadata_once", no_read)
    for path in (
        "https://example.com/video.mp4",
        "../escape.mp4",
        basis.object_path.replace(basis.owner_id, str(uuid.uuid4())),
    ):
        result = evidence.obtain_render_review_evidence(
            basis.model_copy(update={"object_path": path})
        )
        assert result.reason == "unowned_artifact_path"


def test_frame_cap_and_cleanup_return_bytes_not_dead_paths(local_video, monkeypatch):
    from pathlib import Path

    basis, _, _ = local_video
    directories = []
    original = evidence.tempfile.TemporaryDirectory

    def tracked(*args, **kwargs):
        directory = original(*args, **kwargs)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(evidence.tempfile, "TemporaryDirectory", tracked)
    result = evidence.obtain_render_review_evidence(
        basis, limits=evidence.EvidenceLimits(frame_count=6)
    )
    assert result.status == "ready", result.reason
    assert len(result.bundle.frames) == 6
    assert all(not path.exists() for path in directories)
    assert all(
        Image.open(io.BytesIO(frame.image_bytes)).format == "JPEG" for frame in result.bundle.frames
    )
    refused = evidence.obtain_render_review_evidence(
        basis, limits=evidence.EvidenceLimits(max_frame_bytes=1)
    )
    assert refused.reason == "frame_bytes_exceeded"
    assert all(not path.exists() for path in directories)


def test_actual_decoder_deadline_is_bounded(local_video):
    basis, _, _ = local_video
    start = time.monotonic()
    result = evidence.obtain_render_review_evidence(
        basis, limits=evidence.EvidenceLimits(timeout_s=0.01)
    )
    assert result.status == "unavailable"
    assert result.reason in {"decoder_timeout", "deadline_exhausted"}
    assert time.monotonic() - start < 2


def test_storage_unknown_failure_is_typed_unavailable(local_video, monkeypatch):
    basis, _, _ = local_video

    def failure(*args, **kwargs):
        raise RuntimeError("unknown SDK transport state")

    monkeypatch.setattr(storage, "object_metadata_once", failure)
    result = evidence.obtain_render_review_evidence(basis)
    assert result.status == "unavailable"
    assert result.bundle is None


def test_persisted_frame_bytes_and_artifact_binding_cannot_be_changed(local_video):
    basis, _, _ = local_video
    result = evidence.obtain_render_review_evidence(
        basis, limits=evidence.EvidenceLimits(frame_count=1)
    )
    raw = result.bundle.model_dump(mode="json")
    raw["frames"][0]["image_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="Frame bytes"):
        evidence.EvidenceBundle.model_validate(raw)
    raw = result.bundle.model_dump(mode="json")
    raw["frames"][0]["artifact_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="different artifact"):
        evidence.EvidenceBundle.model_validate(raw)


def test_unsupported_resource_platform_fails_before_storage(local_video, monkeypatch):
    basis, _, _ = local_video
    monkeypatch.setattr(evidence, "_resource_limits_supported", lambda: False)

    def no_read(*args, **kwargs):
        pytest.fail("unsupported resource enforcement must not read media")

    monkeypatch.setattr(storage, "object_metadata_once", no_read)
    result = evidence.obtain_render_review_evidence(basis)
    assert result.reason == "resource_limits_unavailable"
    assert result.bundle is None


def test_linux_process_output_is_capped():
    assert evidence._resource_limits_supported(), "Requires non-root Linux hard-limit qualification"
    with pytest.raises(evidence._Unavailable, match="decoder_output_exceeded"):
        evidence._run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=red:s=640x640:r=30",
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            time.monotonic() + 5,
        )


def test_linux_decoder_memory_allocation_is_refused():
    assert evidence._resource_limits_supported(), "Requires non-root Linux hard-limit qualification"
    with pytest.raises(subprocess.CalledProcessError) as refused:
        evidence._run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=red:s=16000x16000:r=1",
                "-vf",
                "format=rgba",
                "-filter_threads",
                "1",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-f",
                "null",
                "-",
            ],
            time.monotonic() + 5,
        )
    assert b"allocate memory" in refused.value.stderr.lower(), refused.value.stderr


def test_linux_decoder_wall_deadline_kills_process_group():
    assert evidence._resource_limits_supported(), "Requires non-root Linux hard-limit qualification"
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        evidence._run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "color=red:s=16x16:r=1",
                "-threads",
                "1",
                "-f",
                "null",
                "-",
            ],
            start + 0.2,
        )
    assert time.monotonic() - start < 2


def test_linux_decoder_output_file_has_hard_size_limit(tmp_path):
    assert evidence._resource_limits_supported(), "Requires non-root Linux hard-limit qualification"
    output = tmp_path / "oversized.bmp"
    with pytest.raises(subprocess.CalledProcessError):
        evidence._run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=red:s=1600x1600:r=1",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-c:v",
                "bmp",
                str(output),
            ],
            time.monotonic() + 5,
        )
    assert output.exists()
    assert output.stat().st_size <= 1024 * 1024
