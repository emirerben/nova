"""Durable inbox journey through real PostgreSQL and authenticated HTTP routes."""

import copy
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth import get_current_user
from app.database import sync_session
from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.main import app
from app.models import CreationThread, Job, NativeActivity, PlanItem
from app.services.device_render import (
    device_record,
    mark_device_failed,
    pin_device_request,
    save_device_record,
)
from app.services.native_activity import activity_statement
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.fixture
def journey():
    owner, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        job = Job(
            user_id=owner,
            status="awaiting_device",
            raw_storage_path="test-only",
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            assembly_plan={
                "variants": [{"variant_id": "first", "render_generation_id": "approved"}]
            },
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        thread.active_job_id = job.id
        recipe = EditRecipeV1(
            assets=[{"id": "a", "relative_path": "a"}],
            tracks=[
                {
                    "id": "v",
                    "kind": "video",
                    "clips": [
                        {
                            "id": "c",
                            "source_asset_id": "a",
                            "source_start": 0,
                            "source_duration": 2,
                            "timeline_start": 0,
                            "rate": 1,
                        }
                    ],
                }
            ],
        )
        request = make_device_request(job_id=job.id, variant_id="first", revision=1, recipe=recipe)
        pin_device_request(job, request, base_generation="approved")
        db.commit()
        job_id = job.id
    app.dependency_overrides[get_current_user] = lambda: type("Owner", (), {"id": owner})()
    try:
        with TestClient(app) as client:
            yield owner, thread_id, job_id, client
    finally:
        app.dependency_overrides.clear()


def settle(job, phase):
    record = copy.deepcopy(device_record(job, "first"))
    record["status"]["phase"] = phase
    if phase == "published":
        attempt = str(uuid.uuid4())
        record.update(published_attempt=attempt, base_generation=attempt)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0].update(
            render_generation_id=attempt,
            ok=True,
            video_path="private/output.mp4",
            render_destination="device",
        )
        job.assembly_plan = assembly
    elif phase == "needs_attention":
        record["status"]["reason_code"] = "export_failed"
    save_device_record(job, "first", record)


def test_ready_is_durable_deduplicated_and_owner_scoped(journey):
    owner, thread_id, job_id, client = journey
    with sync_session() as db:
        job = db.get(Job, job_id)
        assert activity_statement(job, "first") is None
        settle(job, "published")
        db.execute(activity_statement(job, "first"))
        db.execute(activity_statement(job, "first"))
        db.commit()
    response = client.get("/me/activities")
    assert response.status_code == 200
    rows = response.json()["items"]
    assert len(rows) == 1 and rows[0]["kind"] == "video_ready"
    assert rows[0]["thread_id"] == str(thread_id)
    assert "private/" not in response.text and "url" not in response.text
    activity_id = rows[0]["id"]
    assert client.post(f"/me/activities/{activity_id}/read").status_code == 200
    assert client.post(f"/me/activities/{activity_id}/read").status_code == 200
    assert client.get("/me/activities").json()["items"][0]["read_at"] is not None
    assert client.get(f"/me/activities/{activity_id}").json()["thread_id"] == str(thread_id)
    app.dependency_overrides[get_current_user] = lambda: type("Other", (), {"id": uuid.uuid4()})()
    assert client.get("/me/activities").json()["items"] == []
    assert client.post(f"/me/activities/{activity_id}/read").status_code == 404
    assert client.get(f"/me/activities/{activity_id}").status_code == 404


def test_rollback_and_supersession_never_expose_stale_success(journey):
    _, _, job_id, client = journey
    with sync_session() as db:
        job = db.get(Job, job_id)
        settle(job, "published")
        db.execute(activity_statement(job, "first"))
        db.rollback()
    assert client.get("/me/activities").json()["items"] == []
    with sync_session() as db:
        job = db.get(Job, job_id)
        settle(job, "published")
        db.execute(activity_statement(job, "first"))
        db.commit()
    activity_id = client.get("/me/activities").json()["items"][0]["id"]
    with sync_session() as db:
        job = db.get(Job, job_id)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0]["render_generation_id"] = "new-edit"
        job.assembly_plan = assembly
        db.commit()
    assert client.get("/me/activities").json()["items"] == []
    assert client.get(f"/me/activities/{activity_id}").status_code == 404


