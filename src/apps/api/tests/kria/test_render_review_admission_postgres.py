"""Real runtime draft, approval, prepared snapshot and native preparation.

Publication/byte attestation is a supplied synthetic verifier receipt; no native
export or storage verifier is replaced by a claimed end-to-end success.
"""

# ruff: noqa: F811

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
from app.kria.render_review_context import VerifiedArtifactReceipt
from app.kria.runtime import approval_fingerprint, decide_approval
from app.models import (
    CreationThread,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
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
from app.tasks.kria_runtime import (
    _claim_approval_dispatch,
    _observe_dispatched_execution,
    run_kria_turn,
)
from tests.kria.test_editor_state_turns_postgres import _seed_job, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.routes.test_phone_editor_commit import phone_job
from tests.services.test_render_review_evidence import local_video  # noqa: F401


@pytest.fixture
async def approved_publication(monkeypatch):
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
        db.get(CreatorAgentTurn, execution.turn_id).status = "observing"
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
    return claim, thread, job_id, proof


@pytest.mark.asyncio
async def test_observer_admits_without_get_and_lost_publish_is_recoverable(
    approved_publication, monkeypatch
):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.services.render_review_admission import reconcile_generation_reviews
    from app.tasks.render_review import prepare_generation_review

    claim, thread, job_id, proof = approved_publication
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    broker = Mock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr(prepare_generation_review, "apply_async", broker)
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[
        0
    ] == "completed"
    with sync_session() as db:
        rows = list(
            db.scalars(
                select(CreatorGenerationReview).where(
                    CreatorGenerationReview.execution_id == claim.execution_id
                )
            )
        )
        assert len(rows) == 1
        row_id = rows[0].id
        assert rows[0].status == "pending"
        assert db.get(Job, job_id).status == "variants_ready"
    broker.assert_called_once()
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[
        0
    ] == "ignored"
    broker.side_effect = None
    assert reconcile_generation_reviews() >= 1
    assert any(
        call.kwargs["task_id"] == f"generation-review:{row_id}" for call in broker.call_args_list
    )


async def admit_case(case, monkeypatch):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.tasks.render_review import prepare_generation_review

    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    monkeypatch.setattr(prepare_generation_review, "apply_async", Mock())
    assert (await asyncio.to_thread(_observe_dispatched_execution, case[0].execution_id))[
        0
    ] == "completed"
    with sync_session() as db:
        return db.scalar(
            select(CreatorGenerationReview.id).where(
                CreatorGenerationReview.execution_id == case[0].execution_id
            )
        )


@pytest.mark.asyncio
async def test_duplicate_claim_expired_recovery_and_attempt_cap(approved_publication, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.models import CreatorGenerationReview
    from app.services.render_review_admission import claim_generation_review

    rid = await admit_case(approved_publication, monkeypatch)
    first = claim_generation_review(rid)
    assert first is not None
    assert claim_generation_review(rid) is None
    for _ in range(2):
        with sync_session() as db:
            db.get(CreatorGenerationReview, rid).lease_until = datetime.now(UTC) - timedelta(
                seconds=1
            )
            db.commit()
        assert claim_generation_review(rid) is not None
    with sync_session() as db:
        db.get(CreatorGenerationReview, rid).lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert claim_generation_review(rid) is None
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, rid)
        assert row.status == "exhausted" and row.attempts == 3


@pytest.mark.asyncio
async def test_missing_proof_advisory_unavailable_keeps_video(approved_publication, monkeypatch):
    from app.models import CreatorGenerationReview
    from app.tasks.render_review import prepare_generation_review

    rid = await admit_case(approved_publication, monkeypatch)
    result = await asyncio.to_thread(prepare_generation_review.run, str(rid))
    assert result["status"] == "unavailable"
    with sync_session() as db:
        assert db.get(Job, approved_publication[2]).status == "variants_ready"
        assert db.get(CreatorGenerationReview, rid).result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race", [None, "generation", "ownership", "binding", "recipe", "dispatch_settings"]
)
async def test_real_storage_frames_final_fence_and_duplicate_delivery(
    approved_publication, monkeypatch, local_video, race
):
    import hashlib

    from app import storage
    from app.models import CreatorGenerationReview
    from app.services import render_review_evidence as evidence
    from app.services.render_review_admission import admit_generation_review
    from app.tasks.render_review import prepare_generation_review

    claim, thread, job_id, _ = approved_publication
    _, video, _ = local_video
    attempt_id = str(uuid.uuid4())
    with sync_session() as db:
        owner = db.get(Job, job_id).user_id
    path = f"{owner}/{job_id}/device/{attempt_id}.mp4"
    storage.upload_public_read(str(video), path)
    metadata = storage.object_metadata_once(path, timeout_s=2)
    sha = hashlib.sha256(video.read_bytes()).hexdigest()
    with sync_session() as db:
        job = db.get(Job, job_id)
        record = device_record(job, "guided_story")
        record.update(published_attempt=attempt_id, base_generation=attempt_id)
        identity = record["status"]["request"]["identity"]
        record["attempts"][attempt_id] = {
            "path": path,
            "sha256": sha,
            "size": metadata.size,
            "publication_proof": {
                "version": 1,
                "artifact_path": path,
                "storage_generation": metadata.generation,
                "file_size_bytes": metadata.size,
                "sha256": sha,
                "identity": identity,
                "source_receipts_digest": None,
            },
        }
        save_device_record(job, "guided_story", record)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0].update(render_generation_id=attempt_id, video_path=path)
        job.assembly_plan = assembly
        db.commit()
    rid = await admit_case(approved_publication, monkeypatch)
    with sync_session() as db:
        assert admit_generation_review(db, db.get(CreatorAgentExecution, claim.execution_id)) == rid
        db.commit()
    real = evidence.obtain_render_review_evidence
    calls = []

    def outside_locks(basis, **kwargs):
        with sync_session() as db:
            db.scalar(select(Job).where(Job.id == job_id).with_for_update(nowait=True))
            db.scalar(
                select(CreationThread)
                .where(CreationThread.id == thread)
                .with_for_update(nowait=True)
            )
        calls.append(basis)
        result = real(basis, **kwargs)
        assert result.status == "ready", result.reason
        if race:
            with sync_session() as db:
                job = db.get(Job, job_id)
                if race == "ownership":
                    job.content_plan_ownership_epoch += 1
                elif race == "dispatch_settings":
                    job.all_candidates = {
                        **(job.all_candidates or {}),
                        "creator_strategy": {"target_duration_s": 3},
                    }
                elif race == "binding":
                    execution = db.get(CreatorAgentExecution, claim.execution_id)
                    updated = copy.deepcopy(execution.result)
                    updated["brief_binding"]["creator_request"] = "Changed request"
                    execution.result = updated
                else:
                    assembly = copy.deepcopy(job.assembly_plan)
                    if race == "generation":
                        assembly["variants"][0]["render_generation_id"] = "later-generation"
                    else:
                        assembly[DEVICE_RENDER_FIELD]["guided_story"]["recipe_digest"] = "b" * 64
                    job.assembly_plan = assembly
                db.commit()
        return result

    monkeypatch.setattr(evidence, "obtain_render_review_evidence", outside_locks)
    assert (await asyncio.to_thread(prepare_generation_review.run, str(rid)))["status"] == (
        "stale" if race else "evidence_ready"
    )
    assert (await asyncio.to_thread(prepare_generation_review.run, str(rid)))["status"] == "ignored"
    assert len(calls) == 1
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, rid)
        assert db.get(Job, job_id).status == "variants_ready"
        if not race:
            assert row.result["packet"]["original_request"].endswith("make it red")
            bundle = evidence.EvidenceBundle.model_validate(row.result["evidence"])
            assert bundle.frames and bundle.frames[0].image_bytes.startswith(b"\xff\xd8")
            assert bundle.verified_artifact.sha256 == sha
            assert row.result["review_model_received_visual_input"] is False
        else:
            assert row.result is None


