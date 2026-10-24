"""Prepare an opt-in, pinned engine-noise derivative before phone recipe publication."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import structlog

from app.config import settings
from app.kria.brief_binding import BriefBinding
from app.kria.noise_reduction_ask import engine_noise_requirement_ids
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_processed_audio import (
    ProcessedAudioReceipt,
    processed_audio_storage_path,
    replace_audio_with_processed_segments,
)
from app.pipeline.speech_denoise import reduce_stationary_noise
from app.services.phone_sources import PhoneSourceBinding
from app.storage import upload_local_file_immutable

log = structlog.get_logger()


def engine_noise_reduction_available() -> bool:
    return bool(
        settings.phone_engine_noise_reduction_enabled
        and settings.phone_engine_noise_model_path
        and settings.phone_engine_noise_model_sha256
        and {"processedAudio", "audioMix"}.issubset(settings.phone_render_verified_features)
    )


def prepare_phone_noise_reduction(
    recipe: EditRecipeV2,
    snapshot: dict,
    *,
    source: PhoneSourceBinding,
    local_path: str,
    temporary_dir: str,
    job_id: str,
    multiple_sources: bool,
) -> tuple[EditRecipeV2, ProcessedAudioReceipt | None, dict[str, Any] | None]:
    """No ask means no work. A failure leaves original audio and an explicit miss.

    The derivative is generated from this approved source's analysis proxy audio;
    the original remains immutable. It is a conservative noise filter, not voice
    isolation. The caller must persist the private receipt atomically with the
    exact recipe it publishes after checking generation and ownership.
    """
    try:
        brief = BriefBinding.model_validate(snapshot.get("creator_brief_binding")).resolve()
    except (ValueError, TypeError):
        return recipe, None, None
    ids = engine_noise_requirement_ids(brief.live())
    if not ids:
        return recipe, None, None
    public = {
        "version": 1,
        "requirement_ids": list(ids),
        "status": "not_applied",
        "method": "stationary_noise_reduction",
    }
    if multiple_sources or not engine_noise_reduction_available():
        public["reason"] = "multiple_sources" if multiple_sources else "unavailable"
        return recipe, None, public
    try:
        output = reduce_stationary_noise(
            Path(local_path),
            Path(temporary_dir) / "engine-noise-reduced.m4a",
            model_path=Path(settings.phone_engine_noise_model_path),
            expected_sha256=settings.phone_engine_noise_model_sha256,
        )
        with output.path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        path = processed_audio_storage_path(
            job_id=job_id,
            variant_id="subtitled",
            source_media_id=source.media_id,
            object_token=digest,
        )
        stored = upload_local_file_immutable(str(output.path), path, "audio/mp4")
        receipt = ProcessedAudioReceipt(
            job_id=job_id,
            variant_id="subtitled",
            render_generation=snapshot["creator_generation_id"],
            source_media_id=source.media_id,
            source_generation=source.generation,
            path=path,
            generation=str(stored.generation),
            fingerprint=RenderFingerprint(sha256=digest, byte_count=stored.size),
            duration_s=output.duration_s,
        )
        processed_recipe = replace_audio_with_processed_segments(
            recipe, receipts_by_source={source.media_id: receipt}
        )
        if processed_recipe is recipe:
            raise ValueError("no matching source audio")
    except Exception as exc:  # noqa: BLE001 - optional audio treatment, never lose original
        log.warning("phone_engine_noise_reduction_failed", error_type=type(exc).__name__)
        public["reason"] = "processing_failed"
        return recipe, None, public
    public.update(status="applied", reason="reduced_not_isolated")
    return processed_recipe, receipt, public
