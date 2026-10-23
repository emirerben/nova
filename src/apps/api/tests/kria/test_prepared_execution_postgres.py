"""KRI-722 acceptance: approval -> prepared snapshot -> claim -> Job -> device publish.

Failure matrix written before implementation: docs/reviews/kri-722-prepared-execution.md.
Real HTTP (ASGI), real Postgres, the real planner pipeline with a replayed provider
response, real ``decide_approval`` / ``_claim_approval_dispatch`` /
``dispatch_item_render_for``. Only the provider transport and broker publication are
substituted. Each mutated control must refuse; the positive control must trace one
snapshot id from the approval to the Job and its device record.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import func, select

from app.auth import get_current_user
from app.config import settings
from app.database import sync_session
from app.kria import prepared_execution as prepared
from app.kria.brief import load_latest_brief_sync
from app.kria.drafts import KriaDraftDocument
from app.kria.runtime import approval_fingerprint
from app.main import app
from app.models import (
    CreationThread,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorEditDraft,
    CreatorPreparedExecution,
    Job,
    PlanItem,
    PlanItemAsset,
)
from app.services.creator_render_contract import CONTRACT_FIELD
from app.services.device_render import device_record, device_status, verify_device_record_contract
from app.tasks.kria_runtime import execute_kria_approval, prune_kria_drafts
from tests.kria.test_approval_readiness_postgres import prepare, readiness_flags  # noqa: F401
from tests.kria.test_render_retry_postgres import client, recover, seed_failed  # noqa: F401

TRACE = Path(__file__).parents[5] / ".dev" / "kri-722-trace.json"
_trace: dict[str, object] = {}


def _record(case: str, **ids: object) -> None:
    _trace[case] = {k: str(v) if v is not None else None for k, v in ids.items()}
    if os.environ.get("KRI722_TRACE"):
        TRACE.parent.mkdir(parents=True, exist_ok=True)
        TRACE.write_text(json.dumps(_trace, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def broker(monkeypatch):
    """The render broker: record publishes instead of reaching Redis."""
    published: list[str] = []
    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator_sync",
        lambda _task, job_id, **_kw: published.append(job_id),
    )
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda _job: None)
    return published


def _body(approval: CreatorAgentApproval, thread_id: uuid.UUID) -> dict:
    with sync_session() as db:
        return {
            "expected_thread_revision": db.get(CreationThread, thread_id).revision,
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }


async def _approve(monkeypatch, user_id, thread_id, approval, body=None) -> httpx.Response:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    monkeypatch.setattr("app.routes.kria_runtime._publish_approval", lambda _id: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        return await http.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/approve",
            json=body or _body(approval, thread_id),
        )


async def _approved(monkeypatch, **kwargs):
    user_id, thread_id, approvals, events = await prepare(monkeypatch, **kwargs)
    assert len(approvals) == 1, events[-1].content
    approval = approvals[0]
    response = await _approve(monkeypatch, user_id, thread_id, approval)
    assert response.status_code == 200, response.text
    return user_id, thread_id, approval


def _snapshot_row(db, approval_id) -> CreatorPreparedExecution:
    return db.scalars(
        select(CreatorPreparedExecution).where(CreatorPreparedExecution.approval_id == approval_id)
    ).one()


def _dispatch(approval_id) -> dict:
    return execute_kria_approval.run(str(approval_id))


@pytest.mark.asyncio
async def test_positive_control_traces_one_snapshot_from_approval_to_job(monkeypatch, broker):
    user_id, thread_id, approval = await _approved(monkeypatch)
    with sync_session() as db:
        row = _snapshot_row(db, approval.id)
        stored = db.get(CreatorAgentApproval, approval.id)
        snapshot = prepared.parse(row)
        draft = db.get(CreatorEditDraft, approval.draft_id)
        assert stored.prepared_execution_id == row.id
        assert snapshot.draft_id == str(draft.id)
        assert snapshot.draft_snapshot_hash == draft.snapshot_hash
        assert snapshot.approval_id == str(approval.id)
        assert snapshot.readiness_digest is not None
        assert [s.slot for s in snapshot.sources] == ["clip:0"]
        assert snapshot.sources[0].role == "original"
        # F15: the planner wrote every strategy group, so none is recorded as creator
        # intent even where the brief asked for something inside it (KRI-824 owns that).
        assert {d.origin for d in snapshot.decisions.values()} == {"model_default"}
        # F13: a replay of the same stored rows makes no new decision.
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        replay = prepared.build_snapshot(
            creator_id=user_id,
            thread_id=thread_id,
            approval_id=approval.id,
            draft=draft,
            document=KriaDraftDocument.model_validate(draft.snapshot_json),
            sources=prepared.load_source_identities_sync(db, item, user_id),
        )
        assert replay.digest == snapshot.digest == row.snapshot_digest
        assert replay.decision_digest == snapshot.decision_digest
        assert "http" not in json.dumps(row.snapshot)

    first = _dispatch(approval.id)
    assert first["status"] == "dispatched", first
    # F7: Celery redelivery of the same approval cannot mint a second Job.
    assert _dispatch(approval.id)["status"] == "ignored"
    assert len(broker) == 1
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(first["job_id"]))
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        stamp = job.assembly_plan[prepared.STAMP_FIELD]
        assert stamp["id"] == str(row.id) == job.all_candidates[prepared.STAMP_FIELD]["id"]
        assert stamp["approval_id"] == str(approval.id)
        assert stamp["draft_id"] == str(approval.draft_id)
        assert stamp["generation_id"] == job.assembly_plan["creator_generation_id"]
        assert stamp["contract_digest"] == job.assembly_plan[CONTRACT_FIELD]["digest"]
        assert execution.result[prepared.RESULT_FIELD]["attempt_id"] == stamp["attempt_id"]
        assert (
            db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.content_plan_item_id == job.content_plan_item_id)
            )
            == 1
        )
        _record(
            "positive",
            approval=approval.id,
            snapshot=row.id,
            execution=execution.id,
            job=job.id,
            attempt=stamp["attempt_id"],
            generation=stamp["generation_id"],
        )


def _bump_generation(db, item, user_id):
    db.add(
        PlanItemAsset(
            plan_item_id=item.id,
            user_id=user_id,
            gcs_path=item.clip_gcs_paths[0],
            gcs_generation="2",
            kind="video",
            status="ready",
        )
    )


def _flip_lineage(db, item, _user_id):
    rows = copy.deepcopy(item.clip_assignments)
    rows[0]["upload_contract"] = {**rows[0]["upload_contract"], "purpose": "original"}
    item.clip_assignments = rows


def _tamper_draft(db, item, _user_id):
    draft = db.scalars(
        select(CreatorEditDraft).where(
            CreatorEditDraft.item_id == item.id, CreatorEditDraft.is_head.is_(True)
        )
    ).one()
    draft.snapshot_hash = "f" * 64


def _future_schema(db, _item, user_id):
    row = db.scalars(
        select(CreatorPreparedExecution).where(CreatorPreparedExecution.creator_id == user_id)
    ).one()
    row.snapshot = {**row.snapshot, "schema_version": prepared.SCHEMA_VERSION + 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,phone,code",
    [
        (_bump_generation, False, prepared.STALE),
        (_flip_lineage, True, prepared.SOURCE_LINEAGE),
        (_tamper_draft, False, prepared.STALE),
        (_future_schema, False, prepared.UNSUPPORTED),
        ("capabilities", False, prepared.STALE),
    ],
    ids=["source-generation", "proxy-promoted", "draft-mutated", "newer-schema", "capability"],
)
async def test_mutated_controls_refuse_before_any_job(monkeypatch, broker, mutate, phone, code):
    user_id, thread_id, approval = await _approved(monkeypatch, phone=phone)
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        if mutate == "capabilities":
            # A rolling deploy or rollback withdrew a verified capability.
            monkeypatch.setattr(
                settings,
                "phone_render_verified_features",
                [*settings.phone_render_verified_features, "kri722Probe"],
            )
        else:
            mutate(db, item, user_id)
        db.commit()
    assert _dispatch(approval.id)["status"] == "ignored"
    assert broker == []
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        assert execution.status == "stale"
        assert execution.error["code"] == code
        assert db.get(CreatorAgentApproval, approval.id).status == "cancelled"
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        assert item.current_job_id is None
        row = _snapshot_row(db, approval.id)
        assert row.invalidated_at is not None and row.invalidation_code == code


@pytest.mark.asyncio
async def test_stale_and_duplicate_approvals_never_mint_a_second_snapshot(monkeypatch):
    user_id, thread_id, approvals, _ = await prepare(monkeypatch)
    approval = approvals[0]
    body = _body(approval, thread_id)
    # F3/F11: a device holding an older draft revision is refused with 409.
    stale = await _approve(
        monkeypatch,
        user_id,
        thread_id,
        approval,
        {**body, "expected_draft_revision": approval.draft_revision + 1},
    )
    assert stale.status_code == 409, stale.text
    with sync_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(CreatorPreparedExecution)
                .where(CreatorPreparedExecution.thread_id == thread_id)
            )
            == 0
        )
    assert (await _approve(monkeypatch, user_id, thread_id, approval, body)).status_code == 200
    # F7: a double tap after success is refused, and still one snapshot.
    again = await _approve(monkeypatch, user_id, thread_id, approval, body)
    assert again.status_code == 409
    assert again.json()["problem"]["code"] in {
        "approval_not_pending",
        "approval_stale",
        "thread_revision_stale",
    }
    # F12: another creator cannot approve or read it.
    foreign = await _approve(monkeypatch, uuid.uuid4(), thread_id, approval, body)
    assert foreign.status_code in {403, 404}
    with sync_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(CreatorPreparedExecution)
                .where(CreatorPreparedExecution.thread_id == thread_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_later_chat_cannot_change_an_accepted_attempts_receipts(monkeypatch, broker):
    _, thread_id, approval = await _approved(monkeypatch)
    result = _dispatch(approval.id)
    assert result["status"] == "dispatched"
    with sync_session() as db:
        latest = db.scalars(
            select(CreativeBriefVersion)
            .where(CreativeBriefVersion.thread_id == thread_id)
            .order_by(CreativeBriefVersion.version.desc())
        ).first()
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=latest.version + 1,
                requirements=[
                    {**req, "literal": "Matchday."} if req.get("literal") else req
                    for req in latest.requirements
                ],
            )
        )
        db.commit()
        job = db.get(Job, uuid.UUID(result["job_id"]))
        stamped, brief = prepared.pinned_brief_for_job(db, job, thread_id)
        assert stamped
        assert brief.version == latest.version
        assert load_latest_brief_sync(db, thread_id).version == latest.version + 1
        assert "Gameday." in json.dumps(brief.model_dump(mode="json"))
        assert "Matchday." not in json.dumps(brief.model_dump(mode="json"))
        # F14: an unstamped (historical) Job keeps the latest-brief read.
        legacy = SimpleNamespace(assembly_plan={})
        assert prepared.pinned_brief_for_job(db, legacy, thread_id) == (False, None)


@pytest.mark.asyncio
async def test_unbound_creator_keeps_receipts_from_the_approved_brief(monkeypatch, broker):
    """Creative Brief on, brief binding off: the production review path still reports."""
    from app.tasks.kria_runtime import _approved_generation_requirements_review

    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    _, thread_id, approval = await _approved(monkeypatch)
    result = _dispatch(approval.id)
    assert result["status"] == "dispatched", result
    with sync_session() as db:
        snapshot = prepared.parse(_snapshot_row(db, approval.id))
        assert snapshot.binding_digest is None
        approved = load_latest_brief_sync(db, thread_id)
        assert snapshot.brief_version == approved.version
        requirement = approved.live()[0]
        job = db.get(Job, uuid.UUID(result["job_id"]))
        assert "creator_brief_binding" not in job.assembly_plan
        job.assembly_plan = {
            **job.assembly_plan,
            "unified_montage": {
                "brief_version": approved.version,
                "requirement_receipts": [
                    {
                        "requirement_id": requirement.id,
                        "brief_version": approved.version,
                        "status": "met",
                        "verification": "checked",
                        "stage": "applied",
                    }
                ],
            },
        }
        latest = db.scalars(
            select(CreativeBriefVersion).where(
                CreativeBriefVersion.thread_id == thread_id,
                CreativeBriefVersion.version == approved.version,
            )
        ).one()
        # Later chat rewrites the brief after the attempt was accepted.
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=approved.version + 1,
                requirements=[
                    {**req, "literal": "Matchday."} if req.get("literal") else req
                    for req in latest.requirements
                ],
            )
        )
        db.commit()
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        thread = db.get(CreationThread, thread_id)
        text, receipts = _approved_generation_requirements_review(
            db, thread, job, {}, execution, "Your edit is ready."
        )
        assert [r["requirement_id"] for r in receipts] == [requirement.id]
        assert text != "Your edit is ready."


@pytest.mark.asyncio
async def test_accepted_redelivery_is_not_cancelled_by_a_capability_change(monkeypatch, broker):
    """Outcome unknown after dispatch, then a deploy changes capabilities: no cancel."""
    from app.tasks.kria_runtime import _verify_prepared_execution

    _, thread_id, approval = await _approved(monkeypatch)
    result = _dispatch(approval.id)
    assert result["status"] == "dispatched", result
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "kri722Probe"],
    )
    with sync_session() as db:
        stored = db.get(CreatorAgentApproval, approval.id)
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        draft = db.get(CreatorEditDraft, approval.draft_id)
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        execution.status = "accepted"  # the acknowledgement was lost
        record, failure = _verify_prepared_execution(
            db,
            approval=stored,
            draft=draft,
            document=KriaDraftDocument.model_validate(draft.snapshot_json),
            item=item,
            execution=execution,
        )
        assert failure is None
        assert record == execution.result[prepared.RESULT_FIELD]
        assert _snapshot_row(db, approval.id).invalidated_at is None
        db.rollback()


@pytest.mark.asyncio
async def test_flag_off_keeps_the_legacy_path(monkeypatch, broker):
    monkeypatch.setattr(settings, "kria_prepared_execution_enabled", False)
    _, thread_id, approval = await _approved(monkeypatch)
    result = _dispatch(approval.id)
    assert result["status"] == "dispatched"
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approval.id).prepared_execution_id is None
        job = db.get(Job, uuid.UUID(result["job_id"]))
        assert prepared.STAMP_FIELD not in job.assembly_plan
        assert prepared.STAMP_FIELD not in job.all_candidates
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        assert prepared.RESULT_FIELD not in execution.result


def _stamp_failed_device_attempt(case) -> CreatorPreparedExecution:
    """Give the KRI-674 failed device attempt the snapshot its approval would have minted."""
    with sync_session() as db:
        source = db.get(CreatorAgentExecution, case.execution_id)
        approval = db.get(CreatorAgentApproval, uuid.UUID(source.result["approval_id"]))
        draft = db.get(CreatorEditDraft, case.draft_id)
        item = db.get(PlanItem, db.get(CreationThread, case.thread_id).active_plan_item_id)
        snapshot = prepared.build_snapshot(
            creator_id=case.user_id,
            thread_id=case.thread_id,
            approval_id=approval.id,
            draft=draft,
            document=KriaDraftDocument.model_validate(draft.snapshot_json),
            sources=prepared.load_source_identities_sync(db, item, case.user_id),
        )
        row = CreatorPreparedExecution(
            creator_id=case.user_id,
            thread_id=case.thread_id,
            approval_id=approval.id,
            draft_id=draft.id,
            draft_revision=draft.draft_revision,
            schema_version=snapshot.schema_version,
            snapshot=snapshot.model_dump(mode="json"),
            snapshot_digest=snapshot.digest,
        )
        db.add(row)
        db.flush()
        approval.prepared_execution_id = row.id
        job = db.get(Job, case.job_id)
        record = prepared.attempt_record(row, snapshot.digest, "first-attempt")
        prepared.stamp_job(job, record)
        device = copy.deepcopy(device_record(job, "first"))
        device["prepared_execution"] = prepared.variant_fence(job.assembly_plan, "first")
        from app.services.device_render import save_device_record  # noqa: PLC0415

        save_device_record(job, "first", device)
        source.result = {**source.result, prepared.RESULT_FIELD: record}
        db.commit()
        db.refresh(row)
        return row


def test_exact_retry_keeps_the_snapshot_with_a_new_attempt(client, monkeypatch):  # noqa: F811
    http, publish = client
    case = seed_failed()
    row = _stamp_failed_device_attempt(case)
    from app.tasks import kria_runtime

    planner = Mock(side_effect=AssertionError("Retry must not plan"))
    monkeypatch.setattr(kria_runtime, "_plan_with_live_agent", planner)
    with sync_session() as db:
        old_record = copy.deepcopy(device_record(db.get(Job, case.job_id), "first"))
    accepted = recover(http, case).json()
    snap = http.get(
        f"/creation-threads/{case.thread_id}/approvals/{accepted['approval_id']}"
    ).json()
    decision = http.post(
        f"/creation-threads/{case.thread_id}/approvals/{accepted['approval_id']}/approve",
        json={
            "expected_thread_revision": accepted["thread_revision"],
            "expected_draft_revision": 1,
            "expected_approval_fingerprint": snap["approval_fingerprint"],
        },
    )
    assert decision.status_code == 200, decision.text
    assert execute_kria_approval.run(accepted["approval_id"])["status"] == "dispatched"
    planner.assert_not_called()
    with sync_session() as db:
        retry_approval = db.get(CreatorAgentApproval, uuid.UUID(accepted["approval_id"]))
        assert retry_approval.prepared_execution_id == row.id
        retry_execution = db.get(CreatorAgentExecution, uuid.UUID(retry_approval.execution_ids[0]))
        attempt = retry_execution.result[prepared.RESULT_FIELD]
        assert attempt["id"] == str(row.id)
        assert attempt["attempt_id"] != "first-attempt"
        assert attempt["retry_of_attempt_id"] == "first-attempt"
        job = db.get(Job, case.job_id)
        assert job.assembly_plan[prepared.STAMP_FIELD]["attempt_id"] == attempt["attempt_id"]
        current = device_record(job, "first")
        assert current["prepared_execution"] == {
            "id": str(row.id),
            "attempt_id": attempt["attempt_id"],
        }
        # The current attempt publishes.
        verify_device_record_contract(job, current, device_status(job, "first"))
        # F9: a late completion carrying the old attempt is refused; last-good stays.
        from app.services.creator_render_contract import (  # noqa: PLC0415
            CreatorRenderContractError,
        )

        with pytest.raises(CreatorRenderContractError):
            verify_device_record_contract(job, old_record, device_status(job, "first"))
        assert job.assembly_plan["variants"][0]["video_path"] == "last-good.mp4"
        assert db.get(CreatorPreparedExecution, row.id).snapshot_digest == row.snapshot_digest
        _record(
            "retry",
            snapshot=row.id,
            source_execution=case.execution_id,
            retry_execution=retry_execution.id,
            first_attempt="first-attempt",
            retry_attempt=attempt["attempt_id"],
            job=case.job_id,
        )
    publish.assert_called_once()


def test_retry_after_a_source_change_is_refused(client):  # noqa: F811
    http, _ = client
    case = seed_failed()
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, case.thread_id).active_plan_item_id)
        item.clip_gcs_paths = ["users/test/retry-source.mp4"]
        item.clip_assignments = [
            {"media_id": "clip-1", "gcs_path": "users/test/retry-source.mp4", "kind": "video"}
        ]
        db.commit()
    _stamp_failed_device_attempt(case)
    accepted = recover(http, case).json()
    snap = http.get(
        f"/creation-threads/{case.thread_id}/approvals/{accepted['approval_id']}"
    ).json()
    assert (
        http.post(
            f"/creation-threads/{case.thread_id}/approvals/{accepted['approval_id']}/approve",
            json={
                "expected_thread_revision": accepted["thread_revision"],
                "expected_draft_revision": 1,
                "expected_approval_fingerprint": snap["approval_fingerprint"],
            },
        ).status_code
        == 200
    )
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, case.thread_id).active_plan_item_id)
        # Same path, new object generation: the KRI-674 media fence cannot see it.
        assert item.clip_gcs_paths
        _bump_generation(db, item, case.user_id)
        before = copy.deepcopy(device_record(db.get(Job, case.job_id), "first"))
        db.commit()
    assert execute_kria_approval.run(accepted["approval_id"])["status"] == "ignored"
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, uuid.UUID(accepted["approval_id"]))
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        assert execution.status == "failed"
        assert execution.error["code"] == prepared.STALE
        assert device_record(db.get(Job, case.job_id), "first") == before


@pytest.mark.asyncio
async def test_prune_keeps_a_draft_a_live_snapshot_was_approved_from(monkeypatch, broker):
    _, thread_id, approval = await _approved(monkeypatch)
    with sync_session() as db:
        draft = db.get(CreatorEditDraft, approval.draft_id)
        draft.is_head = False
        draft.created_at = draft.created_at.replace(year=draft.created_at.year - 1)
        # No approval protects it any more; only the snapshot does.
        db.get(CreatorAgentApproval, approval.id).status = "denied"
        db.commit()
    await asyncio.to_thread(prune_kria_drafts)
    with sync_session() as db:
        assert db.get(CreatorEditDraft, approval.draft_id).snapshot_json is not None
        _snapshot_row(db, approval.id).invalidated_at = db.scalar(select(func.now()))
        db.commit()
    await asyncio.to_thread(prune_kria_drafts)
    with sync_session() as db:
        assert db.get(CreatorEditDraft, approval.draft_id).snapshot_json is None
