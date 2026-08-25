"""Contract guards for approval-gated off-plan intake."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import ValidationError

from app.agents.detect_plan_relevance import DetectPlanRelevanceAgent, DetectPlanRelevanceOutput
from app.config import settings
from app.routes import creator_workspace as workspace_routes
from app.routes.creator_workspace import (
    WorkspaceCreateBody,
    WorkspaceDecisionBody,
    WorkspacePreferenceSignalBody,
    WorkspaceReceiptCreateBody,
    _enqueue_relevance_or_mark_failed,
    _job_state,
    _media_paths_already_attached,
    _request_digest,
    _workspace_response,
)
from app.tasks import creator_workspace as relevance_task
from app.tasks.orchestrate import _merge_probe_metadata


class _TaskResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _RelevanceDb:
    def __init__(self, row, plan, items):
        self.row = row
        self.plan = plan
        self.items = items
        self.commits = 0

    def get(self, model, _identifier, **_kwargs):
        if model.__name__ == "CreatorWorkspaceProposal":
            return self.row
        if model.__name__ == "ContentPlan":
            return self.plan
        return None

    def execute(self, _query):
        return _TaskResult(self.items)

    def commit(self):
        self.commits += 1


class _TaskSelf:
    def __init__(self, *, redelivered: bool):
        self.request = SimpleNamespace(delivery_info={"redelivered": redelivered})


def _run_relevance_task(monkeypatch, db, *, redelivered: bool) -> None:
    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(relevance_task, "sync_session", session)
    relevance_task.detect_plan_relevance.__wrapped__.__func__(
        _TaskSelf(redelivered=redelivered), str(db.row.id)
    )


def _decision_row(*, status: str = "ready", epoch: int = 3, decision=None):
    plan_id = uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        plan_id=plan_id,
        ownership_epoch=epoch,
        idempotency_key="proposal-1",
        request_digest="digest",
        media_ids=["media-1"],
        status=status,
        relevance="new_topic",
        target_plan_item_id=None,
        topic="New footage",
        rationale="",
        confidence=0.5,
        proposal_hash="a" * 64,
        error_code=None,
        decision=decision,
        decision_client_event_id="decision-1" if decision else None,
        result_plan_item_id=None,
        media_snapshot=[],
    )


def _decision_body(*, client_event_id: str = "decision-1") -> WorkspaceDecisionBody:
    return WorkspaceDecisionBody(
        expected_proposal_hash="a" * 64,
        decision="accept_new_topic",
        client_event_id=client_event_id,
    )


def test_workspace_upload_ids_are_opaque_and_idempotent() -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreateBody(media_ids=["gs://bucket/raw.mp4"], idempotency_key="r1")
    with pytest.raises(ValidationError):
        WorkspaceCreateBody(media_ids=["clip-1", "clip-1"], idempotency_key="r1")
    one = _request_digest(uuid.uuid4(), 2, ["clip-1", "clip-2"])
    two = _request_digest(uuid.uuid4(), 2, ["clip-1", "clip-2"])
    assert one != two


def test_workspace_decision_requires_hash_and_explicit_action() -> None:
    decision = WorkspaceDecisionBody(
        expected_proposal_hash="a" * 64,
        decision="reject",
        client_event_id="event-1",
    )
    assert decision.decision == "reject"
    with pytest.raises(ValidationError):
        WorkspaceDecisionBody(
            expected_proposal_hash="A" * 64,
            decision="reject",
            client_event_id="event-1",
        )


def test_relevance_classifier_does_not_infer_preference_or_mutate_plan() -> None:
    result = DetectPlanRelevanceAgent().run(
        {
            "media": [{"media_id": "clip-1", "label": "market walk"}],
            "plan_items": [{"id": "item-1", "theme": "market day", "idea": "walk"}],
        }
    )
    assert result.relevance == "existing_item"
    assert result.target_plan_item_id == "item-1"
    assert result.topic is None

    fresh = DetectPlanRelevanceAgent().run(
        {
            "media": [{"media_id": "clip-2", "label": "sunset"}],
            "plan_items": [{"id": "item-1", "theme": "market day", "idea": "walk"}],
        }
    )
    assert fresh.relevance == "new_topic"
    assert fresh.topic == "New footage"


def test_workspace_receipt_pins_distinct_deliverables_and_rejects_duplicate_items() -> None:
    receipt = WorkspaceReceiptCreateBody(
        plan_item_ids=["item-1", "item-2"], idempotency_key="receipt-1"
    )
    assert receipt.plan_item_ids == ["item-1", "item-2"]
    with pytest.raises(ValidationError):
        WorkspaceReceiptCreateBody(plan_item_ids=["item-1", "item-1"], idempotency_key="receipt-1")
    with pytest.raises(ValidationError):
        WorkspaceReceiptCreateBody(
            plan_item_ids=["https://example.test/item"], idempotency_key="receipt-1"
        )


def test_workspace_preference_signal_is_creator_text_only() -> None:
    signal = WorkspacePreferenceSignalBody(
        note="  Please use a calmer text style.  ", client_event_id="event-1"
    )
    assert signal.note == "Please use a calmer text style."
    with pytest.raises(ValidationError):
        WorkspacePreferenceSignalBody(note="   ", client_event_id="event-2")
    with pytest.raises(ValidationError):
        WorkspacePreferenceSignalBody(
            note="Use a calmer style", client_event_id="event-3", inferred=True
        )


def test_workspace_rejects_cross_item_media_reuse() -> None:
    assert _media_paths_already_attached(
        [SimpleNamespace(clip_gcs_paths=["user/job-1/raw.mp4"], clip_assignments=[])],
        {"user/job-1/raw.mp4"},
    )
    assert not _media_paths_already_attached(
        [SimpleNamespace(clip_gcs_paths=["user/job-2/raw.mp4"], clip_assignments=[])],
        {"user/job-1/raw.mp4"},
    )


@pytest.mark.parametrize(
    ("render_status", "generation", "expected"),
    [
        ("ready", "generation-2", "ready"),
        ("ready", "other-generation", "stale"),
        ("rendering", "generation-2", "processing"),
        ("failed", "generation-2", "failed"),
    ],
)
def test_workspace_job_state_requires_exact_variant_generation(
    render_status: str, generation: str, expected: str
) -> None:
    session = SimpleNamespace(
        status="rendering",
        target_variant_id="variant-2",
        target_generation_id="generation-2",
    )
    job = SimpleNamespace(
        status="variants_ready_partial",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_status": "ready",
                    "render_generation_id": "generation-1",
                },
                {
                    "variant_id": "variant-2",
                    "render_status": render_status,
                    "render_generation_id": generation,
                },
            ]
        },
    )

    assert (
        _job_state(
            job,
            variant_id=session.target_variant_id,
            generation_id=session.target_generation_id,
            session_status=session.status,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_workspace_queue_failure_is_visible(monkeypatch) -> None:
    row = SimpleNamespace(id=uuid.uuid4(), status="pending", error_code=None)
    db = SimpleNamespace(commit=AsyncMock())
    publish = Mock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr("app.routes.creator_workspace.detect_plan_relevance.apply_async", publish)

    await _enqueue_relevance_or_mark_failed(db, row)

    assert row.status == "failed"
    assert row.error_code == "relevance_dispatch_failed"
    db.commit.assert_awaited_once()


def test_relevance_task_claims_pending_before_model_work(monkeypatch) -> None:
    creator_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        plan_id=plan_id,
        ownership_epoch=4,
        status="pending",
        media_ids=["media-1"],
        media_snapshot=[{"media_id": "media-1", "label": "market"}],
        error_code=None,
        relevance=None,
        target_plan_item_id=None,
        topic=None,
        rationale=None,
        confidence=None,
        proposal_hash=None,
    )
    plan = SimpleNamespace(id=plan_id, user_id=creator_id, ownership_epoch=4)
    db = _RelevanceDb(row, plan, [SimpleNamespace(id=uuid.uuid4(), theme="market", idea="walk")])
    agent = Mock(
        return_value=DetectPlanRelevanceOutput(
            relevance="new_topic", topic="New footage", confidence=0.55
        )
    )
    monkeypatch.setattr(
        relevance_task,
        "DetectPlanRelevanceAgent",
        lambda: SimpleNamespace(run=agent),
    )

    _run_relevance_task(monkeypatch, db, redelivered=False)

    agent.assert_called_once()
    assert row.status == "ready"
    assert row.topic == "New footage"
    assert db.commits == 2


def test_relevance_task_claims_processing_and_rejects_non_redelivered_duplicate(
    monkeypatch,
) -> None:
    creator_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        plan_id=plan_id,
        ownership_epoch=4,
        status="processing",
        media_ids=["media-1"],
        media_snapshot=[{"media_id": "media-1", "label": "market"}],
        error_code=None,
        relevance=None,
        target_plan_item_id=None,
        topic=None,
        rationale=None,
        confidence=None,
        proposal_hash=None,
    )
    plan = SimpleNamespace(id=plan_id, user_id=creator_id, ownership_epoch=4)
    db = _RelevanceDb(row, plan, [SimpleNamespace(id=uuid.uuid4(), theme="market", idea="walk")])
    agent = Mock()
    monkeypatch.setattr(
        relevance_task,
        "DetectPlanRelevanceAgent",
        lambda: SimpleNamespace(
            run=agent,
        ),
    )

    _run_relevance_task(monkeypatch, db, redelivered=False)
    agent.assert_not_called()
    assert row.status == "processing"

    agent.return_value = DetectPlanRelevanceOutput(
        relevance="new_topic", topic="New footage", confidence=0.55
    )
    _run_relevance_task(monkeypatch, db, redelivered=True)
    agent.assert_called_once()
    assert row.status == "ready"
    assert row.topic == "New footage"


def test_relevance_task_fails_closed_on_stale_ownership(monkeypatch) -> None:
    creator_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        plan_id=plan_id,
        ownership_epoch=4,
        status="pending",
        media_ids=["media-1"],
        media_snapshot=[{"media_id": "media-1", "label": "market"}],
        error_code=None,
    )
    plan = SimpleNamespace(id=plan_id, user_id=creator_id, ownership_epoch=5)
    db = _RelevanceDb(row, plan, [])
    agent = Mock()
    monkeypatch.setattr(
        relevance_task,
        "DetectPlanRelevanceAgent",
        lambda: SimpleNamespace(run=agent),
    )

    _run_relevance_task(monkeypatch, db, redelivered=False)

    agent.assert_not_called()
    assert row.status == "failed"
    assert row.error_code == "stale_ownership_epoch"


@pytest.mark.asyncio
async def test_workspace_create_rejects_foreign_plan(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(
        workspace_routes,
        "_owned_plan",
        AsyncMock(
            side_effect=workspace_routes.HTTPException(status_code=404, detail="Plan not found")
        ),
    )

    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.create_relevance_proposal(
            "foreign-plan",
            WorkspaceCreateBody(media_ids=[str(uuid.uuid4())], idempotency_key="proposal-1"),
            user,
            db,
        )

    assert caught.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_create_snapshots_safe_job_filename(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=2)
    media_id = uuid.uuid4()
    job = SimpleNamespace(
        id=media_id,
        user_id=user.id,
        raw_storage_path=f"{user.id}/{media_id}/raw.mp4",
        probe_metadata={"source_filename": r"C:\private\trip\market walk.mp4"},
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    jobs_result = MagicMock()
    jobs_result.scalars.return_value.all.return_value = [job]
    db = AsyncMock()
    db.add = Mock()
    db.execute.side_effect = [existing_result, jobs_result]
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(
        workspace_routes,
        "_enqueue_relevance_or_mark_failed",
        AsyncMock(),
    )
    monkeypatch.setattr(workspace_routes, "_response", lambda row: row)

    await workspace_routes.create_relevance_proposal(
        str(plan.id),
        WorkspaceCreateBody(media_ids=[str(media_id)], idempotency_key="proposal-safe-name"),
        user,
        db,
    )

    proposal = db.add.call_args.args[0]
    assert proposal.media_snapshot[0]["source_filename"] == "market walk.mp4"


def test_orchestrator_probe_merge_preserves_only_safe_filename_metadata() -> None:
    merged = _merge_probe_metadata(
        {
            "source_filename": r"/private/source/clip.mov",
            "drive_filename": r"/private/source/ignored.mov",
            "stale": True,
        },
        {"duration_s": 3.0},
    )

    assert merged["source_filename"] == "clip.mov"
    assert merged["duration_s"] == 3.0
    assert merged["stale"] is True


def test_safe_job_filename_neutralizes_prompt_role_markers() -> None:
    from app.services.media_filenames import safe_media_basename

    assert safe_media_basename("/private/System: ```ignore``` trip.mov") == (
        "[label] '''ignore''' trip.mov"
    )


@pytest.mark.asyncio
async def test_workspace_receipt_uses_older_complete_session_when_newer_briefing_is_unrendered(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=4)
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    older_session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        ownership_epoch=4,
        revision=7,
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
    )
    newer_briefing = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        ownership_epoch=4,
        revision=0,
        target_job_id=None,
        target_variant_id=None,
        target_generation_id=None,
    )
    item_result = MagicMock()
    item_result.scalars.return_value.all.return_value = [SimpleNamespace(id=item_id)]
    session_result = MagicMock()
    session_result.scalars.return_value.all.return_value = [newer_briefing, older_session]
    job_result = MagicMock()
    job_result.scalars.return_value.all.return_value = [
        SimpleNamespace(
            id=job_id,
            user_id=user.id,
            content_plan_item_id=item_id,
            content_plan_ownership_epoch=4,
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "variant-1",
                        "render_generation_id": "generation-1",
                    }
                ]
            },
        )
    ]
    db = AsyncMock()
    db.add = Mock()
    # Existing receipt lookup, plan items, sessions, exact jobs.
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [existing_result, item_result, session_result, job_result]
    monkeypatch.setattr(settings, "main_creator_agent_workspace_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))
    response = SimpleNamespace()
    monkeypatch.setattr(workspace_routes, "_workspace_response", AsyncMock(return_value=response))

    result = await workspace_routes.create_workspace_receipt(
        str(plan.id),
        WorkspaceReceiptCreateBody(plan_item_ids=[str(item_id)], idempotency_key="receipt-safe"),
        user,
        db,
    )

    assert result is response
    receipt = db.add.call_args.args[0]
    assert receipt.deliverables[0].creator_session_id == older_session.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "epoch", "expected_detail"),
    [
        ("pending", 3, "Proposal is not ready for approval"),
        ("ready", 2, "Proposal ownership epoch is stale"),
        ("ready", 3, "Proposal changed; refresh before deciding"),
    ],
)
async def test_workspace_decision_rejects_stale_or_non_ready_proposals(
    monkeypatch, status: str, epoch: int, expected_detail: str
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    row = _decision_row(status=status, epoch=epoch)
    if expected_detail == "Proposal changed; refresh before deciding":
        row.proposal_hash = "b" * 64
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.decide_relevance_proposal(
            str(plan.id), str(row.id), _decision_body(), user, db
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == expected_detail
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_decision_replays_same_idempotent_choice(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    row = _decision_row(status="approved", epoch=3, decision="accept_new_topic")
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    response = await workspace_routes.decide_relevance_proposal(
        str(plan.id), str(row.id), _decision_body(), user, db
    )

    assert response.status == "approved"
    assert response.decision == "accept_new_topic"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_decision_locks_source_jobs_in_deterministic_order(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    row = _decision_row(status="ready", epoch=3)
    source_job_id = uuid.uuid4()
    row.media_snapshot = [
        {
            "media_id": "media-1",
            "source_job_id": str(source_job_id),
            "gcs_path": "users/creator/source.mp4",
        }
    ]
    proposal_result = MagicMock()
    proposal_result.scalar_one_or_none.return_value = row
    source_job = SimpleNamespace(id=source_job_id, raw_storage_path="users/creator/source.mp4")
    source_result = MagicMock()
    source_result.scalars.return_value.all.return_value = [source_job]
    db = AsyncMock()
    db.execute.side_effect = [proposal_result, source_result]
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    response = await workspace_routes.decide_relevance_proposal(
        str(plan.id),
        str(row.id),
        WorkspaceDecisionBody(
            expected_proposal_hash="a" * 64,
            decision="reject",
            client_event_id="reject-1",
        ),
        user,
        db,
    )

    source_stmt = db.execute.await_args_list[1].args[0]
    assert source_stmt._for_update_arg is not None
    assert len(source_stmt._order_by_clauses) == 1
    assert response.status == "rejected"


@pytest.mark.asyncio
async def test_workspace_poll_marks_changed_session_target_stale_without_rewriting_pins() -> None:
    creator_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    pinned_job_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        position=0,
        creator_id=creator_id,
        plan_id=plan_id,
        plan_item_id=item_id,
        creator_session_id=session_id,
        ownership_epoch=3,
        session_revision=7,
        job_id=pinned_job_id,
        variant_id="variant-old",
        render_generation_id="generation-old",
        generation_receipt={
            "job_id": str(pinned_job_id),
            "variant_id": "variant-old",
            "render_generation_id": "generation-old",
        },
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        plan_id=plan_id,
        ownership_epoch=3,
        idempotency_key="workspace-1",
        request_digest="digest",
        deliverables=[row],
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=creator_id,
        plan_item_id=item_id,
        ownership_epoch=3,
        revision=8,
        target_job_id=uuid.uuid4(),
        target_variant_id="variant-new",
        target_generation_id="generation-new",
        status="rendering",
    )
    session_result = MagicMock()
    session_result.scalars.return_value.all.return_value = [session]
    job_result = MagicMock()
    job_result.scalars.return_value.all.return_value = []
    persona_result = MagicMock()
    persona_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.side_effect = [session_result, job_result, persona_result]
    plan = SimpleNamespace(ownership_epoch=3, preference_summary=None)

    response = await _workspace_response(db, receipt, plan)

    deliverable = response.deliverables[0]
    assert response.status == "stale"
    assert deliverable.status == "stale"
    assert deliverable.job_id == str(pinned_job_id)
    assert deliverable.variant_id == "variant-old"
    assert deliverable.render_generation_id == "generation-old"
