"""Focused state-machine tests for durable Main Creator sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.edit_proposal import EditProposal, ProposalFailure
from app.services import creator_sessions


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
        "events": [],
        "created_at": datetime(2026, 8, 24, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 24, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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

    changed = await creator_sessions.reconcile_render_state(db, session)

    assert changed is True
    assert session.phase == "failed"
    assert receipt.status == "failed"
    assert item.edit_proposal["design_fallback"] == "creator_execution_expired"


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
    db = AsyncMock()
    db.execute.side_effect = [asset_result, track_result]

    manifest, media_context = await creator_sessions.resolve_item_creator_context(
        db, item, persona=persona
    )

    assert len(manifest.media) == 50
    assert len(media_context) == 50
    assert all(not media.media_id.startswith("asset-") for media in manifest.media)


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
