"""A proxy may inform analysis but cannot authorize a cloud final render."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.kria.media_sources import (
    AnalysisProxyDescriptor,
    MediaUploadContract,
    OriginalMediaDescriptor,
    require_cloud_source_paths,
)
from app.services.generative_jobs import build_generative_job


def proxy_descriptor():
    return {
        "original": {
            "sha256": "a" * 64,
            "byte_count": 4000,
            "duration_s": 2,
            "width": 1920,
            "height": 1080,
            "orientation_degrees": 90,
            "has_audio": True,
        },
        "duration_s": 2,
        "width": 360,
        "height": 640,
        "frame_rate": 30,
    }


def audio_original(**overrides):
    document = {
        "kind": "audio",
        "sha256": "a" * 64,
        "byte_count": 4000,
        "duration_s": 12,
        "has_audio": True,
    }
    document.update(overrides)
    return document


def audio_proxy_descriptor(**overrides):
    document = {"original": audio_original(), "duration_s": 12}
    document.update(overrides)
    return document


def test_original_media_descriptor_defaults_to_video_kind_for_old_payloads():
    """A payload persisted before `kind` existed decodes exactly as before."""
    document = proxy_descriptor()["original"]
    assert "kind" not in document
    descriptor = OriginalMediaDescriptor.model_validate(document)
    assert descriptor.kind == "video"


def test_video_original_requires_dimensions():
    document = proxy_descriptor()["original"]
    del document["width"]
    with pytest.raises(ValidationError):
        OriginalMediaDescriptor.model_validate(document)


def test_audio_original_forbids_dimensions_and_requires_an_audible_track():
    OriginalMediaDescriptor.model_validate(audio_original())
    with pytest.raises(ValidationError, match="dimensions"):
        OriginalMediaDescriptor.model_validate(audio_original(width=100, height=100))
    with pytest.raises(ValidationError, match="orientation"):
        OriginalMediaDescriptor.model_validate(audio_original(orientation_degrees=90))
    with pytest.raises(ValidationError, match="audible"):
        OriginalMediaDescriptor.model_validate(audio_original(has_audio=False))


def test_audio_proxy_forbids_dimensions_and_frame_rate():
    AnalysisProxyDescriptor.model_validate(audio_proxy_descriptor())
    for field, value in [("width", 100), ("height", 100), ("frame_rate", 30)]:
        with pytest.raises(ValidationError, match="dimensions or frame rate"):
            AnalysisProxyDescriptor.model_validate(audio_proxy_descriptor(**{field: value}))


def test_audio_proxy_still_enforces_complete_timeline_and_verified_registration():
    proxy = AnalysisProxyDescriptor.model_validate(audio_proxy_descriptor())
    proxy.verify_registered(12.02, True)
    with pytest.raises(ValueError):
        proxy.verify_registered(12.02, False)
    document = audio_proxy_descriptor()
    document["duration_s"] = 1
    with pytest.raises(ValidationError):
        AnalysisProxyDescriptor.model_validate(document)


def test_proxy_requires_complete_timeline_original_binding_and_audio():
    proxy = AnalysisProxyDescriptor.model_validate(proxy_descriptor())
    proxy.verify_registered(2.01, True)
    for duration, audio in [(1, True), (2, False)]:
        with pytest.raises(ValueError):
            proxy.verify_registered(duration, audio)
    document = proxy_descriptor()
    document["duration_s"] = 1
    with pytest.raises(ValidationError):
        AnalysisProxyDescriptor.model_validate(document)
    document["duration_s"] = float("nan")
    with pytest.raises(ValidationError):
        AnalysisProxyDescriptor.model_validate(document)
    for contract in [{"purpose": "analysis_proxy"}, {"proxy": proxy_descriptor()}]:
        with pytest.raises(ValidationError):
            MediaUploadContract.model_validate(contract)


def test_cloud_job_constructor_rejects_proxy_before_minting_job():
    user_id = uuid.uuid4()
    path = f"users/{user_id}/creation-threads/{uuid.uuid4()}/analysis-proxy-a.mp4"
    with pytest.raises(ValueError, match="analysis proxies"):
        build_generative_job(user_id=user_id, clip_paths=[path])
    require_cloud_source_paths([f"users/{user_id}/creation-threads/a/original.mp4"])


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("proxy_voiceover", [False, True])
def test_old_or_redelivered_proxy_job_never_enters_cloud_pipeline(cancelled, proxy_voiceover):
    from app.tasks import generative_build

    job = SimpleNamespace(
        id=uuid.uuid4(),
        status="cancelled" if cancelled else "queued",
        assembly_plan={},
        raw_storage_path="users/a/creation-threads/b/analysis-proxy-a.mp4",
        all_candidates={"clip_paths": ["users/a/creation-threads/b/analysis-proxy-a.mp4"]},
    )
    if proxy_voiceover:
        job.all_candidates["voiceover_gcs_path"] = job.raw_storage_path
        job.raw_storage_path = "users/a/creation-threads/b/original.mp4"
        job.all_candidates["clip_paths"] = [job.raw_storage_path]
    db = MagicMock()
    with (
        patch.object(generative_build, "_sync_session") as session,
        patch.object(generative_build, "_lock_owned_entry_job", return_value=(job, None)),
        patch("app.services.creator_direction_snapshot.ensure_job_snapshot") as snapshot,
    ):
        session.return_value.__enter__.return_value = db
        generative_build._run_generative_job_impl(str(job.id))
        snapshot.assert_not_called()
    assert job.status == ("cancelled" if cancelled else "processing_failed")
    assert db.commit.call_count == int(not cancelled)
