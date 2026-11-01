"""Failed renders can be corrected from a current saved editor draft, without replanning."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.runtime import submit_turn
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
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_creative_brief import _MANIFEST
from tests.kria.test_render_retry_postgres import seed_failed
from tests.services.test_kria_editor_ops import _variant

session_factory = async_sessionmaker(
    create_async_engine(settings.asyncpg_database_url, poolclass=NullPool), expire_on_commit=False
)


def _seed(status="processing_failed", *, editor=True):
    case = seed_failed(snapshot=False)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        variant = _variant()
        variant.update(
            variant_id="first",
            render_generation_id="approved",
            render_status="needs_attention" if status == "awaiting_device" else "failed",
            base_video_path="users/test/base.mp4",
            video_path="users/test/last-good.mp4",
        )
        job.status = status
        job.all_candidates = {"clip_paths": ["users/test/clip.mp4"]}
        job.assembly_plan = {"variants": [variant]}
        head = db.get(CreatorEditDraft, case.draft_id)
        if editor:
            head.snapshot_json, head.snapshot_hash = canonical_snapshot(
                KriaDraftDocument(
                    kind="editor",
                    intent="Keep my approved edit",
                    editor_payload={
                        "base_generation": "approved",
                        "text_elements": copy.deepcopy(variant["text_elements"]),
                        "mix": {"music_level": 0.5},
                    },
                )
            )
            head.base_job_id = job.id
            head.base_generation_id = "approved"
        session = db.get(CreatorAgentSession, case.session_id)
        session.last_good = {
            "job_id": str(job.id),
            "variant_id": "first",
            "render_generation_id": "last-good",
        }
        session.manifest_hash = "a" * 64
        item_id = head.item_id
        db.commit()
    return case, item_id


def _agents(monkeypatch, item_id):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda *args: None)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", True)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    monkeypatch.setattr(
        planner,
        "resolve_item_creator_context",
        AsyncMock(return_value=(_MANIFEST.model_copy(update={"item_id": str(item_id)}), [])),
    )
    monkeypatch.setattr(planner, "creator_context", lambda *a: ("creator", "item"))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    creator = Mock(side_effect=AssertionError("A correction must not replan through Main Creator"))
    monkeypatch.setattr(planner, "MainCreatorAgent", creator)

    class Extractor:
        def __init__(self, client):
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(brief_updates=[])

    monkeypatch.setattr(planner, "BriefExtractorAgent", Extractor)
    copilot = AsyncMock(
        return_value=SimpleNamespace(
            ops=[{"op": "edit_text", "bar_index": 0, "text": "Keep every word"}],
            outcome="proposed",
            reply="Corrected the saved title.",
            intent="edit",
            rejection_reasons=[],
            unmet_requests=[],
        )
    )
    monkeypatch.setattr(planner, "run_copilot_turn", copilot)
    return copilot, creator


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["processing_failed", "awaiting_device"])
async def test_failed_draft_correction_stages_without_approval_or_render(monkeypatch, status):
    case, item_id = _seed(status)
    copilot, creator = _agents(monkeypatch, item_id)

    async def planned(_snapshot, message, **kwargs):
        async with session_factory() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=item_id,
                creator_id=case.user_id,
                user_message=message,
            )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    with sync_session() as db:
        before = copy.deepcopy(db.get(Job, case.job_id).assembly_plan)
        last_good = copy.deepcopy(db.get(CreatorAgentSession, case.session_id).last_good)
        jobs = db.scalar(select(func.count()).select_from(Job).where(Job.user_id == case.user_id))
    body = SubmitTurnBody(
        message="Change the title to Keep every word",
        client_event_id=str(uuid.uuid4()),
        expected_thread_revision=3,
    )
    async with session_factory() as db:
        accepted, publish = await submit_turn(
            db, thread_id=case.thread_id, creator_id=case.user_id, body=body
        )
    assert publish
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed", result
    with sync_session() as db:
        head = db.scalar(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == case.thread_id, CreatorEditDraft.is_head.is_(True)
            )
        )
        assert head.id != case.draft_id
        payload = head.snapshot_json["editor_payload"]
        assert payload["text_elements"][0]["text"] == "Keep every word"
        assert (
            payload["text_elements"][0]["start_s"]
            == before["variants"][0]["text_elements"][0]["start_s"]
        )
        assert payload["mix"]["music_level"] == 0.5
        approval = db.scalar(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        )
        assert approval is None
        event = db.scalar(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == case.thread_id,
                CreationThreadEvent.event_type == "approval_requested",
            )
        )
        assert event is None
        assert db.get(Job, case.job_id).assembly_plan == before
        assert db.get(CreatorAgentSession, case.session_id).last_good == last_good
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.user_id == case.user_id))
            == jobs
        )
        assert (
            db.get(CreatorEditDraft, case.draft_id).snapshot_json["intent"]
            == "Keep my approved edit"
        )
        assert (
            db.get(CreatorAgentExecution, case.execution_id).result["creator_request"]
            == "Keep my approved edit"
        )
    async with session_factory() as db:
        replay, _ = await submit_turn(
            db, thread_id=case.thread_id, creator_id=case.user_id, body=body
        )
    assert replay.turn_id == accepted.turn_id
    repeated = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert repeated["status"] == "ignored"
    copilot.assert_awaited_once()
    creator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    ["no_head", "no_variant", "strategy_only", "stale_generation", "bad_hash", "unknown_outcome"],
)
async def test_unsafe_failed_draft_has_an_honest_nonmutating_alternative(monkeypatch, problem):
    case, item_id = _seed(editor=problem != "strategy_only")
    copilot, creator = _agents(monkeypatch, item_id)
    with sync_session() as db:
        head = db.get(CreatorEditDraft, case.draft_id)
        if problem == "no_variant":
            job = db.get(Job, case.job_id)
            assembly = copy.deepcopy(job.assembly_plan)
            assembly["variants"] = []
            job.assembly_plan = assembly
        elif problem == "no_head":
            head.is_head = False
        elif problem == "stale_generation":
            document = KriaDraftDocument.model_validate(copy.deepcopy(head.snapshot_json))
            document.editor_payload["base_generation"] = "older-generation"
            head.snapshot_json, head.snapshot_hash = canonical_snapshot(document)
        elif problem == "bad_hash":
            head.snapshot_hash = "0" * 64
        elif problem == "unknown_outcome":
            db.get(CreatorAgentExecution, case.execution_id).status = "outcome_unknown"
        before = copy.deepcopy(head.snapshot_json)
        before_hash = head.snapshot_hash
        db.commit()
    async with session_factory() as db:
        result = await planner.plan_live_turn(
            db,
            thread_id=case.thread_id,
            item_id=item_id,
            creator_id=case.user_id,
            user_message="Change the title to Keep every word",
        )
    assert result.plan.mode == "respond" and not result.plan.intents
    assert (
        "unknown" in result.plan.response
        if problem == "unknown_outcome"
        else "saved request" in result.plan.response
    )
    with sync_session() as db:
        head = db.get(CreatorEditDraft, case.draft_id)
        assert head.snapshot_json == before and head.snapshot_hash == before_hash
    copilot.assert_not_awaited()
    creator.assert_not_called()


@pytest.mark.asyncio
async def test_failed_correction_keeps_revision_and_owner_admission_guards(monkeypatch):
    from app.kria.runtime import RuntimeFailure

    case, item_id = _seed()
    _agents(monkeypatch, item_id)
    for owner, revision, code in [
        (case.user_id, 2, "thread_revision_stale"),
        (uuid.uuid4(), 3, "thread_not_found"),
    ]:
        async with session_factory() as db:
            with pytest.raises(RuntimeFailure) as failure:
                await submit_turn(
                    db,
                    thread_id=case.thread_id,
                    creator_id=owner,
                    body=SubmitTurnBody(
                        message="Change the title",
                        client_event_id=str(uuid.uuid4()),
                        expected_thread_revision=revision,
                    ),
                )
        assert failure.value.code == code
    with sync_session() as db:
        assert db.get(CreatorEditDraft, case.draft_id).is_head
        assert db.get(CreatorAgentTurn, case.turn_id).status == "failed"


@pytest.mark.asyncio
async def test_unsupported_part_of_a_correction_does_not_apply_a_subset(monkeypatch):
    case, item_id = _seed()
    copilot, creator = _agents(monkeypatch, item_id)
    copilot.return_value.unmet_requests = [
        {"request": "remove the person from the footage", "reason": "not available"}
    ]
    async with session_factory() as db:
        result = await planner.plan_live_turn(
            db,
            thread_id=case.thread_id,
            item_id=item_id,
            creator_id=case.user_id,
            user_message="Change the title",
        )
    assert result.plan.mode == "respond" and not result.plan.intents
    assert "Nothing was changed" in result.plan.response
    creator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race",
    [
        "head_changed",
        "head_changed_then_ready",
        "snapshot_changed_with_old_hash",
        "unknown_outcome",
    ],
)
async def test_correction_rechecks_saved_basis_under_commit_locks(monkeypatch, race):
    case, item_id = _seed()
    copilot, creator = _agents(monkeypatch, item_id)
    answer = copilot.return_value

    async def change_during_model(*args, **kwargs):
        with sync_session() as db:
            if race in {
                "head_changed",
                "head_changed_then_ready",
                "snapshot_changed_with_old_hash",
            }:
                head = db.get(CreatorEditDraft, case.draft_id)
                document = KriaDraftDocument.model_validate(copy.deepcopy(head.snapshot_json))
                document.editor_payload["text_elements"][0]["text"] = "Concurrent saved title"
                snapshot, snapshot_hash = canonical_snapshot(document)
                head.snapshot_json = snapshot
                if race != "snapshot_changed_with_old_hash":
                    head.snapshot_hash = snapshot_hash
                if race == "head_changed_then_ready":
                    job = db.get(Job, case.job_id)
                    assembly = copy.deepcopy(job.assembly_plan)
                    assembly["variants"][0]["render_status"] = "ready"
                    job.assembly_plan = assembly
                    job.status = "completed"
            else:
                db.get(CreatorAgentExecution, case.execution_id).status = "outcome_unknown"
            db.commit()
        return answer

    copilot.side_effect = change_during_model

    async def planned(_snapshot, message, **kwargs):
        async with session_factory() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=item_id,
                creator_id=case.user_id,
                user_message=message,
            )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    async with session_factory() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=case.thread_id,
            creator_id=case.user_id,
            body=SubmitTurnBody(
                message="Change the title",
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=3,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed"
    with sync_session() as db:
        assert db.get(CreatorEditDraft, case.draft_id).is_head
        assert (
            db.scalar(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            )
            is None
        )
        assert (
            db.get(Job, case.job_id).assembly_plan["variants"][0]["text_elements"][0]["text"]
            == "Old hook"
        )
    creator.assert_not_called()


@pytest.mark.asyncio
async def test_phone_attention_correction_recompiles_recipe_only_after_explicit_save(monkeypatch):
    from app.kria.device_render import make_device_request
    from app.services.device_render import (
        DEVICE_RENDER_FIELD,
        device_record,
        device_status,
        pin_device_request,
        save_device_record,
    )
    from tests.routes.test_phone_editor_commit import phone_job

    case, item_id = _seed("awaiting_device")
    template = phone_job(monkeypatch)
    original_recipe = device_status(template, "guided_story").request.recipe
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        assembly = copy.deepcopy(template.assembly_plan)
        assembly.pop(DEVICE_RENDER_FIELD, None)
        variant = assembly["variants"][0]
        variant.update(
            variant_id="first",
            render_generation_id="approved",
            render_status="needs_attention",
            video_path="users/test/last-good.mp4",
        )
        job.assembly_plan = assembly
        pin_device_request(
            job,
            make_device_request(
                job_id=job.id, variant_id="first", revision=1, recipe=original_recipe
            ),
            base_generation="approved",
        )
        record = copy.deepcopy(device_record(job, "first"))
        record["status"].update(phase="needs_attention", reason_code="unsupported_recipe")
        save_device_record(job, "first", record)
        head = db.get(CreatorEditDraft, case.draft_id)
        head.snapshot_json, head.snapshot_hash = canonical_snapshot(
            KriaDraftDocument(
                kind="editor",
                intent="Keep my approved edit",
                editor_payload={
                    "base_generation": "approved",
                    "text_elements": copy.deepcopy(variant["text_elements"]),
                },
            )
        )
        before = copy.deepcopy(job.assembly_plan)
        last_good = copy.deepcopy(db.get(CreatorAgentSession, case.session_id).last_good)
        db.commit()
    copilot, creator = _agents(monkeypatch, item_id)

    async def planned(_snapshot, message, **kwargs):
        async with session_factory() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=item_id,
                creator_id=case.user_id,
                user_message=message,
            )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    async with session_factory() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=case.thread_id,
            creator_id=case.user_id,
            body=SubmitTurnBody(
                message="Change the title to Keep every word",
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=3,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed", result
    from app.routes import generative_jobs as gj

    with sync_session() as db:
        head = db.scalar(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == case.thread_id, CreatorEditDraft.is_head.is_(True)
            )
        )
        assert head.id != case.draft_id
        assert (
            db.scalar(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            )
            is None
        )
        job = db.get(Job, case.job_id)
        assert job.assembly_plan == before
        assert device_status(job, "first").request.recipe == original_recipe
        payload = gj.EditorCommitRequest.model_validate(head.snapshot_json["editor_payload"])
        gj.prepare_editor_commit(job, "first", payload)
        db.commit()
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        status = device_status(job, "first")
        assert status.request.identity.recipe_revision == 2
        assert [
            " ".join(run.text for run in layer.runs) for layer in status.request.recipe.text_layers
        ] == ["Keep every word"]
        assert status.request.recipe.tracks == original_recipe.tracks
        assert status.request.recipe.audio == original_recipe.audio
        assert status.request.recipe.assets == original_recipe.assets
        corrected = next(
            layer
            for layer in status.request.recipe.text_layers
            if " ".join(run.text for run in layer.runs) == "Keep every word"
        )
        assert corrected.start == original_recipe.text_layers[0].start
        assert corrected.end == original_recipe.text_layers[0].end
        assert db.get(CreatorAgentSession, case.session_id).last_good == last_good
    copilot.assert_awaited_once()
    creator.assert_not_called()


@pytest.mark.asyncio
async def test_deictic_correction_uses_the_existing_saved_draft(monkeypatch):
    case, item_id = _seed()
    copilot, creator = _agents(monkeypatch, item_id)
    copilot.return_value.reply = planner._WEB_PROPOSED_REPLY
    copilot.return_value.ops = [
        {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}
    ]
    async with session_factory() as db:
        result = await planner.plan_live_turn(
            db,
            thread_id=case.thread_id,
            item_id=item_id,
            creator_id=case.user_id,
            user_message="Make it red",
        )
    assert [intent.tool_name for intent in result.plan.intents] == [
        "draft.apply_editor_ops",
    ]
    assert result.plan.diagnostics["editor_recovery_basis"]["draft_id"] == str(case.draft_id)
    assert result.plan.intents[0].arguments["summary"] == planner._PHONE_STAGED_REPLY
    copilot.assert_awaited_once()
    creator.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_operation_bundle_does_not_stage_a_partial_correction(monkeypatch):
    case, item_id = _seed()
    copilot, creator = _agents(monkeypatch, item_id)
    copilot.return_value.ops.append({"op": "remove_person", "target": "background person"})
    async with session_factory() as db:
        result = await planner.plan_live_turn(
            db,
            thread_id=case.thread_id,
            item_id=item_id,
            creator_id=case.user_id,
            user_message="Change the title and remove the person from the footage",
        )
    assert result.plan.mode == "respond" and not result.plan.intents
    assert "Nothing was changed" in result.plan.response
    creator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["processing_failed", "awaiting_device"])
async def test_flag_off_keeps_failed_render_unavailable_to_editor(monkeypatch, status):
    case, item_id = _seed(status)
    copilot, creator = _agents(monkeypatch, item_id)
    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", False)
    with sync_session() as db:
        before = copy.deepcopy(db.get(Job, case.job_id).assembly_plan)
    async with session_factory() as db:
        item = await db.get(PlanItem, item_id)
        result = await planner._plan_editor_revision(
            db,
            thread_id=case.thread_id,
            item=item,
            user_message="Change the title to Keep every word",
        )
    assert result is None
    copilot.assert_not_awaited()
    creator.assert_not_called()
    with sync_session() as db:
        assert db.get(CreatorEditDraft, case.draft_id).is_head
        assert db.get(Job, case.job_id).assembly_plan == before


@pytest.mark.asyncio
async def test_separate_explicit_render_approval_blocks_late_unknown_outcome(monkeypatch):
    from dataclasses import replace

    from app.kria.api_schemas import ApprovalDecisionBody
    from app.kria.runtime import approval_fingerprint, decide_approval
    from app.tasks.kria_runtime import _claim_approval_dispatch

    case, item_id = _seed()
    _agents(monkeypatch, item_id)

    async def planned(_snapshot, message, **kwargs):
        async with session_factory() as db:
            result = await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=item_id,
                creator_id=case.user_id,
                user_message=message,
            )
        explicit_render = planner.adapt_editor_action(
            reply="Render the corrected draft after approval.",
            ops=[{"op": "edit_text", "bar_index": 0, "text": "Keep every word"}],
            request_render=True,
        )
        return replace(
            result,
            plan=explicit_render.model_copy(update={"diagnostics": result.plan.diagnostics}),
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    async with session_factory() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=case.thread_id,
            creator_id=case.user_id,
            body=SubmitTurnBody(
                message="Change the title",
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=3,
            ),
        )
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
        "status"
    ] == "awaiting_approval"
    with sync_session() as db:
        approval = db.scalar(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        )
        revision = db.get(CreationThread, case.thread_id).revision
        fingerprint = approval_fingerprint(approval)
        before = copy.deepcopy(db.get(Job, case.job_id).assembly_plan)
        db.get(CreatorAgentExecution, case.execution_id).status = "outcome_unknown"
        db.commit()
    async with session_factory() as db:
        await decide_approval(
            db,
            thread_id=case.thread_id,
            approval_id=approval.id,
            creator_id=case.user_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=revision,
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=fingerprint,
            ),
        )
    assert await asyncio.to_thread(_claim_approval_dispatch, approval.id) is None
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval.id)
        assert approval.status == "cancelled"
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        assert execution.error["code"] == "render_outcome_unknown"
        assert db.get(Job, case.job_id).assembly_plan == before
        assert db.get(CreatorEditDraft, approval.draft_id).is_head


@pytest.mark.asyncio
async def test_provider_deadline_context_keeps_supported_correction_on_the_editor(monkeypatch):
    case, item_id = _seed()
    copilot, creator = _agents(monkeypatch, item_id)
    with sync_session() as db:
        db.get(CreatorAgentTurn, case.turn_id).error = {
            "code": "runtime_provider_outcome_unknown",
            "provider_deadline": True,
        }
        db.commit()
    token = planner.start_degraded_retry.set(True)
    try:
        async with session_factory() as db:
            result = await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=item_id,
                creator_id=case.user_id,
                user_message="Change the title to Keep every word",
            )
    finally:
        planner.start_degraded_retry.reset(token)
    assert [intent.tool_name for intent in result.plan.intents] == ["draft.apply_editor_ops"]
    assert result.plan.diagnostics["editor_recovery_basis"]["draft_id"] == str(case.draft_id)
    copilot.assert_awaited_once()
    creator.assert_not_called()


# Leverkusen M2 run 3 (10-31): the failed render and the follow-up turn b2b5089b, verbatim.
_KRI1103 = json.loads(
    (Path(__file__).parent / "kri1103_leverkusen_m2_failed_render_followup.json").read_text()
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", [_KRI1103["follow_up"]["creator_message"], "Selfie etiketini kaldır"]
)
async def test_failed_first_render_change_ask_replans_with_the_change(monkeypatch, message):
    """A render that failed before it made any video leaves only the strategy draft:
    nothing exists to correct in place and nothing is lost by re-planning. The creator's
    change (here: drop the label that failed the phone text gate) must reach the planner,
    not the "no current editable draft" refusal that tells them to resend the old request."""
    from app.agents._schemas.creator_agent import AskUser
    from tests.kria.test_creative_brief import _ASK

    case, item_id = _seed(editor=False)
    copilot, _ = _agents(monkeypatch, item_id)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        assert not _KRI1103["failed_render"]["assembly_plan_has_variants_key"]
        job.status = _KRI1103["failed_render"]["job_status"]
        job.assembly_plan = {"native_device_only": True, "creator_generation_id": "approved"}
        head = db.get(CreatorEditDraft, case.draft_id)
        assert head.snapshot_json["kind"] == _KRI1103["failed_render"]["head_draft_kind"]
        before = copy.deepcopy(head.snapshot_json)
        db.commit()
    runs = []

    class Planner:
        def __init__(self, client):
            pass

        def run(self, agent_input, **kwargs):
            runs.append(agent_input)
            return SimpleNamespace(action=AskUser(**_ASK), brief_updates=[], creative_decision=None)

    monkeypatch.setattr(planner, "MainCreatorAgent", Planner)
    async with session_factory() as db:
        result = await planner.plan_live_turn(
            db,
            thread_id=case.thread_id,
            item_id=item_id,
            creator_id=case.user_id,
            user_message=message,
        )
    assert result.plan.response != _KRI1103["follow_up"]["prod_reply"]
    assert "editable draft" not in (result.plan.response or "")
    assert "saved request" not in (result.plan.response or "")
    assert len(runs) == 1
    copilot.assert_not_awaited()
    with sync_session() as db:
        head = db.get(CreatorEditDraft, case.draft_id)
        assert head.is_head and head.snapshot_json == before
