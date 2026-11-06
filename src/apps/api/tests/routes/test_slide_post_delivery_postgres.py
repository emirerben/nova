# ruff: noqa: F811
"""Keyed first-render delivery failures, specified before delivery implementation.

Only SQL transactions run: broker, render, and provider work are replaced by counters.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database import sync_session
from app.models import Job, PlanItem, SlidePostGenerationReceipt
from tests.routes.test_slide_post_generation_receipts_postgres import (  # noqa: F401
    _flags,
    _post,
    _receipt_flags,
    client,
    publishes,
    seeded,
)


def _delivery():
    from app.services import slide_post_generation_delivery

    return slide_post_generation_delivery


def _accepted(client, seeded):
    key = uuid.uuid4()
    response = _post(client, seeded, key)
    assert response.status_code == 200, response.text
    return key, uuid.UUID(response.json()["generation_receipt"]["job_id"])


def _forget_publish_ack(key):
    """Model the crash window after broker I/O but before ack metadata commit."""
    with sync_session() as db:
        db.get(SlidePostGenerationReceipt, key).dispatch_state = "unconfirmed"
        db.commit()


def test_two_deliveries_claim_once_and_redelivery_never_recomputes(client, seeded, publishes):
    delivery = _delivery()
    _key, job_id = _accepted(client, seeded)
    computed = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: delivery.run_claimed(str(job_id), lambda: computed.append(job_id)),
                range(2),
            )
        )
    assert sorted(results) == [False, True]
    assert computed == [job_id]
    assert delivery.run_claimed(str(job_id), lambda: computed.append(job_id)) is False
    assert computed == [job_id]


def test_pending_recovery_is_gated_and_claimed_work_is_never_republished(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    _forget_publish_ack(key)
    publishes.clear()
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", False)
    delivery.reconcile_generations()
    assert publishes == []
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    with sync_session() as db:
        pending = db.scalar(
            select(func.count())
            .select_from(SlidePostGenerationReceipt)
            .where(
                SlidePostGenerationReceipt.execution_state == "pending",
            )
        )
    # Other journey fixtures leave synthetic rows; exercise the real bounded
    # scanner through enough batches, counting this request's publication only.
    for _ in range(pending // 50 + 1):
        delivery.reconcile_generations(now=datetime.now(UTC) + timedelta(minutes=10))
    assert publishes.count(str(job_id)) == 1
    delivery.run_claimed(str(job_id), lambda: None)
    delivery.reconcile_generations(now=datetime.now(UTC) + timedelta(hours=1))
    assert publishes.count(str(job_id)) == 1
    with sync_session() as db:
        assert db.get(SlidePostGenerationReceipt, key).execution_state in {"running", "failed"}


def test_accepted_broker_then_exception_does_not_terminalize_or_duplicate_compute(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    _forget_publish_ack(key)
    computed = []
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)

    def accepted_but_raised(*_args, **_kwargs):
        delivery.run_claimed(str(job_id), lambda: computed.append(job_id))
        raise RuntimeError("ack lost after acceptance")

    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", accepted_but_raised)
    delivery.publish_pending(key, now=datetime.now(UTC) + timedelta(minutes=10))
    assert computed == [job_id]
    with sync_session() as db:
        assert db.get(Job, job_id).status == "processing"
    delivery.publish_pending(key, now=datetime.now(UTC) + timedelta(hours=1))
    assert computed == [job_id]


def test_claimed_exception_has_bounded_failure_without_resend(client, seeded, publishes):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)

    class ProcessDied(BaseException):
        pass

    def die():
        raise ProcessDied()

    with pytest.raises(ProcessDied):
        delivery.run_claimed(str(job_id), die)
    delivery.reconcile_claimed(key, now=datetime.now(UTC) + timedelta(hours=2))
    assert delivery.run_claimed(str(job_id), lambda: pytest.fail("paid work repeated")) is False
    with sync_session() as db:
        assert db.get(SlidePostGenerationReceipt, key).execution_state == "failed"
        assert db.get(Job, job_id).status == "processing_failed"
    assert len(publishes) == 1


def test_completion_stamp_is_atomic_and_does_not_overwrite_newer_draft(client, seeded, publishes):
    delivery = _delivery()
    from app.tasks.generative_build import _set_status

    key, job_id = _accepted(client, seeded)

    def complete():
        with sync_session() as db:
            item = db.get(PlanItem, seeded.item_id)
            item.slide_post = {**item.slide_post, "version": 2, "caption": "Newer"}
            db.commit()
        assert _set_status(str(job_id), "variants_ready")

    assert delivery.run_claimed(str(job_id), complete)
    with sync_session() as db:
        assert db.get(SlidePostGenerationReceipt, key).execution_state == "completed"
        draft = db.get(PlanItem, seeded.item_id).slide_post
        assert draft["version"] == 2 and draft["caption"] == "Newer"
        assert draft.get("rendered_version") != 1


def test_legacy_job_has_no_claim_side_effect(client, seeded, publishes):
    delivery = _delivery()
    response = client.post(
        f"/plan-items/{seeded.item_id}/slide-post/generate", json={"expected_version": 1}
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["current_job_id"]
    calls = []
    assert delivery.run_claimed(job_id, lambda: calls.append("legacy"))
    assert delivery.run_claimed(job_id, lambda: calls.append("legacy"))
    assert calls == ["legacy", "legacy"]


def _ready_result(job_id):
    return {
        "variant_id": "slides",
        "rank": 1,
        "text_mode": "slides",
        "ok": True,
        "render_status": "ready",
        "video_path": f"generative-jobs/{job_id}/preview.mp4",
        "render_generation_id": uuid.uuid4().hex,
    }


def test_staged_result_survives_exception_and_finishes_without_compute(client, seeded, publishes):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    result = _ready_result(job_id)

    class ProcessDied(BaseException):
        pass

    def stage_then_die():
        assert delivery.stage_result(str(job_id), result)
        raise ProcessDied()

    with pytest.raises(ProcessDied):
        delivery.run_claimed(str(job_id), stage_then_die)
    assert delivery.reconcile_claimed(key) == "completed"
    assert (
        delivery.run_claimed(str(job_id), lambda: pytest.fail("recomputed completed output"))
        is False
    )
    with sync_session() as db:
        receipt = db.get(SlidePostGenerationReceipt, key)
        job = db.get(Job, job_id)
        item = db.get(PlanItem, seeded.item_id)
        assert receipt.execution_state == "completed"
        assert job.status == "variants_ready"
        assert job.assembly_plan["variants"][0]["video_path"] == result["video_path"]
        assert item.slide_post["rendered_version"] == 1


def test_late_worker_after_policy_cancellation_cannot_publish_or_mark_ready(
    client, seeded, publishes
):
    delivery = _delivery()
    from app.tasks.generative_build import _set_status

    key, job_id = _accepted(client, seeded)

    def paused():
        assert (
            delivery.reconcile_claimed(key, now=datetime.now(UTC) + timedelta(hours=2)) == "failed"
        )
        assert not delivery.stage_result(str(job_id), _ready_result(job_id))
        assert not _set_status(str(job_id), "variants_ready")

    assert delivery.run_claimed(str(job_id), paused)
    with sync_session() as db:
        assert db.get(Job, job_id).status == "processing_failed"
        assert db.get(SlidePostGenerationReceipt, key).execution_state == "failed"


def test_pending_publication_cap_is_actionable_and_never_mints_replacement(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    _forget_publish_ack(key)
    publishes.clear()
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)

    def unknown_publish(_task, published_id, **_kwargs):
        publishes.append(published_id)
        raise RuntimeError("broker outcome unknown")

    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", unknown_publish)
    now = datetime.now(UTC)
    for attempt in range(1, delivery.PUBLISH_ATTEMPTS):
        assert delivery.publish_pending(key, now=now + timedelta(hours=attempt)) == "unconfirmed"
    assert delivery.publish_pending(key, now=now + timedelta(hours=10)) == "exhausted"
    assert (
        delivery.run_claimed(str(job_id), lambda: pytest.fail("late queued message ran")) is False
    )
    with sync_session() as db:
        assert db.get(Job, job_id).failure_reason == "slide_post_dispatch_unconfirmed"
        assert db.get(SlidePostGenerationReceipt, key).job_id == job_id
    assert publishes == [str(job_id)] * (delivery.PUBLISH_ATTEMPTS - 1)


def test_concurrent_publishers_respect_one_attempt_window(client, seeded, publishes, monkeypatch):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    _forget_publish_ack(key)
    publishes.clear()
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    now = datetime.now(UTC) + timedelta(minutes=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: delivery.publish_pending(key, now=now), range(2)))
    assert sorted(outcomes) == ["published", "waiting"]
    assert publishes == [str(job_id)]


def test_successor_target_rejects_staged_completion(client, seeded, publishes):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)

    def stopped_by_successor():
        assert delivery.stage_result(str(job_id), _ready_result(job_id))
        with sync_session() as db:
            db.get(PlanItem, seeded.item_id).current_job_id = None
            db.commit()
        assert delivery.reconcile_claimed(key) == "skip"

    assert delivery.run_claimed(str(job_id), stopped_by_successor)
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).current_job_id is None
        assert db.get(SlidePostGenerationReceipt, key).execution_state == "failed"
        assert db.get(Job, job_id).status == "processing"


def test_commit_before_first_publish_is_recovered_with_same_receipt(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    from app.models import ContentPlan
    from app.services.slide_post_generation_receipts import (
        SlidePostGenerationRequest,
        dispatch_generation,
    )

    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    key = uuid.uuid4()
    original = delivery.publish_pending

    class ProcessDied(BaseException):
        pass

    def die(_key):
        raise ProcessDied()

    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        epoch = db.get(ContentPlan, item.content_plan_id).ownership_epoch
    monkeypatch.setattr(delivery, "publish_pending", die)
    with pytest.raises(ProcessDied):
        dispatch_generation(
            SlidePostGenerationRequest(key, seeded.user_id, seeded.item_id, 1), epoch
        )
    with sync_session() as db:
        receipt = db.get(SlidePostGenerationReceipt, key)
        job_id = receipt.job_id
        assert receipt.publish_attempts == 0
        assert db.get(Job, job_id).status == "queued"
    assert publishes == []
    monkeypatch.setattr(delivery, "publish_pending", original)
    assert delivery.publish_pending(key) == "published"
    computed = []
    assert delivery.run_claimed(str(job_id), lambda: computed.append(job_id))
    assert delivery.run_claimed(str(job_id), lambda: computed.append(job_id)) is False
    assert computed == [job_id] and publishes == [str(job_id)]
    assert _post(client, seeded, key).json()["generation_receipt"]["job_id"] == str(job_id)


def test_publish_before_ack_receipt_loss_allows_only_one_worker(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    from app.services import slide_post_generation_receipts

    key, job_id = _accepted(client, seeded)
    _forget_publish_ack(key)
    publishes.clear()
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    original = slide_post_generation_receipts.record_publication

    class ProcessDied(BaseException):
        pass

    def die(*_args, **_kwargs):
        raise ProcessDied()

    monkeypatch.setattr(slide_post_generation_receipts, "record_publication", die)
    with pytest.raises(ProcessDied):
        delivery.publish_pending(key, now=datetime.now(UTC) + timedelta(minutes=10))
    monkeypatch.setattr(slide_post_generation_receipts, "record_publication", original)
    assert (
        delivery.publish_pending(key, now=datetime.now(UTC) + timedelta(minutes=20)) == "published"
    )
    computed = []
    for published_id in publishes:
        delivery.run_claimed(published_id, lambda: computed.append(job_id))
    assert len(publishes) == 2 and computed == [job_id]


def test_nonqueued_pending_receipt_retires_without_stamping_or_publication(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    with sync_session() as db:
        db.get(Job, job_id).status = "variants_ready"
        db.commit()
    publishes.clear()
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    assert delivery.publish_pending(key) == "skip"
    with sync_session() as db:
        assert db.get(SlidePostGenerationReceipt, key).execution_state != "pending"
        assert db.get(PlanItem, seeded.item_id).slide_post.get("rendered_version") is None
    assert publishes == []


def test_real_task_entry_claims_before_body_even_when_recovery_flag_is_off(
    client, seeded, publishes, monkeypatch
):
    from contextlib import nullcontext

    from app.tasks import generative_build

    _key, job_id = _accepted(client, seeded)
    calls = []
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", False)
    monkeypatch.setattr(generative_build, "_run_generative_job", lambda job: calls.append(job))
    monkeypatch.setattr(generative_build, "job_heartbeat", lambda *_: nullcontext())
    monkeypatch.setattr(generative_build, "mark_finished", lambda *_: None)
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda *_: nullcontext())
    monkeypatch.setattr(
        "app.services.cloud_render_policy.block_cloud_render_task", lambda *_a, **_kw: False
    )
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda *_: None)
    generative_build.orchestrate_generative_job.run(str(job_id))
    generative_build.orchestrate_generative_job.run(str(job_id))
    assert calls == [str(job_id)]
    with sync_session() as db:
        assert db.get(Job, job_id).status == "processing"


@pytest.mark.parametrize("persist_result", [False, True], ids=["claimed", "staged"])
@pytest.mark.timeout(60)
def test_killed_process_recovers_original_receipt_without_recompute(
    client, seeded, publishes, persist_result
):
    """A separate process commits, is SIGKILLed, and cannot finish via cleanup."""
    import json
    import os
    import selectors
    import signal
    import subprocess
    import sys
    from urllib.parse import urlparse

    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    result = _ready_result(job_id) if persist_result else None
    database_url = settings.database_url
    parsed = urlparse(database_url)
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    assert parsed.path.endswith("_test"), "Process-kill evidence requires an isolated test DB"
    # Fresh interpreter and pool; no inherited parent connection or thread state.
    # The child can connect only to this exact local test database. It cannot
    # reach a broker/provider, and dotenv loading is disabled before app imports.
    code = r"""
