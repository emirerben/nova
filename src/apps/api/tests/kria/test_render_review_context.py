"""Failure matrix: exact approved authority, publication, verified bytes, and evidence source.

Real domain models and native recipe pinning; no provider/native-export claim.
"""

import uuid
from pathlib import Path

import pytest

from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.device_render import make_device_request
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.recipes import EditRecipeV1
from app.kria.render_review_context import (
    ReviewRows,
    TimestampedObservation,
    VerifiedArtifactReceipt,
    build_render_review_context,
)
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.device_render import device_record, pin_device_request, save_device_record


@pytest.fixture
def rows():
    owner, tid, sid, itemid, jid, did, eid, aid, pid, turnid = [uuid.uuid4() for _ in range(10)]
    binding = BriefBinding.create(
        tid,
        CreativeBrief(version=1),
        full_creator_request="Keep EXACT 29 Ekim words, music and selected clips.",
    )
    snapshot, digest = canonical_snapshot(
        KriaDraftDocument(
            kind="editor",
            editor_payload={"text_elements": [{"text": "29 Ekim"}]},
            brief_binding=binding,
        )
    )
    job = Job(
        id=jid,
        user_id=owner,
        status="variants_ready",
        content_plan_item_id=itemid,
        content_plan_ownership_epoch=0,
        assembly_plan={"variants": [{"variant_id": "first", "render_generation_id": "approved"}]},
    )
    recipe = EditRecipeV1.model_validate_json(
        (Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v1.json").read_text()
    )
    request = make_device_request(job_id=jid, variant_id="first", revision=1, recipe=recipe)
    pin_device_request(job, request, base_generation="approved")
    record = device_record(job, "first")
    record.update(
        published_attempt="published",
        base_generation="published",
        requirement_generation="approved",
    )
    from app.services.creator_render_contract import build_render_contract

    contract = build_render_contract({}, generation_id="approved", brief=binding.brief)
    record.update(
        recipe_digest=request.identity.recipe_digest,
        contract_snapshot=contract.model_dump(mode="json"),
        contract_digest=contract.digest,
        attempts={"published": {"path": "renders/published.mp4", "size": 100, "sha256": "a" * 64}},
    )
    record["status"]["phase"] = "published"
    save_device_record(job, "first", record)
    job.assembly_plan["variants"][0].update(
        render_generation_id="published", render_status="ready", video_path="renders/published.mp4"
    )
    return ReviewRows(
        plan=ContentPlan(id=pid, user_id=owner, ownership_epoch=0),
        item=PlanItem(id=itemid, content_plan_id=pid, current_job_id=jid),
        job=job,
        thread=CreationThread(
            id=tid,
            creator_id=owner,
            runtime_version=2,
            active_plan_item_id=itemid,
            active_creator_agent_session_id=sid,
        ),
        session=CreatorAgentSession(
            id=sid, creator_id=owner, plan_item_id=itemid, target_job_id=jid, ownership_epoch=0
        ),
        turn=CreatorAgentTurn(id=turnid, thread_id=tid, session_id=sid),
        draft=CreatorEditDraft(
            id=did,
            creator_id=owner,
            thread_id=tid,
            item_id=itemid,
            variant_key="first",
            base_job_id=jid,
            draft_revision=2,
            snapshot_json=snapshot,
            snapshot_hash=digest,
        ),
        approval=CreatorAgentApproval(
            id=aid,
            creator_id=owner,
            thread_id=tid,
            session_id=sid,
            turn_id=turnid,
            draft_id=did,
            draft_revision=2,
            target_job_id=jid,
            target_variant_id="first",
            target_ownership_epoch=0,
            status="approved",
            execution_ids=[str(eid)],
        ),
        execution=CreatorAgentExecution(
            id=eid,
            session_id=sid,
            turn_id=turnid,
            target_thread_id=tid,
            target_job_id=jid,
            target_draft_id=did,
            target_draft_revision=2,
            target_variant_id="first",
            target_generation_id="approved",
            target_ownership_epoch=0,
            status="completed",
            tool_name="render.request",
            result={
                "approval_id": str(aid),
                "brief_binding": binding.model_dump(mode="json"),
                "editor_prep": {"device_recipe_revision": 1},
            },
        ),
    )


def receipt(rows):
    return VerifiedArtifactReceipt(
        verification_id="storage-read-1",
        execution_id=str(rows.execution.id),
        job_id=str(rows.job.id),
        variant_id="first",
        published_attempt="published",
        object_path="renders/published.mp4",
        object_version="storage-generation-7",
        sha256="a" * 64,
        byte_count=100,
        recipe_digest=device_record(rows.job, "first")["recipe_digest"],
    )


def test_exact_authority_detached_and_third_party_is_not_direct_visual(rows):
    proof = receipt(rows)
    observation = TimestampedObservation(
        evidence_id="gemini-1",
        artifact_sha256=proof.sha256,
        start_s=0,
        end_s=1,
        source="provider_observation",
        observer="gemini",
        observation="Title may overlap",
    )
    captured = build_render_review_context(
        rows, verified_artifact=proof, observations=(observation,)
    )
    assert captured.status == "ready"
    packet = captured.packet
    assert packet.original_request == "Keep EXACT 29 Ekim words, music and selected clips."
    assert packet.evidence_coverage == ("provider_observation",)
    assert packet.review_model_received_visual_input is False
    frozen = packet.model_dump_json()
    rows.draft.snapshot_json["editor_payload"]["text_elements"][0]["text"] = "Changed"
    assert packet.model_dump_json() == frozen


@pytest.mark.parametrize(
    "change", ["generation", "binding", "draft", "revision", "recipe", "attempt", "owner", "epoch"]
)
def test_stale_or_tampered_authority_refuses(rows, change):
    proof = receipt(rows)
    if change == "generation":
        rows.job.assembly_plan["variants"][0]["render_generation_id"] = "new"
    elif change == "binding":
        rows.execution.result["brief_binding"]["creator_request"] = "Different"
    elif change == "draft":
        rows.draft.snapshot_json["intent"] = "Different"
    elif change == "revision":
        rows.execution.target_draft_revision = 3
    elif change == "recipe":
        rows.job.assembly_plan["_device_render_v1"]["first"]["recipe_digest"] = "b" * 64
    elif change == "attempt":
        rows.job.assembly_plan["_device_render_v1"]["first"]["published_attempt"] = "other"
    elif change == "owner":
        rows.job.user_id = uuid.uuid4()
    else:
        rows.job.content_plan_ownership_epoch = 1
    assert build_render_review_context(rows, verified_artifact=proof).status == "unavailable"


def test_client_hash_is_not_verified_artifact(rows):
    rows.job.assembly_plan["_device_render_v1"]["first"]["attempts"] = {
        "published": {"sha256": "a" * 64}
    }
    result = build_render_review_context(rows, verified_artifact=None)
    assert result.reason == "artifact_verification_missing"


def test_wrong_verified_artifact_refuses(rows):
    wrong = receipt(rows).model_copy(update={"object_path": "other.mp4"})
    assert build_render_review_context(rows, verified_artifact=wrong).status == "unavailable"


@pytest.mark.parametrize("missing", ["request", "binding", "plan", "contract", "recipe_revision"])
def test_missing_required_context_refuses(rows, missing):
    proof = receipt(rows)
    if missing == "request":
        binding = BriefBinding.create(
            rows.thread.id, CreativeBrief(version=1), full_creator_request=""
        )
        rows.execution.result["brief_binding"] = binding.model_dump(mode="json")
    elif missing == "binding":
        rows.execution.result.pop("brief_binding")
    elif missing == "plan":
        rows.draft.snapshot_json = None
    elif missing == "contract":
        rows.job.assembly_plan["_device_render_v1"]["first"].pop("contract_snapshot")
    else:
        rows.execution.result["editor_prep"] = {}
    assert build_render_review_context(rows, verified_artifact=proof).status == "unavailable"


def test_observations_for_other_bytes_refused(rows):
    observation = TimestampedObservation(
        evidence_id="other",
        artifact_sha256="b" * 64,
        start_s=0,
        end_s=1,
        source="provider_observation",
        observer="gemini",
        observation="Other video",
    )
    assert (
        build_render_review_context(
            rows, verified_artifact=receipt(rows), observations=(observation,)
        ).status
        == "unavailable"
    )


def test_available_frame_evidence_does_not_mean_review_model_received_frames(rows):
    observation = TimestampedObservation(
        evidence_id="frame",
        artifact_sha256="a" * 64,
        start_s=0,
        end_s=0,
        source="direct_frame",
        observer="native-verifier",
        observation="A verified frame observation",
    )
    result = build_render_review_context(
        rows, verified_artifact=receipt(rows), observations=(observation,)
    )
    assert result.packet.evidence_coverage == ("direct_frame",)
    assert result.packet.review_model_received_visual_input is False


def test_capture_result_requires_consistent_status():
    from pydantic import ValidationError

    from app.kria.render_review_context import CaptureResult

    with pytest.raises(ValidationError):
        CaptureResult(status="ready")
    with pytest.raises(ValidationError):
        CaptureResult(status="unavailable")


def test_dispatch_settings_preserve_actual_job_strategy_independently_of_approval(rows):
    import json

    rows.job.all_candidates = {
        "creator_strategy": {"target_duration_s": 3, "opening_title": "Keep words"}
    }
    packet = build_render_review_context(rows, verified_artifact=receipt(rows)).packet
    assert json.loads(packet.dispatch_settings_json) == {
        "target_duration_s": 3,
        "opening_title": "Keep words",
    }
    rows.job.all_candidates["creator_strategy"]["target_duration_s"] = 20
    assert json.loads(packet.dispatch_settings_json)["target_duration_s"] == 3
