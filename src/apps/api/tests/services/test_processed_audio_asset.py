"""Trust-boundary checks for optional phone processed-audio receipts."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.kria.device_render import make_device_request
from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import OriginalRenderAsset, RenderAssetManifest, RenderFingerprint
from app.pipeline.phone_processed_audio import (
    PHONE_PROCESSED_AUDIO_FIELD,
    ProcessedAudioReceipt,
    processed_audio_asset,
)
from app.routes import device_render as routes
from app.services import device_render
from app.services.creator_render_contract import CreatorRenderContractError


def _state():
    job_id = uuid.uuid4()
    fingerprint = RenderFingerprint(sha256="a" * 64, byte_count=12)
    receipt = ProcessedAudioReceipt(
        job_id=str(job_id),
        variant_id="subtitled",
        render_generation="approved",
        source_media_id="source",
        source_generation="proxy-generation",
        path=f"generative-jobs/{job_id}/phone-processed-audio/subtitled_source_{'b' * 64}.m4a",
        generation="processed-generation",
        fingerprint=fingerprint,
        duration_s=2,
    )
    original = OriginalRenderAsset(id="source", media_id="source", fingerprint=fingerprint)
    processed = processed_audio_asset(receipt, asset_id="processed-audio-source")
    recipe = EditRecipeV2(
        assets=[
            MediaAsset(
                id="source",
                relative_path="source",
                fingerprint=AssetFingerprint(hex="a" * 64, byte_count=12),
                duration=2,
            ),
            MediaAsset(
                id=processed.id,
                relative_path=processed.id,
                fingerprint=AssetFingerprint(hex="a" * 64, byte_count=12),
                duration=2,
            ),
        ],
        tracks=[
            TimelineTrack(
                id="video",
                kind="video",
                clips=[
                    TimelineClip(
                        id="v",
                        source_asset_id="source",
                        source_start=0,
                        source_duration=2,
                        timeline_start=0,
                        rate=1,
                        volume=0,
                    )
                ],
            ),
            TimelineTrack(
                id="processed-source-audio",
                kind="audio",
                clips=[
                    TimelineClip(
                        id="a",
                        source_asset_id=processed.id,
                        source_start=0,
                        source_duration=2,
                        timeline_start=0,
                        rate=1,
                    )
                ],
            ),
        ],
        asset_manifest=RenderAssetManifest(assets=(original, processed)),
        required_capabilities={"processedAudio"},
    )
    request = make_device_request(job_id=job_id, variant_id="subtitled", revision=1, recipe=recipe)
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            PHONE_PROCESSED_AUDIO_FIELD: {"subtitled": {"source": receipt.model_dump(mode="json")}},
        },
    )
    return job, request, processed, receipt


def _bind(monkeypatch, generation="proxy-generation"):
    monkeypatch.setattr(
        device_render,
        "_approved_source_bindings",
        lambda *_: {"source": SimpleNamespace(generation=generation)},
    )


def test_processed_audio_pin_accepts_exact_job_receipt(monkeypatch):
    job, request, _, _ = _state()
    _bind(monkeypatch)
    device_render._verify_processed_audio_identity(job, request, "approved")


@pytest.mark.parametrize(
    "field,value",
    [("source_generation", "replaced"), ("render_generation", "other"), ("job_id", "other-job")],
)
def test_processed_audio_pin_rejects_stale_or_foreign_identity(monkeypatch, field, value):
    job, request, asset, _ = _state()
    _bind(monkeypatch)
    bad = asset.model_copy(update={field: value})
    recipe = request.recipe.model_copy(
        update={
            "asset_manifest": request.recipe.asset_manifest.model_copy(
                update={"assets": (request.recipe.asset_manifest.assets[0], bad)}
            )
        }
    )
    request = make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe)
    with pytest.raises(CreatorRenderContractError, match="processed audio"):
        device_render._verify_processed_audio_identity(job, request, "approved")


def test_processed_audio_grant_rejects_path_outside_exact_namespace(monkeypatch):
    job, request, asset, receipt = _state()
    job.assembly_plan[PHONE_PROCESSED_AUDIO_FIELD]["subtitled"]["source"]["path"] = (
        "generative-jobs/other/phone-processed-audio/x.m4a"
    )
    record = {"base_generation": "approved", "status": {"request": request}}
    monkeypatch.setattr(
        routes,
        "approved_export_sources",
        lambda *_: {"source": SimpleNamespace(generation="proxy-generation")},
    )
    db = SimpleNamespace(rollback=lambda: None)
    with pytest.raises(routes.HTTPException) as error:
        asyncio.run(routes._processed_audio_download_url(db, job, asset, record, request.identity))
    assert error.value.status_code == 409