import json, os, socket, sys
from urllib.parse import urlparse
import pydantic_settings
import psycopg2
url = os.environ["DATABASE_URL"]
expected = urlparse(url)
connect = socket.socket.connect
def guarded(self, address):
    local = isinstance(address, tuple) and address[0] in {"localhost", "127.0.0.1", "::1"}
    if not (local and address[1] == (expected.port or 5432)):
        raise AssertionError("Child external network blocked")
    return connect(self, address)
socket.socket.connect = guarded
socket.socket.connect_ex = guarded
original = psycopg2.connect
def guarded_db(dsn=None, *args, **kwargs):
    values = psycopg2.extensions.parse_dsn(dsn or "")
    values.update(kwargs)
    assert values.get("dbname") == expected.path.lstrip("/")
    assert values.get("host") in {"localhost", "127.0.0.1", "::1"}
    assert int(values.get("port", 5432)) == (expected.port or 5432)
    return original(dsn, *args, **kwargs)
psycopg2.connect = guarded_db
initialize = pydantic_settings.BaseSettings.__init__
def isolated_settings(self, **values):
    values["_env_file"] = None
    initialize(self, **values)
pydantic_settings.BaseSettings.__init__ = isolated_settings
from app.services import slide_post_generation_delivery as delivery
job_id, result = json.loads(sys.argv[1])
def body():
    if result is not None:
        assert delivery.stage_result(job_id, result)
    print("COMMITTED", flush=True)
    sys.stdin.read()
