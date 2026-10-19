"""A retry must take the media-owner advisory lock before any project row."""

# ruff: noqa: F811 -- shared DB fixture
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select, text

from app.database import sync_session
from app.kria.render_retry import claim_render_retry
from app.models import Job
from app.services.job_storage_paths import project_media_reference_lock_key
from tests.kria.test_render_retry_postgres import client, recover, seed_failed  # noqa: F401


def test_phone_poll_can_lock_the_job_while_retry_waits_for_owner(client):
    http = client[0]
    case = seed_failed()
    accepted = recover(http, case).json()
    approval_id = accepted["approval_id"]
    snapshot = http.get(f"/creation-threads/{case.thread_id}/approvals/{approval_id}").json()
    decision = http.post(
        f"/creation-threads/{case.thread_id}/approvals/{approval_id}/approve",
        json={
            "expected_thread_revision": accepted["thread_revision"],
            "expected_draft_revision": 1,
            "expected_approval_fingerprint": snapshot["approval_fingerprint"],
        },
    )
    assert decision.status_code == 200, decision.text
    with (
        sync_session() as poll,
        sync_session() as monitor,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        poll.execute(
            select(func.pg_advisory_xact_lock(project_media_reference_lock_key(case.user_id)))
        )
        claim = pool.submit(claim_render_retry, uuid.UUID(approval_id))
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = monitor.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event='advisory'"
                    )
                ).scalar_one()
                monitor.rollback()  # pg_stat_activity snapshots are transaction-scoped
                if waiting:
                    break
                monitor.execute(text("SELECT pg_sleep(0.02)"))
                monitor.rollback()
            assert waiting, "Retry did not reach the owner-lock fence"
            poll.execute(text("SET LOCAL lock_timeout = '1s'"))
            # Before the fix, retry held this row while waiting for our owner
            # lock: the same deadlock observed during the physical iPhone flow.
            assert poll.execute(
                select(Job).where(Job.id == case.job_id).with_for_update()
            ).scalar_one()
        finally:
            poll.rollback()
        assert claim.result(timeout=10)[0] is True
