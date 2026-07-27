from __future__ import annotations

import base64
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routes import _omni
from app.routes._omni import (
    OmniAssetClaimBody,
    OmniAssetStartBody,
    cancel_omni_asset,
    claim_omni_asset,
    omni_response,
    start_omni_asset,
)
from app.tasks import omni_generate


def _record(**overrides) -> dict:
    return {
        "asset_id": "asset-1",
        "status": "normalizing",
        "progress": 0.82,
        "model": "gemini-omni-flash-preview",
        "draft_revision": "v1-test",
        "prompt": "A restrained film-burn bridge",
        "insert_at_s": 3.0,
        "duration_s": 4.0,
        **overrides,
    }


def test_restyle_requires_an_explicit_bounded_source_segment() -> None:
    with pytest.raises(ValidationError, match="explicit source segment"):
        OmniAssetStartBody(
            draft_revision="v1-test",
            suggestion_id="suggestion-1",
            action="restyle_segment",
            prompt="Restyle it",
            insert_at_s=2,
            duration_s=4,
        )

    with pytest.raises(ValidationError, match="cannot exceed 10 seconds"):
        OmniAssetStartBody(
            draft_revision="v1-test",
            suggestion_id="suggestion-1",
            action="restyle_segment",
            prompt="Restyle it",
            insert_at_s=2,
            duration_s=4,
            source_clip_index=0,
            source_start_s=1,
            source_end_s=11.1,
        )


def test_reference_frame_contract_is_paired() -> None:
    with pytest.raises(ValidationError, match="provided together"):
        OmniAssetStartBody(
            draft_revision="v1-test",
            suggestion_id="suggestion-1",
            action="generate_insert",
            prompt="Generate a bridge",
            insert_at_s=2,
            duration_s=4,
            reference_clip_index=0,
        )


def test_action_specific_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="does not accept a source segment"):
        OmniAssetStartBody(
            draft_revision="v1-test",
            suggestion_id="suggestion-1",
            action="generate_insert",
            prompt="Generate a bridge",
            insert_at_s=2,
            duration_s=4,
            source_clip_index=0,
        )
    with pytest.raises(ValidationError, match="does not accept a reference frame"):
        OmniAssetStartBody(
            draft_revision="v1-test",
            suggestion_id="suggestion-1",
            action="restyle_segment",
            prompt="Restyle it",
            insert_at_s=2,
            duration_s=4,
            source_clip_index=0,
            source_start_s=0,
            source_end_s=4,
            reference_clip_index=0,
            reference_frame_s=1,
        )


def test_inline_provider_video_is_decoded(tmp_path) -> None:
    output = tmp_path / "provider.mp4"
    omni_generate._write_provider_video(  # noqa: SLF001
        SimpleNamespace(data=base64.b64encode(b"video-bytes").decode(), uri=None),
        str(output),
    )
    assert output.read_bytes() == b"video-bytes"


def test_provider_video_rejects_untrusted_uri(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="untrusted_video_uri"):
        omni_generate._write_provider_video(  # noqa: SLF001
            SimpleNamespace(data=None, uri="http://169.254.169.254/latest/meta-data"),
            str(tmp_path / "provider.mp4"),
        )


def test_interaction_media_inputs_are_uploaded_as_provider_uris(tmp_path) -> None:
    media_path = tmp_path / "reference.jpg"
    media_path.write_bytes(b"image")
    file_ref = SimpleNamespace(
        name="files/reference-1",
        uri="https://generativelanguage.googleapis.com/files/reference-1",
        state=SimpleNamespace(name="ACTIVE"),
    )
    files = SimpleNamespace(
        upload=MagicMock(return_value=file_ref),
        get=MagicMock(),
        delete=MagicMock(),
    )

    resolved, uploaded_names = omni_generate._upload_interaction_inputs(  # noqa: SLF001
        SimpleNamespace(files=files),
        [
            {"type": "text", "text": "Create a bridge"},
            {"type": "image", "data": str(media_path), "mime_type": "image/jpeg"},
        ],
    )

    assert resolved == [
        {"type": "text", "text": "Create a bridge"},
        {
            "type": "image",
            "uri": file_ref.uri,
            "mime_type": "image/jpeg",
        },
    ]
    assert uploaded_names == ["files/reference-1"]
    files.upload.assert_called_once_with(file=str(media_path))