assert delivery.run_claimed(job_id, body)
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_URL": database_url,
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "STORAGE_BUCKET": "offline",
        "OPENAI_API_KEY": "test",
        "GEMINI_API_KEY": "test",
        "ANTHROPIC_API_KEY": "test",
        "TOKEN_ENCRYPTION_KEY": "test",
        "WAITLIST_ADMIN_SECRET": "test",
        "INTERNAL_API_KEY": "test",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    child = subprocess.Popen(
        [sys.executable, "-c", code, json.dumps([str(job_id), result])],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "Child infrastructure did not report commit"
            assert child.stdout.readline().strip() == "COMMITTED"
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        # Confirm the killed process's durable writes before any recovery.
        with sync_session() as db:
            receipt = db.get(SlidePostGenerationReceipt, key)
            assert receipt.execution_state == "running"
            assert receipt.staged_result == result
        assert not delivery.run_claimed(str(job_id), lambda: pytest.fail("duplicate compute"))
        outcome = delivery.reconcile_claimed(key, now=datetime.now(UTC) + timedelta(hours=2))
        assert outcome == ("completed" if persist_result else "failed")
        assert not delivery.run_claimed(str(job_id), lambda: pytest.fail("duplicate compute"))
        with sync_session() as db:
            receipt = db.get(SlidePostGenerationReceipt, key)
            job = db.get(Job, job_id)
            assert receipt.job_id == job_id
            assert job.status == ("variants_ready" if persist_result else "processing_failed")
            if persist_result:
                assert job.assembly_plan["variants"][0]["video_path"] == result["video_path"]
                assert db.get(PlanItem, seeded.item_id).slide_post["rendered_version"] == 1
            else:
                assert job.failure_reason == "slide_post_render_interrupted"
        if not persist_result:
            state = client.get(f"/plan-items/{seeded.item_id}/slide-post")
            assert state.status_code == 200
            assert state.json()["render_status"] == "failed"
            receipt_response = client.get(
                f"/plan-items/{seeded.item_id}/slide-post/generation-requests/{key}"
            )
            assert receipt_response.json()["job_status"] == "processing_failed"
        assert len(publishes) == 1
    finally:
        if child.poll() is None:
            child.kill()
        _stdout, stderr = child.communicate(timeout=10)
        assert not stderr, stderr


def test_unexpired_claim_without_result_does_not_consume_recovery_budget(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    assert delivery.run_claimed(str(job_id), lambda: None)
    observed = []
    monkeypatch.setattr(
        delivery, "reconcile_claimed", lambda key, **_: observed.append(key) or "seen"
    )
    delivery.reconcile_generations()
    assert key not in observed


def test_json_null_results_cannot_starve_a_later_staged_result(client, seeded, publishes):
    from sqlalchemy import JSON, delete

    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    result = _ready_result(job_id)
    assert delivery.run_claimed(str(job_id), lambda: delivery.stage_result(str(job_id), result))
    older_keys = [uuid.uuid4() for _ in range(51)]
    now = datetime.now(UTC)
    try:
        with sync_session() as db:
            original = db.get(SlidePostGenerationReceipt, key)
            for older_key in older_keys:
                db.add(
                    SlidePostGenerationReceipt(
                        client_request_id=older_key,
                        owner_id=original.owner_id,
                        item_id=original.item_id,
                        ownership_epoch=original.ownership_epoch,
                        draft_version=original.draft_version,
                        request_digest=original.request_digest,
                        job_id=uuid.uuid4(),
                        draft_snapshot=original.draft_snapshot,
                        asset_snapshot=original.asset_snapshot,
                        snapshot_digest=original.snapshot_digest,
                        execution_state="running",
                        claimed_at=now - timedelta(minutes=1),
                        staged_result=None,
                    )
                )
            db.commit()
            # Prove these are JSON null, not SQL NULL, before testing selection.
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(SlidePostGenerationReceipt)
                    .where(
                        SlidePostGenerationReceipt.client_request_id.in_(older_keys),
                        SlidePostGenerationReceipt.staged_result == JSON.NULL,
                    )
                )
                == 51
            )
        delivery.reconcile_generations(now=now)
        with sync_session() as db:
            assert db.get(SlidePostGenerationReceipt, key).execution_state == "completed"
            assert db.get(Job, job_id).status == "variants_ready"
            assert all(
                db.get(SlidePostGenerationReceipt, k).execution_state == "running"
                for k in older_keys
            )
    finally:
        with sync_session() as db:
            db.execute(
                delete(SlidePostGenerationReceipt).where(
                    SlidePostGenerationReceipt.client_request_id.in_(older_keys)
                )
            )
            db.commit()


