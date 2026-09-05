from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.config import settings as app_settings
from app.routes import generative_jobs, me, music_jobs, template_jobs, tiktok
from app.services import tiktok_publishable


def _job(**overrides):
    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "status": "cancelled",
        "mode": "generative",
        "job_type": "generative",
        "assembly_plan": {
            "output_url": "https://stale.example/output.mp4",
            "output_path": "jobs/stale/output.mp4",
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/stale/output.mp4",
                    "output_url": "https://stale.example/variant.mp4",
                    "base_video_path": "generative-jobs/stale/base.mp4",
                }
            ],
        },
        "all_candidates": {},
        "error_detail": "late output at https://stale.example/output.mp4",
        "failure_reason": None,
        "template_id": None,
        "music_track_id": None,
        "created_at": now,
        "updated_at": now,
        "current_phase": "upload",
        "phase_log": [{"name": "upload"}],
        "started_at": now,
        "finished_at": now,
        "content_plan_item_id": None,
        "worker_heartbeat_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _async_db_returning(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def test_cancelled_generative_status_suppresses_all_variants_without_signing(monkeypatch) -> None:
    job = _job()
    sign = MagicMock(side_effect=AssertionError("cancelled output must not be signed"))
    monkeypatch.setattr(generative_jobs, "signed_get_url", sign)
    monkeypatch.setattr(generative_jobs.storage, "signed_download_url", sign)

    assert generative_jobs._variants_for_response(job) == []  # noqa: SLF001
    sign.assert_not_called()


def test_cancelled_job_is_not_editable() -> None:
    with pytest.raises(HTTPException) as exc_info:
        generative_jobs.require_editable_variant(_job(), "song_text")
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_cancelled_loader_rejects_mutation_after_requesting_row_lock() -> None:
    job = _job()
    db = _async_db_returning(job)
    user = SimpleNamespace(id=job.user_id)

    with pytest.raises(HTTPException) as exc_info:
        await generative_jobs._load_generative_job(  # noqa: SLF001
            str(job.id), db, user
        )

    assert exc_info.value.status_code == 409
    assert "FOR UPDATE" in str(db.execute.await_args.args[0])


def test_cancelled_library_row_has_no_playback_download_or_publish(monkeypatch) -> None:
    sign = MagicMock(side_effect=AssertionError("cancelled output must not be signed"))
    monkeypatch.setattr(me, "signed_download_url", sign)

    row = me._to_library_job(_job())  # noqa: SLF001

    assert row.output_url is None
    assert row.download_url is None
    assert row.output_variant_id is None
    assert row.tiktok_publishable is False
    sign.assert_not_called()


def test_cancelled_publishable_output_rejected_before_storage_lookup(monkeypatch) -> None:
    metadata = MagicMock(side_effect=AssertionError("cancelled output must not be inspected"))
    monkeypatch.setattr(tiktok_publishable.storage, "object_metadata", metadata)

    with pytest.raises(tiktok_publishable.PublishableOutputError, match="Cancelled"):
        tiktok_publishable.resolve_publishable_output(_job(), "song_text")

    metadata.assert_not_called()


@pytest.mark.asyncio
async def test_tiktok_owned_job_rejects_cancelled_publication() -> None:
    job = _job()
    db = _async_db_returning(job)
    with pytest.raises(HTTPException) as exc_info:
        await tiktok._owned_job(db, job.user_id, job.id)  # noqa: SLF001
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_cancelled_template_status_and_eval_hide_outputs(monkeypatch) -> None:
    job = _job(job_type="template", mode="template")
    user = SimpleNamespace(id=job.user_id)

    status_response = await template_jobs.get_template_job_status(
        str(job.id), current_user=user, db=_async_db_returning(job)
    )
    assert status_response.assembly_plan is None
    assert status_response.error_detail is None

    job.assembly_plan = {
        "steps": [{"slot": {"position": 1, "target_duration_s": 2.0}}],
        "slot_urls": {"0": "https://stale.example/slot.mp4"},
        "output_url": "https://stale.example/output.mp4",
        "comparison_grid_url": "https://stale.example/grid.jpg",
    }
    monkeypatch.setattr(app_settings, "eval_harness_enabled", True, raising=False)
    payload = await template_jobs.get_template_job_eval(str(job.id), db=_async_db_returning(job))
    assert payload["slots"][0]["slot_url"] is None
    assert payload["output_url"] is None
    assert payload["comparison_grid_url"] is None


@pytest.mark.asyncio
async def test_cancelled_music_status_hides_assembly_plan() -> None:
    job = _job(job_type="music", mode="music")
    response = await music_jobs.get_music_job_status(
        str(job.id),
        current_user=SimpleNamespace(id=job.user_id),
        db=_async_db_returning(job),
    )
    assert response.assembly_plan is None
    assert response.error_detail is None