@pytest.mark.asyncio
async def test_flag_off_observer_has_no_outbox_or_broker(approved_publication, monkeypatch):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.tasks.render_review import prepare_generation_review

    monkeypatch.setattr(settings, "kria_intent_review_enabled", False)
    broker = Mock(side_effect=AssertionError("disabled"))
    monkeypatch.setattr(prepare_generation_review, "apply_async", broker)
    assert (
        await asyncio.to_thread(_observe_dispatched_execution, approved_publication[0].execution_id)
    )[0] == "completed"
    broker.assert_not_called()
    with sync_session() as db:
        assert (
            db.scalar(
                select(CreatorGenerationReview.id).where(
                    CreatorGenerationReview.execution_id == approved_publication[0].execution_id
                )
            )
            is None
        )


@pytest.mark.asyncio
async def test_legacy_queue_and_claim_yield_to_canonical_runtime(approved_publication, monkeypatch):
    from unittest.mock import Mock

    from app.services.creator_sessions import reconcile_render_state
    from app.tasks.creator_quality_review import claim_exact_review

    claim, thread, job_id, proof = approved_publication
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_quality_review_enabled", True)
    forbidden = Mock(side_effect=AssertionError("Canonical runtime must not queue Gemini"))
    monkeypatch.setattr("app.tasks.creator_quality_review.queue_creator_quality_review", forbidden)
    async with AsyncSessionLocal() as db:
        session = await db.get(CreatorAgentSession, claim.session_id)
        await reconcile_render_state(db, session)
    forbidden.assert_not_called()
    with sync_session() as db:
        assert (
            claim_exact_review(
                db,
                session_id=str(claim.session_id),
                job_id=str(job_id),
                variant_id="guided_story",
                generation_id="synthetic-publication",
            )
            is None
        )


