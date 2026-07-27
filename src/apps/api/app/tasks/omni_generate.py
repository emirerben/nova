"""Async Gemini Omni short-video generation for editor suggestions."""

from __future__ import annotations

import base64
import copy
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.models import Job
from app.pipeline.probe import probe_video
from app.pipeline.reframe import reframe_and_export
from app.storage import (
    delete_object_best_effort,
    download_to_file,
    upload_public_read,
)
from app.worker import celery_app

log = structlog.get_logger()
_TERMINAL = {"ready", "failed", "cancelled"}
_PROVIDER_TERMINAL = {"completed", "failed", "cancelled", "incomplete", "budget_exceeded"}
_MAX_PROVIDER_VIDEO_BYTES = 150 * 1024 * 1024
_MAX_PROVIDER_REDIRECTS = 3
_PROVIDER_MEDIA_HOST_SUFFIXES = (".googleapis.com", ".googleusercontent.com")


def _locked_job(session, job_id: str) -> Job:  # noqa: ANN001
    job = session.scalar(select(Job).where(Job.id == uuid.UUID(job_id)).with_for_update())
    if job is None:
        raise ValueError("job_not_found")
    return job


def _record(job: Job, asset_id: str) -> tuple[dict, dict]:
    assembly = copy.deepcopy(job.assembly_plan or {})
    records = assembly.get("omni_generated_assets")
    if not isinstance(records, dict):
        raise ValueError("omni_asset_state_missing")
    record = records.get(asset_id)
    if not isinstance(record, dict):
        raise ValueError("omni_asset_not_found")
    return assembly, record


def _update(job_id: str, asset_id: str, **patch: Any) -> dict:
    with sync_session() as session:
        job = _locked_job(session, job_id)
        assembly, record = _record(job, asset_id)
        record.update(patch)
        job.assembly_plan = assembly
        session.commit()
        return copy.deepcopy(record)


def _read(job_id: str, asset_id: str) -> tuple[Job, dict]:
    with sync_session() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            raise ValueError("job_not_found")
        _, record = _record(job, asset_id)
        session.expunge(job)
        return job, copy.deepcopy(record)


def _cancelled(job_id: str, asset_id: str) -> bool:
    _, record = _read(job_id, asset_id)
    return record.get("status") in {"cancellation_requested", "cancelled"}


def _input_parts(
    record: dict,
    clip_paths: list[str],
    workdir: str,
) -> str | list[dict]:
    prompt = str(record["prompt"])
    action = record["action"]
    if action == "restyle_segment":
        source_index = int(record["source_clip_index"])
        source_path = os.path.join(workdir, "source.mp4")
        source_segment = os.path.join(workdir, "source-segment.mp4")
        download_to_file(clip_paths[source_index], source_path)
        start_s = float(record["source_start_s"])
        duration_s = float(record["source_end_s"]) - start_s
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start_s:.3f}",
                "-t",
                f"{duration_s:.3f}",
                "-i",
                source_path,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                source_segment,
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError("source_segment_export_failed")
        return [
            {"type": "text", "text": prompt},
            {
                "type": "video",
                "data": source_segment,
                "mime_type": "video/mp4",
            },
        ]

    reference_index = record.get("reference_clip_index")
    if reference_index is None:
        return prompt
    source_path = os.path.join(workdir, "reference-source.mp4")
    frame_path = os.path.join(workdir, "reference.jpg")
    download_to_file(clip_paths[int(reference_index)], source_path)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{float(record['reference_frame_s']):.3f}",
            "-i",
            source_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            frame_path,
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("reference_frame_export_failed")
    return [
        {"type": "text", "text": prompt},
        {"type": "image", "data": frame_path, "mime_type": "image/jpeg"},
    ]


def _upload_interaction_inputs(client: Any, input_parts: str | list[dict]) -> tuple[Any, list[str]]:
    if isinstance(input_parts, str):
        return input_parts, []
    uploaded_names: list[str] = []
    resolved: list[dict] = []
    try:
        for part in input_parts:
            local_path = part.get("data")
            if part.get("type") not in {"image", "video"} or not isinstance(local_path, str):
                resolved.append(part)
                continue
            file_ref = client.files.upload(file=local_path)
            uploaded_names.append(str(file_ref.name))
            deadline = time.monotonic() + 120
            while True:
                state = getattr(file_ref, "state", "")
                state_name = getattr(state, "name", str(state))
                if state_name == "ACTIVE":
                    break
                if state_name == "FAILED" or time.monotonic() >= deadline:
                    raise RuntimeError("omni_input_upload_failed")
                time.sleep(2)
                file_ref = client.files.get(name=file_ref.name)
            resolved.append(
                {
                    "type": part["type"],
                    "uri": str(file_ref.uri),
                    "mime_type": part["mime_type"],
                }
            )
        return resolved, uploaded_names
    except Exception:
        for name in uploaded_names:
            try:
                client.files.delete(name=name)
            except Exception:  # noqa: BLE001
                pass
        raise


