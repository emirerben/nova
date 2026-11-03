"""Real HTTP approval and dispatch; native recipe/publication below are synthetic fixtures."""

import asyncio
import copy
import uuid

import pytest

from app.config import settings
from app.database import sync_session
from app.kria.device_render import make_device_request
from app.kria.initial_render_review_authority import resolve_initial_render_authority
from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest
from app.kria.render_review_context import ReviewRows
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    CreatorPreparedExecution,
    Job,
    PlanItem,
)
from app.services.device_render import (
    device_record,
    device_status,
    pin_device_request,
    save_device_record,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks.kria_runtime import _observe_dispatched_execution
from tests.kria.test_approval_readiness_postgres import readiness_flags  # noqa: F401
from tests.kria.test_prepared_execution_postgres import _approved, _dispatch, broker  # noqa: F401


@pytest.fixture
async def initial_publication(monkeypatch, broker):  # noqa: F811
    monkeypatch.setattr(settings, "kria_prepared_execution_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_user_ids", [])
    user, thread_id, approval = await _approved(
        monkeypatch,
        phone=True,
        no_title=True,
        updates_override=[
            {
                "kind": "timing",
                "scope": "global",
                "description": "6 seconds",
                "facts": {"duration_s": 6},
            }
        ],
        message="Make a six second video from my clip, without a title",
    )
    response = await asyncio.to_thread(_dispatch, approval.id)
    assert response["status"] == "dispatched", response
    with sync_session() as db:
        e = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        job = db.get(Job, e.target_job_id)
        assert e.target_variant_id is None and e.target_generation_id is None
        source = PhoneSourceBinding.model_validate(job.assembly_plan[PHONE_SOURCES_FIELD][0])
        recipe = EditRecipeV2(
            asset_manifest=RenderAssetManifest(assets=(source.render_asset(),)),
            assets=[
                MediaAsset(
                    id=source.media_id,
                    relative_path=source.media_id,
                    fingerprint=AssetFingerprint(
                        hex=source.original.sha256, byte_count=source.original.byte_count
                    ),
                    duration=source.original.duration_s,
                )
            ],
            tracks=[
                TimelineTrack(
                    id="video",
                    kind="video",
                    clips=[
                        TimelineClip(
                            id="clip",
                            source_asset_id=source.media_id,
                            source_start=0,
                            source_duration=6,
                            timeline_start=0,
                            rate=1,
                        )
                    ],
                )
            ],
        )
        generation = job.assembly_plan["creator_generation_id"]
        job.assembly_plan = {
            **job.assembly_plan,
            "variants": [
                {
                    "variant_id": "initial",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": generation,
                    "duration_s": 6,
                }
            ],
        }
        pin_device_request(
            job,
            make_device_request(job_id=job.id, variant_id="initial", revision=1, recipe=recipe),
            base_generation=generation,
        )
        record = copy.deepcopy(device_record(job, "initial"))
        publication = "synthetic-local-publication"
        status = device_status(job, "initial").model_copy(
            update={"phase": "published", "published_generation": publication}
        )
        record.update(
            status=status.model_dump(mode="json"),
            published_attempt=publication,
            base_generation=publication,
        )
        save_device_record(job, "initial", record)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0].update(
            render_status="ready",
            render_generation_id=publication,
            video_path="synthetic-local.mp4",
        )
        job.assembly_plan = assembly
        job.status = "variants_ready"
        db.commit()
        execution_id = e.id
    assert (await asyncio.to_thread(_observe_dispatched_execution, execution_id))[0] == "completed"
    return execution_id


def rows_for(db, execution_id):
    e = db.get(CreatorAgentExecution, execution_id)
    a = db.get(CreatorAgentApproval, uuid.UUID(e.result["approval_id"]))
    t = db.get(CreationThread, e.target_thread_id)
    i = db.get(PlanItem, t.active_plan_item_id)
    return ReviewRows(
        plan=db.get(ContentPlan, i.content_plan_id),
        item=i,
        job=db.get(Job, e.target_job_id),
        thread=t,
        session=db.get(CreatorAgentSession, e.session_id),
        turn=db.get(CreatorAgentTurn, e.turn_id),
        draft=db.get(CreatorEditDraft, e.target_draft_id),
        approval=a,
        execution=e,
        prepared=db.get(CreatorPreparedExecution, a.prepared_execution_id),
    )


@pytest.mark.asyncio
async def test_actual_initial_approval_dispatch_observer_lineage(initial_publication):
    with sync_session() as db:
        rows = rows_for(db, initial_publication)
        pins = resolve_initial_render_authority(rows)
        assert pins.published_attempt == "synthetic-local-publication"
        assert pins.requirement_generation != pins.published_attempt
        assert pins.requirement_generation == rows.job.assembly_plan["creator_generation_id"]
        assert rows.approval.target_job_id is None
        assert rows.approval.target_variant_id is None
        assert pins.prepared_id == str(rows.prepared.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "job_receipt",
        "attempt",
        "generation",
        "published",
        "source",
        "source_trim",
        "request",
        "base",
        "draft",
        "missing_prepared",
    ],
)
async def test_initial_authority_rejects_changed_lineage(initial_publication, mutation):
    with sync_session() as db:
        rows = rows_for(db, initial_publication)
        if mutation == "job_receipt":
            rows.execution.result = {**rows.execution.result, "job_id": str(uuid.uuid4())}
        elif mutation == "attempt":
            result = copy.deepcopy(rows.execution.result)
            result["prepared_execution"]["attempt_id"] = "other"
            rows.execution.result = result
        elif mutation == "generation":
            rows.job.assembly_plan = {**rows.job.assembly_plan, "creator_generation_id": "other"}
        elif mutation == "published":
            rows.execution.target_generation_id = "other"
        elif mutation == "source":
            assignments = copy.deepcopy(rows.item.clip_assignments)
            assignments[0]["storage_generation"] = "other"
            rows.item.clip_assignments = assignments
        elif mutation == "source_trim":
            assignments = copy.deepcopy(rows.item.clip_assignments)
            assignments[0]["trim_start_s"] = 2
            rows.item.clip_assignments = assignments
        elif mutation == "request":
            rows.execution.result = {
                **rows.execution.result,
                "creator_request": "latest unrelated chat",
            }
        elif mutation == "base":
            assembly = copy.deepcopy(rows.job.assembly_plan)
            assembly["_device_render_v1"]["initial"]["base_generation"] = "other"
            rows.job.assembly_plan = assembly
        elif mutation == "draft":
            rows.draft.snapshot_hash = "other"
        else:
            rows.approval.prepared_execution_id = None
        with pytest.raises(ValueError):
            resolve_initial_render_authority(rows)