def test_failed_interaction_input_upload_cleans_provider_file(tmp_path) -> None:
    media_path = tmp_path / "segment.mp4"
    media_path.write_bytes(b"video")
    file_ref = SimpleNamespace(
        name="files/segment-1",
        uri=None,
        state=SimpleNamespace(name="FAILED"),
    )
    files = SimpleNamespace(
        upload=MagicMock(return_value=file_ref),
        get=MagicMock(),
        delete=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="omni_input_upload_failed"):
        omni_generate._upload_interaction_inputs(  # noqa: SLF001
            SimpleNamespace(files=files),
            [{"type": "video", "data": str(media_path), "mime_type": "video/mp4"}],
        )

    files.delete.assert_called_once_with(name="files/segment-1")


def test_normalize_enforces_timeline_dimensions_codec_and_audio(monkeypatch, tmp_path) -> None:
    probes = iter(
        [
            SimpleNamespace(duration_s=5.0, has_audio=False),
            SimpleNamespace(
                duration_s=4.0,
                has_audio=True,
                codec="h264",
                width=1080,
                height=1920,
            ),
        ]
    )
    reframe = MagicMock()
    monkeypatch.setattr(omni_generate, "probe_video", lambda _path: next(probes))
    monkeypatch.setattr(omni_generate, "reframe_and_export", reframe)

    duration = omni_generate._normalize(  # noqa: SLF001
        str(tmp_path / "source.mp4"),
        str(tmp_path / "normalized.mp4"),
        4.0,
    )

    assert duration == 4.0
    assert reframe.call_args.kwargs["has_audio"] is False


