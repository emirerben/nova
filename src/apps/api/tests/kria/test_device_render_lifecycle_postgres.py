"""Phone render lifecycle on PostgreSQL: the "killer screens" only for genuine failures.

Each test drives the real HTTP route (or the real task) against a seeded runtime-v2
project shaped like the prod jobs that sat in needs_attention (fafa1746 missing
original on an edit with a last good video, f3af4e8b reaped after 24h, c3e51ae5
missing asset, 57602026 output check).

Ways this can fail, written before the change:
- A user Stop is recorded as a failure: the project reads "Needs attention", a chat
  failure card is posted, and a runtime-v2 job can only leave through a new request.
- A Stop report that arrives late, twice, or after publication contradicts the phone
  or flips a published render.
- Try again on a chat-approved render needs a fresh approval bound to the newest
  turn, so any message typed after the failure makes it a dead end.
- Every GET 409 is the same "changed elsewhere" text: the phone cannot tell a newer
  attempt (follow it) from a cancelled render or an emptied draft.
- A contract refusal found on GET is never written, so the phone polls the same 409
  forever and the reaper (which needs a poll touch) never settles it.
- A late failure or the reaper relabels an edit whose last good video still plays.
- A reaped render stays failed after the creator reopens the project.
- An automatic-repair grant lapsing mid-delivery fails the exact approved recipe.
- A verification worker outage fails a render whose bytes were fine.
- A read that renews a repair grant saves a stale copy of the record over the one that
  noted the newest reader (max_reader_client_protocol), so an older build's decline is
  accepted instead of held.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.auth import get_current_user
from app.database import sync_session
from app.main import app
from app.models import CreatorAgentExecution, Job, PlanItem
from app.routes import device_render as routes
from app.services.device_render import device_record, device_status, save_device_record
from app.tasks.device_render_reaper import _reap_job
from tests.kria.test_render_retry_postgres import client, seed_failed  # noqa: F401

PROTOCOL = {"X-Kria-Client-Protocol": "13"}


@pytest.fixture
def nudges(monkeypatch):
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(routes, "_nudge_kria_observer", lambda job_id: calls.append(job_id))
    return calls


def _as(case) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)


def _live(case, *, phase="awaiting_device", last_good=True, **record_fields):
    """Turn the seeded failed record into a live delivery (or another shape)."""
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["status"].update(phase=phase, reason=None, reason_code=None)
        record.update(record_fields)
        save_device_record(job, "first", record)
        assembly = copy.deepcopy(job.assembly_plan)
        variant = assembly["variants"][0]
        variant["render_status"] = "awaiting_device"
        if not last_good:
            variant.pop("video_path", None)
        job.assembly_plan = assembly
        job.failure_reason = None
        db.commit()
        return device_status(job, "first").request.identity


def _get(http, case, headers=None):
    return http.get(f"/me/jobs/{case.job_id}/device-render?variant_id=first", headers=headers)


def _post(http, case, route, identity, **body):
    return http.post(
        f"/me/jobs/{case.job_id}/device-render/{route}",
        json={"identity": identity.model_dump(mode="json"), **body},
    )


def _reap(case) -> int:
    # The real sweep body for this one Job (the shared test DB holds other Jobs).
    return _reap_job(case.job_id, threshold_s=60, now=datetime.now(UTC))


def _job(case) -> Job:
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        db.expunge(job)
        return job


def test_stop_is_a_resumable_pause_that_the_same_recipe_resumes(client, nudges):  # noqa: F811
    http, publish = client
    case = seed_failed()
    _as(case)
    identity = _live(case, last_good=False)

    stop = _post(http, case, "failures", identity, reason_code="cancelled_by_user")
    assert stop.status_code == 200, stop.text
    assert stop.json()["phase"] == "paused"
    job = _job(case)
    record = device_record(job, "first")
    # Nothing failed: the phase, the variant and the Job keep their state, and no
    # chat card is settled for a stop the creator chose.
    assert record["status"]["phase"] == "awaiting_device"
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert job.failure_reason is None
    assert nudges == []
    assert _get(http, case).json()["paused_by_user"] is True

    # A retried (duplicate) Stop report is accepted the same way: the phone's retry
    # loop must converge, never error.
    assert _post(http, case, "failures", identity, reason_code="cancelled_by_user").json()[
        "phase"
    ] == ("paused")

    # The reaper never treats a deliberate pause as an abandoned phone.
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["pinned_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        record["last_polled_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        save_device_record(job, "first", record)
        db.commit()
    assert _reap(case) == 0

    # Resume re-delivers the SAME pinned identity: no approval, no new revision.
    resume = _post(http, case, "retry", identity)
    assert resume.status_code == 200, resume.text
    assert resume.json()["identity"] == identity.model_dump(mode="json")
    assert _get(http, case).json()["paused_by_user"] is False
    with sync_session() as db:
        assert db.get(CreatorAgentExecution, case.execution_id).status == "failed"
    publish.assert_not_called()


def test_stop_after_publication_is_refused_with_a_code_so_the_phone_stops_retrying(
    client,  # noqa: F811
):
    http, _ = client
    case = seed_failed()
    _as(case)
    identity = _live(case, phase="published")
    response = _post(http, case, "failures", identity, reason_code="cancelled_by_user")
    assert response.status_code == 409
    assert response.json()["detail"] == "Render already published"
    assert response.json()["problem"]["code"] == "published"
    assert device_record(_job(case), "first")["status"]["phase"] == "published"


def test_a_failure_on_an_edit_keeps_the_last_good_video_and_relink_retries_directly(
    client,  # noqa: F811
    nudges,
):
    # Prod fafa1746: an edit of a video that already played failed with a missing
    # proxy, and the whole project read "Needs attention".
    http, _ = client
    case = seed_failed()
    _as(case)
    identity = _live(case)
    response = _post(
        http,
        case,
        "failures",
        identity,
        reason_code="original_missing",
        detail='missingOriginal("analysis-proxy-ios-5EDE7CF0.mp4")',
    )
    assert response.status_code == 200, response.text
    job = _job(case)
    assert device_record(job, "first")["status"]["reason_code"] == "original_missing"
    variant = job.assembly_plan["variants"][0]
    assert variant["video_path"] == "last-good.mp4"
    assert variant["render_status"] == "awaiting_device"
    assert job.failure_reason is None
    assert nudges == [case.job_id]

    # After relinking, the phone retries directly: no chat approval detour.
    retry = _post(http, case, "retry", identity)
    assert retry.status_code == 200, retry.text
    assert retry.json()["identity"]["recipe_revision"] == identity.recipe_revision + 1


def test_a_first_render_failure_still_labels_the_project(client, nudges):  # noqa: F811
    # Prod c3e51ae5: nothing played yet, so the failure is the project's real state.
    http, _ = client
    case = seed_failed()
    _as(case)
    identity = _live(case, last_good=False)
    response = _post(
        http, case, "failures", identity, reason_code="original_missing", detail='missingAsset("1")'
    )
    assert response.status_code == 200, response.text
    job = _job(case)
    assert job.assembly_plan["variants"][0]["render_status"] == "needs_attention"
    assert job.failure_reason == "original_missing"


def test_get_conflicts_carry_machine_codes_beside_the_legacy_detail(client):  # noqa: F811
    http, _ = client
    case = seed_failed()
    _as(case)
    _live(case)

    # A newer Job took over the project: point the phone at it.
    with sync_session() as db:
        old = db.get(Job, case.job_id)
        newer = Job(
            user_id=case.user_id,
            content_plan_item_id=old.content_plan_item_id,
            content_plan_ownership_epoch=0,
            mode="content_plan",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            status="processing",
            assembly_plan={},
        )
        db.add(newer)
        db.flush()
        db.get(PlanItem, old.content_plan_item_id).current_job_id = newer.id
        db.commit()
        newer_id = str(newer.id)
    response = _get(http, case)
    assert response.status_code == 409
    assert response.json()["detail"] == "Project changed"
    assert response.json()["problem"] == {
        "code": "superseded",
        "message": "Project changed",
        "active_job_id": newer_id,
    }

    with sync_session() as db:
        old = db.get(Job, case.job_id)
        db.get(PlanItem, old.content_plan_item_id).current_job_id = old.id
        assembly = copy.deepcopy(old.assembly_plan)
        assembly["variants"][0]["editor_state"] = "empty"
        old.assembly_plan = assembly
        db.commit()
    response = _get(http, case)
    assert response.json()["detail"] == "Editor draft has no video to render"
    assert response.json()["problem"]["code"] == "editor_empty"

    with sync_session() as db:
        old = db.get(Job, case.job_id)
        assembly = copy.deepcopy(old.assembly_plan)
        assembly["variants"][0].pop("editor_state")
        old.assembly_plan = assembly
        old.status = "cancelled"
        db.commit()
    response = _get(http, case)
    assert response.json()["detail"] == "Render cancelled"
    assert response.json()["problem"]["code"] == "render_cancelled"


def test_a_newer_approved_attempt_is_superseded_not_a_refusal(client, nudges):  # noqa: F811
    http, _ = client
    case = seed_failed()
    _as(case)
    _live(case, prepared_execution={"id": str(uuid.uuid4()), "attempt_id": "older"})
    response = _get(http, case)
    assert response.status_code == 409
    assert response.json()["detail"] == "Device recipe changed"
    assert response.json()["problem"]["code"] == "superseded"
    record = device_record(_job(case), "first")
    # Nothing about the creator's edit was refused: no decline, no failure, no card.
    assert "contract_decline" not in record
    assert record["status"]["phase"] == "awaiting_device"
    assert nudges == []
    # ...and the reaper leaves a record the fence moved past alone.
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["pinned_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        save_device_record(job, "first", record)
        db.commit()
    assert _reap(case) == 0


def test_a_contract_refusal_on_get_is_persisted_once_and_polling_settles(
    client,  # noqa: F811
    nudges,
):
    http, _ = client
    case = seed_failed()
    _as(case)
    _live(case, contract_digest="0" * 64)  # the approved requirements moved on
    first = _get(http, case)
    assert first.status_code == 200, first.text
    assert first.json()["phase"] == "needs_attention"
    assert first.json()["reason_code"] == "contract_changed"
    record = device_record(_job(case), "first")
    assert record["contract_decline"]["stage"] == "poll"
    assert record["last_polled_at"], "polling must keep feeding the reaper"
    assert nudges == [case.job_id]
    # The next poll reports the same state without writing or nudging again.
    second = _get(http, case)
    assert second.status_code == 200
    assert second.json()["reason_code"] == "contract_changed"
    assert nudges == [case.job_id]
    # Try again on a refused recipe says why, typed, instead of looping.
    identity = device_status(_job(case), "first").request.identity
    retry = _post(http, case, "retry", identity)
    assert retry.status_code == 409
    assert retry.json()["problem"]["code"] == "contract_changed"
    assert device_status(_job(case), "first").request.identity == identity


def test_reopening_a_reaped_project_restarts_the_same_recipe(client):  # noqa: F811
    # Prod f3af4e8b: an edit's phone went silent for a day; the last good video stayed.
    http, _ = client
    case = seed_failed()
    _as(case)
    identity = _live(case)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["pinned_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        save_device_record(job, "first", record)
        db.commit()
    assert _reap(case) == 1
    job = _job(case)
    assert device_record(job, "first")["status"]["reason_code"] == "timed_out"
    assert job.assembly_plan["variants"][0]["video_path"] == "last-good.mp4"
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"

    # A read without a phone build (no protocol) never restarts anything.
    assert _get(http, case).json()["phase"] == "needs_attention"
    reopened = _get(http, case, headers=PROTOCOL)
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["phase"] == "awaiting_device"
    assert body["request"]["identity"]["recipe_revision"] == identity.recipe_revision + 1
    assert body["request"]["recipe"] == case.recipe
    job = _job(case)
    assert job.failure_reason is None
    assert device_record(job, "first")["auto_resumed_at"]


def test_a_lapsed_repair_grant_renews_for_the_exact_recipe_only(client):  # noqa: F811
    http, _ = client
    case = seed_failed()
    _as(case)
    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    identity = _live(case)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["status"]["request"]["recovery"] = {
            "action": "normalize_sources",
            "attempt": 1,
            "asset_ids": ["a"],
        }
        record["repair_authority"] = {
            "identity": identity.model_dump(mode="json"),
            "expires_at": expired,
        }
        save_device_record(job, "first", record)
        db.commit()
    response = _get(http, case)
    assert response.status_code == 200, response.text
    assert response.json()["phase"] == "awaiting_device"
    renewed = device_record(_job(case), "first")["repair_authority"]
    assert datetime.fromisoformat(renewed["expires_at"]) > datetime.now(UTC)
    assert renewed["renewals"] == 1

    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["repair_authority"] = {
            "identity": {**identity.model_dump(mode="json"), "recipe_digest": "f" * 64},
            "expires_at": expired,
        }
        save_device_record(job, "first", record)
        db.commit()
    response = _get(http, case)
    assert response.json()["phase"] == "needs_attention"
    assert response.json()["reason_code"] == "contract_changed"


def test_a_repair_renewal_keeps_the_newest_reader_it_just_noted(client):  # noqa: F811
    http, _ = client
    case = seed_failed()
    _as(case)
    identity = _live(case)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["status"]["request"]["recovery"] = {
            "action": "normalize_sources",
            "attempt": 1,
            "asset_ids": ["a"],
        }
        record["repair_authority"] = {
            "identity": identity.model_dump(mode="json"),
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
        save_device_record(job, "first", record)
        db.commit()

    read = _get(http, case, headers={"X-Kria-Client-Protocol": "15"})

    assert read.status_code == 200, read.text
    record = device_record(_job(case), "first")
    assert record["repair_authority"]["renewals"] == 1
    assert record[routes.READER_PROTOCOL_FIELD] == 15
    held = http.post(
        f"/me/jobs/{case.job_id}/device-render/failures",
        json={
            "identity": identity.model_dump(mode="json"),
            "reason_code": "renderer_outdated",
            "detail": "too new",
        },
        headers={"X-Kria-Client-Protocol": "12"},
    )
    assert held.status_code == 409, held.text
    assert held.json()["problem"]["code"] == "held_by_newer_build"
    assert device_status(_job(case), "first").phase == "awaiting_device"


async def test_a_verification_infrastructure_outage_rechecks_the_same_bytes(
    client,  # noqa: F811
    monkeypatch,
):
    from app.tasks import export_verification

    http, _ = client
    case = seed_failed()
    identity = _live(case, phase="syncing")
    attempt_id = str(uuid.uuid4())
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["attempts"] = {
            attempt_id: {
                "path": "export.mp4",
                "size": 12,
                "sha256": "a" * 64,
                "verification_task": {
                    "state": "running",
                    "runs": export_verification.MAX_CHECK_RUNS,
                    "token": "old",
                    "queued_at": datetime.now(UTC).isoformat(),
                    "lease_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
            }
        }
        save_device_record(job, "first", record)
        db.commit()
    outcome = await export_verification._run(
        str(case.job_id),
        str(case.user_id),
        {"identity": identity.model_dump(mode="json"), "attempt_id": attempt_id},
    )
    assert outcome == "requeued"
    record = device_record(_job(case), "first")
    task = record["attempts"][attempt_id]["verification_task"]
    assert record["status"]["phase"] == "syncing", "the phone keeps 'Checking your video'"
    assert task["state"] == "queued" and task["runs"] == 0 and "token" not in task
    assert _job(case).failure_reason is None


def test_a_rollout_pause_holds_the_render_instead_of_failing_it(
    client,  # noqa: F811
    nudges,
    monkeypatch,
):
    # S3-b: phone rendering is rolled back for this account while a recipe is pinned.
    # The phone (this build or an older one) is refused an asset grant and reports a
    # failure; that is the rollout, not the device, so nothing may be labelled failed.
    from app.services import phone_destination

    http, _ = client
    case = seed_failed()
    _as(case)
    identity = _live(case, last_good=False)
    monkeypatch.setattr(phone_destination, "phone_rendering_allowed_for_job", lambda job: False)

    status = _get(http, case)
    assert status.status_code == 200, status.text
    assert status.json()["phase"] == "awaiting_device"
    assert status.json()["phone_rendering_paused"] is True

    grant = _post(http, case, "assets", identity, asset_id="1")
    assert grant.status_code == 503, grant.text
    assert grant.json()["detail"] == "Phone rendering is unavailable"
    assert grant.json()["problem"]["code"] == "phone_rendering_paused"

    report = _post(http, case, "failures", identity, reason_code="unknown", detail="404")
    assert report.status_code == 200, report.text
    assert report.json()["phase"] == "paused"
    job = _job(case)
    record = device_record(job, "first")
    assert record["status"]["phase"] == "awaiting_device"
    assert record["status"]["reason_code"] is None
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert job.failure_reason is None
    assert nudges == []


async def test_a_superseded_record_closes_its_check_and_never_fails_the_newer_edit(
    client,  # noqa: F811
):
    from app.tasks import export_verification

    http, _ = client
    case = seed_failed()
    attempt_id = str(uuid.uuid4())
    long_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    # The newer approved attempt moved the fence past this syncing record.
    identity = _live(
        case,
        phase="syncing",
        last_good=False,
        prepared_execution={"id": str(uuid.uuid4()), "attempt_id": "older"},
        attempts={
            attempt_id: {
                "path": "export.mp4",
                "size": 12,
                "sha256": "a" * 64,
                "verification_task": {
                    "state": "queued",
                    "runs": 0,
                    "queued_at": long_ago,
                    "first_queued_at": long_ago,
                },
            }
        },
    )

    def task():
        return device_record(_job(case), "first")["attempts"][attempt_id]["verification_task"]

    # The queue-timeout path (a day of requeues) closes it instead of failing it.
    export_verification._expire_queued(case.job_id, datetime.now(UTC))
    assert task()["state"] == "done" and task()["outcome"] == "superseded"
    job = _job(case)
    assert device_record(job, "first")["status"]["phase"] == "syncing"
    assert device_record(job, "first")["status"]["reason_code"] is None
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert job.failure_reason is None

    # A redelivered check of the same stale record closes it too (never left queued).
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        record["attempts"][attempt_id]["verification_task"] = {
            "state": "queued",
            "runs": 0,
            "queued_at": datetime.now(UTC).isoformat(),
        }
        save_device_record(job, "first", record)
        db.commit()
    outcome = await export_verification._run(
        str(case.job_id),
        str(case.user_id),
        {"identity": identity.model_dump(mode="json"), "attempt_id": attempt_id},
    )
    assert outcome == "superseded"
    assert task()["state"] == "done" and task()["outcome"] == "superseded"
    assert _job(case).failure_reason is None
