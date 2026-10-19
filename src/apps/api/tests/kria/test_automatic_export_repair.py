"""Exact consent, immutable program and bounded measured recovery on PostgreSQL."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.kria.brief_binding import snapshot_media
from app.models import CreatorAgentExecution, CreatorAgentSession, Job, PlanItem
from app.services.device_render import device_record, save_device_record
from tests.kria.test_render_retry_postgres import seed_failed


@pytest.mark.parametrize("live_claim", [False, True])
def test_background_queue_deadline_preserves_last_good_and_respects_live_worker(live_claim):
    from app.tasks.export_verification import _expire_queued

    case = seed_failed()
    now = datetime.now(UTC)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["status"]["phase"] = "syncing"
        record["attempts"] = {
            "pending": {
                "verification_task": {
                    "state": "running" if live_claim else "queued",
                    "runs": 1,
                    "queued_at": (now - timedelta(minutes=21)).isoformat(),
                    "lease_until": (now + timedelta(seconds=300)).isoformat(),
                }
            }
        }
        save_device_record(job, "first", record)
        before = copy.deepcopy(job.assembly_plan["variants"][0])
        db.commit()
    _expire_queued(case.job_id, now)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        assert job.assembly_plan["variants"][0] == before
        assert record["status"]["phase"] == ("syncing" if live_claim else "needs_attention")


@pytest.mark.parametrize(
    "refusal", [None, "legacy", "expired", "unknown", "same_source", "budget", "text", "partial"]
)
async def test_repair_keeps_program_and_requires_specific_live_consent(monkeypatch, refusal):
    from app.kria.automatic_export_repair import bind_repair_grant, repair_failed_export

    case = seed_failed()
    monkeypatch.setattr(settings, "phone_render_user_ids", [case.user_id])
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(case.recipe["required_capabilities"])
    )
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, case.execution_id)
        execution.status = "outcome_unknown" if refusal == "unknown" else "dispatched"
        if refusal != "legacy":
            execution.result = {
                **execution.result,
                "automatic_repair_grant": {
                    "version": 1,
                    "approved_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
                    "max_attempts": 2,
                    "used": 2 if refusal == "budget" else 0,
                    "media_snapshot": snapshot_media(
                        db.get(PlanItem, db.get(CreatorAgentSession, case.session_id).plan_item_id)
                    ),
                    "expires_at": (
                        datetime.now(UTC) + timedelta(minutes=-1 if refusal == "expired" else 27)
                    ).isoformat(),
                },
            }
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["actual_export_policy"] = {"version": 1}
        record["status"]["phase"] = "syncing"
        save_device_record(job, "first", record)
        before = copy.deepcopy(record["status"]["request"]["recipe"])
        db.commit()
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            job = await db.get(Job, case.job_id, with_for_update=True)
            await bind_repair_grant(db, job, "first")
            record = device_record(job, "first")
            asset = before["tracks"][0]["clips"][0]["source_asset_id"]
            if refusal == "same_source":
                record["repair_history"] = [{"asset_ids": [asset]}]
            record["status"].update(phase="needs_attention", reason_code="export_failed")
            save_device_record(job, "first", record)
            checks = [
                {
                    "kind": "picture",
                    "clip_id": before["tracks"][0]["clips"][0]["id"],
                    "status": "failed",
                }
            ]
            if refusal == "text":
                checks.append({"kind": "text", "status": "failed"})
            report = {"status": "partial" if refusal == "partial" else "rejected", "checks": checks}
            repaired = await repair_failed_export(db, job, "first", "failed-attempt", report)
            assert repaired is (refusal is None)
            after = device_record(job, "first")
            assert after["status"]["request"]["recipe"] == before
            assert job.assembly_plan["variants"][0]["video_path"] == "last-good.mp4"
            if repaired:
                assert after["status"]["phase"] == "awaiting_device"
                assert after["status"]["request"]["identity"]["recipe_revision"] == 2
                assert after["status"]["request"]["recovery"]["asset_ids"] == [asset]
                assert len(after["repair_history"]) == 1
            await db.rollback()
    finally:
        await engine.dispose()