@pytest.mark.asyncio
async def test_evidence_failure_retries_without_stale_lease_completion(
    approved_publication, monkeypatch
):
    from app.models import CreatorGenerationReview
    from app.services import render_review_evidence as evidence
    from app.tasks.render_review import prepare_generation_review

    rid = await admit_case(approved_publication, monkeypatch)
    calls = []

    def failure(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("temporary transport failure")

    monkeypatch.setattr(evidence, "obtain_render_review_evidence", failure)
    for _ in range(3):
        assert (await asyncio.to_thread(prepare_generation_review.run, str(rid)))[
            "status"
        ] == "pending"
    assert (await asyncio.to_thread(prepare_generation_review.run, str(rid)))["status"] == "ignored"
    assert len(calls) == 3
    with sync_session() as db:
        assert db.get(CreatorGenerationReview, rid).status == "exhausted"


@pytest.mark.asyncio
async def test_tampered_outbox_input_is_never_claimed(approved_publication, monkeypatch):
    from app.models import CreatorGenerationReview
    from app.services.render_review_admission import claim_generation_review

    rid = await admit_case(approved_publication, monkeypatch)
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, rid)
        row.inputs = {**row.inputs, "owner_epoch": 999}
        db.commit()
    assert claim_generation_review(rid) is None
    with sync_session() as db:
        assert db.get(CreatorGenerationReview, rid).status == "stale"


@pytest.mark.asyncio
async def test_missing_approved_draft_is_durable_unavailable_not_hidden_video(
    approved_publication, monkeypatch
):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.tasks.render_review import prepare_generation_review

    claim, _, job_id, _ = approved_publication
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    broker = Mock(side_effect=AssertionError("Missing authority must not run evidence"))
    monkeypatch.setattr(prepare_generation_review, "apply_async", broker)
    with sync_session() as db:
        db.get(CreatorAgentExecution, claim.execution_id).target_draft_id = None
        db.commit()
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[
        0
    ] == "completed"
    with sync_session() as db:
        row = db.scalar(
            select(CreatorGenerationReview).where(
                CreatorGenerationReview.execution_id == claim.execution_id
            )
        )
        assert row is not None and row.status == "unavailable" and row.reason == "missing_authority"
        assert db.get(Job, job_id).status == "variants_ready"
    broker.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [None, "generation", "owner", "exhaust", "query"])
