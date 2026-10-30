"""Real runtime draft, approval, prepared snapshot and native preparation.

Publication/byte attestation is a supplied synthetic verifier receipt; no native
export or storage verifier is replaced by a claimed end-to-end success.
"""

import asyncio
import copy
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.device_render import make_device_request
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.kria.render_review_context import VerifiedArtifactReceipt, capture_render_review_context
from app.kria.runtime import approval_fingerprint, decide_approval
from app.models import (
    CreationThread,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorEditDraft,
    CreatorPreparedExecution,
    Job,
)
from app.services.creator_render_contract import CONTRACT_FIELD, build_render_contract
from app.services.device_render import (
    DEVICE_RENDER_FIELD,
    device_record,
    device_status,
    pin_device_request,
    save_device_record,
)
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_editor_state_turns_postgres import _seed_job, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.routes.test_phone_editor_commit import phone_job


@pytest.mark.asyncio
async def test_capture_real_approved_native_editor_receipt(monkeypatch):
    user, thread, session = _seed_runtime_project()
    job_id = _seed_job(user, session)
    template = phone_job(monkeypatch)
    recipe = device_status(template, "guided_story").request.recipe
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_prepared_execution_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda *_: None)
    with sync_session() as db:
        job = db.get(Job, job_id)
        assembly = copy.deepcopy(template.assembly_plan)
        assembly.pop(DEVICE_RENDER_FIELD)
        variant = assembly["variants"][0]
        variant.update(render_generation_id="generation-1", render_status="ready")
        initial_brief = CreativeBrief(
            version=1,
            requirements=[
                BriefRequirement(
                    id="r1",
                    kind="text",
                    scope="title",
                    literal="Before",
                    description="Keep exact title",
                )
            ],
        )
        assembly[CONTRACT_FIELD] = build_render_contract(
            {}, generation_id="generation-1", brief=initial_brief
        ).model_dump(mode="json")
        assembly["creator_brief_binding"] = BriefBinding.create(
            thread, initial_brief, latest_message="Keep Before"
        ).model_dump(mode="json")
        job.assembly_plan = assembly
        pin_device_request(
            job,
            make_device_request(
                job_id=job_id, variant_id="guided_story", revision=1, recipe=recipe
            ),
            base_generation="generation-1",
        )
        s = db.get(CreatorAgentSession, session)
        s.target_variant_id = "guided_story"
        requirement = BriefRequirement(
            id="r1", kind="text", scope="title", literal="Before", description="Keep exact title"
        )
        db.add(
            CreativeBriefVersion(
                thread_id=thread, version=1, requirements=[requirement.model_dump(mode="json")]
            )
        )
        db.commit()

    async def plan(*args, **kwargs):
        return PlannedKriaTurn(
            plan=adapt_editor_action(
                reply="Keep the words and make title red",
                request_render=True,
                ops=[{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}],
            ),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", plan)
    accepted = await _submit(user, thread, "Keep the exact title Before and make it red")
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "awaiting_approval", result
    with sync_session() as db:
        approval = db.scalar(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        )
        revision = db.get(CreationThread, thread).revision
        fingerprint = approval_fingerprint(approval)
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread,
            approval_id=approval.id,
            creator_id=user,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=revision,
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=fingerprint,
            ),
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval.id)
    assert claim is not None
    with sync_session() as db:
        job = db.get(Job, job_id)
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        approved = db.get(CreatorAgentApproval, approval.id)
        assert approved.prepared_execution_id is not None
        prepared = db.get(CreatorPreparedExecution, approved.prepared_execution_id)
        assert (
            prepared.snapshot["draft_snapshot_hash"]
            == db.get(CreatorEditDraft, approval.draft_id).snapshot_hash
        )
        record = device_record(job, "guided_story")
        assert execution.result["editor_prep"]["device_recipe_revision"] == 2
        assert record["status"]["request"]["recipe"]["text_layers"]
        record.update(
            published_attempt="synthetic-publication", base_generation="synthetic-publication"
        )
        record["status"]["phase"] = "published"
        record["attempts"]["synthetic-publication"] = {
            "path": "renders/verified.mp4",
            "size": 100,
            "sha256": "a" * 64,
        }
        save_device_record(job, "guided_story", record)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0].update(
            render_generation_id="synthetic-publication",
            render_status="ready",
            video_path="renders/verified.mp4",
        )
        job.assembly_plan = assembly
        job.status = "variants_ready"
        execution.status = "dispatched"
        proof = VerifiedArtifactReceipt(
            verification_id="synthetic-server-verifier",
            execution_id=str(execution.id),
            job_id=str(job_id),
            variant_id="guided_story",
            published_attempt="synthetic-publication",
            object_path="renders/verified.mp4",
            object_version="1",
            sha256="a" * 64,
            byte_count=100,
            recipe_digest=record["recipe_digest"],
        )
        db.commit()
        original = copy.deepcopy(job.assembly_plan)
        execution_before = copy.deepcopy(execution.result)
    captured = await asyncio.to_thread(
        capture_render_review_context, claim.execution_id, verified_artifact=proof
    )
    assert captured.status == "ready", captured.reason
    assert "Keep the exact title Before" in captured.packet.original_request
    assert captured.packet.draft_id == str(approval.draft_id)
    assert captured.packet.requirement_generation == claim.target_generation_id
    assert captured.packet.published_attempt == "synthetic-publication"
    assert captured.packet.review_model_received_visual_input is False
    refused = await asyncio.to_thread(
        capture_render_review_context,
        claim.execution_id,
        verified_artifact=proof.model_copy(update={"sha256": "b" * 64}),
    )
    assert refused.status == "unavailable"
    with sync_session() as db:
        assert db.get(Job, job_id).assembly_plan == original
        assert db.get(CreatorAgentExecution, claim.execution_id).result == execution_before
        assert (
            db.get(CreatorEditDraft, approval.draft_id).snapshot_hash == captured.packet.draft_hash
        )
