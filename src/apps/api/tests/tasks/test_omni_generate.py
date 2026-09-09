from __future__ import annotations

import base64
import copy
import sys
import uuid
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
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


@pytest.fixture(autouse=True)
def _configured_omni_lab_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_omni.settings, "ai_omni_lab_test_run_id", "omni-suite")
    monkeypatch.setattr(_omni.settings, "ai_omni_lab_run_max_cost_usd", 2.5)
    monkeypatch.setattr(_omni.settings, "ai_omni_lab_reservation_approved", True)


def _install_omni_provider(monkeypatch, interaction) -> SimpleNamespace:
    interactions = SimpleNamespace(
        create=MagicMock(return_value=interaction),
        get=MagicMock(return_value=interaction),
        cancel=MagicMock(),
    )
    client = SimpleNamespace(
        interactions=interactions,
        files=SimpleNamespace(delete=MagicMock()),
    )
    google_module = ModuleType("google")
    google_module.genai = SimpleNamespace(Client=MagicMock(return_value=client))
    monkeypatch.setitem(sys.modules, "google", google_module)
    return client


def _run_omni_task(
    monkeypatch,
    interaction,
    *,
    poll_error: Exception | None = None,
) -> tuple[SimpleNamespace, MagicMock, MagicMock, MagicMock]:
    client = _install_omni_provider(monkeypatch, interaction)
    if poll_error is not None:
        client.interactions.get.side_effect = poll_error
        monkeypatch.setattr(omni_generate.time, "sleep", lambda _seconds: None)
    job = SimpleNamespace(
        user_id=uuid.uuid4(),
        all_candidates={"clip_paths": []},
    )
    record = {
        "status": "queued",
        "action": "generate_insert",
        "reference_clip_index": None,
        "duration_s": 4.0,
        "estimated_max_cost_usd": 0.44,
        "test_run_id": "omni-lifecycle-test",
        "approved_run_max_cost_usd": 2.5,
        "cost_confirmed": True,
        "prompt": "Generate a bridge",
    }
    monkeypatch.setattr(omni_generate.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(omni_generate.settings, "ai_usage_environment", "lab")
    monkeypatch.setattr(omni_generate.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(omni_generate, "_read", lambda *_args: (job, record))
    monkeypatch.setattr(omni_generate, "_cancelled", lambda *_args: False)
    monkeypatch.setattr(omni_generate, "_update", MagicMock(return_value={}))
    monkeypatch.setattr(
        omni_generate,
        "_input_parts",
        lambda *_args: [{"type": "text", "text": "Generate a bridge"}],
    )
    monkeypatch.setattr(omni_generate, "_write_provider_video", MagicMock())
    monkeypatch.setattr(omni_generate, "_normalize", MagicMock(return_value=3.25))
    monkeypatch.setattr(omni_generate, "upload_public_read", MagicMock(return_value="url"))
    monkeypatch.setattr(omni_generate, "_commit_ready", MagicMock(return_value=True))
    monkeypatch.setattr(omni_generate.cleanup_unclaimed_omni_asset, "apply_async", MagicMock())

    from app.services import ai_cost_control

    reserve = MagicMock(return_value=SimpleNamespace(id="reservation"))
    settle = MagicMock()
    release = MagicMock()
    unknown = MagicMock()
    monkeypatch.setattr(ai_cost_control, "reserve_paid_call", reserve)
    monkeypatch.setattr(ai_cost_control, "settle_paid_call_cost", settle)
    monkeypatch.setattr(ai_cost_control, "release_paid_call", release)
    monkeypatch.setattr(ai_cost_control, "mark_paid_call_unknown", unknown)
    monkeypatch.setattr(ai_cost_control, "mark_paid_call_started", MagicMock())
    omni_generate.generate_omni_asset.run(job_id=str(uuid.uuid4()), asset_id="asset-1")
    return client, settle, release, unknown


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


def test_omni_cost_settles_only_after_completed_output_is_measured(monkeypatch) -> None:
    interaction = SimpleNamespace(
        id="interactions/completed-1",
        status="completed",
        output_video=SimpleNamespace(data="ignored", uri=None),
    )

    _client, settle, release, unknown = _run_omni_task(monkeypatch, interaction)

    settle.assert_called_once()
    assert settle.call_args.kwargs["cost_usd"] == pytest.approx(
        3.25 * omni_generate.settings.ai_omni_cost_per_second_usd
    )
    assert settle.call_args.kwargs["provider_request_id"] == "interactions/completed-1"
    assert settle.call_args.kwargs["usage_json"]["duration_s"] == 3.25
    release.assert_not_called()
    unknown.assert_not_called()


def test_omni_provider_terminal_failure_releases_without_settlement(monkeypatch) -> None:
    interaction = SimpleNamespace(
        id="interactions/failed-1",
        status="failed",
        output_video=None,
    )

    _client, settle, release, unknown = _run_omni_task(monkeypatch, interaction)

    settle.assert_not_called()
    release.assert_called_once()
    unknown.assert_not_called()


def test_omni_poll_failure_marks_unknown_without_settlement(monkeypatch) -> None:
    interaction = SimpleNamespace(
        id="interactions/running-1",
        status="running",
        output_video=None,
    )
    _client, settle, release, unknown = _run_omni_task(
        monkeypatch,
        interaction,
        poll_error=TimeoutError("poll timed out"),
    )

    settle.assert_not_called()
    release.assert_not_called()
    unknown.assert_called_once()


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
    source_ids = job.all_candidates["clip_source_instance_ids"]
    assert len(source_ids) == 2
    assert len(set(source_ids)) == 2
    assert all(uuid.UUID(value) for value in source_ids)
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
        all_candidates={
            "clip_paths": ["source-a.mp4", storage_path],
            "clip_source_instance_ids": [
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
            ],
        },
    )
    db = SimpleNamespace(commit=AsyncMock())
    delete = MagicMock()
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    monkeypatch.setattr("app.storage.delete_object_best_effort", delete)

    response = await cancel_omni_asset(job, "asset-1", db)

    assert response.status == "cancelled"
    assert response.operation is None
    assert job.all_candidates == {
        "clip_paths": ["source-a.mp4"],
        "clip_source_instance_ids": ["00000000-0000-4000-8000-000000000001"],
    }
    delete.assert_called_once_with(storage_path)


@pytest.mark.asyncio
async def test_claim_rejects_present_malformed_source_identity_without_mutation(
    monkeypatch,
) -> None:
    record = _record(
        status="ready",
        storage_path="generative-jobs/job/omni/asset-1.mp4",
        normalized_duration_s=4.0,
        operation=None,
    )
    original_candidates = {
        "clip_paths": ["source-a.mp4"],
        "clip_source_instance_ids": [],
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates=copy.deepcopy(original_candidates),
    )
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))

    with pytest.raises(HTTPException) as exc_info:
        await claim_omni_asset(
            job,
            "asset-1",
            OmniAssetClaimBody(draft_revision="v1-test"),
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "omni_clip_source_identity_unavailable"
    assert job.all_candidates == original_candidates
    assert record["operation"] is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_requires_explicit_cost_confirmation_before_locking_job(
    monkeypatch,
) -> None:
    job = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", "lab")
    lock = AsyncMock()
    monkeypatch.setattr(_omni, "_lock_job", lock)
    body = OmniAssetStartBody(
        draft_revision="v1-test",
        suggestion_id="suggestion-1",
        action="generate_insert",
        prompt="Generate a bridge",
        insert_at_s=2,
        duration_s=4,
        estimated_max_cost_usd=0.44,
        cost_confirmed=False,
        test_run_id="omni-test-declined",
    )

    with pytest.raises(HTTPException) as exc:
        await start_omni_asset(job, "v1", body, SimpleNamespace())

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "omni_cost_confirmation_required"
    lock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["production", "development", "test"])