def test_ready_commit_waits_for_explicit_claim(monkeypatch) -> None:
    record = _record()
    job = SimpleNamespace(
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    session = SimpleNamespace(commit=lambda: None)

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(omni_generate, "sync_session", fake_session)
    monkeypatch.setattr(omni_generate, "_locked_job", lambda _session, _job_id: job)

    omni_generate._commit_ready(  # noqa: SLF001
        "00000000-0000-0000-0000-000000000001",
        "asset-1",
        storage_path="generative-jobs/job/omni/asset-1.mp4",
        output_url="https://storage.example/asset-1",
        duration_s=4.2345,
    )

    assert job.all_candidates == {"clip_paths": ["source-a.mp4"]}
    ready = job.assembly_plan["omni_generated_assets"]["asset-1"]
    assert ready["status"] == "ready"
    assert ready["normalized_duration_s"] == 4.234
    assert ready.get("operation") is None
    assert omni_response(ready).operation is None


@pytest.mark.asyncio
async def test_claim_atomically_appends_clip_and_emits_operation(monkeypatch) -> None:
    record = _record(
        status="ready",
        storage_path="generative-jobs/job/omni/asset-1.mp4",
        normalized_duration_s=4.234,
        operation=None,
    )
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(
        "app.routes._omni._lock_job",
        AsyncMock(return_value=job),
    )

    response = await claim_omni_asset(
        job,
        "asset-1",
        OmniAssetClaimBody(draft_revision="v1-test"),
        db,
    )

    assert job.all_candidates["clip_paths"][-1] == "generative-jobs/job/omni/asset-1.mp4"
    assert response.operation is not None
    assert response.operation.clip_index == 1
    assert response.operation.duration_s == 4.234


@pytest.mark.asyncio
async def test_claim_restyle_emits_atomic_replacement_operation(monkeypatch) -> None:
    record = _record(
        status="ready",
        action="restyle_segment",
        source_clip_index=0,
        source_start_s=1.0,
        source_end_s=5.0,
        storage_path="generative-jobs/job/omni/asset-1.mp4",
        normalized_duration_s=4.0,
        operation=None,
    )
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr("app.routes._omni._lock_job", AsyncMock(return_value=job))

    response = await claim_omni_asset(
        job,
        "asset-1",
        OmniAssetClaimBody(draft_revision="v1-test"),
        db,
    )

    assert response.operation is not None
    assert response.operation.op == "replace_generated_segment"
    assert response.operation.source_clip_index == 0
    assert response.operation.source_start_s == 1.0
    assert response.operation.source_end_s == 5.0
    assert job.all_candidates["clip_paths"][-1].endswith("asset-1.mp4")


@pytest.mark.asyncio
async def test_claim_rejects_non_ready_asset(monkeypatch) -> None:
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"omni_generated_assets": {"asset-1": _record(status="generating")}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    db = SimpleNamespace(commit=None)
    monkeypatch.setattr(
        "app.routes._omni._lock_job",
        AsyncMock(return_value=job),
    )
    with pytest.raises(HTTPException) as exc:
        await claim_omni_asset(
            job,
            "asset-1",
            OmniAssetClaimBody(draft_revision="v1-test"),
            db,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_claim_rejects_a_different_draft_revision(monkeypatch) -> None:
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={
            "omni_generated_assets": {
                "asset-1": _record(
                    status="ready",
                    storage_path="generative-jobs/job/omni/asset-1.mp4",
                    normalized_duration_s=4.0,
                )
            }
        },
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))

    with pytest.raises(HTTPException) as exc:
        await claim_omni_asset(
            job,
            "asset-1",
            OmniAssetClaimBody(draft_revision="stale-draft"),
            SimpleNamespace(commit=AsyncMock()),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "omni_draft_revision_mismatch"
    assert job.all_candidates == {"clip_paths": ["source-a.mp4"]}


@pytest.mark.asyncio
async def test_cancel_releases_a_claimed_asset_that_never_reached_the_draft(monkeypatch) -> None:
    storage_path = "generative-jobs/job/omni/asset-1.mp4"
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={
            "variants": [{"variant_id": "v1", "ai_timeline": {"slots": []}}],
            "omni_generated_assets": {
                "asset-1": _record(
                    status="ready",
                    storage_path=storage_path,
                    normalized_duration_s=4.0,
                    operation={
                        "op": "insert_generated_asset",
                        "asset_id": "asset-1",
                        "clip_index": 1,
                        "insert_at_s": 2.0,
                        "duration_s": 4.0,
                    },
                )
            },
        },
        all_candidates={"clip_paths": ["source-a.mp4", storage_path]},
    )
    db = SimpleNamespace(commit=AsyncMock())
    delete = MagicMock()
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    monkeypatch.setattr("app.storage.delete_object_best_effort", delete)

    response = await cancel_omni_asset(job, "asset-1", db)

    assert response.status == "cancelled"
    assert response.operation is None
    assert job.all_candidates == {"clip_paths": ["source-a.mp4"]}
    delete.assert_called_once_with(storage_path)


