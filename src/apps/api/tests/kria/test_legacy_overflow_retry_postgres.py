"""KRI-1352: recovery crosses real persistence, disclosure and stale-approve fences.

The planned turn is supplied at the model boundary. Its recovery updates are computed from
the stored brief, then committed by the real runtime, never pre-persisted by the positive test.
A synthetic available renderer manifest replaces capability discovery only: strategy policy,
readiness, approval binding and decide_approval remain real. No renderer/provider is invoked.
A separate stale control advances only the current brief while the old draft remains head,
so APPROVE must reach the readiness fence rather than an unrelated stale-draft check.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    CreatorMediaRef,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.approval_readiness import NOT_READY
from app.kria.brief import load_latest_brief_sync, persist_brief_version_sync
from app.kria.drafts import KriaDraftDocument
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval, read_approval
from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
from app.kria.text_sequence import recover_overflowed_literals
from app.models import (
    CreationThread,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentTurn,
    CreatorEditDraft,
    PlanItem,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_approved_long_sequence_copy import ENGLISH
from tests.kria.test_legacy_overflow_literal_recovery import LEGACY
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit


def _versions(thread_id: uuid.UUID) -> list[tuple[int, list]]:
    with sync_session() as db:
        rows = (
            db.query(CreativeBriefVersion)
            .filter_by(thread_id=thread_id)
            .order_by(CreativeBriefVersion.version)
            .all()
        )
        return [(row.version, row.requirements) for row in rows]


def _approval_of(turn_id: str):
    with sync_session() as db:
        approval = db.query(CreatorAgentApproval).filter_by(turn_id=uuid.UUID(turn_id)).one()
        draft = db.get(CreatorEditDraft, approval.draft_id)
        document = KriaDraftDocument.model_validate(draft.snapshot_json)
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, approval.thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
        return approval.id, document, body, approval.consequence_summary


def _fixture(monkeypatch: pytest.MonkeyPatch):
    user_id, thread_id, _ = _seed_runtime_project()
    legacy = [req.model_dump(mode="json", exclude_none=True) for req in LEGACY.requirements]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "reply_language": "en", "edit_format": "montage"}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = ["users/test/recovery.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": "users/test/recovery.mp4",
                "kind": "video",
                "duration_s": 30,
                "analysis": {"summary": "Synthetic public scene", "has_speech": False},
            }
        ]
        item_id = item.id
        db.add(CreativeBriefVersion(thread_id=thread_id, version=1, requirements=legacy))
        db.commit()
    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    for name in (
        "kria_choice_questions_enabled",
        "ios_device_only_mode",
        "phone_rendering_enabled",
    ):
        monkeypatch.setattr(settings, name, False)
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        media=[CreatorMediaRef(media_id="clip-1", kind="video", duration_s=30)],
        capabilities={
            name: CapabilityAvailability(available=True)
            for name in (
                "dispatch_render",
                "edit_format:montage",
                "guided_story",
                "draft_guided_proposal",
                "set_item_intent",
            )
        },
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )
    checked = check_strategy_for_runtime_v2(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="original_audio",
            render_program="guided",
            selected_media_ids=["clip-1"],
            rationale="Use the saved paragraphs.",
        ),
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
    )
    assert isinstance(checked, CheckedStrategy)
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy", strategy=checked.strategy, summary="Regenerate the saved edit."
        )
    )
    recovery = {"enabled": False}

    async def planned(*_args, **_kwargs):
        with sync_session() as db:
            prior = load_latest_brief_sync(db, thread_id)
        return PlannedKriaTurn(
            plan=plan,
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
            readiness_manifest=manifest,
            brief_route="replan",
            brief_clip_ids=("clip-1",),
            brief_updates=recover_overflowed_literals(prior, ()) if recovery["enabled"] else (),
            brief_expected_version=prior.version,
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    monkeypatch.setattr(
        "app.kria.approval_readiness.resolve_readiness_manifest", AsyncMock(return_value=manifest)
    )
    return user_id, thread_id, item_id, legacy, recovery


async def _pending(user_id, thread_id, message):
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    turn = await _submit(user_id, thread_id, message, revision)
    result = await asyncio.to_thread(run_kria_turn.run, turn.turn_id)
    assert result["status"] == "awaiting_approval", result
    return turn


@pytest.mark.asyncio
async def test_runtime_commits_recovery_and_shows_exact_copy_before_fresh_approval(monkeypatch):
    user_id, thread_id, item_id, legacy, recovery = _fixture(monkeypatch)
    try:
        first = await _pending(user_id, thread_id, "Use the saved wording")
        old_id, old_document, old_body, old_notice = _approval_of(first.turn_id)
        old_binding_bytes = old_document.brief_binding.model_dump_json()
        assert old_document.brief_coverage["approval_readiness"]["ready"] is True
        assert "Review the restored text" not in old_notice
        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=old_id,
                creator_id=user_id,
                decision="deny",
                body=old_body,
            )

        recovery["enabled"] = True
        retry = await _pending(user_id, thread_id, "Try again")
        fresh_id, document, body, notice = _approval_of(retry.turn_id)
        versions = _versions(thread_id)
        assert [version for version, _ in versions] == [1, 2]
        assert versions[0][1] == legacy
        assert {r["id"]: r["literal"] for r in versions[1][1]}["r2"] == ENGLISH
        with sync_session() as db:
            stored = db.query(CreativeBriefVersion).filter_by(thread_id=thread_id, version=2).one()
            assert stored.source_turn_id == uuid.UUID(retry.turn_id)
            assert db.get(PlanItem, item_id).current_job_id is None
        assert _approval_of(first.turn_id)[1].brief_binding.model_dump_json() == old_binding_bytes
        assert document.brief_binding.resolve(thread_id).version == 2
        assert document.brief_binding.digest != old_document.brief_binding.digest
        assert document.brief_coverage["approval_readiness"]["ready"] is True
        assert f'Review the restored text for English translation: "{ENGLISH}"' in notice
        assert notice.count("Review the restored text for English translation:") == 1
        async with AsyncSessionLocal() as db:
            visible = await read_approval(
                db, thread_id=thread_id, approval_id=fresh_id, creator_id=user_id
            )
            assert visible.consequence_summary == notice
            accepted, _ = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=fresh_id,
                creator_id=user_id,
                decision="approve",
                body=body,
            )
        assert accepted.status == "approved"
        assert [version for version, _ in _versions(thread_id)] == [1, 2]
        with sync_session() as db:
            assert db.get(PlanItem, item_id).current_job_id is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_stale_approve_is_rejected_by_real_readiness_after_brief_recovery(monkeypatch):
    user_id, thread_id, item_id, _, _ = _fixture(monkeypatch)
    try:
        first = await _pending(user_id, thread_id, "Use the saved wording")
        approval_id, document, body, _ = _approval_of(first.turn_id)
        original = document.brief_binding.model_dump_json()
        assert document.brief_coverage["approval_readiness"]["ready"] is True
        # Controlled stale-state setup changes only the current brief, not the draft or pin.
        # Automatic runtime persistence is exercised separately by the positive test above.
        with sync_session() as db:
            prior = load_latest_brief_sync(db, thread_id)
            persist_brief_version_sync(
                db,
                thread_id=thread_id,
                turn_id=uuid.UUID(first.turn_id),
                updates=recover_overflowed_literals(prior, ()),
            )
            db.commit()
        assert _approval_of(first.turn_id)[1].brief_binding.model_dump_json() == original
        assert len(_versions(thread_id)) == 2
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    creator_id=user_id,
                    decision="approve",
                    body=body,
                )
        assert caught.value.code == NOT_READY
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            assert approval.status == "cancelled"
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            assert execution.status == "stale" and execution.error["code"] == NOT_READY
            assert db.get(CreatorAgentTurn, uuid.UUID(first.turn_id)).status == "failed"
            assert db.get(PlanItem, item_id).current_job_id is None
            draft = db.get(CreatorEditDraft, approval.draft_id)
            assert draft.is_head
            pin = KriaDraftDocument.model_validate(draft.snapshot_json).brief_binding
            assert pin.model_dump_json() == original
    finally:
        await async_engine.dispose()
