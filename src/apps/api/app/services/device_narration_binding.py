"""Private, generation-pinned narration receipts for device recipes."""

from __future__ import annotations

import hashlib
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app import storage
from app.kria.render_assets import VoiceoverRenderAsset
from app.schemas.edit_proposal import NarrationTrack
from app.services.creator_execution_contract import narration_matches_item
from app.services.guided_speech_cleanup import (
    derivative_object_path,
    has_preflight_snapshot,
    narration_speech_cleanup,
    require_guided_cleanup_binding,
)


class DeviceNarrationBinding(BaseModel):
    """Server-only proof that a recipe's voiceover asset is still authorized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    asset_id: str
    plan_item_id: str
    narration: NarrationTrack
    sha256: str
    byte_count: int


def make_device_narration_binding(
    narration: NarrationTrack, asset: VoiceoverRenderAsset
) -> DeviceNarrationBinding:
    if narration.generation != asset.generation:
        raise ValueError("narration generation differs from recipe asset")
    return DeviceNarrationBinding(
        asset_id=asset.id,
        plan_item_id=asset.plan_item_id,
        narration=narration,
        sha256=asset.fingerprint.sha256,
        byte_count=asset.fingerprint.byte_count,
    )


def _legacy_cleaned_narration(
    assembly: dict[str, Any], *, owner_id: object, item: Any, asset: VoiceoverRenderAsset
) -> NarrationTrack | None:
    """Recover a pre-binding narrated receipt only from immutable evidence."""
    # Guided jobs had their narration in the approved proposal before this receipt.
    guided = assembly.get("guided_edit")
    proposal = guided.get("approved_proposal") if isinstance(guided, dict) else None
    raw = proposal.get("narration") if isinstance(proposal, dict) else None
    if isinstance(raw, dict):
        return NarrationTrack.model_validate(raw)
    if assembly.get("speech_cleanup_contract") != "required_v1":
        return None
    if not has_preflight_snapshot(assembly):
        return None
    from app.pipeline.speech_cleanup_apply import (  # noqa: PLC0415
        cut_fingerprint,
        hydrate_job_speech_cleanup_snapshot,
    )

    snapshot = hydrate_job_speech_cleanup_snapshot(assembly).require_source(
        kind="voiceover", storage_path=str(getattr(item, "voiceover_gcs_path", ""))
    )
    if str(snapshot.generation) != str(getattr(item, "voiceover_generation", "")):
        raise ValueError("cleanup source generation changed")
    path = derivative_object_path(
        owner_id=owner_id,
        item_id=item.id,
        analysis_id=snapshot.analysis_id,
        sha256=asset.fingerprint.sha256,
    )
    metadata = storage.object_metadata(path)
    if (
        str(metadata.generation) != asset.generation
        or int(metadata.size) != asset.fingerprint.byte_count
    ):
        raise ValueError("cleaned derivative metadata changed")
    with tempfile.NamedTemporaryFile() as local:
        storage.download_generation_to_file(path, local.name, generation=asset.generation)
        local.seek(0)
        if hashlib.file_digest(local, "sha256").hexdigest() != asset.fingerprint.sha256:
            raise ValueError("cleaned derivative fingerprint changed")
    return NarrationTrack.model_validate(
        {
            "gcs_path": path,
            "generation": asset.generation,
            "duration_s": sum(end - start for start, end in snapshot.cut_plan.keep_segments),
            "words": [
                {
                    "text": word.text,
                    "start_s": word.start_s,
                    "end_s": word.end_s,
                    "confidence": word.confidence,
                }
                for word in snapshot.transcript(apply_cut=True).words
            ],
            "language": snapshot.analysis.language or "",
            "speech_cleanup": {
                "analysis_id": snapshot.analysis_id,
                "source_gcs_path": snapshot.storage_path,
                "source_generation": snapshot.generation,
                "source_duration_s": snapshot.window_end_s,
                "cut_sha256": cut_fingerprint(snapshot),
            },
        }
    )


def authorized_device_narration(
    record: dict[str, Any],
    *,
    assembly: dict[str, Any],
    owner_id: object,
    item: Any,
    asset: VoiceoverRenderAsset,
) -> NarrationTrack | None:
    """Return a checked clean narration, or ``None`` for an ordinary raw asset."""
    raw_binding = record.get("narration_binding")
    if raw_binding is None:
        narration = _legacy_cleaned_narration(assembly, owner_id=owner_id, item=item, asset=asset)
    else:
        binding = DeviceNarrationBinding.model_validate(raw_binding)
        if (
            binding.asset_id != asset.id
            or binding.plan_item_id != asset.plan_item_id
            or binding.narration.generation != asset.generation
            or binding.sha256 != asset.fingerprint.sha256
            or binding.byte_count != asset.fingerprint.byte_count
        ):
            raise ValueError("device narration binding differs from recipe")
        narration = binding.narration
    require_guided_cleanup_binding(assembly, narration)
    if narration_speech_cleanup(narration) is None:
        return None
    if not narration_matches_item(narration.model_dump(mode="json"), item, owner_id=owner_id):
        raise ValueError("cleaned narration source changed")
    if narration.generation != asset.generation:
        raise ValueError("cleaned narration generation changed")
    return narration