def test_acknowledged_queue_backlog_does_not_exhaust_unknown_attempts(client, seeded, publishes):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    now = datetime.now(UTC)
    for minute in range(1, 31):
        assert delivery.publish_pending(key, now=now + timedelta(minutes=minute)) == "waiting"
    with sync_session() as db:
        assert db.get(Job, job_id).status == "queued"
        assert db.get(SlidePostGenerationReceipt, key).publish_attempts == 1
    assert len(publishes) == 1
    assert delivery.publish_pending(key, now=now + timedelta(minutes=36)) == "queue_timeout"
    with sync_session() as db:
        assert db.get(Job, job_id).failure_reason == "slide_post_queue_timeout"


def test_staged_publication_retries_are_bounded_without_recompute(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    delivery.run_claimed(
        str(job_id), lambda: delivery.stage_result(str(job_id), _ready_result(job_id))
    )
    calls = []

    def broken(*_args):
        calls.append(job_id)
        raise RuntimeError("publication unavailable")

    monkeypatch.setattr("app.tasks.generative_build._finalize_job", broken)
    now = datetime.now(UTC)
    for attempt in range(5):
        assert delivery.reconcile_claimed(key, now=now + timedelta(minutes=10 * attempt)) == "retry"
    assert delivery.reconcile_claimed(key, now=now + timedelta(hours=2)) == "exhausted"
    assert len(calls) == 5
    assert not delivery.run_claimed(str(job_id), lambda: pytest.fail("recompute"))
    with sync_session() as db:
        assert db.get(Job, job_id).failure_reason == "slide_post_publication_failed"
        assert db.get(SlidePostGenerationReceipt, key).execution_state == "failed"


def test_finalization_commit_then_response_loss_does_not_repeat_publication(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    from app.tasks import generative_build

    key, job_id = _accepted(client, seeded)
    delivery.run_claimed(
        str(job_id), lambda: delivery.stage_result(str(job_id), _ready_result(job_id))
    )
    original = generative_build._finalize_job
    calls = []

    def commit_then_raise(*args):
        calls.append(job_id)
        assert original(*args)
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(generative_build, "_finalize_job", commit_then_raise)
    delivery.reconcile_claimed(key)
    delivery.reconcile_claimed(key, now=datetime.now(UTC) + timedelta(hours=1))
    assert calls == [job_id]
    with sync_session() as db:
        assert db.get(Job, job_id).status == "variants_ready"
        assert db.get(SlidePostGenerationReceipt, key).execution_state == "completed"


def test_concurrent_finalizers_reserve_one_attempt(client, seeded, publishes, monkeypatch):
    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    delivery.run_claimed(
        str(job_id), lambda: delivery.stage_result(str(job_id), _ready_result(job_id))
    )
    calls = []
    monkeypatch.setattr(
        "app.tasks.generative_build._finalize_job", lambda *_: calls.append(job_id) or False
    )
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: delivery.reconcile_claimed(key, now=now), range(2)))
    assert sorted(outcomes) == ["retry", "waiting"]
    assert calls == [job_id]


