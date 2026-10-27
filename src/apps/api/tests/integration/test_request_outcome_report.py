"""Request-level HTTP/Postgres acceptance over real exported fixture bytes.

Failures registered before implementation: a ready retry hides a pre-job failure;
a recovery is counted as original fulfillment; recapture loses an assessment;
duplicates count twice; legacy lineage becomes known; revoked consent resurfaces;
version/cost segments reveal private content or invent missing data.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    ContentPlan,
    CreationThreadEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    InternalAccountGrant,
    Job,
    Persona,
    PlanItem,
    RequestFollowingCase,
    User,
)
from tests.integration.test_request_following_queue import (
    collect,
    first,
    outcome_body,
)

pytest_plugins = ("tests.integration.test_request_following_queue",)


async def linked_case(fixture, *, relation="original", failed_first=False, accepted_recovery=True):
    client, case, _, _, job_id = fixture
    thread_id = uuid.UUID(case["thread_id"])
    root, turn = uuid.uuid4(), uuid.uuid4()
    if not failed_first:
        root = turn
    async with AsyncSessionLocal() as db:
        source = await db.scalar(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "user_message",
            )
        )
        at = source.created_at - timedelta(seconds=5)
        job = await db.get(Job, job_id)
        session = CreatorAgentSession(creator_id=job.user_id, plan_item_id=job.content_plan_item_id)
        db.add(session)
        await db.flush()

        async def add_turn(identifier, relation, *, sequence, status):
            event = CreationThreadEvent(
                thread_id=thread_id,
                sequence=sequence,
                revision=sequence,
                role="user",
                event_type="user_message",
                content="PRIVATE-LINKED-REQUEST",
                created_at=at + timedelta(milliseconds=sequence),
                payload={
                    **(
                        {
                            "choice_selection": {
                                "question_id": "recovery-choice",
                                "option_key": "simpler_version",
                            }
                        }
                        if relation == "recovery" and accepted_recovery
                        else {}
                    ),
                    "turn_id": str(identifier),
                    "request_outcome_link": {
                        "schema_version": 1,
                        "request_turn_id": str(root),
                        "intent_revision_turn_id": str(identifier),
                        "relation": relation,
                        "original_known": True,
                        "link_status": "known",
                        "client_build": "1763",
                        "client_protocol": 22,
                        "server_revision": "b" * 40,
                    },
                },
            )
            db.add(event)
            await db.flush()
            db.add(
                CreatorAgentTurn(
                    id=identifier,
                    thread_id=thread_id,
                    source_event_id=event.id,
                    session_id=session.id,
                    client_event_id=str(identifier),
                    request_digest="c" * 64,
                    status=status,
                    created_at=event.created_at,
                )
            )
            await db.flush()

        if failed_first:
            await add_turn(root, "original", sequence=2, status="failed")
        sequence = 3 if failed_first else 2
        if relation == "recovery" and accepted_recovery:
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    revision=sequence,
                    role="assistant",
                    event_type="assistant_response",
                    payload={
                        "choice_question": {
                            "question_id": "recovery-choice",
                            "conflict": "simplification_requires_choice",
                        }
                    },
                )
            )
            await db.flush()
            sequence += 1
        await add_turn(turn, relation, sequence=sequence, status="completed")
        db.add(
            CreatorAgentExecution(
                session_id=session.id,
                turn_id=turn,
                idempotency_key=str(turn),
                request_digest="d" * 64,
                expected_revision=1,
                tool_name="render.request",
                target_thread_id=thread_id,
                target_job_id=job_id,
                target_variant_id="original_text",
                status="succeeded",
            )
        )
        db.add(
            AgentRun(
                job_id=job_id,
                agent_name="fixture",
                prompt_version="1",
                model="fixture",
                outcome="ok",
                attempts=1,
                settled_cost_usd=0.25,
                cost_usd=0.25,
            )
        )
        await db.commit()
    await collect()
    return await first(client), turn


async def assess(client, case, turn, *, basis="original", verdict="met"):
    _, body = await outcome_body(client, case)
    body["outcome_review"].update(basis=basis, turn_id=str(turn))
    for row in body["outcome_review"]["outcomes"]:
        row["verdict"] = verdict
    response = await client.post(f"/admin/request-following/{case['id']}/review", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


async def report(client):
    return (
        await client.get(
            "/request-following-report", headers={"X-Report-Token": "report-read-only"}
        )
    ).json()


async def test_reviewed_original_has_request_denominator_versions_and_deduplicated_cost(
    output_journey,
):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey)
    await assess(client, case, turn)
    result = await report(client)
    data = result["request_outcomes"]
    assert data["requests"] == 1
    assert data["original_fulfilled"] == 1
    assert data["first_attempt"] == {"fulfilled": 1, "requests": 1, "unknown": 0}
    assert data["render_attempts"] == 1
    assert data["known_cost_usd"] == 0.25
    assert data["segments"][0]["client_build"] == "1763"
    assert data["segments"][0]["cohort"] == "internal"
    assert "PRIVATE-" not in str(result)


async def test_successful_recovery_keeps_failed_original_and_both_attempts(output_journey):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey, relation="recovery", failed_first=True)
    await assess(client, case, turn, basis="recovery")
    data = (await report(client))["request_outcomes"]
    assert data["requests"] == 1 and data["original_fulfilled"] == 0
    assert data["recovery_fulfilled"] == 1
    assert data["pre_job_failure_attempts"] == 1
    assert data["render_attempts"] == 1
    assert data["first_attempt"] == {"fulfilled": 0, "requests": 1, "unknown": 0}


async def test_assessment_history_survives_duplicate_review_and_superseding_capture(output_journey):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey)
    saved, body = await assess(client, case, turn, verdict="missed")
    assert len(saved["review"]["outcome_history"]) == 1
    body["review_version"] = saved["review_version"]
    duplicate = await client.post(f"/admin/request-following/{case['id']}/review", json=body)
    assert duplicate.status_code == 200, duplicate.text
    assert len(duplicate.json()["review"]["outcome_history"]) == 1
    async with AsyncSessionLocal() as db:
        event = CreationThreadEvent(
            thread_id=uuid.UUID(case["thread_id"]),
            sequence=4,
            revision=4,
            role="user",
            event_type="user_message",
            content="PRIVATE-FOLLOWUP",
        )
        db.add(event)
        await db.commit()
    await collect()
    async with AsyncSessionLocal() as db:
        assert (await db.get(RequestFollowingCase, uuid.UUID(case["id"]))).superseded
    data = (await report(client))["request_outcomes"]
    assert data["requests"] == 1 and data["original_unfulfilled"] == 1
    assert data["assessments"] == 1
    assert data["unfulfilled_assessments"] == 1


async def test_legacy_export_cannot_be_reviewed_as_a_known_original(output_journey):
    client, case, *_ = output_journey
    _, body = await outcome_body(client, case)
    body["outcome_review"].update(basis="original", turn_id=str(uuid.uuid4()))
    response = await client.post(f"/admin/request-following/{case['id']}/review", json=body)
    assert response.status_code == 422
    assert (await report(client))["request_outcomes"]["requests"] == 0


async def test_revision_success_is_not_original_success_and_revocation_hides_history(
    output_journey,
):
    client, _, _, grant, _ = output_journey
    case, turn = await linked_case(output_journey, relation="revision", failed_first=True)
    await assess(client, case, turn, basis="current_revision")
    result = (await report(client))["request_outcomes"]
    assert result["original_fulfilled"] == 0 and result["revision_fulfilled"] == 1
    async with AsyncSessionLocal() as db:
        row = await db.get(InternalAccountGrant, grant)
        row.status, row.revoked_at = "revoked", datetime.now(UTC)
        await db.commit()
    result = (await report(client))["request_outcomes"]
    assert result["requests"] == 0 and result["assessments"] == 0


async def test_duplicate_fulfilled_assessment_and_recapture_do_not_double_count(output_journey):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey)
    saved, body = await assess(client, case, turn)
    body["review_version"] = saved["review_version"]
    response = await client.post(f"/admin/request-following/{case['id']}/review", json=body)
    assert response.status_code == 200, response.text
    await collect()
    data = (await report(client))["request_outcomes"]
    assert data["requests"] == data["original_fulfilled"] == data["assessments"] == 1
    assert data["known_cost_usd"] == 0.25


async def test_later_native_attempt_makes_first_attempt_unknown(output_journey):
    from app.services.device_render import DEVICE_RENDER_FIELD

    client, _, _, _, job_id = output_journey
    case, turn = await linked_case(output_journey)
    await assess(client, case, turn)
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        job.assembly_plan = {
            **job.assembly_plan,
            DEVICE_RENDER_FIELD: {
                "original_text": {"attempts": {"one": {}, "two": {}}},
            },
        }
        await db.commit()
    await collect()
    data = (await report(client))["request_outcomes"]
    assert data["original_fulfilled"] == 1
    assert data["device_export_attempts"] == 2
    assert data["first_attempt"] == {"fulfilled": 0, "requests": 1, "unknown": 1}


async def test_a_ready_retry_does_not_erase_the_same_jobs_failed_attempt(output_journey):
    client, _, _, _, job_id = output_journey
    case, turn = await linked_case(output_journey)
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        job.status = "processing_failed"
        await db.commit()
    await collect()
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        job.status = "variants_ready"
        first_execution = await db.scalar(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.target_job_id == job_id,
            )
        )
        db.add(
            CreatorAgentExecution(
                session_id=first_execution.session_id,
                turn_id=turn,
                idempotency_key=str(uuid.uuid4()),
                request_digest="f" * 64,
                expected_revision=1,
                tool_name="render.request",
                target_job_id=job_id,
                target_thread_id=uuid.UUID(case["thread_id"]),
                target_variant_id="original_text",
                status="succeeded",
            )
        )
        await db.commit()
    await collect()
    await assess(client, await first(client), turn)
    data = (await report(client))["request_outcomes"]
    assert data["original_fulfilled"] == 1
    assert data["failed_render_jobs"] == 1
    assert data["render_attempts"] == 2


async def test_client_declared_recovery_is_not_an_accepted_recovery(output_journey):
    client, *_ = output_journey
    case, turn = await linked_case(
        output_journey, relation="recovery", failed_first=True, accepted_recovery=False
    )
    url, body = await outcome_body(client, case)
    body["outcome_review"].update(basis="recovery", turn_id=str(turn))
    for row in body["outcome_review"]["outcomes"]:
        row["verdict"] = "met"
    response = await client.post(url + "/review", json=body)
    assert response.status_code == 422, response.text


async def test_rejecting_the_intent_review_preserves_history_without_counting_fulfillment(
    output_journey,
):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey)
    saved, body = await assess(client, case, turn)
    body.update(review_version=saved["review_version"], decision="rejected", outcome_review=None)
    response = await client.post(f"/admin/request-following/{case['id']}/review", json=body)
    assert response.status_code == 200, response.text
    assert len(response.json()["review"]["outcome_history"]) == 1
    result = (await report(client))["request_outcomes"]
    assert result["original_fulfilled"] == 0 and result["original_unknown"] == 1


async def test_a_server_marked_duplicate_does_not_count_as_correction_effort(output_journey):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey)
    copy_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        source = await db.scalar(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == uuid.UUID(case["thread_id"]),
                CreationThreadEvent.payload["turn_id"].astext == str(turn),
            )
        )
        event = CreationThreadEvent(
            thread_id=source.thread_id,
            sequence=3,
            revision=3,
            role="user",
            event_type="user_message",
            content="PRIVATE-LINKED-REQUEST",
            payload={
                "turn_id": str(copy_id),
                "duplicate_of": str(turn),
                "request_outcome_link": {
                    **source.payload["request_outcome_link"],
                    "relation": "revision",
                    "parent_turn_id": str(turn),
                    "intent_revision_turn_id": str(copy_id),
                },
            },
        )
        db.add(event)
        await db.flush()
        db.add(
            CreatorAgentTurn(
                id=copy_id,
                thread_id=event.thread_id,
                source_event_id=event.id,
                client_event_id=str(copy_id),
                request_digest="d" * 64,
                status="completed",
            )
        )
        await db.commit()
    await collect()
    result = (await report(client))["request_outcomes"]
    assert result["requests"] == 1 and result["corrections"] == 0


async def test_unowned_job_marks_capture_incomplete_without_capturing_foreign_cost(output_journey):
    client, *_ = output_journey
    case, turn = await linked_case(output_journey)
    foreign, foreign_job, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as db:
        original = await db.scalar(
            select(CreatorAgentExecution).where(CreatorAgentExecution.turn_id == turn)
        )
        db.add(User(id=foreign, email=f"{foreign}@outcome.test"))
        await db.flush()
        db.add(
            Job(
                id=foreign_job,
                user_id=foreign,
                raw_storage_path="synthetic",
                assembly_plan={"private_marker": "FOREIGN-JOB-MUST-NOT-BE-CAPTURED"},
            )
        )
        await db.flush()
        db.add(
            CreatorAgentExecution(
                id=execution_id,
                session_id=original.session_id,
                turn_id=turn,
                idempotency_key=str(execution_id),
                request_digest="e" * 64,
                expected_revision=1,
                tool_name="render.request",
                target_thread_id=uuid.UUID(case["thread_id"]),
                target_job_id=foreign_job,
                status="failed",
            )
        )
        db.add(
            AgentRun(
                job_id=foreign_job,
                agent_name="foreign-fixture",
                prompt_version="1",
                model="fixture",
                outcome="ok",
                attempts=1,
                settled_cost_usd=99.0,
                cost_usd=99.0,
            )
        )
        await db.commit()
    try:
        await collect()
        case = await first(client)
        _, body = await outcome_body(client, case)
        body["outcome_review"].update(basis="original", turn_id=str(turn))
        approval = await client.post(f"/admin/request-following/{case['id']}/review", json=body)
        detail = await client.get(f"/admin/request-following/{case['id']}")
        data = (await report(client))["request_outcomes"]
        assert "FOREIGN-JOB-MUST-NOT-BE-CAPTURED" not in detail.text
        assert data["known_cost_usd"] == 0.25 and data["cost_runs"] == 1
        assert (
            approval.status_code,
            detail.json()["capture"]["missing"].get("jobs"),
            data["first_attempt"],
        ) == (
            422,
            "missing_or_unowned_jobs",
            {"fulfilled": 0, "requests": 1, "unknown": 1},
        )
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(CreatorAgentExecution).where(CreatorAgentExecution.id == execution_id)
            )
            await db.execute(delete(AgentRun).where(AgentRun.job_id == foreign_job))
            await db.execute(delete(Job).where(Job.id == foreign_job))
            await db.execute(delete(User).where(User.id == foreign))
            await db.commit()


async def test_owned_job_with_foreign_item_cannot_capture_that_items_cost(
    output_journey, monkeypatch
):
    client, *_ = output_journey
    case, _ = await linked_case(output_journey)
    job_id = output_journey[-1]
    foreign, foreign_plan, foreign_item = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(settings, "creation_thread_degraded_projection_enabled", True)
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        original_item = job.content_plan_item_id
        db.add(User(id=foreign, email=f"{foreign}@outcome.test"))
        await db.flush()
        persona = Persona(user_id=foreign)
        db.add(persona)
        await db.flush()
        db.add(ContentPlan(id=foreign_plan, user_id=foreign, persona_id=persona.id))
        await db.flush()
        db.add(
            PlanItem(
                id=foreign_item,
                content_plan_id=foreign_plan,
                idea="FOREIGN-ITEM-MUST-NOT-BE-CAPTURED",
                position=0,
            )
        )
        await db.flush()
        db.add(
            AgentRun(
                plan_item_id=foreign_item,
                agent_name="foreign-fixture",
                prompt_version="1",
                model="fixture",
                outcome="ok",
                attempts=1,
                settled_cost_usd=99.0,
                cost_usd=99.0,
            )
        )
        job.content_plan_item_id = foreign_item
        await db.commit()
    try:
        await collect()
        case = await first(client)
        detail = await client.get(f"/admin/request-following/{case['id']}")
        assert "FOREIGN-ITEM-MUST-NOT-BE-CAPTURED" not in detail.text
        assert detail.json()["capture"]["missing"]["cost"] == "missing_or_unowned_plan_items"
        data = (await report(client))["request_outcomes"]
        assert data["known_cost_usd"] == 0.25 and data["cost_runs"] == 1
    finally:
        async with AsyncSessionLocal() as db:
            job = await db.get(Job, job_id)
            job.content_plan_item_id = original_item
            await db.flush()
            await db.execute(delete(AgentRun).where(AgentRun.plan_item_id == foreign_item))
            await db.execute(delete(ContentPlan).where(ContentPlan.id == foreign_plan))
            await db.execute(delete(User).where(User.id == foreign))
            await db.commit()
