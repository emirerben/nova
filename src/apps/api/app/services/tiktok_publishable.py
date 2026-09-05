"""Canonical owned-output resolution for TikTok publishing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app import storage
from app.models import Job
from app.services.public_assembly_plan import project_public_assembly_plan_with_metadata


class PublishableOutputError(ValueError):
    pass


TIKTOK_PUBLISHABLE_JOB_STATUSES: frozenset[str] = frozenset(
    {
        "done",
        "clips_ready",
        "clips_ready_partial",
        "template_ready",
        "music_ready",
        "variants_ready",
        "variants_ready_partial",
    }
)


def job_is_terminal_ready(job: Job) -> bool:
    """Return whether ``job`` has finished rendering a publishable output.

    A ready variant may exist while a generative Job is still ``rendering``.
    Publishing during that window races later variant writes and, more
    importantly, lets an admin cancellation overlap a queued TikTok submit.
    Keep the release boundary on terminal success states instead.
    """
    return str(job.status or "") in TIKTOK_PUBLISHABLE_JOB_STATUSES


def require_terminal_ready_job(job: Job) -> None:
    if str(job.status or "") == "cancelled":
        raise PublishableOutputError("Cancelled videos cannot be published")
    if not job_is_terminal_ready(job):
        raise PublishableOutputError("The video is still being prepared and cannot be published")


@dataclass(frozen=True)
class PublishableOutput:
    object_path: str
    generation: str
    etag: str | None
    size: int
    content_type: str
    duration_s: float | None
    preview_url: str
    variant_id: str | None
    source_revision: str
    edit_signature: dict[str, Any]


def resolve_publishable_output(job: Job, variant_id: str | None = None) -> PublishableOutput:
    require_terminal_ready_job(job)
    projection = project_public_assembly_plan_with_metadata(job.assembly_plan)
    plan = dict(projection.value) if isinstance(projection.value, dict) else {}
    selected_variant: dict[str, Any] | None = None
    path: str | None = None

    variants = plan.get("variants")
    if isinstance(variants, list):
        ready = [
            v
            for v in variants
            if isinstance(v, dict) and v.get("render_status") == "ready" and v.get("video_path")
        ]
        if variant_id:
            selected_variant = next((v for v in ready if v.get("variant_id") == variant_id), None)
        elif ready:
            selected_variant = ready[0]
        if selected_variant is None:
            raise PublishableOutputError("The selected video is not ready to publish")
        path = str(selected_variant["video_path"])
        variant_id = str(selected_variant.get("variant_id") or "") or None
    elif projection.active_speech_projection:
        # A malformed active plan can lose its variant vector.  Do not bypass
        # the public projection by synthesizing a conventional template/music
        # object key after it deliberately removed every media locator.
        raise PublishableOutputError("The selected video is not ready to publish")
    else:
        explicit = plan.get("output_path")
        if isinstance(explicit, str) and explicit:
            path = explicit
        elif (job.mode or job.job_type) == "template":
            path = f"jobs/{job.id}/template_output.mp4"
        elif (job.mode or job.job_type) in {"music", "auto_music"}:
            path = f"music-jobs/{job.id}/output.mp4"

    if not path:
        raise PublishableOutputError("This older video must be re-rendered before publishing")
    if not _owned_output_path(job, path):
        raise PublishableOutputError("The video does not have a trusted Nova storage identity")

    meta = storage.object_metadata(path)
    revision_payload = {
        "job_id": str(job.id),
        "variant_id": variant_id,
        "path": path,
        "generation": meta.generation,
        "etag": meta.etag,
    }
    revision = hashlib.sha256(
        json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    preview = storage.signed_get_url(path, expiration_minutes=60)
    return PublishableOutput(
        object_path=path,
        generation=meta.generation,
        etag=meta.etag,
        size=meta.size,
        content_type=meta.content_type,
        duration_s=_duration_seconds(job, selected_variant),
        preview_url=preview,
        variant_id=variant_id,
        source_revision=revision,
        edit_signature=_edit_signature(job, selected_variant),
    )


def _duration_seconds(job: Job, variant: dict[str, Any] | None) -> float | None:
    candidates = [
        (variant or {}).get("duration_s"),
        (job.assembly_plan or {}).get("duration_s"),
        (job.probe_metadata or {}).get("duration"),
        (job.probe_metadata or {}).get("duration_s"),
    ]
    for value in candidates:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    return None


def _owned_output_path(job: Job, path: str) -> bool:
    prefixes = (
        f"generative-jobs/{job.id}/",
        f"jobs/{job.id}/",
        f"music-jobs/{job.id}/",
        f"auto-music-jobs/{job.id}/",
        f"users/{job.user_id}/",
    )
    return path.startswith(prefixes) and ".." not in path


def _edit_signature(job: Job, variant: dict[str, Any] | None) -> dict[str, Any]:
    variant = variant or {}
    plan = job.assembly_plan or {}
    duration = variant.get("duration_s") or plan.get("duration_s")
    try:
        duration_value = float(duration)
    except (TypeError, ValueError):
        duration_value = 0
    duration_bucket = "under_15s" if duration_value and duration_value < 15 else "15_to_30s"
    if duration_value >= 30:
        duration_bucket = "30s_plus"
    return {
        "archetype": str(variant.get("archetype") or job.mode or job.job_type or "unknown")[:40],
        "duration_bucket": duration_bucket,
        "text_mode": str(variant.get("text_mode") or "unknown")[:40],
        "music": bool(variant.get("music_track_id") or job.music_track_id),
        "style_family": str(variant.get("style_set_id") or "unknown")[:80],
    }