async def test_admission_failure_has_durable_bounded_recovery(
    approved_publication, monkeypatch, drift
):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.services import render_review_admission as admission
    from app.tasks.render_review import prepare_generation_review

    claim, _, job_id, _ = approved_publication
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    broker = Mock()
    monkeypatch.setattr(prepare_generation_review, "apply_async", broker)
    real_insert = admission.insert
    real_load = admission.load_review_rows
    if drift == "query":
        monkeypatch.setattr(
            admission, "load_review_rows", Mock(side_effect=RuntimeError("query failed"))
        )
    monkeypatch.setattr(admission, "insert", Mock(side_effect=RuntimeError("transient insert")))
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[
        0
    ] == "completed"
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        assert execution.result["intent_review_admission"]["status"] == "pending"
        assert db.get(Job, job_id).status == "variants_ready"
        assert (
            db.scalar(
                select(CreatorGenerationReview.id).where(
                    CreatorGenerationReview.execution_id == claim.execution_id
                )
            )
            is None
        )
        if drift == "generation":
            job = db.get(Job, job_id)
            assembly = copy.deepcopy(job.assembly_plan)
            assembly["variants"][0]["render_generation_id"] = str(uuid.uuid4())
            job.assembly_plan = assembly
        elif drift == "owner":
            session = db.get(CreatorAgentSession, execution.session_id)
            session.ownership_epoch += 1
        db.commit()
    if drift != "exhaust":
        monkeypatch.setattr(admission, "insert", real_insert)
        monkeypatch.setattr(admission, "load_review_rows", real_load)
    for _ in range(4):
        admission.reconcile_generation_reviews()
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        marker = execution.result["intent_review_admission"]
        rows = list(
            db.scalars(
                select(CreatorGenerationReview).where(
                    CreatorGenerationReview.execution_id == claim.execution_id
                )
            )
        )
        if drift in {None, "query"}:
            assert marker["status"] == "admitted"
            assert len(rows) == 1
            assert any(
                call.kwargs["task_id"] == f"generation-review:{rows[0].id}"
                for call in broker.call_args_list
            )
        else:
            assert marker["status"] == ("exhausted" if drift == "exhaust" else "stale")
            assert not rows


@pytest.mark.asyncio
@pytest.mark.parametrize("native_records", [None, ["malformed-record-map"]])
async def test_ready_non_native_video_survives_missing_review_authority(
    approved_publication, monkeypatch, native_records
):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.tasks.render_review import prepare_generation_review

    claim, _, job_id, _ = approved_publication
    with sync_session() as db:
        job = db.get(Job, job_id)
        assembly = copy.deepcopy(job.assembly_plan)
        if native_records is None:
            assembly.pop(DEVICE_RENDER_FIELD, None)
        else:
            assembly[DEVICE_RENDER_FIELD] = native_records
        job.assembly_plan = assembly
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        execution.target_generation_id = "synthetic-publication"
        db.commit()
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    broker = Mock()
    monkeypatch.setattr(prepare_generation_review, "apply_async", broker)
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[
        0
    ] == "completed"
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        assert execution.status == "completed"
        assert execution.result["intent_review_admission"]["status"] == "unavailable"
        assert (
            execution.result["intent_review_admission"]["reason"] == "native_authority_unavailable"
        )
        assert db.get(Job, job_id).status == "variants_ready"
        assert (
            db.scalar(
                select(CreatorGenerationReview.id).where(
                    CreatorGenerationReview.execution_id == claim.execution_id
                )
            )
            is None
        )
    broker.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_fails", [False, True])
async def test_reconcile_rotates_past_undelivered_backlog(
    approved_publication, monkeypatch, broker_fails
):
    from unittest.mock import Mock

    from app.models import CreatorGenerationReview
    from app.services.render_review_admission import reconcile_generation_reviews
    from app.tasks.render_review import prepare_generation_review

    row_id = await admit_case(approved_publication, monkeypatch)
    with sync_session() as db:
        original = db.get(CreatorGenerationReview, row_id)
        added = set()
        for _ in range(30):
            identifier = uuid.uuid4()
            added.add(identifier)
            db.add(
                CreatorGenerationReview(
                    id=identifier,
                    execution_id=original.execution_id,
                    owner_id=original.owner_id,
                    review_key=str(identifier),
                    input_digest=original.input_digest,
                    inputs=copy.deepcopy(original.inputs),
                    status="pending",
                    attempts=0,
                )
            )
        db.commit()
        pending_count = len(
            list(
                db.scalars(
                    select(CreatorGenerationReview.id).where(
                        CreatorGenerationReview.status == "pending"
                    )
                )
            )
        )
    from app.services import render_review_admission as admission

    monkeypatch.setattr(admission.log, "warning", Mock())
    broker = Mock(side_effect=RuntimeError("lost broker") if broker_fails else None)
    monkeypatch.setattr(prepare_generation_review, "apply_async", broker)
    for _ in range(pending_count // 25 + 2):
        reconcile_generation_reviews()
    dispatched = {uuid.UUID(call.kwargs["args"][0]) for call in broker.call_args_list}
    assert added <= dispatched
    with sync_session() as db:
        assert all(
            db.get(CreatorGenerationReview, identifier).status == "pending" for identifier in added
        )