def test_scanner_contains_one_row_failure_and_finishes_following_result(
    client, seeded, publishes, monkeypatch
):
    from sqlalchemy import delete

    delivery = _delivery()
    key, job_id = _accepted(client, seeded)
    result = _ready_result(job_id)
    delivery.run_claimed(str(job_id), lambda: delivery.stage_result(str(job_id), result))
    poison = uuid.uuid4()
    with sync_session() as db:
        original = db.get(SlidePostGenerationReceipt, key)
        values = {
            column.name: getattr(original, column.name) for column in original.__table__.columns
        }
        values.update(
            client_request_id=poison,
            job_id=uuid.uuid4(),
            claimed_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.add(SlidePostGenerationReceipt(**values))
        db.commit()
    original_reconcile = delivery.reconcile_claimed

    def one_broken(candidate, **kwargs):
        if candidate == poison:
            raise RuntimeError("one row unavailable")
        return original_reconcile(candidate, **kwargs)

    monkeypatch.setattr(delivery, "reconcile_claimed", one_broken)
    try:
        delivery.reconcile_generations()
        with sync_session() as db:
            assert db.get(SlidePostGenerationReceipt, key).execution_state == "completed"
    finally:
        with sync_session() as db:
            db.execute(
                delete(SlidePostGenerationReceipt).where(
                    SlidePostGenerationReceipt.client_request_id == poison
                )
            )
            db.commit()


def test_flag_disabled_after_admission_never_falls_back_to_legacy_publish(
    client, seeded, publishes, monkeypatch
):
    from app.tasks import content_plan_build

    original = content_plan_build.capture_receipt

    def disable_after_capture(*args, **kwargs):
        receipt = original(*args, **kwargs)
        monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", False)
        return receipt

    monkeypatch.setattr(content_plan_build, "capture_receipt", disable_after_capture)
    key, job_id = _accepted(client, seeded)
    assert publishes == []
    with sync_session() as db:
        receipt = db.get(SlidePostGenerationReceipt, key)
        assert receipt.publish_attempts == 0
        assert receipt.execution_state == "pending"
        assert db.get(Job, job_id).status == "queued"
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    assert _delivery().publish_pending(key) == "published"
    assert publishes == [str(job_id)]


def test_initial_send_reserves_budget_before_io_and_blocks_scanner_race(
    client, seeded, publishes, monkeypatch
):
    delivery = _delivery()
    observed = []

    def inspect_reserved(_task, job_id, **_kwargs):
        with sync_session() as db:
            receipt = db.scalar(
                select(SlidePostGenerationReceipt).where(
                    SlidePostGenerationReceipt.job_id == uuid.UUID(job_id)
                )
            )
            assert receipt.publish_attempts == 1
            assert receipt.next_publish_at > datetime.now(UTC)
            key = receipt.client_request_id
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(delivery.publish_pending, key).result(timeout=10) == "waiting"
        observed.append(job_id)

    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", inspect_reserved)
    _key, job_id = _accepted(client, seeded)
    assert observed == [str(job_id)]


def test_process_loss_before_first_send_spends_reserved_attempt_safely(
    client, seeded, publishes, monkeypatch
):
    from app.models import ContentPlan
    from app.services.slide_post_generation_receipts import (
        SlidePostGenerationRequest,
        dispatch_generation,
    )

    delivery = _delivery()
    key = uuid.uuid4()

    class ProcessDied(BaseException):
        pass

    def stop_before_send(*_args, **_kwargs):
        raise ProcessDied()

    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        epoch = db.get(ContentPlan, item.content_plan_id).ownership_epoch
    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", stop_before_send)
    with pytest.raises(ProcessDied):
        dispatch_generation(
            SlidePostGenerationRequest(key, seeded.user_id, seeded.item_id, 1), epoch
        )
    with sync_session() as db:
        receipt = db.get(SlidePostGenerationReceipt, key)
        job_id = receipt.job_id
        assert receipt.publish_attempts == 1
        deadline = receipt.next_publish_at
    assert delivery.publish_pending(key) == "waiting"
    sent = []

    def unknown_send(_task, published_id, **_kwargs):
        sent.append(published_id)
        raise RuntimeError("unknown broker outcome")

    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", unknown_send)
    for attempt in range(4):
        assert (
            delivery.publish_pending(key, now=deadline + timedelta(minutes=10 * attempt))
            == "unconfirmed"
        )
    assert delivery.publish_pending(key, now=deadline + timedelta(hours=2)) == "exhausted"
    assert sent == [str(job_id)] * 4
    with sync_session() as db:
        assert db.get(SlidePostGenerationReceipt, key).publish_attempts == 5


def test_saved_newer_draft_before_first_variant_gets_one_fenced_catchup(
    client, seeded, publishes, monkeypatch
):
    from app.tasks import generative_build
    from tests.routes.test_slide_post_binding_save_postgres import _save_body, _stored

    delivery = _delivery()
    monkeypatch.setattr(settings, "cloud_render_execution_enabled", True)
    monkeypatch.setattr(settings, "ios_native_device_only_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    key, job_id = _accepted(client, seeded)
    current = _stored(seeded)
    saved = client.put(
        f"/plan-items/{seeded.item_id}/slide-post",
        json=_save_body(current, expected_version=1, caption="Saved while first render queued"),
    )
    assert saved.status_code == 200, saved.text
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).slide_post["version"] == 2
        assert not (db.get(Job, job_id).assembly_plan or {}).get("variants")
    rebuilt = []
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant,
        "delay",
        lambda *a, **kw: (
            rebuilt.append((a, kw))
            if (a and a[0] == str(job_id)) or kw.get("args") == [str(job_id)]
            else None
        ),
    )
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant,
        "apply_async",
        lambda *a, **kw: (
            rebuilt.append((a, kw))
            if (a and a[0] == str(job_id)) or kw.get("args") == [str(job_id)]
            else None
        ),
    )
    result = {**_ready_result(job_id), "resolved_archetype": "slides"}
    assert delivery.run_claimed(str(job_id), lambda: delivery.stage_result(str(job_id), result))
    assert delivery.reconcile_claimed(key) == "completed"
    delivery.reconcile_generations()
    delivery.reconcile_generations()
    with sync_session() as db:
        receipt = db.get(SlidePostGenerationReceipt, key)
        draft = db.get(PlanItem, seeded.item_id).slide_post
        assert receipt.draft_version == 1
        assert draft["version"] == 2 and draft.get("rendered_version") != 2
    assert len(rebuilt) == 1, (
        "Saved v2 must receive a fenced rebuild, not remain unrendered forever"
    )
    assert publishes == [str(job_id)], "Catchup must not repeat the first generation"


def _save_revision(client, seeded, *, version, caption):
    from tests.routes.test_slide_post_binding_save_postgres import _save_body, _stored

    response = client.put(
        f"/plan-items/{seeded.item_id}/slide-post",
        json=_save_body(_stored(seeded), expected_version=version, caption=caption),
    )
    assert response.status_code == 200, response.text
    from app.models import SlidePostRenderRevision

    with sync_session() as db:
        return db.scalar(
            select(SlidePostRenderRevision.id).where(
                SlidePostRenderRevision.item_id == seeded.item_id,
                SlidePostRenderRevision.draft_version == version + 1,
            )
        )


def test_failed_parent_settles_saved_revision_without_repeating_first_intent(
    client, seeded, publishes
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions

    key, job_id = _accepted(client, seeded)
    revision = _save_revision(client, seeded, version=1, caption="v2")
    assert revision is not None
    _delivery().run_claimed(str(job_id), lambda: None)
    _delivery().reconcile_claimed(key, now=datetime.now(UTC) + timedelta(hours=2))
    revisions.reconcile_revisions()
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, revision).state == "failed"
    assert publishes == [str(job_id)]


def test_old_revision_timeout_cannot_fail_newer_revision_or_global_job(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    monkeypatch.setattr(settings, "cloud_render_execution_enabled", True)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    key, job_id = _accepted(client, seeded)
    second = _save_revision(client, seeded, version=1, caption="v2")
    result = {**_ready_result(job_id), "resolved_archetype": "slides"}
    _delivery().run_claimed(str(job_id), lambda: _delivery().stage_result(str(job_id), result))
    _delivery().reconcile_claimed(key)
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant, "apply_async", lambda **_: None
    )
    revisions.observe_revision(second)
    assert revisions.claim_revision(second)
    third = _save_revision(client, seeded, version=2, caption="v3")
    revisions.observe_revision(second, now=datetime.now(UTC) + timedelta(hours=2))
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, second).state == "superseded"
        assert db.get(SlidePostRenderRevision, third).state == "pending"
        assert db.get(Job, job_id).status == "variants_ready"
        assert db.get(PlanItem, seeded.item_id).slide_post["version"] == 3


