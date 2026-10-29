"""Real DB + HTTP: an operator reads a failed phone export's measured evidence."""

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models import Job, User

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Requires disposable migrated PostgreSQL", allow_module_level=True)

SIGNED = "https://storage.googleapis.com/nova/out.mp4?X-Goog-Signature=abc123"
DIGEST = "a" * 64


def _report(job_id, attempt_id, ssim, *, status="rejected"):
    return {
        "verifier_version": "phone-simple-cut-v3",
        "audio_comparison": "sample-amplitude-per-stereo-channel-v1",
        "identity": {
            "job_id": job_id,
            "variant_id": "phone",
            "recipe_revision": 2,
            "recipe_digest": DIGEST,
        },
        "status": status,
        "picture_sampling": "at_most_0.5s_between_samples",
        "renderer_version": 1,
        "recipe_schema_version": 2,
        "required_capabilities": ["cuts"],
        "sha256": "b" * 64,
        "file_size_bytes": 1048576,
        "artifact_path": f"device-exports/{job_id}/{attempt_id}.mp4",
        "storage_generation": "1729000000000000",
        "source_receipts_digest": "c" * 64,
        "elapsed_s": 12.5,
        "checks": [
            {"kind": "duration", "status": "passed", "actual_s": 9.02, "expected_s": 9.0},
            {
                "kind": "picture",
                "clip_id": "clip-2",
                "status": "failed",
                "samples": 7,
                "picture_clock": "output-pts-bracketed-v1",
                "neighbour_samples": 7,
                "reason": "whole_window_neighbour_shift",
                "max_mean_pixel_error": 0.081,
                "min_local_ssim": ssim,
            },
            {
                "kind": "audio",
                "clip_id": "clip-2",
                "chunk": 0,
                "status": "passed",
                "correlation": 0.991,
                "gain": 1.01,
                "lag_samples": 3,
                "reason": SIGNED,
            },
            {"kind": "evidence", "status": "missing", "reason": "TimeoutError"},
        ],
    }


@pytest.fixture
async def seeded(monkeypatch):
    from app.routes import admin_jobs

    monkeypatch.setattr(settings, "admin_api_key", "export-evidence-admin")
    owner, job_id = uuid.uuid4(), uuid.uuid4()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    unverified = "00000000-0000-4000-8000-000000000000"
    record = {
        "status": {
            "phase": "needs_attention",
            "reason_code": "output_check_failed",
            "reason": "Kria's quality check didn't pass for this video, Troya.",
            "request": {
                "identity": {
                    "job_id": str(job_id),
                    "variant_id": "phone",
                    "recipe_revision": 2,
                    "recipe_digest": DIGEST,
                },
                "recipe": {"text_layers": [{"text": "my private caption"}]},
            },
        },
        "base_generation": "gen-1",
        "pinned_at": "2026-10-28T10:00:00+00:00",
        "actual_export_policy": {
            "version": 1,
            "verifier_version": "phone-simple-cut-v3",
            "source_receipts_digest": "c" * 64,
        },
        "verification_attempt": second,
        "attempts": {
            unverified: {
                "path": f"device-exports/{job_id}/{unverified}.mp4",
                "size": 1048576,
                "sha256": "d" * 64,
                "brand_tail": "standard",
            },
            second: {
                "path": f"device-exports/{job_id}/{second}.mp4",
                "size": 1048576,
                "sha256": "b" * 64,
                "brand_tail": "standard",
                "upload_url": SIGNED,
                "verification_task": {
                    "state": "done",
                    "runs": 1,
                    "queued_at": "2026-10-28T10:05:00+00:00",
                },
                "actual_export_verification": _report(str(job_id), second, 0.912),
            },
        },
        "repair_history": [
            {
                "attempt": 1,
                "failed_attempt_id": first,
                "failed_identity": {
                    "job_id": str(job_id),
                    "variant_id": "phone",
                    "recipe_revision": 1,
                    "recipe_digest": DIGEST,
                },
                "report": _report(str(job_id), first, 0.874),
                "cause": {
                    "code": "source_decode_mismatch",
                    "requirement_ids": ["picture:clip-2"],
                    "field_paths": ["tracks.0.clips.1"],
                    "measurements": [{"min_local_ssim": 0.874}],
                },
                "change": {"recipe_fields_changed": [], "source_assets_normalized": ["asset-2"]},
                "asset_ids": ["asset-2"],
                "action": "normalize_sources",
                "started_at": "2026-10-28T10:01:00+00:00",
                "model_calls": 0,
                "outcome": "rejected",
                "completed_at": "2026-10-28T10:06:00+00:00",
                "elapsed_s": 300.0,
                "result": _report(str(job_id), second, 0.912),
            }
        ],
        "contract_decline": {
            "decline_reason": "evidence_missing",
            "field_path": "export_verification",
            "stage": "actual_export",
            "message": "I couldn't confirm the exported video's picture matches your edit.",
            "alternative": "Retry this same approved edit.",
            "recorded_at": "2026-10-28T10:06:01+00:00",
        },
    }
    async with AsyncSessionLocal() as db:
        db.add(User(id=owner, email=f"{owner}@export-evidence.test"))
        await db.flush()
        db.add(
            Job(
                id=job_id,
                user_id=owner,
                status="failed",
                raw_storage_path=f"raw/{job_id}.mp4",
                assembly_plan={
                    "output_url": SIGNED,
                    "variants": [{"variant_id": "phone", "output_url": SIGNED}],
                    "_device_render_v1": {"phone": record},
                },
            )
        )
        await db.commit()
    app = FastAPI()
    app.include_router(admin_jobs.router, prefix="/admin/jobs")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, job_id, first, second, unverified
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Job).where(Job.id == job_id))
        await db.execute(delete(User).where(User.id == owner))
        await db.commit()
    await engine.dispose()


