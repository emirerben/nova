"""Real DB + task + HTTP journey; failures preregistered before implementation."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models import CreationThread, CreationThreadEvent, InternalAccountGrant, User

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Requires disposable migrated PostgreSQL", allow_module_level=True)


@pytest.fixture
async def journey(monkeypatch):
    from app.models import RequestFollowingCase, RequestFollowingCollector
    from app.routes import admin_request_following

    owner = uuid.uuid4()
    thread = uuid.uuid4()
    grant = uuid.uuid4()
    monkeypatch.setattr(settings, "request_following_collection_enabled", True)
    monkeypatch.setattr(settings, "request_following_all_users", False)
    monkeypatch.setattr(settings, "request_following_user_ids", [str(owner)])
    monkeypatch.setattr(settings, "request_following_thread_ids", [])
    monkeypatch.setattr(settings, "request_following_holdout_thread_ids", [])
    monkeypatch.setattr(settings, "admin_api_key", "queue-test-admin")
    async with AsyncSessionLocal() as db:
        await db.execute(delete(RequestFollowingCase))
        await db.execute(delete(RequestFollowingCollector))
        db.add(User(id=owner, email=f"{owner}@queue.test"))
        await db.flush()
        db.add(
            InternalAccountGrant(
                id=grant, creator_id=owner, granted_by="test", idempotency_key=str(grant)
            )
        )
        db.add(CreationThread(id=thread, creator_id=owner))
        await db.flush()
        db.add(
            CreationThreadEvent(
                thread_id=thread,
                sequence=1,
                event_type="user_message",
                role="user",
                revision=1,
                content="Keep the title and English subtitles; remove labels.",
            )
        )
        await db.commit()
    app = FastAPI()
    app.include_router(admin_request_following.router, prefix="/admin/request-following")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Admin-Token": "queue-test-admin"},
    ) as client:
        yield client, owner, thread, grant
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == owner))
        await db.execute(delete(RequestFollowingCollector))
        await db.commit()
    await engine.dispose()


async def collect():
    from app.tasks.request_following import collect_request_following
    from app.worker import celery_app

    entry = celery_app.conf.beat_schedule["request-following-collection"]
    route = celery_app.amqp.router.route(entry.get("options") or {}, entry["task"])
    assert route["queue"].name == "maintenance", (
        "Collector must run while render workers are stopped"
    )
    assert entry["schedule"] == 900.0
    return await asyncio.to_thread(collect_request_following.run)


async def first(client):
    response = await client.get("/admin/request-following")
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def review(detail):
    return {
        "source_sha256": detail["source_sha256"],
        "review_version": detail["review_version"],
        "decision": "approved",
        "reviewer": "Synthetic reviewer",
        "rationale": "Creator distinguishes labels from subtitles.",
        "loss_stage": "unknown",
        "tickets": ["KRI-215"],
        "expectations": [
            {
                "text": "Keep title and English subtitles; remove labels.",
                "source_pointers": ["/sections/events/0/content"],
            }
        ],
    }


async def test_task_queue_review_export_source_change(journey, tmp_path):
    client, _, thread, _ = journey
    assert (
        await client.get("/admin/request-following", headers={"X-Admin-Token": "wrong"})
    ).status_code == 401
    from app.models import CreatorAgentTurn

    async with AsyncSessionLocal() as db:
        event = await db.scalar(
            select(CreationThreadEvent).where(CreationThreadEvent.thread_id == thread)
        )
        db.add(
            CreatorAgentTurn(
                thread_id=thread,
                source_event_id=event.id,
                client_event_id="synthetic-turn",
                request_digest="synthetic",
                status="failed",
                plan_json={"intent": "remove labels"},
                error={"code": "synthetic_execution_failure"},
            )
        )
        await db.commit()
    await collect()
    case = await first(client)
    await collect()
    assert (await first(client))["id"] == case["id"]
    url = f"/admin/request-following/{case['id']}"
    detail = (await client.get(url)).json()
    assert detail["capture"]["sections"]["turns"][0]["plan"]["intent"] == "remove labels"
    assert detail["diagnostics"]["failed_turns"]
    assert detail["diagnostics"]["intent_verdict"] == "unassessed"
    assert detail["review"] is None
    assert detail["evidence"]["rendered_output"] == "missing"
    assert (await client.get(url + "/export")).status_code == 409
    assert (await client.post(url + "/review", json=review(detail))).status_code == 200
    assert (await client.post(url + "/review", json=review(detail))).status_code == 409
    packet = (await client.get(url + "/export")).json()
    assert packet["candidate"]["review"] is None  # intent review cannot approve promotion
    assert packet["candidate"]["intent_review"]["reviewer"] == "Synthetic reviewer"
    assert packet["candidate"]["expectations"]
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[5]
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps(packet))
    command = [
        sys.executable,
        str(root / "scripts/request-following-loop.py"),
        "--store",
        str(tmp_path / "private"),
    ]
    imported = subprocess.run(
        command + ["import-queue", "--input", str(exported)], capture_output=True, text=True
    )
    assert imported.returncode == 0, imported.stderr
    candidate_path = Path(imported.stdout.strip())
    candidate = json.loads(candidate_path.read_text())
    assert candidate["review"] is None
    assert candidate["expectations"] == packet["candidate"]["expectations"]
    refused = subprocess.run(
        command + ["promote", "--candidate", str(candidate_path)], capture_output=True, text=True
    )
    assert refused.returncode == 1
    assert "needs human approval" in refused.stderr
    duplicate = subprocess.run(
        command + ["import-queue", "--input", str(exported)], capture_output=True, text=True
    )
    assert duplicate.returncode == 1
    assert "Candidate exists" in duplicate.stderr
    async with AsyncSessionLocal() as db:
        db.add(
            CreationThreadEvent(
                thread_id=thread,
                sequence=2,
                event_type="user_message",
                role="user",
                revision=2,
                content="Actually remove the title too.",
            )
        )
        await db.commit()
    assert (await client.get(url + "/export")).status_code == 409
    await collect()
    fresh = await first(client)
    assert fresh["id"] != case["id"]
    assert fresh["review_state"] == "unreviewed"
    assert (await client.get(url + "/export")).status_code == 409


async def test_disabled_scope_consent_and_regrant(journey, monkeypatch):
    client, owner, _, grant = journey
    monkeypatch.setattr(settings, "request_following_collection_enabled", False)
    assert (await collect())["status"] == "disabled"
    monkeypatch.setattr(settings, "request_following_collection_enabled", True)
    monkeypatch.setattr(settings, "request_following_user_ids", [])
    assert (await collect())["status"] == "empty_scope"
    monkeypatch.setattr(settings, "request_following_user_ids", [str(owner), "invalid"])
    assert (await collect())["status"] == "empty_scope"
    monkeypatch.setattr(settings, "request_following_user_ids", [str(owner)])
    async with AsyncSessionLocal() as db:
        row = await db.get(InternalAccountGrant, grant)
        row.status = "revoked"
        await db.commit()
    await collect()
    assert (await client.get("/admin/request-following")).json()["items"] == []
    async with AsyncSessionLocal() as db:
        row = await db.get(InternalAccountGrant, grant)
        row.status = "active"
        await db.commit()
    await collect()
    case = await first(client)
    async with AsyncSessionLocal() as db:
        row = await db.get(InternalAccountGrant, grant)
        row.status = "revoked"
        await db.commit()
    assert (await client.get(f"/admin/request-following/{case['id']}")).status_code == 404
    async with AsyncSessionLocal() as db:
        db.add(
            InternalAccountGrant(
                creator_id=owner, granted_by="test", idempotency_key=str(uuid.uuid4())
            )
        )
        await db.commit()
    assert (await client.get(f"/admin/request-following/{case['id']}")).status_code == 404
    await collect()
    assert (await first(client))["id"] != case["id"]


async def test_holdout_never_exposes_source_or_changes_split(journey, monkeypatch):
    client, _, thread, _ = journey
    monkeypatch.setattr(settings, "request_following_holdout_thread_ids", [str(thread)])
    await collect()
    response = (await client.get("/admin/request-following")).json()
    assert response["items"] == []
    assert response["holdout_count"] == 1
    monkeypatch.setattr(settings, "request_following_holdout_thread_ids", [])
    await collect()
    assert (await client.get("/admin/request-following")).json()["items"] == []
    from app.models import RequestFollowingCase

    async with AsyncSessionLocal() as db:
        case = (
            await db.scalars(
                select(RequestFollowingCase).where(RequestFollowingCase.thread_id == thread)
            )
        ).first()
        assert case.capture is None
        assert (await client.get(f"/admin/request-following/{case.id}")).status_code == 404


async def test_expiry_deletion_and_non_creator_expectations(journey):
    from app.models import RequestFollowingCase

    client, _, thread, _ = journey
    await collect()
    case = await first(client)
    url = f"/admin/request-following/{case['id']}"
    detail = (await client.get(url)).json()
    bad = review(detail)
    bad["expectations"][0]["source_pointers"] = ["/sections/turns/0/plan"]
    assert (await client.post(url + "/review", json=bad)).status_code == 422
    async with AsyncSessionLocal() as db:
        row = await db.get(RequestFollowingCase, uuid.UUID(case["id"]))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert (await client.get(url)).status_code == 404
    await collect()
    async with AsyncSessionLocal() as db:
        row = await db.get(RequestFollowingCase, uuid.UUID(case["id"]))
        assert row.capture is None
        await db.execute(delete(CreationThread).where(CreationThread.id == thread))
        await db.commit()
        assert not (
            await db.scalars(
                select(RequestFollowingCase).where(RequestFollowingCase.thread_id == thread)
            )
        ).all()


async def test_persistent_cursor_concurrency_and_late_changes(journey, monkeypatch):
    from app.models import RequestFollowingCase, RequestFollowingCollector
    from app.services import request_following_collection as collection

    client, owner, original, _ = journey
    monkeypatch.setattr(collection, "BATCH_SIZE", 1)
    extra = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(CreationThread(id=extra, creator_id=owner))
        await db.flush()
        db.add(
            CreationThreadEvent(
                thread_id=extra,
                sequence=1,
                revision=1,
                role="user",
                event_type="user_message",
                content="Use original sound.",
            )
        )
        await db.commit()
    # Separate task sessions compete for the same durable cursor lock.
    results = await asyncio.gather(collect(), collect())
    assert all(r["status"] in {"busy", "complete"} for r in results)
    for _ in range(4):
        await collect()
    async with AsyncSessionLocal() as db:
        current = (
            await db.scalars(
                select(RequestFollowingCase).where(RequestFollowingCase.superseded.is_(False))
            )
        ).all()
        assert {c.thread_id for c in current} == {original, extra}
        assert len(current) == 2
        assert await db.get(RequestFollowingCollector, 1)
        # A late event may not update the CreationThread timestamp. The sweep
        # must still discover it after wrapping, without a timestamp watermark.
        db.add(
            CreationThreadEvent(
                thread_id=extra,
                sequence=2,
                revision=2,
                role="user",
                event_type="user_message",
                content="Keep all speech.",
            )
        )
        await db.commit()
    for _ in range(4):
        await collect()
    async with AsyncSessionLocal() as db:
        versions = (
            await db.scalars(
                select(RequestFollowingCase).where(RequestFollowingCase.thread_id == extra)
            )
        ).all()
        assert len(versions) == 2
        assert sum(not c.superseded for c in versions) == 1
    response = (await client.get("/admin/request-following?limit=1")).json()
    assert len(response["items"]) == 1 and response["next_cursor"]
    second = (
        await client.get(
            "/admin/request-following", params={"limit": 1, "cursor": response["next_cursor"]}
        )
    ).json()
    assert second["items"][0]["id"] != response["items"][0]["id"]


async def test_local_app_real_http_proxy_and_review(journey):
    """Browser-facing local server -> real API -> PostgreSQL, no mocked responses."""
    import socket
    import subprocess
    import sys
    from pathlib import Path

    import uvicorn

    from app.routes import admin_request_following

    await collect()
    app = FastAPI()
    app.include_router(admin_request_following.router, prefix="/admin/request-following")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    upstream_port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        await asyncio.sleep(0.01)
    root = Path(__file__).resolve().parents[5]
    process = subprocess.Popen(
        [
            sys.executable,
            str(root / "scripts/request-following-review.py"),
            "--api-url",
            f"http://127.0.0.1:{upstream_port}",
            "--port",
            "0",
            "--no-open",
        ],
        cwd=root,
        env={**os.environ, "ADMIN_API_KEY": "queue-test-admin"},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=10)
        url = line.strip().removeprefix("Review app: ")
        assert url.startswith("http://127.0.0.1:"), line
        async with httpx.AsyncClient(base_url=url) as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert "Did the final video follow the request?" in page.text
            assert "queue-test-admin" not in page.text
            config = (await client.get("/config")).json()
            assert "queue-test-admin" not in str(config)
            assert (await client.get("/api/cases")).status_code == 403
            assert (
                await client.get("/config", headers={"Host": "attacker.test"})
            ).status_code == 403
            headers = {"X-Review-Session": config["session"], "Origin": url}
            queue = (await client.get("/api/cases?review_state=unreviewed", headers=headers)).json()
            case = queue["items"][0]
            path = f"/api/cases/{case['id']}"
            detail = (await client.get(path, headers=headers)).json()
            assert (
                await client.post(
                    path + "/review",
                    headers={**headers, "Origin": "http://attacker.test"},
                    json=review(detail),
                )
            ).status_code == 403
            saved = await client.post(path + "/review", headers=headers, json=review(detail))
            assert saved.status_code == 200, saved.text
            assert (await client.get("/api/cases?review_state=approved", headers=headers)).json()[
                "items"
            ][0]["id"] == case["id"]
            assert (await client.get(path + "/export", headers=headers)).json()["candidate"][
                "review"
            ] is None
            assert (await client.post("/api/cases", headers=headers, json={})).status_code == 403
            assert (await client.get("/api/templates", headers=headers)).status_code == 404
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)
        server.should_exit = True
        await task
        listener.close()


async def test_cross_owner_projection_never_captures_foreign_video(journey, monkeypatch):
    from app.models import Job

    client, _, thread, _ = journey
    monkeypatch.setattr(settings, "creation_thread_degraded_projection_enabled", True)
    foreign = uuid.uuid4()
    job_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=foreign, email=f"{foreign}@queue.test"))
        await db.flush()
        db.add(
            Job(
                id=job_id,
                user_id=foreign,
                raw_storage_path="synthetic",
                assembly_plan={"variants": [{"variant_id": "FOREIGN-PRIVATE-MARKER"}]},
            )
        )
        await db.flush()
        row = await db.get(CreationThread, thread)
        row.active_job_id = job_id
        await db.commit()
    try:
        await collect()
        case = await first(client)
        detail = await client.get(f"/admin/request-following/{case['id']}")
        assert "FOREIGN-PRIVATE-MARKER" not in detail.text
        assert detail.json()["capture"]["sections"]["integrity"]["video"] is None
        assert detail.json()["diagnostics"]["broken_edges"]
    finally:
        async with AsyncSessionLocal() as db:
            row = await db.get(CreationThread, thread)
            row.active_job_id = None
            await db.flush()
            await db.execute(delete(Job).where(Job.id == job_id))
            await db.execute(delete(User).where(User.id == foreign))
            await db.commit()


async def test_all_users_scope_still_requires_recorded_eligibility(journey, monkeypatch):
    from app.services import request_following_collection as collection

    client, owner, original, _ = journey
    monkeypatch.setattr(settings, "request_following_user_ids", [])
    monkeypatch.setattr(settings, "request_following_all_users", True, raising=False)
    monkeypatch.setattr(collection, "BATCH_SIZE", 1000)
    eligible, ineligible = uuid.uuid4(), uuid.uuid4()
    threads = {eligible: uuid.uuid4(), ineligible: uuid.uuid4()}
    async with AsyncSessionLocal() as db:
        for user_id, thread_id in threads.items():
            db.add(User(id=user_id, email=f"{user_id}@queue.test"))
            await db.flush()
            db.add(CreationThread(id=thread_id, creator_id=user_id))
        db.add(
            InternalAccountGrant(
                creator_id=eligible, granted_by="test", idempotency_key=str(uuid.uuid4())
            )
        )
        await db.commit()
    try:
        result = await collect()
        assert result["status"] == "complete"
        response = (await client.get("/admin/request-following?limit=50")).json()
        ids = {item["thread_id"] for item in response["items"]}
        assert str(original) in ids
        assert str(threads[eligible]) in ids
        assert str(threads[ineligible]) not in ids
        assert response["collector"]["all_users"] is True
        monkeypatch.setattr(settings, "request_following_holdout_thread_ids", ["invalid"])
        assert (await collect())["status"] == "empty_scope"
        assert (await client.get("/admin/request-following")).json()["items"] == []
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id.in_([eligible, ineligible])))
            await db.commit()


@pytest.fixture
async def output_journey(journey, monkeypatch, tmp_path):
    """Real persisted thread/plan/job and a real local MP4 object."""
    import subprocess

    from app.models import ContentPlan, Job, Persona, PlanItem
    from app.routes import admin_request_following

    client, owner, thread, grant = journey
    monkeypatch.setattr(settings, "request_following_report_token", "report-read-only")
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "local_storage_base_url", "http://127.0.0.1:8000/dev-qa/storage")
    client._transport.app.include_router(
        admin_request_following.report_router, prefix="/request-following-report"
    )
    plan_id, item_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    path = f"generative-jobs/{job_id}/review.mp4"
    media = tmp_path / path
    media.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=180x320:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ],
        check=True,
    )
    async with AsyncSessionLocal() as db:
        persona = Persona(user_id=owner)
        db.add(persona)
        await db.flush()
        db.add(ContentPlan(id=plan_id, user_id=owner, persona_id=persona.id))
        await db.flush()
        item = PlanItem(id=item_id, content_plan_id=plan_id, idea="Fixture export", position=0)
        db.add(item)
        await db.flush()
        db.add(
            Job(
                id=job_id,
                user_id=owner,
                content_plan_item_id=item_id,
                raw_storage_path="fixture",
                status="variants_ready",
                mode="generative",
                assembly_plan={
                    "variants": [
                        {
                            "variant_id": "original_text",
                            "render_status": "ready",
                            "video_path": path,
                        }
                    ]
                },
            )
        )
        await db.flush()
        item.current_job_id = job_id
        row = await db.get(CreationThread, thread)
        row.content_plan_id = plan_id
        row.active_plan_item_id = item_id
        row.active_job_id = job_id
        await db.commit()
    await collect()
    case = await first(client)
    yield client, case, media, grant, job_id
    async with AsyncSessionLocal() as db:
        row = await db.get(CreationThread, thread)
        row.active_job_id = row.active_plan_item_id = row.content_plan_id = None
        await db.flush()
        item = await db.get(PlanItem, item_id)
        item.current_job_id = None
        await db.flush()
        await db.execute(delete(Job).where(Job.id == job_id))
        await db.execute(delete(ContentPlan).where(ContentPlan.id == plan_id))
        await db.commit()


async def outcome_body(client, case):
    url = f"/admin/request-following/{case['id']}"
    body = review((await client.get(url)).json())
    media = await client.get(url + "/media")
    assert media.status_code == 200, media.text
    assert media.json()["status"] == "available"
    body["expectations"] += [
        {
            "text": "Remove labels PRIVATE-SECRET-REQUEST",
            "source_pointers": ["/sections/events/0/content"],
        }
    ]
    body["outcome_review"] = {
        "artifact": media.json()["artifact"],
        "watched": True,
        "listened": True,
        "outcomes": [
            {
                "expectation_index": 0,
                "category": "captions",
                "verdict": "missed",
                "note": "PRIVATE-REVIEW-NOTE",
                "time_s": 0.2,
            },
            {
                "expectation_index": 1,
                "category": "labels",
                "verdict": "unclear",
                "note": "Cannot judge all frames",
            },
        ],
    }
    return url, body


async def test_actual_export_review_reloads_and_enters_private_safe_daily_report(output_journey):
    client, case, _, _, _ = output_journey
    url, body = await outcome_body(client, case)
    assert (await client.post(url + "/review", json=body)).status_code == 200
    detail = (await client.get(url)).json()
    assert detail["review"]["outcome_review"]["outcomes"][0]["verdict"] == "missed"
    assert detail["evidence"]["rendered_output"] == "recorded_human_review_recheck_media"
    headers = {"X-Report-Token": "report-read-only"}
    report = await client.get("/request-following-report", headers=headers)
    assert report.status_code == 200, report.text
    data = report.json()
    assert data["counts"]["export_reviewed_sessions"] == 1
    assert data["delivery"]["unfulfilled_reviewed_sessions"] == 1
    assert data["delivery"]["fulfilled_reviewed_sessions"] == 0
    assert data["requirements"] == {
        "met": 0,
        "partial": 0,
        "missed": 1,
        "unclear": 1,
        "creator_changed": 0,
        "total": 2,
    }
    assert data["next_actions"][0]["category"] == "captions"
    assert data["next_actions"][0]["sessions"] == 1
    for private in (
        "PRIVATE-SECRET",
        "PRIVATE-REVIEW",
        "queue.test",
        "source_pointers",
        "video_path",
        "storage.googleapis.com",
    ):
        assert private not in report.text
    assert (
        await client.get("/request-following-report", headers={"X-Report-Token": "wrong"})
    ).status_code == 401
    # Report credential cannot be used as an admin credential.
    assert (await client.get(url, headers={"X-Admin-Token": "report-read-only"})).status_code == 401
    assert (await client.post(url + "/review", json=body)).status_code == 409


async def test_ready_job_without_review_remains_unknown_fulfillment(output_journey):
    client, _, _, _, _ = output_journey
    response = await client.get(
        "/request-following-report", headers={"X-Report-Token": "report-read-only"}
    )
    data = response.json()
    assert data["delivery"]["ready_job_sessions"] == 1
    assert data["delivery"]["unknown_fulfillment_sessions"] == 1
    assert data["delivery"]["fulfilled_reviewed_sessions"] == 0
    assert data["delivery"]["verified_device_exports"] == 0


async def test_complete_human_review_is_separate_from_device_export_ack(output_journey):
    client, case, _, _, _ = output_journey
    url, body = await outcome_body(client, case)
    for outcome in body["outcome_review"]["outcomes"]:
        outcome["verdict"] = "met"
    assert (await client.post(url + "/review", json=body)).status_code == 200
    data = (
        await client.get(
            "/request-following-report", headers={"X-Report-Token": "report-read-only"}
        )
    ).json()
    assert data["delivery"]["fulfilled_reviewed_sessions"] == 1
    assert data["delivery"]["unknown_fulfillment_sessions"] == 0
    assert data["delivery"]["verified_device_exports"] == 0
    assert data["delivery"]["first_attempt_fulfillment"] is None


async def test_output_review_requires_actual_viewing_and_complete_verdicts(output_journey):
    import copy

    client, case, _, _, _ = output_journey
    url, body = await outcome_body(client, case)
    for mutate in (
        lambda b: b["outcome_review"].update(watched=False),
        lambda b: b["outcome_review"].update(outcomes=b["outcome_review"]["outcomes"][:1]),
        lambda b: b["outcome_review"]["outcomes"][1].update(expectation_index=0),
        lambda b: b["outcome_review"]["artifact"].update(job_id=str(uuid.uuid4())),
    ):
        bad = copy.deepcopy(body)
        mutate(bad)
        response = await client.post(url + "/review", json=bad)
        assert response.status_code in {409, 422}, response.text
    assert (await client.get(url)).json()["review"] is None
    # An ordinary approved intent must not be reported as verified output.
    body.pop("outcome_review")
    assert (await client.post(url + "/review", json=body)).status_code == 200
    data = (
        await client.get(
            "/request-following-report", headers={"X-Report-Token": "report-read-only"}
        )
    ).json()
    assert data["counts"]["export_reviewed_sessions"] == 0
    assert data["requirements"]["total"] == 0


async def test_changed_export_rejects_save_and_retires_old_verdict(output_journey):
    client, case, media, _, _ = output_journey
    url, body = await outcome_body(client, case)
    assert (await client.post(url + "/review", json=body)).status_code == 200
    media.write_bytes(media.read_bytes() + b"changed-object-generation")
    body["review_version"] = 1
    assert (await client.post(url + "/review", json=body)).status_code == 409
    data = (
        await client.get(
            "/request-following-report", headers={"X-Report-Token": "report-read-only"}
        )
    ).json()
    assert data["counts"]["stale_output_reviews"] == 1
    assert data["counts"]["export_reviewed_sessions"] == 0
    assert not data["next_actions"]


async def test_revocation_hides_review_media_and_report(output_journey):
    client, case, _, grant, _ = output_journey
    url, body = await outcome_body(client, case)
    assert (await client.post(url + "/review", json=body)).status_code == 200
    async with AsyncSessionLocal() as db:
        row = await db.get(InternalAccountGrant, grant)
        row.status = "revoked"
        row.revoked_at = datetime.now(UTC)
        await db.commit()
    assert (await client.get(url + "/media")).status_code == 404
    data = (
        await client.get(
            "/request-following-report", headers={"X-Report-Token": "report-read-only"}
        )
    ).json()
    assert data["counts"]["captured_sessions"] == 0
    assert data["counts"]["export_reviewed_sessions"] == 0
    assert case["id"] not in str(data)


async def test_foreign_media_path_and_unfinished_output_never_play(output_journey):
    from app.models import Job

    client, case, _, _, job_id = output_journey
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        job.assembly_plan = {
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "video_path": f"generative-jobs/{uuid.uuid4()}/foreign.mp4",
                }
            ]
        }
        await db.commit()
    await collect()
    current = await first(client)
    response = await client.get(f"/admin/request-following/{current['id']}/media")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "url" not in response.json()


async def test_manual_creator_changes_are_excluded_from_ai_failures(output_journey):
    client, case, _, _, _ = output_journey
    url, body = await outcome_body(client, case)
    body["outcome_review"]["outcomes"][0]["verdict"] = "creator_changed"
    body["outcome_review"]["listened"] = False
    response = await client.post(url + "/review", json=body)
    assert response.status_code == 200, response.text
    report = (
        await client.get(
            "/request-following-report", headers={"X-Report-Token": "report-read-only"}
        )
    ).json()
    assert report["counts"]["export_reviewed_sessions"] == 1
    assert report["requirements"]["creator_changed"] == 1
    assert report["requirements"]["unclear"] == 1
    assert report["requirements"]["met"] == report["requirements"]["missed"] == 0
    assert report["next_actions"] == []