def _ready_revision(client, seeded, monkeypatch):
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    monkeypatch.setattr(settings, "cloud_render_execution_enabled", True)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    key, job_id = _accepted(client, seeded)
    revision = _save_revision(client, seeded, version=1, caption="v2")
    result = {**_ready_result(job_id), "resolved_archetype": "slides"}
    _delivery().run_claimed(str(job_id), lambda: _delivery().stage_result(str(job_id), result))
    _delivery().reconcile_claimed(key)
    sent = []
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant,
        "apply_async",
        lambda **kw: sent.append(kw) if kw.get("args") == [str(job_id)] else None,
    )
    assert revisions.observe_revision(revision) == "published"
    return key, job_id, revision, sent


def _revision_result(job_id, revision):
    from app.services.slide_post_render_revisions import artifact_prefix

    prefix = artifact_prefix(str(job_id), str(revision))
    return {
        **_ready_result(job_id),
        "render_generation_id": str(revision),
        "video_path": prefix + "/preview.mp4",
        "poster_path": prefix + "/cover.jpg",
        "slide_post": {"bundle_gcs_path": prefix + "/bundle.zip"},
        "resolved_archetype": "slides",
    }


def test_stop_settles_revision_and_save_cannot_revive_cancelled_job(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions

    _key, job_id, second, sent = _ready_revision(client, seeded, monkeypatch)
    assert revisions.claim_revision(second)
    with sync_session() as db:
        db.get(Job, job_id).status = "cancelled"
        db.commit()
    assert not revisions.stage_result(second, _revision_result(job_id, second))
    third = _save_revision(client, seeded, version=2, caption="saved after Stop")
    revisions.reconcile_revisions()
    with sync_session() as db:
        assert db.get(Job, job_id).status == "cancelled"
        assert db.get(SlidePostRenderRevision, second).state == "failed"
        assert db.get(SlidePostRenderRevision, third).state == "failed"
    assert len(sent) == 1


def test_queued_v2_superseded_by_v3_does_not_consume_latest_render(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions

    _key, job_id, second, sent = _ready_revision(client, seeded, monkeypatch)
    third = _save_revision(client, seeded, version=2, caption="v3")
    revisions.reconcile_revisions()
    assert not revisions.claim_revision(second)
    assert revisions.claim_revision(third)
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, second).state == "superseded"
        assert db.get(SlidePostRenderRevision, third).state == "running"
    assert len(sent) == 2
    assert revisions.stage_result(third, _revision_result(job_id, third))
    assert revisions.publish_result(third)
    assert not revisions.stage_result(second, _revision_result(job_id, second))
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).slide_post["rendered_version"] == 3
        assert db.get(Job, job_id).status == "variants_ready"


def test_revision_worker_claim_is_exclusive_and_binding_is_checked(
    client, seeded, publishes, monkeypatch
):
    from app.services import slide_post_render_revisions as revisions

    _key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    assert not revisions.claim_revision(revision, uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: revisions.claim_revision(revision, job_id), range(2)))
    assert sorted(results) == [False, True]
    assert revisions.stage_result(revision, _revision_result(job_id, revision))
    assert revisions.publish_result(revision)
    assert not revisions.claim_revision(revision, job_id)


def test_save_after_first_upsert_does_not_start_legacy_render_before_finalize(
    client, seeded, publishes, monkeypatch
):
    from app.tasks import generative_build

    key, job_id = _accepted(client, seeded)
    result = {**_ready_result(job_id), "resolved_archetype": "slides"}
    legacy = []
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant,
        "delay",
        lambda *a, **kw: legacy.append((a, kw)),
    )

    def first_body():
        assert _delivery().stage_result(str(job_id), result)
        assert generative_build._upsert_variant_entry(str(job_id), result)
        assert _save_revision(client, seeded, version=1, caption="v2 after upsert") is not None
        assert legacy == []

    _delivery().run_claimed(str(job_id), first_body)
    assert _delivery().reconcile_claimed(key) == "completed"
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).slide_post.get("rendered_version") != 2


def test_real_revision_task_uses_exact_snapshot_and_publishes_once(
    client, seeded, publishes, monkeypatch
):
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    _key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    computed = []

    def render(received_job, **kwargs):
        assert received_job == str(job_id)
        assert kwargs["caption"] == "v2"
        assert kwargs["artifact_generation"] == str(revision)
        assert kwargs["strict_inputs"] is True
        computed.append(kwargs)
        return _revision_result(job_id, revision)

    monkeypatch.setattr(generative_build, "_build_slide_post_result", render)
    for _ in range(2):
        generative_build.rebuild_slide_post_variant.run(
            str(job_id), render_gen_id=str(revision), revision_id=str(revision)
        )
    assert len(computed) == 1
    assert not revisions.claim_revision(revision)
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).slide_post["rendered_version"] == 2
        assert (
            db.get(Job, job_id).assembly_plan["variants"][0]["video_path"]
            == _revision_result(job_id, revision)["video_path"]
        )


def test_late_revision_task_cannot_publish_after_newer_save(client, seeded, publishes, monkeypatch):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    _key, job_id, second, _sent = _ready_revision(client, seeded, monkeypatch)
    newer = []

    def delayed_render(_job, **_kwargs):
        newer.append(_save_revision(client, seeded, version=2, caption="v3"))
        return _revision_result(job_id, second)

    monkeypatch.setattr(generative_build, "_build_slide_post_result", delayed_render)
    generative_build.rebuild_slide_post_variant.run(
        str(job_id), render_gen_id=str(second), revision_id=str(second)
    )
    assert not revisions.publish_result(second)
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        assert item.slide_post["version"] == 3 and item.slide_post.get("rendered_version") != 2
        assert db.get(SlidePostRenderRevision, second).state == "superseded"
        assert db.get(SlidePostRenderRevision, newer[0]).state == "pending"


def test_blocked_render_policy_keeps_valid_save_and_exposes_reason(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision

    _key, job_id = _accepted(client, seeded)
    monkeypatch.setattr(settings, "cloud_render_execution_enabled", False)
    revision = _save_revision(client, seeded, version=1, caption="still saved")
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).slide_post["caption"] == "still saved"
        assert db.get(SlidePostRenderRevision, revision).state == "blocked"
        assert db.get(Job, job_id).status == "queued"
    state = client.get(f"/plan-items/{seeded.item_id}/slide-post")
    assert state.status_code == 200
    assert any(e["code"] == "slide_post_render_blocked" for e in state.json()["validation_errors"])