def _write_provider_video(output_video: Any, path: str) -> None:
    data = getattr(output_video, "data", None)
    if data:
        if len(data) > (_MAX_PROVIDER_VIDEO_BYTES * 4 // 3) + 4:
            raise RuntimeError("omni_inline_video_too_large")
        try:
            decoded = base64.b64decode(data, validate=True)
            if len(decoded) > _MAX_PROVIDER_VIDEO_BYTES:
                raise RuntimeError("omni_inline_video_too_large")
            Path(path).write_bytes(decoded)
            return
        except (ValueError, TypeError) as exc:
            raise RuntimeError("omni_invalid_inline_video") from exc
    uri = getattr(output_video, "uri", None)
    if not uri:
        raise RuntimeError("omni_missing_video_output")

    def _validated_url(value: str) -> tuple[str, str]:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not any(hostname.endswith(suffix) for suffix in _PROVIDER_MEDIA_HOST_SUFFIXES)
        ):
            raise RuntimeError("omni_untrusted_video_uri")
        return value, hostname

    current_url, current_host = _validated_url(str(uri))
    with httpx.Client(timeout=120.0, follow_redirects=False) as client:
        for redirect_count in range(_MAX_PROVIDER_REDIRECTS + 1):
            headers = (
                {"x-goog-api-key": settings.gemini_api_key}
                if current_host.endswith(".googleapis.com")
                else {}
            )
            with client.stream("GET", current_url, headers=headers) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirect_count == _MAX_PROVIDER_REDIRECTS:
                        raise RuntimeError("omni_invalid_video_redirect")
                    current_url, current_host = _validated_url(urljoin(current_url, location))
                    continue
                response.raise_for_status()
                total = 0
                with Path(path).open("wb") as output:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_PROVIDER_VIDEO_BYTES:
                            raise RuntimeError("omni_provider_video_too_large")
                        output.write(chunk)
                return
    raise RuntimeError("omni_video_download_failed")


def _normalize(source_path: str, output_path: str, requested_duration_s: float) -> float:
    probe = probe_video(source_path)
    duration_s = min(float(probe.duration_s or 0.0), requested_duration_s, 10.0)
    if duration_s < 0.1:
        raise RuntimeError("omni_empty_video_output")
    reframe_and_export(
        source_path,
        0.0,
        duration_s,
        "9:16",
        None,
        output_path,
        has_audio=probe.has_audio,
    )
    normalized = probe_video(output_path)
    if normalized.width != settings.output_width or normalized.height != settings.output_height:
        raise RuntimeError("omni_normalization_dimensions_failed")
    if normalized.codec != "h264" or not normalized.has_audio:
        raise RuntimeError("omni_normalization_codec_failed")
    return min(duration_s, float(normalized.duration_s or duration_s))


def _commit_ready(
    job_id: str,
    asset_id: str,
    *,
    storage_path: str,
    output_url: str,
    duration_s: float,
) -> bool:
    with sync_session() as session:
        job = _locked_job(session, job_id)
        assembly, record = _record(job, asset_id)
        if record.get("status") in {"cancellation_requested", "cancelled"}:
            record.update(status="cancelled", progress=0.0, error=None)
            job.assembly_plan = assembly
            session.commit()
            delete_object_best_effort(storage_path)
            return False
        record.update(
            status="ready",
            progress=1.0,
            storage_path=storage_path,
            output_url=output_url,
            normalized_duration_s=round(duration_s, 3),
            error=None,
        )
        job.assembly_plan = assembly
        session.commit()
        return True


@celery_app.task(
    name="tasks.cleanup_unclaimed_omni_asset",
    soft_time_limit=90,
    time_limit=120,
)
def cleanup_unclaimed_omni_asset(*, job_id: str, asset_id: str) -> None:
    """Expire a ready asset that was never claimed into the editor clip pool."""
    storage_path: str | None = None
    with sync_session() as session:
        job = _locked_job(session, job_id)
        assembly, record = _record(job, asset_id)
        if record.get("status") != "ready" or record.get("operation") is not None:
            return
        storage_path = str(record.get("storage_path") or "") or None
        record.update(
            status="cancelled",
            progress=0.0,
            storage_path=None,
            output_url=None,
            error="generated_asset_expired",
        )
        job.assembly_plan = assembly
        session.commit()
    if storage_path:
        delete_object_best_effort(storage_path)


