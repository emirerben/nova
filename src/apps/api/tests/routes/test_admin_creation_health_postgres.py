"""GET /admin/kria/creation-health: the counting rules, against real Postgres rows.

Failure modes the numbers must survive (each has an assertion):

* a turn or job outside the window leaks in (the day before the first day, the next day's
  midnight) or one on the boundary seconds goes missing (UTC day edges);
* a superseded or cancelled turn, or a cancelled job, counts as an attempt;
* a turn or job still running counts as a failure, or drags the rate down;
* a refused turn (completed, with an outcome code) counts as a success;
* `awaiting_approval` (a draft was offered) counts as a failure;
* a failed turn with no `error.code`, or a failed job with no `failure_reason`, vanishes instead
  of showing up as an unknown reason;
* `posting_failed` (the render finished, publishing failed) counts as a failed creation, and
  `posting_partial` / `clips_ready_partial` stay in progress forever instead of a success;
* an iPhone render that failed (`awaiting_device` + `failure_reason`, the only trace the phone, the
  reaper and export verification leave) reads as still waiting, so phone failures never show;
* a Job an approval minted and then compensated (`status="failed"`, not a JobStatus member) is
  in progress forever and its `kria_dispatch_aborted` reason never shows;
* an admin test render (the synthetic admin user) counts as a creation attempt;
* a `draft` job, or a turn that ended with the `superseded` outcome, counts as an attempt;
* the failure share pools turn and render failures into one denominator;
* a day with no attempts is missing from the series or reports a 0% rate instead of null;
* turns and renders are added together (one creation counted twice);
* the render reason label comes from a second table that can drift from the sentence the client
  shows;
* a failed iPhone render stops being counted once it is retried (the retry reuses the Job and
  clears `failure_reason`), so a phone failure leaves no evidence; or the same failure is logged
  twice, or a cancelled or admin test Job's retried failure counts, or a malformed log entry
  crashes the report;
* the top-reasons list is one shared top N, so a long tail of `failed` codes pushes every refusal
  off it, the very rows the ticket is for;
* the endpoint is readable without the admin token, or accepts a window it cannot afford.

Days are far in the past and unique per test, so rows other tests or earlier runs left on the
shared database never enter an assertion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.auth import SYNTHETIC_USER_ID
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.main import app
from app.models import CreatorAgentTurn, Job, User
from app.services.creation_health import MAX_DAYS, TOP_REASONS, creation_health
from app.services.device_render import apply_retry_variant_reset
from app.services.job_failure_reasons import JOB_FAILURE_REASONS
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _seed_user_turn

TOKEN = "test-admin-token"


@pytest.fixture
def project():
    user_id, thread_id, session_id = _seed_runtime_project()
    return user_id, thread_id, session_id


def _day() -> datetime:
    return datetime(1980, 1, 1, tzinfo=UTC) + timedelta(days=uuid.uuid4().int % 9000)


def _turn(project, at, status, *, plan=None, error=None):
    _user_id, thread_id, session_id = project
    turn_id = _seed_user_turn(thread_id, session_id, content="x", status=status, created_at=at)
    if plan is not None or error is not None:
        with sync_session() as db:
            db.execute(
                update(CreatorAgentTurn)
                .where(CreatorAgentTurn.id == turn_id)
                .values(plan_json=plan, error=error)
            )
            db.commit()


def _job(project, at, status, reason=None, user_id=None):
    with sync_session() as db:
        if user_id == SYNTHETIC_USER_ID and db.get(User, user_id) is None:
            db.add(User(id=user_id, email="synthetic@test.local"))
            db.flush()
        db.add(
            Job(
                user_id=user_id or project[0],
                status=status,
                job_type="default",
                raw_storage_path="raw/test.mp4",
                failure_reason=reason,
                created_at=at,
            )
        )
        db.commit()


def _outcome(kind, code):
    return {"diagnostics": {"outcome": {"kind": kind, "code": code}}}


async def _report(day, days):
    async with AsyncSessionLocal() as db:
        return await creation_health(db, days=days, now=day + timedelta(hours=12))


@pytest.mark.asyncio
async def test_turn_counts_follow_the_stated_definition(project):
    day = _day()
    midnight = day.replace(hour=0, minute=0, second=0)
    for status, kwargs in (
        ("completed", {}),
        ("awaiting_approval", {}),
        ("completed", {"plan": _outcome("refused", "title_unavailable")}),
        ("failed", {"error": {"code": "runtime_provider_outcome_unknown"}}),
        ("failed", {}),
        ("superseded", {}),
        ("cancelled", {}),
        ("planning", {}),
        ("completed", {"plan": _outcome("superseded", "continuation_superseded")}),
    ):
        _turn(project, midnight + timedelta(seconds=1), status, **kwargs)
    _turn(project, midnight - timedelta(seconds=1), "completed")
    _turn(project, midnight + timedelta(days=1), "completed")

    report = await _report(day, 1)

    assert report["turns"] == {
        "attempts": 5,
        "successes": 2,
        "success_rate": 0.4,
        "in_progress": 1,
        "failed": 2,
        "refused": 1,
    }
    reasons = {
        (r["source"], r["kind"], r["code"]): r["count"] for r in report["top_failure_reasons"]
    }
    assert reasons == {
        ("turn", "refused", "title_unavailable"): 1,
        ("turn", "failed", "runtime_provider_outcome_unknown"): 1,
        ("turn", "failed", "unknown_failure"): 1,
    }
    assert report["renders"]["attempts"] == 0


@pytest.mark.asyncio
async def test_render_counts_follow_the_stated_definition_and_labels_come_from_the_one_table(
    project,
):
    day = _day()
    at = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=1)
    _job(project, at, "variants_ready")
    _job(project, at, "posting_failed")
    _job(project, at, "processing_failed", "ffmpeg_failed")
    _job(project, at, "processing_failed", "ffmpeg_failed")
    _job(project, at, "processing_failed")
    _job(project, at, "cancelled")
    _job(project, at, "draft")
    _job(project, at, "processing")

    report = await _report(day, 1)

    assert report["renders"] == {
        "attempts": 5,
        "successes": 2,
        "success_rate": 0.4,
        "in_progress": 1,
        "failed": 3,
    }
    assert report["turns"]["attempts"] == 0
    top = report["top_failure_reasons"]
    assert [(r["code"], r["count"]) for r in top] == [
        ("ffmpeg_failed", 2),
        ("no_reason_processing_failed", 1),
    ]
    assert top[0]["label"] == JOB_FAILURE_REASONS["ffmpeg_failed"][0]
    assert top[0]["share"] == round(2 / 3, 4)
    assert top[1]["label"] is None


@pytest.mark.asyncio
async def test_phone_render_failures_and_finished_partials_are_classified(project):
    day = _day()
    at = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=1)
    _job(project, at, "awaiting_device", "device_render_stale")
    _job(project, at, "awaiting_device", "device_render_stale")
    _job(project, at, "awaiting_device")
    _job(project, at, "posting_partial")
    _job(project, at, "clips_ready_partial")

    report = await _report(day, 1)

    assert report["renders"] == {
        "attempts": 4,
        "successes": 2,
        "success_rate": 0.5,
        "in_progress": 1,
        "failed": 2,
    }
    assert [(r["source"], r["code"], r["count"]) for r in report["top_failure_reasons"]] == [
        ("render", "device_render_stale", 2)
    ]


@pytest.mark.asyncio
async def test_a_compensated_dispatch_is_a_failed_render_and_admin_test_renders_are_not_attempts(
    project,
):
    day = _day()
    at = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=1)
    _job(project, at, "failed", "kria_dispatch_aborted")
    _job(project, at, "variants_ready")
    _job(project, at, "variants_ready", user_id=SYNTHETIC_USER_ID)
    _job(project, at, "processing_failed", "ffmpeg_failed", user_id=SYNTHETIC_USER_ID)

    report = await _report(day, 1)

    assert report["renders"] == {
        "attempts": 2,
        "successes": 1,
        "success_rate": 0.5,
        "in_progress": 0,
        "failed": 1,
    }
    assert [(r["code"], r["count"]) for r in report["top_failure_reasons"]] == [
        ("kria_dispatch_aborted", 1)
    ]


def _failed_phone_job(project, at, status="awaiting_device", user_id=None):
    _job(project, at, status, "device_export_failed", user_id=user_id)
    with sync_session() as db:
        return (
            db.execute(
                select(Job.id).where(Job.created_at == at, Job.user_id == (user_id or project[0]))
            )
            .scalars()
            .all()[-1]
        )


def _retry_phone_render(job_id, at, *, retries=1, log=None):
    with sync_session() as db:
        job = db.get(Job, job_id)
        for _ in range(retries):
            apply_retry_variant_reset(job, "v1")
        plan = dict(job.assembly_plan)
        plan["cleared_failures"] = [
            {**entry, "at": at.isoformat()} for entry in plan["cleared_failures"]
        ] + (log or [])
        job.assembly_plan = plan
        db.commit()


@pytest.mark.asyncio
async def test_a_retried_phone_failure_stays_a_failed_attempt(project):
    day = _day()
    at = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=1)
    retried_ready = _failed_phone_job(project, at + timedelta(minutes=1))
    retried_waiting = _failed_phone_job(project, at + timedelta(minutes=2))
    cancelled = _failed_phone_job(project, at + timedelta(minutes=3))
    admin_test = _failed_phone_job(project, at + timedelta(minutes=4), user_id=SYNTHETIC_USER_ID)
    _failed_phone_job(project, at + timedelta(minutes=5))
    for job_id in (retried_ready, retried_waiting, cancelled, admin_test):
        _retry_phone_render(job_id, at + timedelta(hours=1), retries=2)
    _retry_phone_render(
        retried_waiting,
        at,
        log=[{"reason": "device_render_stale", "at": "not-a-time"}, "junk", {"at": 3}],
    )
    with sync_session() as db:
        db.execute(update(Job).where(Job.id == retried_ready).values(status="variants_ready"))
        db.execute(update(Job).where(Job.id == cancelled).values(status="cancelled"))
        db.commit()

    report = await _report(day, 1)

    assert report["renders"] == {
        "attempts": 4,
        "successes": 1,
        "success_rate": 0.25,
        "in_progress": 1,
        "failed": 3,
    }
    assert [(r["code"], r["count"]) for r in report["top_failure_reasons"]] == [
        ("device_export_failed", 3)
    ]
    assert report["daily"][0]["renders"]["failed"] == 3


@pytest.mark.asyncio
async def test_each_kind_keeps_its_own_top_reasons(project):
    day = _day()
    at = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=1)
    for n in range(TOP_REASONS + 2):
        for _ in range(2):
            _turn(project, at, "failed", error={"code": f"runtime_failure_{n:02d}"})
    _turn(project, at, "completed", plan=_outcome("refused", "pinned_text_too_many"))

    report = await _report(day, 1)

    top = report["top_failure_reasons"]
    assert sum(1 for r in top if r["kind"] == "failed") == TOP_REASONS
    assert [r["code"] for r in top if r["kind"] == "refused"] == ["pinned_text_too_many"]


@pytest.mark.asyncio
async def test_failure_share_is_per_source_not_one_pooled_denominator(project):
    day = _day()
    at = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=1)
    _turn(project, at, "failed", error={"code": "runtime_provider_outcome_unknown"})
    for _ in range(3):
        _job(project, at, "processing_failed", "ffmpeg_failed")

    report = await _report(day, 1)

    shares = {r["source"]: r["share"] for r in report["top_failure_reasons"]}
    assert shares == {"turn": 1.0, "render": 1.0}


@pytest.mark.asyncio
async def test_a_quiet_day_is_in_the_series_with_a_null_rate_and_stages_stay_separate(project):
    day = _day()
    start = day.replace(hour=0, minute=0, second=0)
    _turn(project, start + timedelta(seconds=1), "completed")
    _job(project, start + timedelta(seconds=1), "variants_failed", "render_oom")
    _turn(project, start - timedelta(days=3, seconds=-5), "completed")

    report = await _report(day, 3)

    assert [d["date"] for d in report["daily"]] == [
        (start - timedelta(days=2)).date().isoformat(),
        (start - timedelta(days=1)).date().isoformat(),
        start.date().isoformat(),
    ]
    quiet, last = report["daily"][0], report["daily"][2]
    assert quiet["turns"]["attempts"] == 0 and quiet["turns"]["success_rate"] is None
    assert quiet["renders"]["success_rate"] is None
    assert last["turns"]["success_rate"] == 1.0
    assert last["renders"]["success_rate"] == 0.0
    assert report["window"]["since"] == (start - timedelta(days=2)).isoformat()
    assert report["window"]["until"] == (start + timedelta(days=1)).isoformat()


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", TOKEN)


def _get(path, token=TOKEN):
    headers = {"X-Admin-Token": token} if token else {}
    try:
        with TestClient(app) as client:
            return client.get(path, headers=headers)
    finally:
        async_engine.sync_engine.dispose(close=False)


def test_the_route_needs_the_admin_token_and_bounds_the_window(admin):
    assert _get("/admin/kria/creation-health", token=None).status_code == 422
    assert _get("/admin/kria/creation-health", token="wrong").status_code == 401
    assert _get("/admin/kria/creation-health?days=0").status_code == 422
    assert _get(f"/admin/kria/creation-health?days={MAX_DAYS + 1}").status_code == 422


def test_the_route_returns_the_report_with_its_definitions(admin):
    response = _get("/admin/kria/creation-health?days=3")
    assert response.status_code == 200
    body = response.json()
    assert len(body["daily"]) == 3
    assert body["window"]["days"] == 3 and body["window"]["timezone"] == "UTC"
    assert {
        "turn_attempt",
        "turn_success",
        "render_attempt",
        "render_success",
        "share",
        "success_rate",
    } <= set(body["definitions"])
    assert set(body) == {
        "window",
        "definitions",
        "turns",
        "renders",
        "daily",
        "top_failure_reasons",
    }