async def test_admin_reads_failed_export_scores_without_urls_or_creator_text(seeded):
    client, job_id, first, second, unverified = seeded
    path = f"/admin/jobs/{job_id}/export-verification"

    refused = await client.get(path, headers={"X-Admin-Token": "wrong"})
    assert refused.status_code == 401, "admin token is required"

    response = await client.get(path, headers={"X-Admin-Token": "export-evidence-admin"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_status"] == "failed"
    (variant,) = body["variants"]
    assert variant["variant_id"] == "phone"
    assert variant["phase"] == "needs_attention"
    assert variant["reason_code"] == "output_check_failed"
    assert variant["identity"]["recipe_revision"] == 2
    assert variant["actual_export_policy"]["verifier_version"] == "phone-simple-cut-v3"
    assert variant["contract_decline"]["decline_reason"] == "evidence_missing"

    attempt, never_verified = variant["attempts"]
    assert (attempt["index"], attempt["attempt_id"]) == (0, second), "verified upload first"
    assert (never_verified["index"], never_verified["attempt_id"]) == (1, unverified)
    assert never_verified["actual_export_verification"] is None
    assert attempt["verification_task"]["state"] == "done"
    report = attempt["actual_export_verification"]
    assert (report["status"], report["verifier_version"]) == ("rejected", "phone-simple-cut-v3")
    picture = next(c for c in report["checks"] if c["kind"] == "picture")
    assert picture["min_local_ssim"] == 0.912, "measured SSIM reaches the operator"
    assert picture["neighbour_samples"] == 7
    assert picture["reason"] == "whole_window_neighbour_shift"
    audio = next(c for c in report["checks"] if c["kind"] == "audio")
    assert audio["correlation"] == 0.991 and audio["reason"] is None, "URL-shaped reason dropped"
    evidence = next(c for c in report["checks"] if c["kind"] == "evidence")
    assert (evidence["status"], evidence["reason"]) == ("missing", "TimeoutError")
    assert report["thresholds"]["picture_min_local_ssim"] == 0.95
    assert report["thresholds"]["whole_window_neighbour_shift_rejects"] is True

    (repair,) = variant["repair_history"]
    assert repair["failed_attempt_id"] == first
    assert repair["cause"]["requirement_ids"] == ["picture:clip-2"]
    assert repair["outcome"] == "rejected"
    failed_picture = next(c for c in repair["report"]["checks"] if c["kind"] == "picture")
    assert failed_picture["min_local_ssim"] == 0.874, "the replaced attempt stays auditable"
    assert repair["result"]["checks"][1]["min_local_ssim"] == 0.912

    text = response.text
    for leaked in ("https://", "X-Goog-Signature", "Troya", "private caption", "confirm the"):
        assert leaked not in text, f"{leaked!r} must not leave the device record"

    missing = await client.get(
        f"/admin/jobs/{uuid.uuid4()}/export-verification",
        headers={"X-Admin-Token": "export-evidence-admin"},
    )
    assert missing.status_code == 404