def test_device_owned_revision_never_enters_cloud_builder(client, seeded, publishes, monkeypatch):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    _key, job_id = _accepted(client, seeded)
    with sync_session() as db:
        job = db.get(Job, job_id)
        job.assembly_plan = {**job.assembly_plan, "_device_render_v1": {"version": 1}}
        db.commit()
    monkeypatch.setattr(settings, "cloud_render_execution_enabled", False)
    revision = _save_revision(client, seeded, version=1, caption="phone edit")
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, revision).state == "blocked"
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant,
        "apply_async",
        lambda **_: pytest.fail("device revision published to cloud"),
    )
    monkeypatch.setattr(
        generative_build,
        "_build_slide_post_result",
        lambda *a, **kw: pytest.fail("device revision rendered on cloud"),
    )
    revisions.observe_revision(revision)
    generative_build.rebuild_slide_post_variant.run(
        str(job_id), render_gen_id=str(revision), revision_id=str(revision)
    )


def test_revision_queue_deadline_starts_at_publication_not_save(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    monkeypatch.setattr(settings, "cloud_render_execution_enabled", True)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    key, job_id = _accepted(client, seeded)
    revision = _save_revision(client, seeded, version=1, caption="saved early")
    now = datetime.now(UTC)
    with sync_session() as db:
        db.get(SlidePostRenderRevision, revision).created_at = now - timedelta(minutes=34)
        db.commit()
    result = {**_ready_result(job_id), "resolved_archetype": "slides"}
    _delivery().run_claimed(str(job_id), lambda: _delivery().stage_result(str(job_id), result))
    _delivery().reconcile_claimed(key)
    monkeypatch.setattr(
        generative_build.rebuild_slide_post_variant, "apply_async", lambda **_: None
    )
    assert revisions.observe_revision(revision, now=now) == "published"
    assert revisions.observe_revision(revision, now=now + timedelta(minutes=2)) == "waiting"
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, revision).state == "pending"
    assert revisions.observe_revision(revision, now=now + timedelta(minutes=36)) == "failed"


def test_revision_publication_rechecks_contract_without_inheriting_old_evidence(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions
    from app.services.creator_render_contract import CONTRACT_FIELD, build_render_contract

    _key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    assert revisions.claim_revision(revision)
    contract = build_render_contract({"audio_strategy": "voiceover"}, generation_id=str(revision))
    assert contract is not None
    with sync_session() as db:
        job = db.get(Job, job_id)
        old = dict(job.assembly_plan["variants"][0])
        old["cloud_evidence"] = {"narration_applied": True}
        job.assembly_plan = {
            **job.assembly_plan,
            CONTRACT_FIELD: contract.model_dump(mode="json"),
            "variants": [old],
        }
        previous_path = old["video_path"]
        db.commit()
    assert revisions.stage_result(revision, _revision_result(job_id, revision))
    assert not revisions.publish_result(revision)
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, revision).state == "failed"
        assert db.get(PlanItem, seeded.item_id).slide_post.get("rendered_version") != 2
        variant = db.get(Job, job_id).assembly_plan["variants"][0]
        assert variant["video_path"] == previous_path
        assert variant["error_class"] == "creator_render_contract_unverified"


def test_stop_projection_is_terminal_before_revision_scanner_settlement(
    client, seeded, publishes, monkeypatch
):
    _key, job_id, _revision, _sent = _ready_revision(client, seeded, monkeypatch)
    with sync_session() as db:
        db.get(Job, job_id).status = "cancelled"
        db.commit()
    response = client.get(f"/plan-items/{seeded.item_id}/slide-post")
    assert response.status_code == 200
    assert response.json()["render_status"] == "failed"


@pytest.mark.parametrize("persist_result", [False, True], ids=["claimed", "staged"])
@pytest.mark.timeout(60)
def test_killed_revision_process_settles_without_recompute(
    client, seeded, publishes, monkeypatch, persist_result
):
    import json
    import os
    import selectors
    import signal
    import subprocess
    import sys

    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions

    _key, job_id, revision, sent = _ready_revision(client, seeded, monkeypatch)
    result = _revision_result(job_id, revision) if persist_result else None
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_URL": settings.database_url,
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "STORAGE_BUCKET": "offline",
        "OPENAI_API_KEY": "test",
        "GEMINI_API_KEY": "test",
        "ANTHROPIC_API_KEY": "test",
        "TOKEN_ENCRYPTION_KEY": "test",
        "WAITLIST_ADMIN_SECRET": "test",
        "INTERNAL_API_KEY": "test",
        "CLOUD_RENDER_EXECUTION_ENABLED": "true",
        "IOS_DEVICE_ONLY_MODE": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.slide_post_revision_kill_worker",
            json.dumps([str(revision), result]),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "Child infrastructure did not report commit"
            assert child.stdout.readline().strip() == "COMMITTED"
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        assert not revisions.claim_revision(revision)
        revisions.observe_revision(revision, now=datetime.now(UTC) + timedelta(hours=2))
        assert not revisions.claim_revision(revision)
        with sync_session() as db:
            row = db.get(SlidePostRenderRevision, revision)
            assert row.state == ("completed" if persist_result else "failed")
            assert row.job_id == job_id
            assert db.get(Job, job_id).status == "variants_ready"
            draft = db.get(PlanItem, seeded.item_id).slide_post
            assert (draft.get("rendered_version") == 2) is persist_result
        assert len(sent) == 1 and len(publishes) == 1
    finally:
        if child.poll() is None:
            child.kill()
        _out, error = child.communicate(timeout=10)
        assert not error, error


def test_first_result_recovery_preserves_already_newer_variant_generation(
    client, seeded, publishes
):
    from app.tasks import generative_build

    key, job_id = _accepted(client, seeded)
    result = {**_ready_result(job_id), "resolved_archetype": "slides"}
    successor = uuid.uuid4().hex

    def first_body():
        assert _delivery().stage_result(str(job_id), result)
        assert generative_build._upsert_variant_entry(str(job_id), result)
        with sync_session() as db:
            job = db.get(Job, job_id)
            variant = {
                **job.assembly_plan["variants"][0],
                "render_generation_id": successor,
                "render_status": "rendering",
            }
            job.assembly_plan = {**job.assembly_plan, "variants": [variant]}
            item = db.get(PlanItem, seeded.item_id)
            item.slide_post = {**item.slide_post, "version": 2}
            db.commit()
        assert not generative_build._upsert_variant_entry(str(job_id), result)

    _delivery().run_claimed(str(job_id), first_body)
    assert _delivery().reconcile_claimed(key) == "completed"
    with sync_session() as db:
        variant = db.get(Job, job_id).assembly_plan["variants"][0]
        assert variant["render_generation_id"] == successor
        assert variant["render_status"] == "rendering"
        assert db.get(PlanItem, seeded.item_id).slide_post.get("rendered_version") != 2