async def test_start_is_hidden_outside_lab_even_with_confirmation(
    monkeypatch, environment: str
) -> None:
    job = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", environment)
    body = OmniAssetStartBody(
        draft_revision="v1-test",
        suggestion_id="suggestion-1",
        action="generate_insert",
        prompt="Generate a bridge",
        insert_at_s=2,
        duration_s=4,
        estimated_max_cost_usd=0.44,
        cost_confirmed=True,
        test_run_id="omni-test-production",
    )

    with pytest.raises(HTTPException) as exc:
        await start_omni_asset(job, "v1", body, SimpleNamespace())

    assert exc.value.status_code == 404
    assert exc.value.detail == "omni_generated_video_available_only_in_lab"


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
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", "lab")
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
        estimated_max_cost_usd=2.0,
        cost_confirmed=True,
        test_run_id="omni-test-out-of-bounds",
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
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", "lab")
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
        estimated_max_cost_usd=2.0,
        cost_confirmed=True,
        test_run_id="omni-test-unsaved-slot",
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
    assert record["test_run_id"] == "omni-suite"
    assert record["approved_run_max_cost_usd"] == 2.5
    enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_start_deduplicates_retried_confirmation_under_job_lock(monkeypatch) -> None:
    variant = {
        "variant_id": "v1",
        "ai_timeline": {"slots": [{"clip_index": 0, "duration_s": 5.0}]},
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", "lab")
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    enqueue = MagicMock()
    monkeypatch.setattr(omni_generate.generate_omni_asset, "apply_async", enqueue)
    db = SimpleNamespace(commit=AsyncMock())
    body = OmniAssetStartBody(
        draft_revision="v1-test",
        suggestion_id="suggestion-1",
        action="generate_insert",
        prompt="Generate a bridge",
        insert_at_s=2,
        duration_s=3.4,
        estimated_max_cost_usd=0.38,
        cost_confirmed=True,
        test_run_id="omni-test-retry",
    )

    first = await start_omni_asset(job, "v1", body, db)
    retried = await start_omni_asset(job, "v1", body, db)

    assert retried.asset_id == first.asset_id
    assert retried.status == "queued"
    enqueue.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_does_not_deduplicate_a_changed_generation_request(monkeypatch) -> None:
    variant = {
        "variant_id": "v1",
        "ai_timeline": {"slots": [{"clip_index": 0, "duration_s": 5.0}]},
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", "lab")
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    enqueue = MagicMock()
    monkeypatch.setattr(omni_generate.generate_omni_asset, "apply_async", enqueue)
    db = SimpleNamespace(commit=AsyncMock())
    base = {
        "draft_revision": "v1-test",
        "suggestion_id": "suggestion-1",
        "action": "generate_insert",
        "insert_at_s": 2,
        "duration_s": 3.4,
        "estimated_max_cost_usd": 0.38,
        "cost_confirmed": True,
    }

    first = await start_omni_asset(
        job,
        "v1",
        OmniAssetStartBody(prompt="Generate a bridge", **base),
        db,
    )
    changed = await start_omni_asset(
        job,
        "v1",
        OmniAssetStartBody(prompt="Generate a paper bridge", **base),
        db,
    )

    assert changed.asset_id != first.asset_id
    assert enqueue.call_count == 2


@pytest.mark.asyncio
async def test_start_enqueue_failure_never_rewrites_cancelled_job(monkeypatch) -> None:
    variant = {
        "variant_id": "v1",
        "ai_timeline": {"slots": [{"clip_index": 0, "duration_s": 4.0}]},
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        status="variants_ready",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    monkeypatch.setattr(_omni.settings, "omni_generated_video_enabled", True)
    monkeypatch.setattr(_omni.settings, "ai_usage_environment", "lab")
    monkeypatch.setattr(_omni, "_lock_job", AsyncMock(return_value=job))
    monkeypatch.setattr(
        omni_generate.generate_omni_asset,
        "apply_async",
        MagicMock(side_effect=RuntimeError("broker unavailable")),
    )
    locked_result = MagicMock()
    locked_result.scalar_one_or_none.return_value = job
    db = MagicMock()
    db.execute = AsyncMock(return_value=locked_result)

    async def commit_then_cancel() -> None:
        job.status = "cancelled"

    db.commit = AsyncMock(side_effect=commit_then_cancel)
    db.rollback = AsyncMock()
    body = OmniAssetStartBody(
        draft_revision="v1-test",
        suggestion_id="suggestion-1",
        action="generate_insert",
        prompt="Generate a bridge",
        insert_at_s=2,
        duration_s=3,
        estimated_max_cost_usd=2.0,
        cost_confirmed=True,
        test_run_id="omni-test-enqueue-failure",
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_omni_asset(job, "v1", body, db)

    assert exc_info.value.status_code == 503
    asset = next(iter(job.assembly_plan["omni_generated_assets"].values()))
    assert asset["status"] == "queued"
    assert job.status == "cancelled"
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()


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


def test_whole_job_cancellation_rejects_child_updates_and_late_ready_output(
    monkeypatch,
) -> None:
    """Per-asset state cannot outlive the parent Job cancellation tombstone."""
    storage_path = "generative-jobs/00000000-0000-0000-0000-000000000001/omni/asset-1.mp4"
    record = _record(status="generating", progress=0.55)
    job = SimpleNamespace(
        status="cancelled",
        assembly_plan={"omni_generated_assets": {"asset-1": record}},
        all_candidates={"clip_paths": ["source-a.mp4"]},
    )
    session = MagicMock()
    session.scalar.return_value = job

    @contextmanager
    def fake_session():
        yield session

    deleted = MagicMock(return_value=True)
    monkeypatch.setattr(omni_generate, "sync_session", fake_session)
    monkeypatch.setattr(omni_generate, "delete_object_best_effort", deleted)

    assert (
        omni_generate._update(  # noqa: SLF001
            "00000000-0000-0000-0000-000000000001",
            "asset-1",
            status="ready",
            progress=1.0,
        )
        == {}
    )
    assert (
        omni_generate._commit_ready(  # noqa: SLF001
            "00000000-0000-0000-0000-000000000001",
            "asset-1",
            storage_path=storage_path,
            output_url="https://storage.example/asset-1",
            duration_s=4.0,
        )
        is False
    )

    assert job.assembly_plan == {
        "omni_generated_assets": {"asset-1": _record(status="generating", progress=0.55)}
    }
    assert job.all_candidates == {"clip_paths": ["source-a.mp4"]}
    session.commit.assert_not_called()
    deleted.assert_called_once_with(storage_path)


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
