"""Contract guards for approval-gated off-plan intake."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.agents.detect_plan_relevance import DetectPlanRelevanceAgent, DetectPlanRelevanceOutput
from app.config import settings
from app.models import CreatorWorkspacePreferenceSignal, VideoFeedback
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


@pytest.mark.asyncio
async def test_workspace_media_reuse_uses_unbounded_jsonb_exists_query() -> None:
    db = AsyncMock()
    db.scalar.return_value = False
    await workspace_routes._media_paths_already_attached_db(
        db,
        creator_id=uuid.uuid4(),
        excluded_item_id=uuid.uuid4(),
        paths={"users/creator/job/raw.mp4"},
    )

    query = db.scalar.call_args.args[0]
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "EXISTS" in sql
    assert "clip_gcs_paths @>" in sql
    assert "clip_assignments @>" in sql
    assert " LIMIT " not in sql.upper()


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


def test_relevance_task_fails_closed_when_plan_context_exceeds_bound(monkeypatch) -> None:
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
    plan = SimpleNamespace(id=plan_id, user_id=creator_id, ownership_epoch=4)
    db = _RelevanceDb(
        row,
        plan,
        [
            SimpleNamespace(id=uuid.uuid4(), theme="market", idea="walk")
            for _ in range(relevance_task.MAX_RELEVANCE_PLAN_ITEMS + 1)
        ],
    )
    agent = Mock()
    monkeypatch.setattr(
        relevance_task,
        "DetectPlanRelevanceAgent",
        lambda: SimpleNamespace(run=agent),
    )

    _run_relevance_task(monkeypatch, db, redelivered=False)

    agent.assert_not_called()
    assert row.status == "failed"
    assert row.error_code == "plan_context_too_large"
    assert db.commits == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "jobs", "expected_status"),
    [
        ("missing", [], 404),
        (
            "foreign",
            [
                SimpleNamespace(
                    id=uuid.UUID("d7f3c86c-9888-4b72-b9d6-86bf8bb7edb5"),
                    user_id=uuid.uuid4(),
                    raw_storage_path="other-user/raw.mp4",
                    probe_metadata={},
                )
            ],
            409,
        ),
        (
            "empty-storage-path",
            [
                SimpleNamespace(
                    id=uuid.UUID("d7f3c86c-9888-4b72-b9d6-86bf8bb7edb5"),
                    user_id=uuid.uuid4(),
                    raw_storage_path="",
                    probe_metadata={},
                )
            ],
            409,
        ),
    ],
)
async def test_workspace_create_rejects_missing_or_unattachable_uploads_without_side_effects(
    monkeypatch, case: str, jobs: list[SimpleNamespace], expected_status: int
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=2)
    media_id = jobs[0].id if jobs else uuid.uuid4()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    jobs_result = MagicMock()
    jobs_result.scalars.return_value.all.return_value = jobs
    db = AsyncMock()
    db.add = Mock()
    db.execute.side_effect = [existing_result, jobs_result]
    enqueue = AsyncMock()
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(workspace_routes, "_enqueue_relevance_or_mark_failed", enqueue)

    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.create_relevance_proposal(
            str(plan.id),
            WorkspaceCreateBody(media_ids=[str(media_id)], idempotency_key=f"reject-{case}"),
            user,
            db,
        )

    assert caught.value.status_code == expected_status
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    enqueue.assert_not_awaited()


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


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _rows_result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _workspace_media_snapshot(job_id: uuid.UUID, path: str) -> list[dict[str, str | None]]:
    return [
        {
            "media_id": str(job_id),
            "source_job_id": str(job_id),
            "gcs_path": path,
            "gcs_generation": None,
            "kind": "video",
        }
    ]


@pytest.mark.asyncio
async def test_workspace_decision_accept_existing_attaches_clips_and_commits_atomically(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    target_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_path = f"{user.id}/{source_id}/raw.mp4"
    row = _decision_row(status="ready", epoch=3)
    row.relevance = "existing_item"
    row.target_plan_item_id = target_id
    row.media_snapshot = _workspace_media_snapshot(source_id, source_path)
    target = SimpleNamespace(
        id=target_id,
        clip_assignments=[],
        clip_gcs_paths=[],
        conformance={"status": "approved"},
        edit_proposal=None,
    )
    source_job = SimpleNamespace(
        id=source_id,
        user_id=user.id,
        raw_storage_path=source_path,
    )
    db = AsyncMock()
    db.add = Mock()
    db.scalar.return_value = False
    db.execute.side_effect = [
        _scalar_result(row),
        _rows_result([source_job]),
        _scalar_result(target),
        _rows_result([]),
    ]
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    response = await workspace_routes.decide_relevance_proposal(
        str(plan.id),
        str(row.id),
        WorkspaceDecisionBody(
            expected_proposal_hash="a" * 64,
            decision="accept_existing",
            client_event_id="accept-existing-1",
        ),
        user,
        db,
    )

    assert response.status == "approved"
    assert response.decision == "accept_existing"
    assert response.result_plan_item_id == str(target_id)
    assert target.clip_gcs_paths == [source_path]
    assert target.clip_assignments[0]["media_id"] == str(source_id)
    assert target.conformance is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspace_decision_accept_new_topic_creates_item_attaches_clips_and_commits(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    source_id = uuid.uuid4()
    source_path = f"{user.id}/{source_id}/raw.mp4"
    row = _decision_row(status="ready", epoch=3)
    row.relevance = "new_topic"
    row.topic = "A new walk"
    row.media_snapshot = _workspace_media_snapshot(source_id, source_path)
    source_job = SimpleNamespace(
        id=source_id,
        user_id=user.id,
        raw_storage_path=source_path,
    )
    max_position = MagicMock()
    max_position.scalar_one.return_value = 4
    db = AsyncMock()
    db.add = Mock()
    db.scalar.return_value = False
    db.execute.side_effect = [
        _scalar_result(row),
        _rows_result([source_job]),
        max_position,
        _rows_result([]),
    ]
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    response = await workspace_routes.decide_relevance_proposal(
        str(plan.id),
        str(row.id),
        WorkspaceDecisionBody(
            expected_proposal_hash="a" * 64,
            decision="accept_new_topic",
            client_event_id="accept-new-1",
        ),
        user,
        db,
    )

    created = db.add.call_args.args[0]
    assert created.content_plan_id == plan.id
    assert created.idea == "A new walk"
    assert created.position == 5
    assert created.item_status == "awaiting_clips"
    assert created.clip_gcs_paths == [source_path]
    assert created.clip_assignments[0]["media_id"] == str(source_id)
    assert created.conformance is None
    assert response.status == "approved"
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspace_decision_rejects_cross_item_media_reuse_before_commit(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    target_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_path = f"{user.id}/{source_id}/raw.mp4"
    row = _decision_row(status="ready", epoch=3)
    row.relevance = "existing_item"
    row.target_plan_item_id = target_id
    row.media_snapshot = _workspace_media_snapshot(source_id, source_path)
    target = SimpleNamespace(
        id=target_id,
        clip_assignments=[],
        clip_gcs_paths=[],
        conformance={"status": "approved"},
        edit_proposal=None,
    )
    other = SimpleNamespace(clip_gcs_paths=[source_path], clip_assignments=[])
    source_job = SimpleNamespace(
        id=source_id,
        user_id=user.id,
        raw_storage_path=source_path,
    )
    db = AsyncMock()
    db.add = Mock()
    db.scalar.return_value = True
    db.execute.side_effect = [
        _scalar_result(row),
        _rows_result([source_job]),
        _scalar_result(target),
        _rows_result([other]),
    ]
    monkeypatch.setattr(settings, "main_creator_agent_freeform_uploads_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.decide_relevance_proposal(
            str(plan.id),
            str(row.id),
            WorkspaceDecisionBody(
                expected_proposal_hash="a" * 64,
                decision="accept_existing",
                client_event_id="reuse-1",
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Proposal media is already attached to another plan item"
    assert target.clip_gcs_paths == []
    assert target.conformance == {"status": "approved"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_preference_signal_rejects_stale_receipt_epoch_without_writes(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=4, preference_summary=None)
    receipt = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=3)
    db = AsyncMock()
    db.add = Mock()
    db.execute.side_effect = [
        _scalar_result(None),  # idempotency lookup
        _scalar_result(receipt),
    ]
    monkeypatch.setattr(settings, "main_creator_agent_workspace_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.record_workspace_preference_signal(
            str(plan.id),
            WorkspacePreferenceSignalBody(
                note="Use calmer captions",
                client_event_id="stale-receipt-1",
                receipt_id=str(receipt.id),
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Workspace receipt ownership epoch is stale"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_preference_signal_replays_same_event_and_rejects_digest_conflict(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=4, preference_summary="calm")
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        ownership_epoch=4,
        request_digest=workspace_routes._preference_request_digest(
            plan.id, 4, "Use calmer captions", None
        ),
        note="Use calmer captions",
    )
    persona = SimpleNamespace(style={"style_set_id": "default", "status": "edited"})
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar_result(existing),
        _scalar_result(persona),
    ]
    monkeypatch.setattr(settings, "main_creator_agent_workspace_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    replay = await workspace_routes.record_workspace_preference_signal(
        str(plan.id),
        WorkspacePreferenceSignalBody(
            note="Use calmer captions",
            client_event_id="same-event-1",
        ),
        user,
        db,
    )
    assert replay.signal_id == str(existing.id)
    assert replay.preference_summary == "calm"
    db.commit.assert_not_awaited()

    conflict_db = AsyncMock()
    conflict_db.execute.return_value = _scalar_result(existing)
    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.record_workspace_preference_signal(
            str(plan.id),
            WorkspacePreferenceSignalBody(
                note="Use louder captions",
                client_event_id="same-event-1",
            ),
            user,
            conflict_db,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "Preference event reused with different content"
    conflict_db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("persona_present", [False, True])
async def test_workspace_preference_signal_style_path_requires_flag_and_persona(
    monkeypatch, persona_present: bool
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=4, preference_summary=None)
    db = AsyncMock()
    db.add = Mock()
    db.execute.side_effect = [
        _scalar_result(None),
        _scalar_result(None),  # persona is absent when the flag is enabled too
    ]
    monkeypatch.setattr(settings, "main_creator_agent_workspace_enabled", True)
    monkeypatch.setattr(settings, "user_style_enabled", persona_present)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    with pytest.raises(workspace_routes.HTTPException) as caught:
        await workspace_routes.record_workspace_preference_signal(
            str(plan.id),
            WorkspacePreferenceSignalBody(
                note="Use a large title",
                client_event_id=f"style-path-{persona_present}",
                style_edit={"instruction_level": "light"},
            ),
            user,
            db,
        )

    if persona_present:
        # The feature flag is on but no Persona row exists in this branch.
        assert caught.value.status_code == 404
        assert caught.value.detail == "Persona not found"
    else:
        # The route checks the kill switch before reading Persona.
        assert caught.value.status_code == 404
        assert caught.value.detail == "style_not_enabled"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_preference_signal_persists_signal_feedback_style_and_summary(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4(), ownership_epoch=4, preference_summary=None)
    persona = SimpleNamespace(
        style={
            "style_set_id": "default",
            "knobs": {"text_anchor": "center"},
            "instruction_level": "full",
            "status": "ready",
        }
    )
    counts = MagicMock()
    counts.all.return_value = [("note", 2)]
    notes = MagicMock()
    notes.scalars.return_value.all.return_value = ["Prefer calm captions"]
    db = AsyncMock()
    db.add = Mock()
    db.execute.side_effect = [
        _scalar_result(None),  # idempotency lookup
        _scalar_result(persona),  # persona for explicit style edit
        counts,  # summary signal counts
        notes,  # summary recent notes
    ]
    monkeypatch.setattr(settings, "main_creator_agent_workspace_enabled", True)
    monkeypatch.setattr(settings, "user_style_enabled", True)
    monkeypatch.setattr(workspace_routes, "_owned_plan", AsyncMock(return_value=plan))

    response = await workspace_routes.record_workspace_preference_signal(
        str(plan.id),
        WorkspacePreferenceSignalBody(
            note="Prefer calm captions",
            client_event_id="persisted-signal-1",
            style_edit={
                "knobs": {"text_anchor": "left"},
                "instruction_level": "light",
            },
        ),
        user,
        db,
    )

    assert response.source == "creator_explicit"
    assert response.note == "Prefer calm captions"
    assert response.style["instruction_level"] == "light"
    assert response.style["style_set_id"] == "default"
    assert response.style["knobs"]["text_anchor"] == "left"
    assert response.style["status"] == "edited"
    assert response.preference_summary
    assert plan.preference_summary == response.preference_summary
    assert len(db.add.call_args_list) == 2
    signal, feedback = (call.args[0] for call in db.add.call_args_list)
    assert isinstance(signal, CreatorWorkspacePreferenceSignal)
    assert signal.source == "creator_explicit"
    assert signal.signal == "note"
    assert signal.ownership_epoch == 4
    assert isinstance(feedback, VideoFeedback)
    assert feedback.content_plan_id == plan.id
    assert feedback.signal == "note"
    assert feedback.note == "Prefer calm captions"
    db.commit.assert_awaited_once()


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