def test_waiting_parents_do_not_consume_revision_completion_budget(
    client, seeded, publishes, monkeypatch
):
    from sqlalchemy import delete, update

    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions

    key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    assert revisions.claim_revision(revision)
    assert revisions.stage_result(revision, _revision_result(job_id, revision))
    item_ids, job_ids, waiting_ids = [], [], []
    try:
        with sync_session() as db:
            parent = db.get(SlidePostGenerationReceipt, key)
            original = db.get(SlidePostRenderRevision, revision)
            source_item = db.get(PlanItem, seeded.item_id)
            for index in range(51):
                item_id, waiting_job, parent_id, waiting_id = [uuid.uuid4() for _ in range(4)]
                item_ids.append(item_id)
                job_ids.append(waiting_job)
                waiting_ids.append(waiting_id)
                item = PlanItem(
                    id=item_id,
                    content_plan_id=source_item.content_plan_id,
                    position=1000 + index,
                    idea="waiting",
                    slide_post={**source_item.slide_post},
                )
                db.add(item)
                db.flush()
                db.add(
                    Job(
                        id=waiting_job,
                        user_id=seeded.user_id,
                        content_plan_item_id=item_id,
                        content_plan_ownership_epoch=parent.ownership_epoch,
                        status="processing",
                        raw_storage_path="synthetic/waiting",
                        selected_platforms=[],
                    )
                )
                db.flush()
                item.current_job_id = waiting_job
                parent_values = {c.name: getattr(parent, c.name) for c in parent.__table__.columns}
                parent_values.update(
                    client_request_id=parent_id,
                    job_id=waiting_job,
                    item_id=item_id,
                    execution_state="running",
                )
                db.add(SlidePostGenerationReceipt(**parent_values))
                db.flush()
                values = {c.name: getattr(original, c.name) for c in original.__table__.columns}
                values.update(
                    id=waiting_id,
                    parent_request_id=parent_id,
                    job_id=waiting_job,
                    item_id=item_id,
                    state="pending",
                    staged_result=None,
                    next_observe_at=None,
                    created_at=datetime.now(UTC) - timedelta(minutes=5),
                )
                db.add(SlidePostRenderRevision(**values))
            db.commit()
        seen = []
        observe = revisions.observe_revision

        def record(identifier, **kwargs):
            seen.append(identifier)
            return observe(identifier, **kwargs)

        monkeypatch.setattr(revisions, "observe_revision", record)
        revisions.reconcile_revisions()
        assert not set(waiting_ids).intersection(seen)
        with sync_session() as db:
            assert db.get(SlidePostRenderRevision, revision).state == "completed"
    finally:
        with sync_session() as db:
            db.execute(
                update(PlanItem).where(PlanItem.id.in_(item_ids)).values(current_job_id=None)
            )
            db.execute(delete(Job).where(Job.id.in_(job_ids)))
            db.execute(delete(PlanItem).where(PlanItem.id.in_(item_ids)))
            db.commit()


def test_revision_unknown_dispatch_and_staged_failure_have_separate_finite_budgets(
    client, seeded, publishes, monkeypatch
):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions
    from app.tasks import generative_build

    _key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    with sync_session() as db:
        row = db.get(SlidePostRenderRevision, revision)
        row.published = False
        row.next_observe_at = None
        db.commit()
    sends = []

    def unknown(**_kwargs):
        sends.append(revision)
        raise RuntimeError("ack unknown")

    monkeypatch.setattr(generative_build.rebuild_slide_post_variant, "apply_async", unknown)
    now = datetime.now(UTC)
    for attempt in range(4):
        assert (
            revisions.observe_revision(revision, now=now + timedelta(minutes=10 * attempt))
            == "unconfirmed"
        )
    assert revisions.observe_revision(revision, now=now + timedelta(hours=2)) == "failed"
    assert len(sends) == 4
    assert not revisions.claim_revision(revision)
    with sync_session() as db:
        assert db.get(Job, job_id).status == "variants_ready"
        assert db.get(SlidePostRenderRevision, revision).error == "revision_dispatch_unconfirmed"


def test_revision_staged_publication_failure_is_bounded(client, seeded, publishes, monkeypatch):
    from app.models import SlidePostRenderRevision
    from app.services import slide_post_render_revisions as revisions

    _key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    assert revisions.claim_revision(revision)
    assert revisions.stage_result(revision, _revision_result(job_id, revision))
    calls = []

    def unavailable(_identifier):
        calls.append(revision)
        raise RuntimeError("publication temporarily unavailable")

    monkeypatch.setattr(revisions, "publish_result", unavailable)
    now = datetime.now(UTC)
    for attempt in range(5):
        assert (
            revisions.observe_revision(revision, now=now + timedelta(minutes=10 * attempt))
            == "retry"
        )
    assert revisions.observe_revision(revision, now=now + timedelta(hours=2)) == "failed"
    assert len(calls) == 5
    with sync_session() as db:
        assert db.get(SlidePostRenderRevision, revision).error == "revision_publication_failed"
        assert db.get(Job, job_id).status == "variants_ready"
        assert db.get(PlanItem, seeded.item_id).slide_post.get("rendered_version") != 2


def test_revision_commit_then_response_loss_observes_completed_receipt(
    client, seeded, publishes, monkeypatch
):
    from app.services import slide_post_render_revisions as revisions

    _key, job_id, revision, _sent = _ready_revision(client, seeded, monkeypatch)
    assert revisions.claim_revision(revision)
    assert revisions.stage_result(revision, _revision_result(job_id, revision))
    publish = revisions.publish_result
    calls = []

    def committed_then_lost(identifier):
        calls.append(identifier)
        assert publish(identifier)
        raise RuntimeError("response lost after SQL commit")

    monkeypatch.setattr(revisions, "publish_result", committed_then_lost)
    assert revisions.observe_revision(revision) == "retry"
    assert (
        revisions.observe_revision(revision, now=datetime.now(UTC) + timedelta(hours=2)) == "skip"
    )
    assert calls == [revision]
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).slide_post["rendered_version"] == 2
