"""Focused state-machine tests for durable Main Creator sessions."""

from __future__ import annotations

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
async def test_reconcile_does_not_expire_a_creator_task_still_waiting_in_queue(
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
    runtime = MagicMock(return_value=SimpleNamespace(state="queued"))
    monkeypatch.setattr(creator_sessions, "append_event", append)
    monkeypatch.setattr("app.services.queue_state.get_task_runtime_state", runtime)

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is False
    assert session.phase == "executing"
    assert receipt.status == "succeeded"
    append.assert_not_awaited()
    runtime.assert_called_once()


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
