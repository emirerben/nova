"""Bounded server reads and headless decoding of an already-published native export.

The caller supplies a detached, ownership-checked basis and rechecks it before
persistence. This adapter does no database work, publication, or provider transfer.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import storage
from app.kria.device_render import DeviceRenderIdentity
from app.kria.render_review_context import TimestampedObservation, VerifiedArtifactReceipt

_LOCAL_MP4_INPUT = (
    "-protocol_whitelist",
    "file,pipe",
    "-f",
    "mov",
    "-enable_drefs",
    "0",
    "-use_absolute_path",
    "0",
)
_DECODER_OUTPUT_BYTES = 64 * 1024
# Executed only in a fresh isolated interpreter, never preexec_fn in a threaded worker.
# The script is static application code. Its sole executable target is a resolved
# ffmpeg/ffprobe path selected by _run, never a user command or downloaded program.
_LIMITED_DECODER = """
import os, resource, sys
if sys.platform != "linux" or os.geteuid() == 0:
    sys.exit(125)
limits = (
    (resource.RLIMIT_AS, 1024 * 1024 * 1024),
    (resource.RLIMIT_CPU, 15),
    (resource.RLIMIT_FSIZE, 1024 * 1024),
    (resource.RLIMIT_NOFILE, 32),
    (resource.RLIMIT_NPROC, 128),
    (resource.RLIMIT_CORE, 0),
)
try:
    for kind, maximum in limits:
        _, inherited = resource.getrlimit(kind)
        ceiling = maximum if inherited == resource.RLIM_INFINITY else min(maximum, inherited)
        resource.setrlimit(kind, (ceiling, ceiling))
        if resource.getrlimit(kind) != (ceiling, ceiling):
            sys.exit(125)
except (OSError, ValueError):
    sys.exit(125)