def test_attention_disappears_on_retry_and_deleted_thread_cascades(journey):
    _, thread_id, job_id, client = journey
    with sync_session() as db:
        job = db.get(Job, job_id)
        mark_device_failed(job, "first", reason_code="export_failed", detail="private error")
        db.execute(activity_statement(job, "first"))
        db.commit()
    assert client.get("/me/activities").json()["items"][0]["kind"] == "action_required"
    with sync_session() as db:
        job = db.get(Job, job_id)
        settle(job, "awaiting_device")
        db.commit()
    assert client.get("/me/activities").json()["items"] == []
    with sync_session() as db:
        db.execute(delete(CreationThread).where(CreationThread.id == thread_id))
        db.commit()
        assert (
            db.execute(select(NativeActivity).where(NativeActivity.job_id == job_id))
            .scalars()
            .all()
            == []
        )


def test_thread_moving_to_another_item_hides_the_old_activity(journey):
    owner, thread_id, job_id, client = journey
    with sync_session() as db:
        job = db.get(Job, job_id)
        settle(job, "published")
        db.execute(activity_statement(job, "first"))
        db.commit()
    activity_id = client.get("/me/activities").json()["items"][0]["id"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        replacement_item = PlanItem(
            content_plan_id=thread.content_plan_id, position=2, idea="New edit"
        )
        db.add(replacement_item)
        db.flush()
        replacement_job = Job(
            user_id=owner,
            raw_storage_path="test-only",
            content_plan_item_id=replacement_item.id,
            content_plan_ownership_epoch=0,
        )
        db.add(replacement_job)
        db.flush()
        replacement_item.current_job_id = replacement_job.id
        thread.active_plan_item_id = replacement_item.id
        thread.active_job_id = replacement_job.id
        db.commit()
    assert client.get("/me/activities").json()["items"] == []
    assert client.get(f"/me/activities/{activity_id}").status_code == 404
    assert client.post(f"/me/activities/{activity_id}/read").status_code == 404


@pytest.mark.parametrize(
    "change", ["cancel", "epoch", "quarantine", "missing_output", "missing_proof"]
)
def test_current_target_and_proof_are_required_on_every_read(journey, change):
    from app.models import ContentPlan

    _, _, job_id, client = journey
    with sync_session() as db:
        job = db.get(Job, job_id)
        settle(job, "published")
        db.execute(activity_statement(job, "first"))
        db.commit()
    activity_id = client.get("/me/activities").json()["items"][0]["id"]
    with sync_session() as db:
        job = db.get(Job, job_id)
        item = db.get(PlanItem, job.content_plan_item_id)
        plan = db.get(ContentPlan, item.content_plan_id)
        if change == "cancel":
            job.status = "cancelled"
        elif change == "epoch":
            plan.ownership_epoch += 1
        elif change == "quarantine":
            from datetime import UTC, datetime

            plan.ownership_quarantined_at = datetime.now(UTC)
        else:
            assembly = copy.deepcopy(job.assembly_plan)
            if change == "missing_output":
                assembly["variants"][0].pop("video_path")
            else:
                assembly["_device_render_v1"]["first"]["actual_export_policy"] = {"version": 1}
            job.assembly_plan = assembly
        db.commit()
    assert client.get("/me/activities").json()["items"] == []
    assert client.get(f"/me/activities/{activity_id}").status_code == 404


def test_actual_failure_endpoint_records_attention_but_user_stop_does_not(journey, monkeypatch):
    from app.config import settings
    from app.routes import device_render as routes

    _, _, job_id, client = journey
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(routes.limiter, "enabled", False)
    monkeypatch.setattr(routes, "_nudge_kria_observer", lambda _: None)
    with sync_session() as db:
        identity = device_record(db.get(Job, job_id), "first")["status"]["request"]["identity"]
    url = f"/me/jobs/{job_id}/device-render/failures"
    paused = client.post(url, json={"identity": identity, "reason_code": "cancelled_by_user"})
    assert paused.status_code == 200
    assert client.get("/me/activities").json()["items"] == []
    with sync_session() as db:
        from app.services.device_render import clear_device_pause

        job = db.get(Job, job_id)
        clear_device_pause(job, "first")
        db.commit()
    failed = client.post(url, json={"identity": identity, "reason_code": "export_failed"})
    assert failed.status_code == 200, failed.text
    assert client.get("/me/activities").json()["items"][0]["kind"] == "action_required"
    client.post(url, json={"identity": identity, "reason_code": "export_failed"})
    assert len(client.get("/me/activities").json()["items"]) == 1


def test_stale_device_sweep_records_attention_in_its_transaction(journey):
    from datetime import UTC, datetime, timedelta

    from app.tasks.device_render_reaper import _reap_job

    _, _, job_id, client = journey
    now = datetime.now(UTC)
    with sync_session() as db:
        job = db.get(Job, job_id)
        record = copy.deepcopy(device_record(job, "first"))
        record["pinned_at"] = (now - timedelta(hours=25)).isoformat()
        save_device_record(job, "first", record)
        db.commit()
    assert _reap_job(job_id, threshold_s=86400, now=now) == 1
    assert client.get("/me/activities").json()["items"][0]["kind"] == "action_required"
    assert _reap_job(job_id, threshold_s=86400, now=now) == 0
    assert len(client.get("/me/activities").json()["items"]) == 1


def test_exhausted_verification_records_attention_without_a_render(journey, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.tasks import export_verification

    _, _, job_id, client = journey
    now = datetime.now(UTC)
    queued_at = (now - timedelta(hours=25)).isoformat()
    monkeypatch.setattr(
        export_verification,
        "enqueue",
        lambda *_: pytest.fail("must not rerender or requeue exhausted work"),
    )
    with sync_session() as db:
        job = db.get(Job, job_id)
        record = copy.deepcopy(device_record(job, "first"))
        record["status"]["phase"] = "syncing"
        record["attempts"] = {
            str(uuid.uuid4()): {"verification_task": {"state": "queued", "queued_at": queued_at}}
        }
        save_device_record(job, "first", record)
        db.commit()
    export_verification._expire_queued(job_id, now)
    assert client.get("/me/activities").json()["items"][0]["kind"] == "action_required"


def test_actual_publication_endpoint_creates_ready_only_after_media_checks(
    journey, monkeypatch, tmp_path
):
    import hashlib
    import shutil
    import subprocess
    from types import SimpleNamespace

    from app.config import settings
    from app.routes import device_render as routes

    _, _, job_id, client = journey
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(routes.limiter, "enabled", False)
    monkeypatch.setattr(routes, "_nudge_kria_observer", lambda _: None)
    media = tmp_path / "synthetic-native-output.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1080x1920:r=30:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        check=True,
        timeout=20,
    )
    payload = media.read_bytes()
    monkeypatch.setattr(
        routes.storage, "signed_put_url", lambda *_: "https://fixture.invalid/upload"
    )
    monkeypatch.setattr(
        routes.storage, "signed_get_url", lambda *_: "https://fixture.invalid/video"
    )
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: SimpleNamespace(size=len(payload), content_type="video/mp4", generation="42"),
    )
    monkeypatch.setattr(
        routes.storage,
        "download_generation_to_file",
        lambda _path, destination, **_: shutil.copyfile(media, destination),
    )
    monkeypatch.setattr(routes, "upload_video_poster", lambda *_args, **_kwargs: None)
    with sync_session() as db:
        identity = device_record(db.get(Job, job_id), "first")["status"]["request"]["identity"]
    attempt_id = str(uuid.uuid4())
    base = f"/me/jobs/{job_id}/device-render"
    reservation = client.post(
        base + "/uploads",
        json={
            "identity": identity,
            "attempt_id": attempt_id,
            "file_size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "brand_tail": "none",
        },
    )
    assert reservation.status_code == 200, reservation.text
    assert client.get("/me/activities").json()["items"] == []
    completed = client.post(
        base + "/complete", json={"identity": identity, "attempt_id": attempt_id}
    )
    assert completed.status_code == 200, completed.text
    rows = client.get("/me/activities").json()["items"]
    assert len(rows) == 1 and rows[0]["kind"] == "video_ready"
    assert (
        client.post(
            base + "/complete", json={"identity": identity, "attempt_id": attempt_id}
        ).status_code
        == 200
    )
    assert len(client.get("/me/activities").json()["items"]) == 1
