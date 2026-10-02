"""Cloud-render kill-switch policy and dispatch admission tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.cloud_render_policy import (
    CLOUD_RENDER_DISABLED_DETAIL,
    CLOUD_RENDER_DISABLED_REASON,
    block_cloud_render_before_publish,
    block_cloud_render_before_publish_sync,
    block_cloud_render_task,
    cloud_render_mutation_block_reason,
)
from app.services.job_dispatch import (
    claim_and_enqueue_orchestrator_sync,
    enqueue_orchestrator,
    enqueue_orchestrator_sync,
)


def _job(*, status: str = "queued", device: bool = False, **extra):
    assembly = {"_device_render_v1": {"variant_id": "v1"}} if device else {}
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        assembly_plan=assembly,
        failure_reason=extra.pop("failure_reason", None),
        error_detail=extra.pop("error_detail", None),
        **extra,
    )


def _async_db(job):
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def test_device_only_admission_blocks_new_cloud_mutation(monkeypatch):
    monkeypatch.setattr("app.services.cloud_render_policy.settings.ios_device_only_mode", True)
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", True
    )

    assert cloud_render_mutation_block_reason(_job(status="done")) == ("device_render_unsupported")
    assert cloud_render_mutation_block_reason(_job(status="done", device=True)) is None


def test_execution_kill_switch_blocks_new_cloud_mutation(monkeypatch):
    monkeypatch.setattr("app.services.cloud_render_policy.settings.ios_device_only_mode", False)
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )

    assert cloud_render_mutation_block_reason(_job(status="done")) == (CLOUD_RENDER_DISABLED_REASON)


def test_exact_cloud_variant_cannot_hide_behind_device_job_marker(monkeypatch):
    monkeypatch.setattr("app.services.cloud_render_policy.settings.ios_device_only_mode", True)
    job = _job(status="done", device=True)

    assert (
        cloud_render_mutation_block_reason(
            job, variant={"variant_id": "legacy-cloud", "render_destination": "cloud"}
        )
        == "device_render_unsupported"
    )


@pytest.mark.asyncio
async def test_async_policy_terminalizes_queued_cloud_job_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job()
    db = _async_db(job)

    assert await block_cloud_render_before_publish(db, job.id, task_name="test") is True
    assert job.status == "processing_failed"
    assert job.failure_reason == CLOUD_RENDER_DISABLED_REASON
    assert job.error_detail == CLOUD_RENDER_DISABLED_DETAIL
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_policy_preserves_ready_cloud_job_and_output(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job(status="done", output_url="https://signed.example/old.mp4")
    db = _async_db(job)

    assert await block_cloud_render_before_publish(db, job.id, task_name="test") is True
    assert job.status == "done"
    assert job.output_url == "https://signed.example/old.mp4"
    assert job.failure_reason is None
    db.commit.assert_not_awaited()


def test_sync_policy_allows_device_job_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job(device=True)
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    session = MagicMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with patch("app.database.sync_session", return_value=context):
        assert block_cloud_render_before_publish_sync(job.id, task_name="test") is False
    session.commit.assert_not_called()


def test_task_entry_guard_terminalizes_queued_cloud_job(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    session = MagicMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with patch("app.database.sync_session", return_value=context):
        assert block_cloud_render_task(str(job.id), task_name="render") is True
    assert job.status == "processing_failed"
    session.commit.assert_called_once()


def test_sync_dispatch_does_not_publish_disabled_cloud_job(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    session = MagicMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    task = MagicMock(name="orchestrate_generative_job")
    task.name = "orchestrate_generative_job"

    with patch("app.database.sync_session", return_value=context):
        assert enqueue_orchestrator_sync(task, job.id) == str(job.id)
    task.apply_async.assert_not_called()
    assert job.failure_reason == CLOUD_RENDER_DISABLED_REASON


def test_claim_and_enqueue_sync_does_not_publish_disabled_cloud_job(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    session = MagicMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    task = MagicMock(name="orchestrate_generative_job")
    task.name = "orchestrate_generative_job"

    with patch("app.database.sync_session", return_value=context):
        assert claim_and_enqueue_orchestrator_sync(task, job.id) is False
    task.apply_async.assert_not_called()


def test_generative_task_entry_guard_skips_render_work_when_cloud_disabled():
    """A queued legacy task must stop before downloading or rendering media."""
    import app.tasks.generative_build as generative_build

    with (
        patch("app.services.cloud_render_policy.block_cloud_render_task", return_value=True),
        patch.object(generative_build, "_run_generative_job") as run_job,
        patch.object(generative_build, "_owned_job_task_fence") as fence,
    ):
        generative_build.orchestrate_generative_job.run(str(uuid.uuid4()))

    run_job.assert_not_called()
    fence.assert_not_called()


@pytest.mark.asyncio
async def test_async_dispatch_allows_device_job_and_publishes(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job(device=True)
    status = MagicMock()
    status.scalar_one_or_none.return_value = "queued"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_async_db(job).execute.return_value, status, MagicMock()])
    db.commit = AsyncMock()
    task = MagicMock()
    task.name = "orchestrate_generative_job"

    await enqueue_orchestrator(task, job.id, db)
    task.apply_async.assert_called_once()


@pytest.mark.asyncio
async def test_async_dispatch_does_not_publish_disabled_cloud_job(monkeypatch):
    monkeypatch.setattr(
        "app.services.cloud_render_policy.settings.cloud_render_execution_enabled", False
    )
    job = _job()
    db = _async_db(job)
    task = MagicMock()
    task.name = "orchestrate_generative_job"

    assert await enqueue_orchestrator(task, job.id, db) == str(job.id)
    task.apply_async.assert_not_called()
    assert job.status == "processing_failed"
    assert job.failure_reason == CLOUD_RENDER_DISABLED_REASON
    db.commit.assert_awaited_once()