@pytest.mark.asyncio
async def test_template_status_projects_private_generation_and_identity_state() -> None:
    private_plan = {
        "output_url": "https://safe.example/output.mp4",
        "variants": [
            {
                "variant_id": "subtitled",
                "clip_source_instance_ids": ["private-source-id"],
                "nested": {"clip_metadata_identity_index_v2": {"secret": True}},
            }
        ],
        "_speech_cleanup_internal": {
            "required_speech_generation_locks": {"subtitled": "private-generation"},
            "staged_render_results": {"subtitled:private-generation": {"secret": True}},
        },
    }
    stored = copy.deepcopy(private_plan)
    job = _job(
        status="template_ready",
        job_type="template",
        mode="template",
        assembly_plan=private_plan,
        all_candidates={"clip_source_instance_ids": ["also-private"]},
    )

    response = await template_jobs.get_template_job_status(
        str(job.id),
        current_user=SimpleNamespace(id=job.user_id),
        db=_async_db_returning(job),
    )

    payload = response.model_dump()
    assert payload["assembly_plan"] == {
        "variants": [
            {
                "variant_id": "subtitled",
                "nested": {},
                "render_status": "rendering",
                "ok": False,
            }
        ],
    }
    assert "all_candidates" not in payload
    assert job.assembly_plan == stored


@pytest.mark.asyncio
async def test_music_status_projects_private_generation_and_identity_state() -> None:
    private_plan = {
        "output_url": "https://safe.example/music.mp4",
        "_speech_cleanup_internal": {"terminal_pending": {"secret": True}},
        "clip_metadata_identity_index_v2": {"private": True},
    }
    stored = copy.deepcopy(private_plan)
    job = _job(
        status="music_ready",
        job_type="music",
        mode="music",
        assembly_plan=private_plan,
        all_candidates={"clip_source_instance_ids": ["also-private"]},
    )

    response = await music_jobs.get_music_job_status(
        str(job.id),
        current_user=SimpleNamespace(id=job.user_id),
        db=_async_db_returning(job),
    )

    payload = response.model_dump()
    assert payload["assembly_plan"] == {}
    assert "all_candidates" not in payload
    assert job.assembly_plan == stored


@pytest.mark.asyncio
async def test_retext_commits_locked_mutation_before_broker_publish(monkeypatch) -> None:
    job = _job(status="variants_ready")
    order: list[str] = []
    receipt = object()

    async def load(*_args, **_kwargs):
        return job

    def dispatch(*_args, **kwargs):
        assert kwargs["publish"] is False
        return receipt

    async def publish_committed(value, _db):
        assert value is receipt
        order.append("publish")

    db = MagicMock()
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    monkeypatch.setattr(generative_jobs, "_load_generative_job", load)
    monkeypatch.setattr(generative_jobs, "dispatch_retext", dispatch)
    monkeypatch.setattr(generative_jobs, "_publish_committed_variant_render", publish_committed)

    await generative_jobs.retext(
        str(job.id),
        "song_text",
        generative_jobs.RetextRequest(text="new hook"),
        current_user=SimpleNamespace(id=job.user_id),
        db=db,
    )

    assert order == ["commit", "publish"]


@pytest.mark.asyncio
async def test_failed_post_commit_publish_restores_only_matching_generation() -> None:
    previous = {
        "variant_id": "song_text",
        "render_status": "ready",
        "render_generation_id": "old-generation",
        "video_path": "generative-jobs/existing/output.mp4",
    }
    attempted_at = datetime.now(UTC)
    job = _job(
        status="variants_ready",
        started_at=attempted_at,
        assembly_plan={
            "variants": [
                {
                    **previous,
                    "render_status": "rendering",
                    "render_generation_id": "new-generation",
                    "render_started_at": "2026-08-11T12:00:00Z",
                    # A concurrent metadata-only write must survive recovery.
                    "unrelated_metadata": "keep-me",
                }
            ]
        },
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    def fail_publish() -> None:
        raise RuntimeError("broker unavailable")

    receipt = generative_jobs.PendingVariantPublish(
        callback=fail_publish,
        job_id=job.id,
        variant_id="song_text",
        render_generation_id="new-generation",
        previous_variant=previous,
        rollback_fields=frozenset({"render_status", "render_generation_id", "render_started_at"}),
        previous_started_at=None,
        attempted_started_at=attempted_at,
    )

    with pytest.raises(HTTPException) as exc_info:
        await generative_jobs._publish_committed_variant_render(receipt, db)  # noqa: SLF001

    assert exc_info.value.status_code == 503
    restored = job.assembly_plan["variants"][0]
    assert restored["render_status"] == "ready"
    assert restored["render_generation_id"] == "old-generation"
    assert "render_started_at" not in restored
    assert restored["unrelated_metadata"] == "keep-me"
    assert job.started_at is None
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    assert "FOR UPDATE" in str(db.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_failed_post_commit_publish_never_mutates_cancelled_job() -> None:
    job = _job()
    before = {
        **job.assembly_plan,
        "variants": [dict(row) for row in job.assembly_plan["variants"]],
    }
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    receipt = generative_jobs.PendingVariantPublish(
        callback=MagicMock(side_effect=RuntimeError("broker unavailable")),
        job_id=job.id,
        variant_id="song_text",
        render_generation_id="new-generation",
        previous_variant={"variant_id": "song_text", "render_status": "ready"},
        rollback_fields=frozenset({"render_status", "render_generation_id"}),
        previous_started_at=None,
        attempted_started_at=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await generative_jobs._publish_committed_variant_render(receipt, db)  # noqa: SLF001

    assert exc_info.value.status_code == 503
    assert job.assembly_plan == before
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