@celery_app.task(
    bind=True,
    name="tasks.generate_omni_asset",
    soft_time_limit=840,
    time_limit=900,
    acks_late=True,
)
def generate_omni_asset(self, *, job_id: str, asset_id: str) -> None:  # noqa: ANN001
    """Generate, normalize, and register one optional editor source clip."""
    storage_path: str | None = None
    provider_client: Any = None
    provider_input_names: list[str] = []
    try:
        if not settings.omni_generated_video_enabled:
            _update(job_id, asset_id, status="failed", progress=0.0, error="feature_disabled")
            return
        if not settings.gemini_api_key:
            raise RuntimeError("gemini_api_key_missing")

        job, record = _read(job_id, asset_id)
        if record.get("status") in _TERMINAL or _cancelled(job_id, asset_id):
            _update(job_id, asset_id, status="cancelled", progress=0.0)
            return
        clip_paths = list((job.all_candidates or {}).get("clip_paths") or [])
        _update(job_id, asset_id, status="generating", progress=0.08, error=None)

        with tempfile.TemporaryDirectory(prefix=f"nova-omni-{asset_id[:8]}-") as workdir:
            from google import genai  # type: ignore[import]  # noqa: PLC0415

            input_parts = _input_parts(record, clip_paths, workdir)
            task = (
                "edit"
                if record["action"] == "restyle_segment"
                else (
                    "image_to_video"
                    if record.get("reference_clip_index") is not None
                    else "text_to_video"
                )
            )
            provider_client = genai.Client(api_key=settings.gemini_api_key)
            input_parts, provider_input_names = _upload_interaction_inputs(
                provider_client,
                input_parts,
            )
            interaction = provider_client.interactions.create(
                model=settings.edit_omni_model,
                input=input_parts,
                background=True,
                store=True,
                response_modalities=["video"],
                response_format={
                    "type": "video",
                    "delivery": "inline",
                    "aspect_ratio": "9:16",
                    "duration": f"{float(record['duration_s']):g}s",
                },
                generation_config={"video_config": {"task": task}},
                timeout=90.0,
            )
            interaction_id = str(getattr(interaction, "id", "") or "")
            if not interaction_id:
                raise RuntimeError("omni_missing_interaction_id")
            _update(
                job_id,
                asset_id,
                provider_interaction_id=interaction_id,
                progress=0.15,
            )

            deadline = time.monotonic() + 720
            progress_recorded = False
            while str(getattr(interaction, "status", "")) not in _PROVIDER_TERMINAL:
                if _cancelled(job_id, asset_id):
                    try:
                        provider_client.interactions.cancel(interaction_id, timeout=20.0)
                    except Exception:  # noqa: BLE001
                        pass
                    _update(job_id, asset_id, status="cancelled", progress=0.0)
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("omni_generation_timeout")
                time.sleep(10)
                interaction = provider_client.interactions.get(interaction_id, timeout=60.0)
                if not progress_recorded:
                    _update(job_id, asset_id, progress=0.55)
                    progress_recorded = True

            provider_status = str(getattr(interaction, "status", "failed"))
            if provider_status == "cancelled":
                _update(job_id, asset_id, status="cancelled", progress=0.0)
                return
            if provider_status != "completed":
                raise RuntimeError(f"omni_provider_{provider_status}")

            raw_path = os.path.join(workdir, "provider-output.mp4")
            normalized_path = os.path.join(workdir, "normalized.mp4")
            _write_provider_video(getattr(interaction, "output_video", None), raw_path)
            _update(job_id, asset_id, status="normalizing", progress=0.82)
            duration_s = _normalize(raw_path, normalized_path, float(record["duration_s"]))
            if _cancelled(job_id, asset_id):
                _update(job_id, asset_id, status="cancelled", progress=0.0)
                return

            storage_path = f"generative-jobs/{job_id}/omni/{asset_id}.mp4"
            output_url = upload_public_read(normalized_path, storage_path)
            ready = _commit_ready(
                job_id,
                asset_id,
                storage_path=storage_path,
                output_url=output_url,
                duration_s=duration_s,
            )
            if ready:
                cleanup_unclaimed_omni_asset.apply_async(
                    kwargs={"job_id": job_id, "asset_id": asset_id},
                    task_id=f"omni-cleanup-{asset_id}",
                    countdown=24 * 60 * 60,
                )
            if ready:
                log.info(
                    "omni_asset.ready",
                    job_id=job_id,
                    asset_id=asset_id,
                    model=settings.edit_omni_model,
                    storage_path=storage_path,
                )
    except SoftTimeLimitExceeded:
        if storage_path:
            delete_object_best_effort(storage_path)
        _update(job_id, asset_id, status="failed", progress=0.0, error="generation_timeout")
        raise
    except Exception as exc:  # noqa: BLE001
        if storage_path:
            delete_object_best_effort(storage_path)
        try:
            if _cancelled(job_id, asset_id):
                _update(job_id, asset_id, status="cancelled", progress=0.0, error=None)
            else:
                _update(
                    job_id,
                    asset_id,
                    status="failed",
                    progress=0.0,
                    error=str(exc)[:300],
                )
        except Exception:  # noqa: BLE001
            log.exception("omni_asset.state_update_failed", job_id=job_id, asset_id=asset_id)
        log.exception("omni_asset.failed", job_id=job_id, asset_id=asset_id)
    finally:
        if provider_client is not None:
            for name in provider_input_names:
                try:
                    provider_client.files.delete(name=name)
                except Exception:  # noqa: BLE001
                    log.info(
                        "omni_asset.input_cleanup_failed",
                        job_id=job_id,
                        asset_id=asset_id,
                        provider_file=name,
                    )
