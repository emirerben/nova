"""Focused state-machine tests for durable Main Creator sessions."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.schemas.edit_proposal import EditProposal, ProposalFailure
from app.services import creator_sessions
from app.tasks import edit_proposal_build


def test_execution_lease_exceeds_proposal_task_hard_limit() -> None:
    assert creator_sessions.EXECUTION_RECEIPT_LEASE_S == 1650
    assert (
        creator_sessions.EXECUTION_RECEIPT_LEASE_S > edit_proposal_build._TASK_LIMITS["time_limit"]
    )


def _session(**overrides):
    values = {
        "id": uuid.uuid4(),
        "creator_id": uuid.uuid4(),
        "plan_item_id": uuid.uuid4(),
        "phase": "briefing",
        "revision": 0,
        "ownership_epoch": 0,
        "render_attempts": 0,
        "max_render_attempts": 2,
        "active_plan": None,
        "target_job_id": None,
        "target_variant_id": None,
        "target_generation_id": None,
        "events": [],
        "created_at": datetime(2026, 8, 24, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 24, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_session_variant_target_preserves_exact_nonfirst_variant() -> None:
    session = _session(
        target_variant_id="chosen",
        target_generation_id="generation-chosen",
    )
    variants = [
        {
            "variant_id": "first",
            "render_status": "ready",
            "render_generation_id": "generation-first",
        },
        {
            "variant_id": "chosen",
            "render_status": "ready",
            "render_generation_id": "generation-chosen",
        },
    ]

    variant, state = creator_sessions._session_variant_target(session, variants)

    assert state == "ready"
    assert variant["variant_id"] == "chosen"


@pytest.mark.parametrize(
    ("variants", "expected_state"),
    [
        ([], "stale"),
        (
            [
                {
                    "variant_id": "chosen",
                    "render_status": "ready",
                    "render_generation_id": "new-generation",
                }
            ],
            "stale",
        ),
        (
            [
                {
                    "variant_id": "chosen",
                    "render_status": "rendering",
                    "render_generation_id": "generation-chosen",
                }
            ],
            "processing",
        ),
    ],
)
def test_session_variant_target_never_falls_back_from_exact_target(
    variants, expected_state
) -> None:
    session = _session(
        target_variant_id="chosen",
        target_generation_id="generation-chosen",
    )

    variant, state = creator_sessions._session_variant_target(session, variants)

    assert variant is None
    assert state == expected_state


def test_session_variant_target_never_infers_missing_generation() -> None:
    session = _session(target_variant_id="chosen", target_generation_id=None)

    variant, state = creator_sessions._session_variant_target(
        session,
        [
            {
                "variant_id": "chosen",
                "render_status": "ready",
                "render_generation_id": "current-generation",
            }
        ],
    )

    assert variant is None
    assert state == "stale"


def test_rollout_eligibility_fails_closed_and_honors_full_rollout(monkeypatch) -> None:
    user_id = uuid.uuid4()
    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_enabled", False)
    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_rollout_percent", 100)
    assert creator_sessions.rollout_eligible(user_id) is False

    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_rollout_percent", 0)
    assert creator_sessions.rollout_eligible(user_id) is False

    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_rollout_percent", 100)
    assert creator_sessions.rollout_eligible(user_id) is True


def test_serialize_session_only_exposes_pending_plan_at_confirmation() -> None:
    plan = {"version": 1, "plan_hash": "a" * 64}
    confirming = _session(phase="awaiting_confirmation", active_plan=plan)
    rendering = _session(phase="rendering", active_plan=plan)

    assert creator_sessions.serialize_session(confirming)["pending_plan"] == plan
    assert creator_sessions.serialize_session(rendering)["pending_plan"] is None


def test_serialize_session_exposes_only_bounded_review_receipt() -> None:
    session = _session(
        last_review={
            "status": "complete",
            "job_id": "job-1",
            "variant_id": "variant-1",
            "render_generation_id": "generation-1",
            "provider_raw_response": "must not be public",
            "evidence": [
                {
                    "evidence_id": f"e-{index}",
                    "kind": "visual",
                    "severity": "warning",
                    "start_s": 0,
                    "end_s": 1,
                    "observation": "observation",
                    "private_debug": "omit",
                }
                for index in range(20)
            ],
            "proposed_revision": {
                "revision_id": "revision-1",
                "summary": "Tighten the opening.",
                "rationale": "The first beat is generic.",
                "evidence_ids": [f"e-{index}" for index in range(20)],
                "strategy": {"private": "omit"},
            },
        },
    )

    review = creator_sessions.serialize_session(session)["last_review"]

    assert review["status"] == "complete"
    assert "provider_raw_response" not in review
    assert len(review["evidence"]) == 12
    assert "private_debug" not in review["evidence"][0]
    assert review["proposed_revision"]["evidence_ids"] == [f"e-{index}" for index in range(8)]
    assert "strategy" not in review["proposed_revision"]


def test_creator_context_is_bounded_before_agent_input() -> None:
    persona = SimpleNamespace(
        persona={"summary": "creator", "content_pillars": []},
        style={"voice": "x" * 10_000},
    )
    item = SimpleNamespace(
        idea="idea",
        theme="theme",
        notes="notes",
        filming_guide=[{"what": "y" * 10_000}],
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
    )

    creator, item_context = creator_sessions.creator_context(persona, item)

    assert len(creator) <= creator_sessions.CREATOR_CONTEXT_MAX_CHARS
    assert len(item_context) <= creator_sessions.CREATOR_CONTEXT_MAX_CHARS


@pytest.mark.asyncio
async def test_reconcile_ignores_sessions_outside_render_phases() -> None:
    db = AsyncMock()
    changed = await creator_sessions.reconcile_render_state(db, _session(phase="briefing"))

    assert changed is False
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_missing_exact_job_fails_stably(monkeypatch) -> None:
    session = _session(phase="rendering", target_job_id=uuid.uuid4())
    db = AsyncMock()
    db.get.return_value = None
    append = AsyncMock()
    monkeypatch.setattr(creator_sessions, "append_event", append)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert session.last_error["code"] == "target_job_missing"
    append.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_keeps_partial_job_rendering_while_variant_is_in_flight() -> None:
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = _session(
        creator_id=creator_id,
        plan_item_id=item_id,
        phase="awaiting_feedback",
        target_job_id=job_id,
        target_variant_id="failed-variant",
        target_generation_id="retry-generation",
        last_review={"status": "pending"},
    )
    job = SimpleNamespace(
        id=job_id,
        status="variants_ready_partial",
        user_id=creator_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "failed-variant",
                    "render_status": "rendering",
                    "render_generation_id": "retry-generation",
                },
                {"variant_id": "ready-variant", "render_status": "ready"},
            ]
        },
    )
    item = SimpleNamespace(id=item_id, content_plan_id=uuid.uuid4())
    plan = SimpleNamespace(user_id=creator_id, ownership_epoch=0)
    db = AsyncMock()
    db.get.side_effect = [job, item, plan]

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "rendering"


@pytest.mark.asyncio
async def test_reconcile_retries_only_transient_review_enqueue_failure(monkeypatch) -> None:
    job_id = uuid.uuid4()
    session = _session(
        phase="awaiting_feedback",
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
        last_review={
            "status": "unavailable",
            "dispatch_status": "failed",
            "error_code": "review_enqueue_failed",
        },
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        content_plan_id=uuid.uuid4(),
    )
    plan = SimpleNamespace(
        user_id=session.creator_id,
        ownership_epoch=session.ownership_epoch,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=session.creator_id,
        content_plan_item_id=session.plan_item_id,
        content_plan_ownership_epoch=session.ownership_epoch,
        status="variants_ready",
        assembly_plan={"variants": []},
    )
    db = AsyncMock()
    db.get.side_effect = [job, item, plan]
    monkeypatch.setattr(
        creator_sessions,
        "_session_variant_target",
        lambda *_args: (
            {
                "variant_id": "variant-1",
                "render_status": "ready",
                "render_generation_id": "generation-1",
            },
            "ready",
        ),
    )
    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(
        creator_sessions.settings, "main_creator_agent_quality_review_enabled", True
    )
    queue = Mock(return_value=True)
    monkeypatch.setattr("app.tasks.creator_quality_review.queue_creator_quality_review", queue)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    queue.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_closes_pending_review_when_quality_flag_turns_off(monkeypatch) -> None:
    job_id = uuid.uuid4()
    session = _session(
        phase="awaiting_feedback",
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
        last_review={
            "status": "pending",
        },
    )
    item = SimpleNamespace(id=session.plan_item_id, content_plan_id=uuid.uuid4())
    plan = SimpleNamespace(
        user_id=session.creator_id,
        ownership_epoch=session.ownership_epoch,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=session.creator_id,
        content_plan_item_id=session.plan_item_id,
        content_plan_ownership_epoch=session.ownership_epoch,
        status="variants_ready",
        assembly_plan={"variants": []},
    )
    db = AsyncMock()
    db.get.side_effect = [job, item, plan]
    monkeypatch.setattr(
        creator_sessions,
        "_session_variant_target",
        lambda *_args: (
            {
                "variant_id": "variant-1",
                "render_status": "ready",
                "render_generation_id": "generation-1",
            },
            "ready",
        ),
    )
    monkeypatch.setattr(creator_sessions.settings, "main_creator_agent_review_enabled", False)
    monkeypatch.setattr(
        creator_sessions.settings, "main_creator_agent_quality_review_enabled", False
    )

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.last_review["status"] == "unavailable"
    assert session.last_review["error_code"] == "review_disabled"


@pytest.mark.asyncio
async def test_reconcile_expires_a_running_receipt_when_dispatch_never_started(
    monkeypatch,
) -> None:
    session = _session(phase="executing")
    item = SimpleNamespace(id=session.plan_item_id, current_job_id=None)
    receipt = SimpleNamespace(
        status="running",
        created_at=datetime.now(UTC)
        - timedelta(seconds=creator_sessions.EXECUTION_RECEIPT_LEASE_S + 1),
        error=None,
        completed_at=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.return_value = item
    db.execute.return_value = result
    append = AsyncMock()
    monkeypatch.setattr(creator_sessions, "append_event", append)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert session.last_error["code"] == "execution_lease_expired"
    assert receipt.status == "failed"
    assert receipt.completed_at is not None
    append.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_expires_the_exact_failed_guided_attempt(monkeypatch) -> None:
    attempt_id = str(uuid.uuid4())
    session = _session(
        phase="executing",
        active_plan={"guided_generation_attempt_id": attempt_id},
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=3,
            generation_attempt_id=attempt_id,
            status="failed",
            failure=ProposalFailure(
                code="guided_edit_infeasible",
                message="The story could not fit this footage.",
            ),
        ).model_dump(mode="json"),
    )
    receipt = SimpleNamespace(
        status="succeeded",
        created_at=datetime.now(UTC)
        - timedelta(seconds=creator_sessions.EXECUTION_RECEIPT_LEASE_S + 1),
        error=None,
        completed_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.return_value = item
    db.execute.return_value = result
    append = AsyncMock()
    monkeypatch.setattr(creator_sessions, "append_event", append)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert session.last_error["code"] == "guided_edit_infeasible"
    assert receipt.error == {"code": "guided_edit_infeasible"}
    assert item.edit_proposal["status"] == "failed"
    assert item.edit_proposal["design_fallback"] == "creator_execution_expired"
    append.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_exact_main_creator_failure_without_waiting_for_lease(
    monkeypatch,
) -> None:
    attempt_id = str(uuid.uuid4())
    session = _session(
        phase="executing",
        active_plan={
            "guided_generation_attempt_id": attempt_id,
            "edit_plan": {"strategy": {"render_program": "guided"}},
        },
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=3,
            generation_attempt_id=attempt_id,
            status="failed",
            approval_mode="auto",
            failure=ProposalFailure(
                code="proposal_generation_failed",
                message="Kria couldn't plan this edit. Try again.",
            ),
            design_fallback="main_creator_fail_closed",
        ).model_dump(mode="json"),
    )
    receipt = SimpleNamespace(
        status="succeeded",
        created_at=datetime.now(UTC),
        error=None,
        completed_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.return_value = item
    db.execute.return_value = result
    append = AsyncMock()
    monkeypatch.setattr(creator_sessions, "append_event", append)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert session.last_error["code"] == "proposal_generation_failed"
    assert receipt.status == "failed"
    assert receipt.error == {"code": "proposal_generation_failed"}
    assert item.edit_proposal["design_fallback"] == "main_creator_fail_closed"
    append.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_expires_a_succeeded_but_stalled_guided_attempt(monkeypatch) -> None:
    attempt_id = str(uuid.uuid4())
    session = _session(
        phase="executing",
        active_plan={"guided_generation_attempt_id": attempt_id},
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id=attempt_id,
            status="analyzing",
            approval_mode="auto",
        ).model_dump(mode="json"),
    )
    receipt = SimpleNamespace(
        status="succeeded",
        created_at=datetime.now(UTC)
        - timedelta(seconds=creator_sessions.EXECUTION_RECEIPT_LEASE_S + 1),
        error=None,
        completed_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.return_value = item
    db.execute.return_value = result
    monkeypatch.setattr(creator_sessions, "append_event", AsyncMock())
    monkeypatch.setattr(
        "app.services.queue_state.get_task_runtime_state",
        MagicMock(return_value=SimpleNamespace(state="not_found")),
    )

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert receipt.status == "failed"
    assert item.edit_proposal["design_fallback"] == "creator_execution_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_narration", [False, True])
async def test_reconcile_does_not_expire_a_creator_task_still_waiting_in_queue(
    monkeypatch,
    has_narration,
) -> None:
    attempt_id = str(uuid.uuid4())
    session = _session(
        phase="executing",
        active_plan={"guided_generation_attempt_id": attempt_id},
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id=attempt_id,
            status="analyzing",
            approval_mode="auto",
            brief={
                "narration": {"gcs_path": "voiceover/a.m4a", "generation": "99", "duration_s": 4.7}
            }
            if has_narration
            else {},
        ).model_dump(mode="json"),
    )
    receipt = SimpleNamespace(
        status="succeeded",
        created_at=datetime.now(UTC)
        - timedelta(seconds=creator_sessions.EXECUTION_RECEIPT_LEASE_S + 1),
        error=None,
        completed_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.return_value = item
    db.execute.return_value = result
    append = AsyncMock()
    runtime = MagicMock(return_value=SimpleNamespace(state="queued"))
    monkeypatch.setattr(creator_sessions, "append_event", append)
    monkeypatch.setattr("app.services.queue_state.get_task_runtime_state", runtime)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is False
    assert session.phase == "executing"
    assert receipt.status == "succeeded"
    append.assert_not_awaited()
    runtime.assert_called_once()
    from app.config import settings

    assert runtime.call_args.kwargs["queue_name"] == (
        "creator-fidelity-v1" if has_narration else settings.pool_asset_analysis_queue
    )


@pytest.mark.asyncio
async def test_reconcile_does_not_expire_a_started_attempt_inside_its_task_budget(
    monkeypatch,
) -> None:
    attempt_id = str(uuid.uuid4())
    session = _session(
        phase="executing",
        active_plan={"guided_generation_attempt_id": attempt_id},
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id=attempt_id,
            planning_started_at=datetime.now(UTC),
            status="analyzing",
            approval_mode="auto",
        ).model_dump(mode="json"),
    )
    receipt = SimpleNamespace(
        status="succeeded",
        created_at=datetime.now(UTC)
        - timedelta(seconds=creator_sessions.EXECUTION_RECEIPT_LEASE_S + 1),
        error=None,
        completed_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.return_value = item
    db.execute.return_value = result
    append = AsyncMock()
    monkeypatch.setattr(creator_sessions, "append_event", append)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is False
    assert session.phase == "executing"
    assert receipt.status == "succeeded"
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_ignores_an_old_terminal_job_when_guided_attempt_stalls(
    monkeypatch,
) -> None:
    creator_id = uuid.uuid4()
    attempt_id = str(uuid.uuid4())
    old_job_id = uuid.uuid4()
    receipt_created = datetime.now(UTC) - timedelta(
        seconds=creator_sessions.EXECUTION_RECEIPT_LEASE_S + 1
    )
    session = _session(
        creator_id=creator_id,
        phase="executing",
        active_plan={
            "guided_generation_attempt_id": attempt_id,
            "edit_plan": {"strategy": {"render_program": "guided"}},
        },
    )
    item = SimpleNamespace(
        id=session.plan_item_id,
        current_job_id=old_job_id,
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id=attempt_id,
            status="drafting",
            approval_mode="auto",
        ).model_dump(mode="json"),
    )
    old_job = SimpleNamespace(
        id=old_job_id,
        status="completed",
        created_at=receipt_created - timedelta(days=1),
        user_id=creator_id,
        content_plan_item_id=session.plan_item_id,
        content_plan_ownership_epoch=0,
        all_candidates={},
        assembly_plan={},
    )
    receipt = SimpleNamespace(
        status="succeeded",
        created_at=receipt_created,
        error=None,
        completed_at=receipt_created,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.side_effect = [item, old_job]
    db.execute.return_value = result
    monkeypatch.setattr(creator_sessions, "append_event", AsyncMock())
    monkeypatch.setattr(
        "app.services.queue_state.get_task_runtime_state",
        MagicMock(return_value=SimpleNamespace(state="not_found")),
    )

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert session.target_job_id is None
    assert item.edit_proposal["design_fallback"] == "creator_execution_expired"


@pytest.mark.asyncio
async def test_context_caps_combined_clips_and_assets_at_manifest_limit() -> None:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[
            {"media_id": f"clip-{index}", "gcs_path": f"users/u/{index}.mp4"} for index in range(50)
        ],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        kind="image",
        duration_s=None,
        user_context=None,
        analysis=None,
    )
    asset_result = MagicMock()
    asset_result.scalars.return_value = [asset]
    track_result = MagicMock()
    track_result.scalars.return_value = []
    sfx_result = MagicMock()
    sfx_result.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [asset_result, track_result, sfx_result]

    manifest, media_context = await creator_sessions.resolve_item_creator_context(
        db, item, persona=persona
    )

    assert len(manifest.media) == 50
    assert len(media_context) == 50
    assert all(not media.media_id.startswith("asset-") for media in manifest.media)


@pytest.mark.asyncio
async def test_context_respects_direct_guided_proposal_gate(monkeypatch) -> None:
    monkeypatch.setattr(creator_sessions.settings, "guided_edit_capability_enabled", False)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=["users/u/clip.mp4"],
        clip_assignments=[],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())
    empty_result = MagicMock()
    empty_result.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [empty_result, empty_result, empty_result]

    manifest, _ = await creator_sessions.resolve_item_creator_context(db, item, persona=persona)

    assert manifest.capabilities["draft_guided_proposal"].available is False


@pytest.mark.asyncio
async def test_context_can_advertise_chat_internal_guided_proposals(monkeypatch) -> None:
    monkeypatch.setattr(creator_sessions.settings, "guided_edit_capability_enabled", False)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=["users/u/clip.mp4"],
        clip_assignments=[],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())
    empty_result = MagicMock()
    empty_result.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [empty_result, empty_result, empty_result]

    manifest, _ = await creator_sessions.resolve_item_creator_context(
        db,
        item,
        persona=persona,
        guided_capability_enabled=True,
    )

    assert manifest.capabilities["draft_guided_proposal"].available is True


@pytest.mark.asyncio
async def test_context_preserves_analyzed_clip_assignment_duration() -> None:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[
            {
                "media_id": "match-a",
                "gcs_path": "users/u/a.mp4",
                "duration_s": 6.633,
            },
            {
                "media_id": "match-b",
                "gcs_path": "users/u/b.mp4",
                "duration_s": "66.433",
            },
        ],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        kind="video",
        duration_s=90.5,
        user_context=None,
        analysis=None,
    )
    asset_result = MagicMock()
    asset_result.scalars.return_value = [asset]
    empty_result = MagicMock()
    empty_result.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [asset_result, empty_result, empty_result]

    manifest, media_context = await creator_sessions.resolve_item_creator_context(
        db, item, persona=persona
    )

    assert [media.duration_s for media in manifest.media] == [6.633, 66.433, 90.5]
    assert [media.get("duration_s") for media in media_context] == [6.633, 66.433, 90.5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "disabled",
        "outside_cohort",
        "invalid",
        "mixed",
        "mixed_unassigned",
        "legacy_missing",
        "beyond_manifest",
    ],
)
async def test_context_keeps_phone_provenance_and_validates_receipts(monkeypatch, case) -> None:
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.agents._schemas.creator_policy import MixedMediaTimingUnavailableError
    from app.services.creator_capabilities import compile_strategy_to_plan
    from tests.services.test_phone_sources import receipt

    owner = uuid.uuid4()
    assignment = receipt("phone-a")
    assignments = [assignment]
    paths = [assignment["gcs_path"]]
    if case == "invalid":
        assignment["upload_contract"] = {}
    elif case == "mixed":
        assignments.append({"media_id": "cloud-b", "gcs_path": "users/u/cloud.mp4"})
        paths.append("users/u/cloud.mp4")
    elif case == "mixed_unassigned":
        paths.append("users/u/cloud.mp4")
    elif case == "legacy_missing":
        assignments = []
    elif case == "beyond_manifest":
        assignments = [
            {"media_id": f"cloud-{index}", "gcs_path": f"users/u/{index}.mp4"}
            for index in range(50)
        ] + assignments
        paths = [row["gcs_path"] for row in assignments]
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="original",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=paths,
        clip_assignments=assignments,
    )
    monkeypatch.setattr(creator_sessions.settings, "phone_rendering_enabled", case != "disabled")
    monkeypatch.setattr(
        creator_sessions.settings,
        "phone_render_user_ids",
        [uuid.uuid4() if case == "outside_cohort" else owner],
    )
    empty = MagicMock()
    empty.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [empty, empty, empty]

    manifest, _ = await creator_sessions.resolve_item_creator_context(
        db,
        item,
        persona=SimpleNamespace(user_id=owner),
        guided_capability_enabled=True,
    )

    assert "phone_source_audio" in manifest.capabilities
    assert manifest.capabilities["phone_source_audio"].available is (case == "valid")
    strategy = CreativeStrategy(audio_strategy="original_audio", render_program="native")
    if case == "valid":
        assert compile_strategy_to_plan(manifest, strategy).strategy.render_program == "guided"
    else:
        with pytest.raises(MixedMediaTimingUnavailableError):
            compile_strategy_to_plan(manifest, strategy)
    assert "analysis-proxy" not in manifest.model_dump_json()


@pytest.mark.asyncio
async def test_context_exposes_only_ready_published_sound_effect_catalog_refs() -> None:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=["users/u/clip.mp4"],
        clip_assignments=[],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())
    asset_result = MagicMock()
    asset_result.scalars.return_value = []
    track_result = MagicMock()
    track_result.scalars.return_value = []
    sfx = SimpleNamespace(id="catalog-pop", name="Soft pop")
    sfx_result = MagicMock()
    sfx_result.scalars.return_value = [sfx]
    db = AsyncMock()
    db.execute.side_effect = [asset_result, track_result, sfx_result]

    manifest, _ = await creator_sessions.resolve_item_creator_context(db, item, persona=persona)

    assert [ref.model_dump() for ref in manifest.catalog] == [
        {"catalog_id": "catalog-pop", "kind": "sound_effect", "label": "Soft pop"}
    ]


@pytest.mark.asyncio
async def test_reconcile_never_adopts_a_job_for_a_different_native_strategy() -> None:
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    receipt_time = datetime(2026, 8, 24, tzinfo=UTC)
    session = _session(
        creator_id=creator_id,
        plan_item_id=item_id,
        phase="rendering",
        active_plan={
            "edit_plan": {
                "strategy": {
                    "edit_format": "montage",
                    "audio_strategy": "licensed_music",
                    "pacing": "fast",
                    "render_program": "native",
                    "selected_media_ids": ["clip-1"],
                }
            }
        },
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    candidate = SimpleNamespace(
        id=job_id,
        created_at=receipt_time,
        user_id=creator_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        all_candidates={"creator_strategy": {"pacing": "relaxed"}},
        assembly_plan={},
    )
    receipt = SimpleNamespace(created_at=receipt_time, status="succeeded")
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.get.side_effect = [item, candidate]
    db.execute.return_value = result

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is False
    assert session.target_job_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_key", ["guided_edit", "creator_guided_fallback"])
async def test_reconcile_adopts_guided_job_by_stable_attempt_across_version_changes(
    snapshot_key,
) -> None:
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    job_id = uuid.uuid4()
    receipt_time = datetime(2026, 8, 24, tzinfo=UTC)
    attempt_id = str(uuid.uuid4())
    session = _session(
        creator_id=creator_id,
        plan_item_id=item_id,
        phase="executing",
        active_plan={
            "guided_generation_attempt_id": attempt_id,
            # The reservation was version 2. Draft + approval advanced it to
            # version 4 before dispatch; correlation must survive that.
            "guided_proposal_version": 2,
            "edit_plan": {"strategy": {"render_program": "guided"}},
        },
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id, content_plan_id=plan_id)
    candidate = SimpleNamespace(
        id=job_id,
        created_at=receipt_time,
        user_id=creator_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        all_candidates={},
        assembly_plan={
            snapshot_key: {
                "proposal_version": 4,
                "generation_attempt_id": attempt_id,
            }
        },
        status="processing",
    )
    receipt = SimpleNamespace(created_at=receipt_time)
    result = MagicMock()
    result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    plan = SimpleNamespace(user_id=creator_id, ownership_epoch=0)
    db.get.side_effect = [item, candidate, candidate, item, plan]
    db.execute.return_value = result

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.target_job_id == job_id
    assert session.phase == "rendering"


def _pending_visual_item() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[
            {"media_id": "ios-clip-1.mp4", "gcs_path": "users/u/1.mp4", "kind": "video"},
        ],
    )


def _visual(
    asset_id: uuid.UUID, *, status: str, analysis: dict | None, kind: str = "image"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=asset_id,
        kind=kind,
        status=status,
        duration_s=None,
        user_context=None,
        analysis=analysis,
    )


async def _resolve_with_visuals(item, persona, visuals):
    asset_result = MagicMock()
    asset_result.scalars.return_value = visuals
    track_result = MagicMock()
    track_result.scalars.return_value = []
    sfx_result = MagicMock()
    sfx_result.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [asset_result, track_result, sfx_result]
    manifest, media_context = await creator_sessions.resolve_item_creator_context(
        db, item, persona=persona
    )
    return manifest, media_context, db


@pytest.mark.asyncio
async def test_context_keeps_visuals_in_manifest_while_their_analysis_is_pending() -> None:
    """Regression: prod thread 168b17ec (2026-09-16).

    A direction planned while Visuals were still analyzing hashed a manifest
    without them; once analysis finished, every confirm hit the
    "Footage or capabilities changed" fence. Manifest identity must not depend
    on the asynchronous analysis state of a registered Visual.
    """

    item = _pending_visual_item()
    persona = SimpleNamespace(user_id=uuid.uuid4())
    asset_id = uuid.uuid4()

    pending, pending_context, db = await _resolve_with_visuals(
        item, persona, [_visual(asset_id, status="analyzing", analysis=None)]
    )
    ready, ready_context, _ = await _resolve_with_visuals(
        item,
        persona,
        [
            _visual(
                asset_id,
                status="ready",
                # `description` is the real legacy top-level key `clip_record`
                # projects into the shared record's `summary` field (KRI-127).
                analysis={"description": "A close-up photo of Lionel Messi"},
            )
        ],
    )

    # The Visual is footage as soon as it is registered, not once Gemini is done.
    assert [media.media_id for media in pending.media] == ["ios-clip-1.mp4", f"asset-{asset_id}"]
    assert pending.manifest_hash == ready.manifest_hash

    # The planner is told the evidence is still on its way, and never sees
    # analysis text before the row is ready.
    pending_entry = pending_context[-1]
    assert pending_entry["media_id"] == f"asset-{asset_id}"
    assert pending_entry["analysis_status"] == "pending"
    assert pending_entry["analysis_only_not_copy"] == {}
    ready_entry = ready_context[-1]
    assert "analysis_status" not in ready_entry
    assert ready_entry["analysis_only_not_copy"] == {"summary": "A close-up photo of Lionel Messi"}

    # The query itself admits every registered analysis state and nothing else:
    # reservations, cleanup claims and failed rows stay invisible to the planner.
    asset_query = db.execute.call_args_list[0].args[0]
    compiled = str(asset_query.compile(compile_kwargs={"literal_binds": True}))
    assert "plan_item_assets.status IN ('uploaded', 'queued', 'analyzing', 'ready')" in compiled
    assert "plan_item_assets.status = 'ready'" not in compiled
    assert "deduplicated_to_asset_id IS NULL" in compiled


# --- KRI-127: chat evidence reads the shared clip-understanding record -----


@pytest.mark.asyncio
async def test_chat_evidence_for_pool_asset_exposes_full_shared_record() -> None:
    """A new-style analysis exposes far more than the old dead-key dict did.

    Before KRI-127, `analysis_only_not_copy` was built from top-level
    `summary`/`description`/`setting`/`activity` keys no analyzer ever wrote
    at that level, so chat effectively only ever saw `description`.
    """
    from app.services.clip_understanding import UNDERSTANDING_KEY, understanding_payload

    meta = SimpleNamespace(
        detected_subject="man cooking pasta",
        transcript="okay so first we boil the water",
        clip_summary="A man narrates cooking pasta in a home kitchen.",
        setting="home kitchen",
        activity="cooking pasta",
        people_count=1,
        speaks_to_camera=True,
        people_note="one man faces the camera and narrates",
        clip_brands=["Barilla"],
        clip_content_type="tutorial",
        clip_audio_type="dialogue",
    )
    analysis = {
        "subject": "man cooking pasta",
        "description": "a man narrates cooking pasta",
        UNDERSTANDING_KEY: understanding_payload(
            meta, best_moments=[{"start_s": 0.0, "end_s": 2.0, "description": "adds pasta"}]
        ),
    }
    item = _pending_visual_item()
    item.clip_assignments = []
    persona = SimpleNamespace(user_id=uuid.uuid4())
    asset_id = uuid.uuid4()

    _manifest, media_context, _db = await _resolve_with_visuals(
        item, persona, [_visual(asset_id, status="ready", analysis=analysis, kind="video")]
    )

    evidence = media_context[-1]["analysis_only_not_copy"]
    assert evidence["subject"] == "man cooking pasta"
    assert evidence["summary"] == "A man narrates cooking pasta in a home kitchen."
    assert evidence["setting"] == "home kitchen"
    assert evidence["activity"] == "cooking pasta"
    assert evidence["speech"]["to_camera"] is True
    assert evidence["speech"]["transcript"].startswith("okay so first we boil")
    # Chat-specific trims: brands dropped, moments capped.
    assert "brands" not in evidence
    assert len(evidence["notable_moments"]) <= creator_sessions.CHAT_EVIDENCE_MAX_MOMENTS
    # Worst case stays bounded: 50 clips share one chat prompt.
    assert len(json.dumps(evidence)) < 1200


@pytest.mark.asyncio
async def test_chat_evidence_for_legacy_video_analysis_exposes_subject_and_transcript() -> None:
    """Legacy (pre-KRI-127) video analyses stored the transcript under
    `on_screen_text`. Chat must see subject + transcript + moments, not just
    `description` as before.
    """
    legacy_analysis = {
        "subject": "people playing soccer",
        "description": "a goal is scored",
        "on_screen_text": "what a goal",
        "source": "clip_metadata",
        "best_moments": [{"start_s": 0.0, "end_s": 2.0, "description": "goal"}],
        "analysis_version": 7,
    }
    item = _pending_visual_item()
    item.clip_assignments = []
    persona = SimpleNamespace(user_id=uuid.uuid4())
    asset_id = uuid.uuid4()

    _manifest, media_context, _db = await _resolve_with_visuals(
        item,
        persona,
        [_visual(asset_id, status="ready", analysis=legacy_analysis, kind="video")],
    )

    evidence = media_context[-1]["analysis_only_not_copy"]
    assert evidence["subject"] == "people playing soccer"
    assert evidence["summary"] == "a goal is scored"
    assert evidence["speech"]["transcript"] == "what a goal"
    assert evidence["notable_moments"][0]["description"] == "goal"
    # The old, effectively-blind projection only ever surfaced `description`.
    assert "on_screen_text" not in evidence


@pytest.mark.asyncio
async def test_chat_evidence_for_raw_clip_assignment_reads_planner_analysis() -> None:
    """Raw (non-pool) clips gain analysis once the guided planner analyzes them
    (app/tasks/edit_proposal_build.py writes it back onto the assignment).
    Chat must see it through the same shared record, not stay permanently blind.
    """
    item = _pending_visual_item()
    item.clip_assignments = [
        {
            "media_id": "ios-clip-1.mp4",
            "gcs_path": "users/u/1.mp4",
            "kind": "video",
            "analysis": {
                "subject": "friends at a pub",
                "description": "friends share a round of drinks",
                "on_screen_text": "cheers to that",
                "source": "clip_metadata",
            },
        }
    ]
    persona = SimpleNamespace(user_id=uuid.uuid4())

    _manifest, media_context, _db = await _resolve_with_visuals(item, persona, [])

    evidence = media_context[0]["analysis_only_not_copy"]
    assert evidence["subject"] == "friends at a pub"
    assert evidence["summary"] == "friends share a round of drinks"
    assert evidence["speech"]["transcript"] == "cheers to that"


# --- KRI-121 round 2: a project holding only Visuals plans for the iPhone ----


def _rows(values: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = values
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["native_photos", "native_pool_video", "web_thread", "voiceover", "talking_head"]
)
async def test_visuals_only_context_plans_for_the_phone_only_when_the_rule_holds(
    monkeypatch, case
) -> None:
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.services.creator_capabilities import compile_strategy_to_plan
    from app.services.phone_destination import DEVICE_INTENT_KEY

    owner = uuid.uuid4()
    kind = "video" if case == "native_pool_video" else "image"
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="talking_head" if case == "talking_head" else "montage",
        audio_mode="voiceover" if case == "voiceover" else "kria",
        voiceover_gcs_path="users/u/voice.m4a" if case == "voiceover" else None,
        voiceover_generation=None,
        voiceover_duration_s=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[],
    )
    asset = SimpleNamespace(
        id=uuid.uuid4(), kind=kind, duration_s=None, user_context=None, analysis=None
    )
    monkeypatch.setattr(creator_sessions.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(creator_sessions.settings, "phone_render_user_ids", [owner])
    monkeypatch.setattr(
        creator_sessions.settings,
        "phone_render_verified_features",
        ["stillImages", "visualVideos"],
    )
    state = {"media": []} if case == "web_thread" else {DEVICE_INTENT_KEY: "device"}
    db = AsyncMock()
    # A voiceover stops the rule before it reads anything; a thread without the
    # stamp stops it before the pool read.
    rule_reads = {"voiceover": [], "web_thread": [_rows([state])]}.get(
        case, [_rows([state]), _rows([kind])]
    )
    db.execute.side_effect = [*rule_reads, _rows([asset]), _rows([]), _rows([])]

    manifest, _ = await creator_sessions.resolve_item_creator_context(
        db,
        item,
        persona=SimpleNamespace(user_id=owner),
        guided_capability_enabled=True,
    )

    on_phone = case in {"native_photos", "native_pool_video"}
    assert ("phone_source_audio" in manifest.capabilities) is on_phone
    if on_phone:
        assert manifest.capabilities["phone_source_audio"].available
        assert manifest.capabilities["dispatch_render"].available
        plan = compile_strategy_to_plan(manifest, CreativeStrategy(media_scope="all"))
        assert plan.strategy.render_program == "guided"
        assert plan.strategy.selected_media_ids == [f"asset-{asset.id}"]
