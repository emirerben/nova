"""KRI-651: preparation failures and a repaired render settle through the real observer."""

import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentExecution,
    CreatorAgentSession,
    Job,
    PlanItem,
)
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.creation_text_composition import compose_creation_text
from app.services.device_render import device_record, pin_device_request, save_device_record
from app.services.phone_sources import PhoneSourceBinding
from app.tasks.kria_runtime import _observe_dispatched_execution
from tests.evals.request_following.test_approved_hook_journey import CASE, CTX, approved_plan
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _seed_user_turn


@pytest.mark.parametrize("language", ["en", "tr"])
def test_failure_then_retry_preserves_hook_and_observer_waits_for_device_publication(
    monkeypatch, language
):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    user_id, thread_id, session_id = _seed_runtime_project()
    plan, brief, clip = approved_plan()
    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=brief.version,
                requirements=[r.model_dump(mode="json") for r in brief.requirements],
            )
        )
        db.commit()
    plan.snapshot = compose_creation_text(
        plan.snapshot, creator_request=CASE["context"], text_requirements=brief.live(), ctx=CTX
    )
    execution_plan = GuidedStoryExecutionPlan.model_validate(
        compile_proposal_execution_plan(plan.snapshot)
    )
    binding = PhoneSourceBinding(
        media_id=clip.media_id,
        proxy_path=clip.proxy_path,
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=clip.duration_s,
            width=1920,
            height=1080,
            orientation_degrees=90,
            has_audio=True,
        ),
    )
    recipe = compile_phone_guided_plan(execution_plan, (binding,))
    for attempt, job_status in enumerate(["processing_failed", "awaiting_device"]):
        turn_id = _seed_user_turn(
            thread_id,
            session_id,
            content="Generate" if attempt == 0 else "Try generating this edit again",
            status="observing",
        )
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id)
            thread.state = {**thread.state, "reply_language": language}
            session = db.get(CreatorAgentSession, session_id)
            generation = f"approved-hook-{attempt}"
            montage_record = {**plan.record(), "generation_id": generation}
            montage_record["requirement_receipts"] = [
                {
                    **r.model_dump(mode="json"),
                    "brief_version": brief.version,
                    "generation_id": generation,
                }
                for r in build_receipts(
                    brief.live(),
                    plan_facts_from_unified_montage(montage_record),
                    include_unchecked=True,
                )
            ]
            job = Job(
                user_id=user_id,
                status=job_status,
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=session.plan_item_id,
                content_plan_ownership_epoch=0,
                failure_reason="creation_text_failed" if attempt == 0 else None,
                error_detail="PRIVATE MODEL RESPONSE" if attempt == 0 else None,
                assembly_plan={
                    "creator_generation_id": generation,
                    "creator_brief_binding": BriefBinding.create(
                        thread_id, brief, latest_message="Generate"
                    ).model_dump(mode="json"),
                    "unified_montage": montage_record,
                    "variants": [
                        {
                            "variant_id": "guided_story",
                            "render_status": "awaiting_device",
                            "render_destination": "device",
                            "render_generation_id": generation,
                        }
                    ],
                },
            )
            db.add(job)
            db.flush()
            if attempt:
                pin_device_request(
                    job,
                    make_device_request(
                        job_id=job.id, variant_id="guided_story", revision=1, recipe=recipe
                    ),
                    base_generation=generation,
                )
            session.target_job_id = job.id
            session.status = "rendering"
            db.get(PlanItem, session.plan_item_id).current_job_id = job.id
            execution = CreatorAgentExecution(
                session_id=session_id,
                turn_id=turn_id,
                idempotency_key=uuid.uuid4().hex,
                request_digest=uuid.uuid4().hex,
                expected_revision=thread.revision,
                tool_name="render.request",
                tool_version=1,
                risk="approval_required",
                target_thread_id=thread_id,
                target_job_id=job.id,
                target_ownership_epoch=0,
                status="dispatched",
                result={"job_id": str(job.id)},
            )
            db.add(execution)
            db.commit()
            execution_id, job_id = execution.id, job.id
        result, _ = _observe_dispatched_execution(execution_id)
        if attempt == 0:
            assert result == "failed"
            with sync_session() as db:
                event = db.execute(
                    select(CreationThreadEvent).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_render_failed",
                    )
                ).scalar_one()
                assert "iPhone" not in event.content and "PRIVATE" not in event.content
                assert ("wording" if language == "en" else "yazı") in event.content
                assert event.payload["code"] == "creation_text_failed"
                assert event.payload["recovery"] == "retry"
            continue
        assert result == "pending"
        # This models a verified phone completion receipt, not an actual native export.
        # The paired XCTest independently performs the export and inspects its pixels.
        with sync_session() as db:
            job = db.get(Job, job_id)
            record = device_record(job, "guided_story")
            record["status"]["phase"] = "published"
            record.update(published_attempt="device-attempt", base_generation="device-attempt")
            record["requirement_generation"] = generation
            save_device_record(job, "guided_story", record)
            job.assembly_plan = {
                **job.assembly_plan,
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "ok": True,
                        "render_status": "ready",
                        "render_destination": "device",
                        "render_generation_id": "device-attempt",
                        "output_url": "https://example.test/approved-hook.mp4",
                    }
                ],
            }
            job.status = "variants_ready"
            db.commit()
        assert _observe_dispatched_execution(execution_id)[0] == "completed"
        with sync_session() as db:
            assert db.get(CreatorAgentExecution, execution_id).status == "completed"
            review = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_review",
                )
            ).scalar_one()
            assert review.payload["requirement_receipts"][0]["status"] == "met"
            assert (
                db.get(Job, job_id).assembly_plan["unified_montage"]["title"] == CASE["candidate"]
            )
