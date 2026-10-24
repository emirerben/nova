"""Private receipts and public identities for phone noise-reduced audio.

Only worker-owned code may write these receipts.  A portable recipe exposes an
identity and hash, never the storage path.  The device-download route rechecks
the receipt against the Job's source binding and immutable device request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.kria.render_assets import ProcessedAudioRenderAsset, RenderFingerprint

if TYPE_CHECKING:
    from app.kria.recipes_v2 import EditRecipeV2

PHONE_PROCESSED_AUDIO_FIELD = "_phone_processed_audio_v1"


class ProcessedAudioReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    job_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    variant_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    render_generation: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    source_media_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    source_generation: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    path: str = Field(min_length=1, max_length=1024)
    generation: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    fingerprint: RenderFingerprint
    duration_s: float = Field(gt=0, le=60 * 60)


def processed_audio_storage_path(
    *,
    job_id: UUID | str,
    variant_id: str,
    source_media_id: str,
    object_token: str | None = None,
) -> str:
    """Make a unique, retention-owned object name for one processing attempt."""
    object_token = object_token or uuid4().hex
    for value in (variant_id, source_media_id, object_token):
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("invalid processed-audio identity")
    return (
        f"generative-jobs/{job_id}/phone-processed-audio/"
        f"{variant_id}_{source_media_id}_{object_token}.m4a"
    )


def processed_audio_asset(
    receipt: ProcessedAudioReceipt, *, asset_id: str
) -> ProcessedAudioRenderAsset:
    """Make the path-free identity permitted in a portable recipe."""
    return ProcessedAudioRenderAsset(
        id=asset_id,
        job_id=receipt.job_id,
        variant_id=receipt.variant_id,
        render_generation=receipt.render_generation,
        source_media_id=receipt.source_media_id,
        source_generation=receipt.source_generation,
        generation=receipt.generation,
        fingerprint=receipt.fingerprint,
    )


def processed_audio_receipt_for(
    assembly: dict, *, variant_id: str, source_media_id: str
) -> ProcessedAudioReceipt | None:
    """Read exactly one private, worker-persisted receipt; never request input."""
    rows = assembly.get(PHONE_PROCESSED_AUDIO_FIELD)
    variant = rows.get(variant_id) if isinstance(rows, dict) else None
    raw = variant.get(source_media_id) if isinstance(variant, dict) else None
    return ProcessedAudioReceipt.model_validate(raw) if isinstance(raw, dict) else None


def replace_audio_with_processed_segments(
    recipe: EditRecipeV2, *, receipts_by_source: dict[str, ProcessedAudioReceipt]
) -> EditRecipeV2:
    """Replace each eligible footage-audio segment with its matching derivative.

    Video clips keep their original bytes and timing.  Their embedded audio is
    muted and an audio-track clip points at the processed asset using the same
    source start, duration, rate and timeline start.  This is intentionally
    segment-for-segment; it never lays one full-length audio file across the
    edit timeline.
    """
    # Keep importing the full recipe stack off the small receipt/read helpers:
    # they run on the grant path and must not initialize storage configuration.
    from app.kria.recipes import (  # noqa: PLC0415
        AssetFingerprint,
        MediaAsset,
        TimelineClip,
        TimelineTrack,
    )

    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    assets = {asset.id: asset for asset in recipe.assets}
    processed_ids: dict[str, str] = {}
    processed_clips: list[TimelineClip] = []
    tracks: list[TimelineTrack] = []

    for track in recipe.tracks:
        if track.kind != "video":
            tracks.append(track)
            continue
        updated: list[TimelineClip] = []
        for clip in track.clips:
            source = manifest.get(clip.source_asset_id)
            media_id = getattr(source, "media_id", None)
            receipt = receipts_by_source.get(media_id) if isinstance(media_id, str) else None
            if receipt is None:
                updated.append(clip)
                continue
            # AVFoundation refuses a range that runs beyond the downloaded
            # asset.  A container-duration tolerance here would create a
            # late-render failure, so optional processing fails closed.
            if clip.source_start + clip.source_duration > receipt.duration_s + 1e-6:
                raise ValueError("processed audio does not cover an approved source segment")
            processed_id = processed_ids.get(media_id)
            if processed_id is None:
                processed_id = f"processed-audio-{media_id}"
                processed_ids[media_id] = processed_id
                asset = processed_audio_asset(receipt, asset_id=processed_id)
                manifest[processed_id] = asset
                assets[processed_id] = MediaAsset(
                    id=processed_id,
                    relative_path=processed_id,
                    fingerprint=AssetFingerprint(
                        hex=asset.fingerprint.sha256, byte_count=asset.fingerprint.byte_count
                    ),
                    duration=receipt.duration_s,
                )
            updated.append(clip.model_copy(update={"volume": 0.0}))
            processed_clips.append(
                TimelineClip(
                    id=f"processed-{clip.id}",
                    source_asset_id=processed_id,
                    source_start=clip.source_start,
                    source_duration=clip.source_duration,
                    timeline_start=clip.timeline_start,
                    rate=clip.rate,
                    volume=clip.volume,
                    audio_fade_in=clip.audio_fade_in,
                    audio_fade_out=clip.audio_fade_out,
                )
            )
        tracks.append(track.model_copy(update={"clips": updated}))
    if not processed_clips:
        return recipe
    tracks.append(TimelineTrack(id="processed-source-audio", kind="audio", clips=processed_clips))
    return recipe.model_copy(
        update={
            "assets": list(assets.values()),
            "asset_manifest": recipe.asset_manifest.model_copy(
                update={"assets": tuple(manifest.values())}
            ),
            "tracks": tracks,
            "required_capabilities": recipe.required_capabilities | {"processedAudio"},
        }
    )