@pytest.mark.asyncio
async def test_start_rejects_source_times_beyond_authoritative_duration(monkeypatch) -> None:
    variant = {
        "variant_id": "v1",
        "ai_timeline": {
            "slots": [
                {
                    "clip_index": 0,
                    "duration_s": 4.0,
                    "source_duration_s": 5.0,
                }
            ]
        },
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    body = OmniAssetStartBody(
        draft_revision="v1-test",
        suggestion_id="suggestion-1",
        action="restyle_segment",
        prompt="Restyle it",
        insert_at_s=2,
        duration_s=4,
        source_clip_index=0,
        source_start_s=4,
        source_end_s=6,
    )

    with pytest.raises(HTTPException) as exc:
        await start_omni_asset(job, "v1", body, SimpleNamespace())
    assert exc.value.status_code == 422
    assert exc.value.detail == "omni_source_time_out_of_bounds"


@pytest.mark.asyncio
async def test_start_restyle_accepts_one_complete_unsaved_draft_slot(monkeypatch) -> None:
    variant = {
        "variant_id": "v1",
        "ai_timeline": {
            "slots": [
                {
                    "clip_index": 0,
                    "in_s": 1.0,
                    "duration_s": 4.0,
                    "source_duration_s": 8.0,
                }
            ]
        },
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    enqueue = MagicMock()
    monkeypatch.setattr(omni_generate.generate_omni_asset, "apply_async", enqueue)
    body = OmniAssetStartBody(
        draft_revision="v1-test",
        suggestion_id="suggestion-1",
        action="restyle_segment",
        prompt="Restyle it",
        insert_at_s=2,
        duration_s=3,
        source_clip_index=0,
        source_start_s=2,
        source_end_s=4,
    )

    response = await start_omni_asset(
        job,
        "v1",
        body,
        SimpleNamespace(commit=AsyncMock()),
    )

    assert response.status == "queued"
    record = job.assembly_plan["omni_generated_assets"][response.asset_id]
    assert record["draft_revision"] == "v1-test"
    assert record["source_slot_fingerprint"] == {
        "clip_index": 0,
        "start_s": 2.0,
        "end_s": 4.0,
    }
    enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_queued_asset_becomes_terminal_without_worker(monkeypatch) -> None:
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={
            "omni_generated_assets": {
                "asset-1": _record(
                    status="queued",
                    progress=0.02,
                    provider_interaction_id=None,
                )
            }
        },
    )
    db = SimpleNamespace(commit=AsyncMock())
    revoke = MagicMock()
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    monkeypatch.setattr("app.worker.celery_app.control.revoke", revoke)

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(_omni.asyncio, "to_thread", run_inline)
    response = await cancel_omni_asset(job, "asset-1", db)

    assert response.status == "cancelled"
    assert response.progress == 0.0
    revoke.assert_called_once_with("omni-asset-1", terminate=False)


def test_cancelled_ready_commit_preserves_original_candidates_and_cleans_storage(
    monkeypatch,
) -> None:
    record = _record(status="cancellation_requested")
    job = SimpleNamespace(
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    session = SimpleNamespace(commit=lambda: None)
    deleted: list[str] = []

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(omni_generate, "sync_session", fake_session)
    monkeypatch.setattr(omni_generate, "_locked_job", lambda _session, _job_id: job)
    monkeypatch.setattr(omni_generate, "delete_object_best_effort", deleted.append)

    omni_generate._commit_ready(  # noqa: SLF001
        "00000000-0000-0000-0000-000000000001",
        "asset-1",
        storage_path="generative-jobs/job/omni/asset-1.mp4",
        output_url="https://storage.example/asset-1",
        duration_s=4,
    )

    assert job.all_candidates == {"clip_paths": ["source-a.mp4"]}
    cancelled = job.assembly_plan["omni_generated_assets"]["asset-1"]
    assert cancelled["status"] == "cancelled"
    assert cancelled.get("operation") is None
    assert deleted == ["generative-jobs/job/omni/asset-1.mp4"]


def test_cleanup_expires_unclaimed_asset_and_deletes_storage(monkeypatch) -> None:
    record = _record(
        status="ready",
        storage_path="generative-jobs/job/omni/asset-1.mp4",
        operation=None,
    )
    job = SimpleNamespace(
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    session = SimpleNamespace(commit=lambda: None)
    deleted: list[str] = []

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(omni_generate, "sync_session", fake_session)
    monkeypatch.setattr(omni_generate, "_locked_job", lambda _session, _job_id: job)
    monkeypatch.setattr(omni_generate, "delete_object_best_effort", deleted.append)

    omni_generate.cleanup_unclaimed_omni_asset.run(
        job_id="00000000-0000-0000-0000-000000000001",
        asset_id="asset-1",
    )

    expired = job.assembly_plan["omni_generated_assets"]["asset-1"]
    assert expired["status"] == "cancelled"
    assert expired["error"] == "generated_asset_expired"
    assert deleted == ["generative-jobs/job/omni/asset-1.mp4"]
