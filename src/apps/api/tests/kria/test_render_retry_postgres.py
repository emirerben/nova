"""KRI-674: HTTP recovery must retain consent and frozen execution, with no planner.

Failure catalogue was written before implementation in /private/tmp/kri674-plan.md:
duplicates, lost dispatch acknowledgement, stale source/draft/owner, unknown work,
missing snapshots, deterministic refusal, expired consent and stale publication.
The original incident had no completed recipe; recovery must say so explicitly.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth import get_current_user
from app.config import settings
from app.database import get_db, sync_session
from app.kria.device_render import make_device_request
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.recipes_v2 import EditRecipeV2
from app.main import app
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.device_render import (
    device_record,
    device_status,
    pin_device_request,
    save_device_record,
)
from app.tasks.kria_runtime import execute_kria_approval
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


def seed_failed(*, snapshot=True, reason="export_failed"):
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        job = Job(
            user_id=user_id,
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            mode="content_plan",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            status="awaiting_device" if snapshot else "processing_failed",
            assembly_plan={
                "native_device_only": True,
                "variants": [
                    {
                        "variant_id": "first",
                        "render_generation_id": "approved",
                        "render_destination": "device",
                        "render_status": "needs_attention",
                        "video_path": "last-good.mp4",
                    }
                ],
            },
        )
        db.add(job)
        db.flush()
        recipe = EditRecipeV2.model_validate_json(
            (Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json").read_text()
        )
        recipe.required_capabilities |= {"basicComposition", "local1080Export"}
        if snapshot:
            request = make_device_request(
                job_id=job.id, variant_id="first", revision=1, recipe=recipe
            )
            pin_device_request(job, request, base_generation="approved")
            record = copy.deepcopy(device_record(job, "first"))
            record["status"].update(phase="needs_attention", reason_code=reason)
            save_device_record(job, "first", record)
        event = CreationThreadEvent(
            thread_id=thread_id,
            sequence=2,
            revision=3,
            role="user",
            event_type="user_message",
            content="Keep my approved edit",
        )
        db.add(event)
        db.flush()
        turn = CreatorAgentTurn(
            thread_id=thread_id,
            session_id=session_id,
            source_event_id=event.id,
            client_event_id=str(uuid.uuid4()),
            request_digest="a" * 64,
            status="failed",
        )
        db.add(turn)
        db.flush()
        document = KriaDraftDocument(
            kind="strategy",
            strategy={"edit_format": "montage", "target_duration_s": 2},
            intent="Keep my approved edit",
        )
        body, digest = canonical_snapshot(document)
        draft = CreatorEditDraft(
            creator_id=user_id,
            thread_id=thread_id,
            item_id=item.id,
            variant_key="first",
            draft_revision=1,
            snapshot_json=body,
            snapshot_hash=digest,
            is_head=True,
        )
        db.add(draft)
        db.flush()
        execution = CreatorAgentExecution(
            session_id=session_id,
            turn_id=turn.id,
            idempotency_key=str(uuid.uuid4()),
            request_digest="a" * 64,
            expected_revision=0,
            tool_name="render.request",
            tool_version=1,
            risk="approval_required",
            target_thread_id=thread_id,
            target_draft_id=draft.id,
            target_draft_revision=1,
            target_job_id=job.id,
            target_variant_id="first",
            target_generation_id="approved",
            target_ownership_epoch=0,
            status="failed",
            error={"code": "device_render_failed", "recovery": "manual", "retryable": False},
            result={"creator_request": document.intent},
        )
        db.add(execution)
        db.flush()
        approval = CreatorAgentApproval(
            creator_id=user_id,
            thread_id=thread_id,
            session_id=session_id,
            turn_id=turn.id,
            draft_id=draft.id,
            draft_revision=1,
            target_job_id=job.id,
            target_variant_id="first",
            target_generation_id="approved",
            target_ownership_epoch=0,
            execution_ids=[str(execution.id)],
            consequence_summary="Render this exact edit",
            status="consumed",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            consumed_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        db.add(approval)
        db.flush()
        execution.result = {**execution.result, "approval_id": str(approval.id)}
        item.current_job_id = job.id
        thread.active_job_id = job.id
        thread.revision = 3
        session.target_job_id = job.id
        session.target_variant_id = "first"
        session.target_generation_id = "approved"
        session.status = "awaiting_feedback"
        db.commit()
        return SimpleNamespace(
            user_id=user_id,
            thread_id=thread_id,
            session_id=session_id,
            job_id=job.id,
            turn_id=turn.id,
            draft_id=draft.id,
            execution_id=execution.id,
            recipe=recipe.model_dump(mode="json"),
        )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "ios_native_device_only_enabled", True)
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    recipe = EditRecipeV2.model_validate_json(
        (Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json").read_text()
    )
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities | {"basicComposition", "local1080Export"}),
    )
    from app.routes import kria_runtime

    monkeypatch.setattr(kria_runtime.limiter, "enabled", False)
    publish = Mock()
    monkeypatch.setattr(kria_runtime, "_publish_approval", publish)
    engine = create_async_engine(
        settings.database_url.replace("postgresql://", "postgresql+asyncpg://"), poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app, raise_server_exceptions=True) as http:
        yield http, publish
    app.dependency_overrides.clear()


def recover(http, case, revision=3):
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)
    return http.post(
        f"/creation-threads/{case.thread_id}/render-retries",
        json={"execution_id": str(case.execution_id), "expected_thread_revision": revision},
    )


def test_legacy_device_retry_cannot_bypass_runtime_approval(client):
    http, publish = client
    case = seed_failed()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        identity = device_status(job, "first").request.identity
        assert "retry_requires_approval" not in device_record(job, "first")["status"]
    status = http.get(f"/me/jobs/{case.job_id}/device-render?variant_id=first")
    assert status.status_code == 200, status.text
    assert status.json()["retry_requires_approval"] is True
    response = http.post(
        f"/me/jobs/{case.job_id}/device-render/retry",
        json={"identity": identity.model_dump(mode="json")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "Open this project's chat and review Retry approved edit before rendering again."
    )
    with sync_session() as db:
        status = device_status(db.get(Job, case.job_id), "first")
        assert status.request.identity == identity
        assert status.phase == "needs_attention"
        assert (
            db.scalar(
                select(func.count())
                .select_from(CreatorAgentApproval)
                .where(CreatorAgentApproval.thread_id == case.thread_id)
            )
            == 1
        )
    publish.assert_not_called()


def test_a_new_manual_editor_generation_keeps_direct_device_retry(client):
    http, publish = client
    case = seed_failed()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = copy.deepcopy(device_record(job, "first"))
        record["base_generation"] = "manual-save-generation"
        save_device_record(job, "first", record)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0]["render_generation_id"] = "manual-save-generation"
        job.assembly_plan = assembly
        identity = device_status(job, "first").request.identity
        db.commit()
    status = http.get(f"/me/jobs/{case.job_id}/device-render?variant_id=first")
    assert status.status_code == 200, status.text
    assert status.json()["retry_requires_approval"] is False
    response = http.post(
        f"/me/jobs/{case.job_id}/device-render/retry",
        json={"identity": identity.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    with sync_session() as db:
        status = device_status(db.get(Job, case.job_id), "first")
        assert status.request.identity.recipe_revision == identity.recipe_revision + 1
        assert status.request.recipe.model_dump(mode="json") == case.recipe
        assert db.get(CreatorAgentExecution, case.execution_id).status == "failed"
    publish.assert_not_called()


def test_retry_reuses_turn_draft_and_recipe_with_fresh_consent(client, monkeypatch):
    http, publish = client
    case = seed_failed()
    # Any accidental new creative planning or analysis is a real failure.
    from app.tasks import kria_runtime

    planner = Mock(side_effect=AssertionError("Retry must not plan"))
    monkeypatch.setattr(kria_runtime, "_plan_with_live_agent", planner)
    response = recover(http, case)
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["turn_id"] == str(case.turn_id)
    approval_id = accepted["approval_id"]
    replay = recover(http, case)
    assert replay.status_code == 202
    assert replay.json()["approval_id"] == approval_id
    publish.assert_not_called()
    snap = http.get(f"/creation-threads/{case.thread_id}/approvals/{approval_id}").json()
    assert snap["draft_id"] == str(case.draft_id)
    assert snap["status"] == "pending"
    decision = http.post(
        f"/creation-threads/{case.thread_id}/approvals/{approval_id}/approve",
        json={
            "expected_thread_revision": accepted["thread_revision"],
            "expected_draft_revision": 1,
            "expected_approval_fingerprint": snap["approval_fingerprint"],
        },
    )
    assert decision.status_code == 200, decision.text
    publish.assert_called_once_with(approval_id)
    assert execute_kria_approval.run(approval_id)["status"] == "dispatched"
    assert execute_kria_approval.run(approval_id)["status"] == "ignored"
    planner.assert_not_called()
    with sync_session() as db:
        status = device_status(db.get(Job, case.job_id), "first")
        assert status.request.identity.recipe_revision == 2
        assert status.request.recipe.model_dump(mode="json") == case.recipe
        assert (
            db.scalar(
                select(func.count())
                .select_from(CreatorAgentTurn)
                .where(CreatorAgentTurn.thread_id == case.thread_id)
            )
            == 1
        )
        assert db.get(CreatorAgentExecution, case.execution_id).status == "failed"
        assert (
            db.get(Job, case.job_id).assembly_plan["variants"][0]["video_path"] == "last-good.mp4"
        )


@pytest.mark.parametrize(
    "snapshot,reason,code",
    [
        (False, "export_failed", "render_retry_snapshot_unavailable"),
        (True, "unsupported_recipe", "render_retry_requires_change"),
        (True, "original_missing", "render_retry_originals_missing"),
    ],
)
def test_unsafe_retries_are_specific_and_do_not_replan(client, snapshot, reason, code):
    case = seed_failed(snapshot=snapshot, reason=reason)
    response = recover(client[0], case)
    assert response.status_code == 409, response.text
    assert response.json()["problem"]["code"] == code
    client[1].assert_not_called()


def test_newer_draft_and_foreign_owner_cannot_retry(client):
    case = seed_failed()
    with sync_session() as db:
        db.get(CreatorEditDraft, case.draft_id).is_head = False
        db.commit()
    assert recover(client[0], case).status_code == 409
    stranger = copy.copy(case)
    stranger.user_id = uuid.uuid4()
    assert recover(client[0], stranger).status_code == 404


def test_concurrent_taps_coalesce_before_approval(client):
    from concurrent.futures import ThreadPoolExecutor

    case = seed_failed()
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: recover(client[0], case), range(4)))
    assert [r.status_code for r in replies] == [202] * 4
    assert len({r.json()["approval_id"] for r in replies}) == 1
    with sync_session() as db:
        assert (
            device_status(db.get(Job, case.job_id), "first").request.identity.recipe_revision == 1
        )


def test_unknown_work_does_not_start_another_attempt(client):
    case = seed_failed()
    with sync_session() as db:
        db.get(CreatorAgentExecution, case.execution_id).status = "outcome_unknown"
        db.commit()
    response = recover(client[0], case)
    assert response.status_code == 409
    assert response.json()["problem"]["code"] == "render_retry_outcome_unknown"


def test_observer_does_not_promise_an_exact_retry_without_saved_program(client):
    from app.tasks.kria_runtime import _observe_dispatched_execution

    case = seed_failed(snapshot=False)
    with sync_session() as db:
        db.get(CreatorAgentExecution, case.execution_id).status = "dispatched"
        db.get(CreatorAgentTurn, case.turn_id).status = "observing"
        db.get(CreatorAgentSession, case.session_id).status = "rendering"
        db.get(Job, case.job_id).failure_reason = "creation_text_timeout"
        db.commit()
    assert _observe_dispatched_execution(case.execution_id)[0] == "failed"
    with sync_session() as db:
        event = db.scalar(
            select(CreationThreadEvent)
            .where(CreationThreadEvent.thread_id == case.thread_id)
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        )
        assert event.payload["recovery"] == "ask_user"
        assert "complete render plan" in event.content
        assert "retry without rebuilding" not in event.content


def test_expired_retry_approval_does_not_repin(client):
    case = seed_failed()
    http = client[0]
    reply = recover(http, case)
    assert reply.status_code == 202, reply.text
    a = reply.json()
    snap = http.get(f"/creation-threads/{case.thread_id}/approvals/{a['approval_id']}").json()
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, uuid.UUID(a["approval_id"]))
        approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    snap = http.get(f"/creation-threads/{case.thread_id}/approvals/{a['approval_id']}").json()
    reply = http.post(
        f"/creation-threads/{case.thread_id}/approvals/{a['approval_id']}/approve",
        json={
            "expected_thread_revision": a["thread_revision"],
            "expected_draft_revision": 1,
            "expected_approval_fingerprint": snap["approval_fingerprint"],
        },
    )
    assert reply.status_code == 409
    with sync_session() as db:
        assert (
            device_status(db.get(Job, case.job_id), "first").request.identity.recipe_revision == 1
        )


def test_expired_consent_is_renewed_for_the_same_prepared_retry(client):
    case = seed_failed()
    first = recover(client[0], case).json()
    with sync_session() as db:
        db.get(CreatorAgentApproval, uuid.UUID(first["approval_id"])).expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        db.commit()
    response = recover(client[0], case, first["thread_revision"])
    assert response.status_code == 202, response.text
    assert response.json()["approval_id"] != first["approval_id"]
    with sync_session() as db:
        assert (
            device_status(db.get(Job, case.job_id), "first").request.identity.recipe_revision == 1
        )
        assert db.get(CreatorAgentApproval, uuid.UUID(first["approval_id"])).status == "expired"


def test_lost_acknowledgement_resumes_the_accepted_attempt_without_repinning(client, monkeypatch):
    from app.tasks.kria_runtime import _claim_approval_dispatch

    case = seed_failed()
    http = client[0]
    accepted = recover(http, case).json()
    url = f"/creation-threads/{case.thread_id}/approvals/{accepted['approval_id']}"
    snap = http.get(url).json()
    response = http.post(
        url + "/approve",
        json={
            "expected_thread_revision": accepted["thread_revision"],
            "expected_draft_revision": 1,
            "expected_approval_fingerprint": snap["approval_fingerprint"],
        },
    )
    assert response.status_code == 200, response.text
    claim = _claim_approval_dispatch(uuid.UUID(accepted["approval_id"]))
    assert claim is not None
    # Pausing device features after acceptance must not strand the delivery
    # receipt: recovery only finishes bookkeeping for the already-pinned attempt.
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    with sync_session() as db:
        identity = device_status(db.get(Job, case.job_id), "first").request.identity
    assert execute_kria_approval.run(accepted["approval_id"])["status"] == "dispatched"
    with sync_session() as db:
        assert device_status(db.get(Job, case.job_id), "first").request.identity == identity


def test_expired_retry_cannot_renew_after_a_new_request(client):
    case = seed_failed()
    prepared = recover(client[0], case).json()
    with sync_session() as db:
        db.get(CreatorAgentApproval, uuid.UUID(prepared["approval_id"])).expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        event = CreationThreadEvent(
            thread_id=case.thread_id,
            sequence=20,
            revision=prepared["thread_revision"] + 1,
            role="user",
            event_type="user_message",
            content="Use a different title",
        )
        db.get(CreationThread, case.thread_id).revision = prepared["thread_revision"] + 1
        db.add(event)
        db.flush()
        db.add(
            CreatorAgentTurn(
                thread_id=case.thread_id,
                session_id=case.session_id,
                source_event_id=event.id,
                client_event_id=str(uuid.uuid4()),
                request_digest="b" * 64,
                status="failed",
            )
        )
        db.commit()
    response = recover(client[0], case, prepared["thread_revision"] + 1)
    assert response.status_code == 409, response.text
    assert response.json()["problem"]["code"] == "render_retry_target_stale"


@pytest.mark.parametrize("reason", ["unsupported_recipe", "cancelled_by_user", "original_missing"])
def test_observer_hint_does_not_offer_unrecoverable_native_retry(client, reason):
    from app.kria.render_retry import retry_program_available

    case = seed_failed(reason=reason)
    with sync_session() as db:
        assert not retry_program_available(
            db.get(Job, case.job_id), db.get(CreatorAgentExecution, case.execution_id)
        )


def test_recipe_from_another_generation_is_not_the_approved_program(client):
    case = seed_failed()
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = copy.deepcopy(device_record(job, "first"))
        record["base_generation"] = "a-new-edit"
        save_device_record(job, "first", record)
        db.commit()
    response = recover(client[0], case)
    assert response.status_code == 409, response.text
    assert response.json()["problem"]["code"] == "render_retry_target_stale"


def test_accepted_recovery_retires_a_stale_receipt_without_overwriting_new_work(client):
    from app.tasks.kria_runtime import _claim_approval_dispatch

    case = seed_failed()
    http = client[0]
    prepared = recover(http, case).json()
    url = f"/creation-threads/{case.thread_id}/approvals/{prepared['approval_id']}"
    snap = http.get(url).json()
    assert (
        http.post(
            url + "/approve",
            json={
                "expected_thread_revision": prepared["thread_revision"],
                "expected_draft_revision": 1,
                "expected_approval_fingerprint": snap["approval_fingerprint"],
            },
        ).status_code
        == 200
    )
    aid = uuid.UUID(prepared["approval_id"])
    claim = _claim_approval_dispatch(aid)
    assert claim is not None
    with sync_session() as db:
        db.get(CreatorEditDraft, case.draft_id).is_head = False
        db.get(CreatorAgentSession, case.session_id).status = "awaiting_feedback"
        db.commit()
    assert _claim_approval_dispatch(aid) is None
    with sync_session() as db:
        assert db.get(CreatorAgentExecution, claim.execution_id).status == "stale"
        assert db.get(CreatorAgentSession, case.session_id).status == "awaiting_feedback"
        assert (
            device_status(db.get(Job, case.job_id), "first").request.identity.recipe_revision == 2
        )


def test_retry_refuses_capabilities_that_are_no_longer_verified(client, monkeypatch):
    case = seed_failed()
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    response = recover(client[0], case)
    assert response.status_code == 409, response.text
    assert response.json()["problem"]["code"] == "render_retry_capability_unavailable"