os.execv(sys.argv[1], sys.argv[1:])
"""


def _resource_limits_supported() -> bool:
    return sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() != 0


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


class ReviewBasis(FrozenModel):
    execution_id: str
    owner_id: str
    job_id: str
    variant_id: str
    published_attempt: str
    requirement_generation: str = Field(min_length=1)
    recipe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipe_identity_json: str
    object_path: str
    publication_proof_json: str


class EvidenceLimits(FrozenModel):
    frame_count: int = Field(default=4, ge=1, le=6)
    max_artifact_bytes: int = Field(default=256 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)
    max_frame_bytes: int = Field(default=128 * 1024, ge=1, le=128 * 1024)
    max_total_frame_bytes: int = Field(default=1024 * 1024, ge=1, le=1024 * 1024)
    max_side: int = Field(default=640, ge=32, le=640)
    timeout_s: float = Field(default=90, gt=0, le=90)


class FrameEvidence(FrozenModel):
    image_bytes: bytes = Field(min_length=1, max_length=128 * 1024)
    mime_type: Literal["image/jpeg"] = "image/jpeg"
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp_s: float = Field(ge=0)
    width: int = Field(ge=1, le=640)
    height: int = Field(ge=1, le=640)
    decoder_version: str
    extraction_version: Literal["render-review-frames-v1"] = "render-review-frames-v1"

    @model_validator(mode="after")
    def bytes_match(self):
        if hashlib.sha256(self.image_bytes).hexdigest() != self.image_sha256:
            raise ValueError("Frame bytes do not match their hash")
        return self


class EvidenceBundle(FrozenModel):
    verified_artifact: VerifiedArtifactReceipt
    frames: tuple[FrameEvidence, ...] = Field(min_length=1, max_length=6)
    observations: tuple[TimestampedObservation, ...]
    evidence_coverage: tuple[Literal["actual_render_bytes", "direct_frame"], ...] = (
        "actual_render_bytes",
        "direct_frame",
    )
    audio_inspected: Literal[False] = False
    review_model_received_visual_input: Literal[False] = False

    @model_validator(mode="after")
    def artifact_matches(self):
        if any(frame.artifact_sha256 != self.verified_artifact.sha256 for frame in self.frames):
            raise ValueError("Frame belongs to a different artifact")
        if any(o.artifact_sha256 != self.verified_artifact.sha256 for o in self.observations):
            raise ValueError("Observation belongs to a different artifact")
        return self


class EvidenceResult(FrozenModel):
    status: Literal["ready", "unavailable"]
    reason: str | None = None
    bundle: EvidenceBundle | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "ready" and (self.bundle is None or self.reason is not None):
            raise ValueError("Ready evidence requires a bundle")
        if self.status == "unavailable" and (self.bundle is not None or not self.reason):
            raise ValueError("Unavailable evidence requires a reason")
        return self


class _PublicationProof(FrozenModel):
    version: Literal[1]
    artifact_path: str
    storage_generation: str = Field(min_length=1)
    file_size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity: DeviceRenderIdentity
    source_receipts_digest: str | None = None


class _Unavailable(Exception):
    pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Unavailable("deadline_exhausted")
    return remaining


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Unavailable(reason)


def _proof(basis: ReviewBasis) -> _PublicationProof:
    # This is the exact object naming contract used by reserve_device_export.
    owner, job, attempt = (
        str(uuid.UUID(value)) for value in (basis.owner_id, basis.job_id, basis.published_attempt)
    )
    uuid.UUID(basis.execution_id)
    _require(basis.object_path == f"{owner}/{job}/device/{attempt}.mp4", "unowned_artifact_path")
    identity = DeviceRenderIdentity.model_validate_json(basis.recipe_identity_json)
    proof = _PublicationProof.model_validate_json(basis.publication_proof_json)
    _require(
        proof.artifact_path == basis.object_path
        and proof.identity == identity
        and str(identity.job_id) == job
        and identity.variant_id == basis.variant_id
        and identity.recipe_digest == basis.recipe_digest,
        "publication_proof_changed",
    )
    return proof


def _run(arguments: list[str], deadline: float) -> subprocess.CompletedProcess:
    if not _resource_limits_supported():
        raise _Unavailable("resource_limits_unavailable")
    if not arguments or arguments[0] not in {"ffmpeg", "ffprobe"}:
        raise _Unavailable("decoder_command_unavailable")
    executable = shutil.which(arguments[0])
    if executable is None:
        raise _Unavailable("decoder_command_unavailable")
    timeout = min(20.0, _remaining(deadline))
    stop = time.monotonic() + timeout
    environment = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    command = [sys.executable, "-I", "-c", _LIMITED_DECODER, executable, *arguments[1:]]
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=environment,
    ) as child:
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = stop - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(arguments, timeout)
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output = outputs[key.data]
                        if len(output) + len(chunk) > _DECODER_OUTPUT_BYTES:
                            raise _Unavailable("decoder_output_exceeded")
                        output.extend(chunk)
            returncode = child.wait(timeout=max(0.001, stop - time.monotonic()))
            if returncode == 125:
                raise _Unavailable("resource_limits_unavailable")
            if returncode:
                raise subprocess.CalledProcessError(
                    returncode,
                    arguments,
                    output=bytes(outputs["stdout"]),
                    stderr=bytes(outputs["stderr"]),
                )
            return subprocess.CompletedProcess(
                arguments, returncode, bytes(outputs["stdout"]), bytes(outputs["stderr"])
            )
        finally:
            # Descendants cannot keep pipes or CPU alive after the parent deadline.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def _frames(local: Path, artifact_sha: str, limits: EvidenceLimits, deadline: float):
    info = json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                *_LOCAL_MP4_INPUT,
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(local),
            ],
            deadline,
        ).stdout
    )
    stream = info["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    duration = float(info["format"]["duration"])
    _require(
        0 < width <= 8192
        and 0 < height <= 8192
        and width * height <= 34_000_000
        and math.isfinite(duration)
        and 0 < duration <= 1800,
        "media_bounds_exceeded",
    )
    version = _run(["ffmpeg", "-version"], deadline).stdout.decode().splitlines()[0][:300]
    frames = []
    total = 0
    for index in range(limits.frame_count):
        requested = duration * (index + 0.5) / limits.frame_count
        output = local.parent / f"frame-{index}.jpg"
        # select keeps source presentation timestamps; showinfo reports the actual
        # selected decoded frame, rather than pretending the requested time was exact.
        filters = (
            f"select=gte(t\\,{requested:.9f}),"
            f"scale=w='min({limits.max_side},iw)':h='min({limits.max_side},ih)'"
            ":force_original_aspect_ratio=decrease,showinfo"
        )
        decoded = _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "info",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                *_LOCAL_MP4_INPUT,
                "-i",
                str(local),
                "-map",
                "0:v:0",
                "-vf",
                filters,
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-q:v",
                "4",
                str(output),
            ],
            deadline,
        )
        timestamp_match = re.search(rb"\bn:\s*0\s+pts:.*?pts_time:([0-9.+eE-]+)", decoded.stderr)
        _require(timestamp_match is not None, "frame_timestamp_missing")
        timestamp = float(timestamp_match[1])
        _require(
            math.isfinite(timestamp)
            and 0 <= timestamp <= duration
            and timestamp + 1e-6 >= requested,
            "frame_timestamp_invalid",
        )
        _require(output.stat().st_size <= limits.max_frame_bytes, "frame_bytes_exceeded")
        image_bytes = output.read_bytes()
        total += len(image_bytes)
        _require(total <= limits.max_total_frame_bytes, "frame_bytes_exceeded")
        with Image.open(io.BytesIO(image_bytes)) as image:
            frame_width, frame_height = image.size
            _require(
                image.format == "JPEG"
                and 0 < frame_width <= limits.max_side
                and 0 < frame_height <= limits.max_side,
                "frame_bounds_exceeded",
            )
            image.verify()
        frames.append(
            FrameEvidence(
                image_bytes=image_bytes,
                image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                artifact_sha256=artifact_sha,
                timestamp_s=timestamp,
                width=frame_width,
                height=frame_height,
                decoder_version=version,
            )
        )
        _remaining(deadline)
    return tuple(frames)


def obtain_render_review_evidence(
    basis: ReviewBasis,
    *,
    limits: EvidenceLimits = EvidenceLimits(),
    deadline_monotonic: float | None = None,
) -> EvidenceResult:
    """Read one pinned object version and return actual frame bytes, never expired paths."""
    deadline = time.monotonic() + limits.timeout_s
    if deadline_monotonic is not None:
        deadline = min(deadline, deadline_monotonic)
    try:
        _remaining(deadline)
        _require(_resource_limits_supported(), "resource_limits_unavailable")
        proof = _proof(basis)
        _require(proof.file_size_bytes <= limits.max_artifact_bytes, "artifact_size_exceeded")
        metadata = storage.object_metadata_once(basis.object_path, timeout_s=_remaining(deadline))
        _require(
            metadata.path == basis.object_path
            and metadata.generation == proof.storage_generation
            and metadata.size == proof.file_size_bytes
            and metadata.content_type == "video/mp4",
            "artifact_metadata_changed",
        )
        with tempfile.TemporaryDirectory(prefix="render-review-evidence-") as directory:
            local = Path(directory) / "verified.mp4"
            storage.download_generation_to_file(
                basis.object_path,
                str(local),
                generation=proof.storage_generation,
                timeout_s=_remaining(deadline),
            )
            _require(local.stat().st_size <= limits.max_artifact_bytes, "artifact_size_exceeded")
            digest = hashlib.sha256()
            size = 0
            with local.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    _remaining(deadline)
                    size += len(chunk)
                    _require(size <= limits.max_artifact_bytes, "artifact_size_exceeded")
                    digest.update(chunk)
            actual_sha = digest.hexdigest()
            _require(
                size == proof.file_size_bytes and actual_sha == proof.sha256,
                "artifact_bytes_changed",
            )
            frames = _frames(local, actual_sha, limits, deadline)
        receipt = VerifiedArtifactReceipt(
            verification_id="server-read-"
            + hashlib.sha256(
                f"{basis.execution_id}:{proof.storage_generation}:{actual_sha}".encode()
            ).hexdigest(),
            execution_id=basis.execution_id,
            job_id=basis.job_id,
            variant_id=basis.variant_id,
            published_attempt=basis.published_attempt,
            object_path=basis.object_path,
            object_version=proof.storage_generation,
            sha256=actual_sha,
            byte_count=size,
            recipe_digest=basis.recipe_digest,
        )
        observations = tuple(
            TimestampedObservation(
                evidence_id=f"frame-{index}-{frame.image_sha256[:16]}",
                artifact_sha256=actual_sha,
                start_s=frame.timestamp_s,
                end_s=frame.timestamp_s,
                source="direct_frame",
                observer="render-review-frames-v1",
                observation=f"Decoded frame at {frame.timestamp_s:g}s; "
                f"image SHA256 {frame.image_sha256}. "
                "No semantic or audio inspection has been performed.",
            )
            for index, frame in enumerate(frames)
        )
        bundle = EvidenceBundle(verified_artifact=receipt, frames=frames, observations=observations)
        _require(len(bundle.model_dump_json().encode()) <= 2_000_000, "evidence_bytes_exceeded")
        _remaining(deadline)
        return EvidenceResult(status="ready", bundle=bundle)
    except _Unavailable as exc:
        return EvidenceResult(status="unavailable", reason=str(exc))
    except subprocess.TimeoutExpired:
        return EvidenceResult(status="unavailable", reason="decoder_timeout")
    except Exception:  # storage SDK and decoder failures never authorize evidence
        return EvidenceResult(status="unavailable", reason="artifact_or_decoder_unavailable")
