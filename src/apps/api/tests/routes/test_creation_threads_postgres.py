"""PostgreSQL regression coverage for creation-thread deletion.

The deletion ordering bug only exists in PostgreSQL: deleting a Job invokes
``ON DELETE SET NULL`` on its edit-learning rows, and the database append-only
trigger rejects that UPDATE.  This test seeds the real rows and calls the
route with a real AsyncSession so the FK actions and trigger are exercised.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.requests import Request

from app.config import settings
from app.database import AsyncSessionLocal, sync_engine
from app.models import (
    ContentPlan,
    CreationThread,
    EditArtifact,
    EditInteractionReceipt,
    InternalAccountGrant,
    Job,
    Persona,
    PlanItem,
    User,
)
from app.routes.creation_threads import delete_thread

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(
        f"refusing to write creation-thread integration fixtures to {_database_name!r}",
        allow_module_level=True,
    )
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT 1"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"nova_test Postgres not reachable: {exc!r}", allow_module_level=True)


@dataclass(frozen=True)
class _SeededProjects:
    user_id: uuid.UUID
    target_thread_id: uuid.UUID
    target_plan_id: uuid.UUID
    target_item_id: uuid.UUID
    target_job_id: uuid.UUID
    target_artifact_id: uuid.UUID
    target_receipt_id: uuid.UUID
    unrelated_thread_id: uuid.UUID
    unrelated_plan_id: uuid.UUID
    unrelated_item_id: uuid.UUID
    unrelated_job_id: uuid.UUID
    unrelated_artifact_id: uuid.UUID
    unrelated_receipt_id: uuid.UUID


def _artifact(
    *,
    artifact_id: uuid.UUID,
    creator_id: uuid.UUID,
    plan_item_id: uuid.UUID,
    job_id: uuid.UUID,
    grant_id: uuid.UUID,
) -> EditArtifact:
    return EditArtifact(
        id=artifact_id,
        creator_id=creator_id,
        plan_item_id=plan_item_id,
        job_id=job_id,
        render_generation_id=f"generation-{artifact_id}",
        artifact_kind="final_render",
        render_hash=f"render-hash-{artifact_id}",
        render_receipt_hash=f"receipt-hash-{artifact_id}",
        render_receipt={},
        storage_path=f"users/{creator_id}/edit-feedback/{artifact_id}/render.mp4",
        storage_generation="1",
        storage_content_hash=f"content-hash-{artifact_id}",
        capture_origin="creator",
        eligibility_basis="internal_grant",
        internal_grant_id=grant_id,
        creator_split="test",
        plan_item_split="test",
    )


def _receipt(
    *,
    receipt_id: uuid.UUID,
    creator_id: uuid.UUID,
    plan_item_id: uuid.UUID,
    job_id: uuid.UUID,
    grant_id: uuid.UUID,
) -> EditInteractionReceipt:
    return EditInteractionReceipt(
        id=receipt_id,
        event_kind="proposal",
        creator_id=creator_id,
        plan_item_id=plan_item_id,
        job_id=job_id,
        variant_id="original_text",
        utterance="make the opening faster",
        inferred_intent="pacing",
        model_reply="I can tighten the opening.",
        eligibility_basis="internal_grant",
        internal_grant_id=grant_id,
        proposed_operations=[],
        proposed_operations_digest=f"operations-digest-{receipt_id}",
        prompt_version="test",
        model="test-model",
        proposal_outcome="proposed",
        rejection_reasons=[],
    )


@pytest.fixture()
async def seeded_projects() -> _SeededProjects:
    ids = _SeededProjects(
        user_id=uuid.uuid4(),
        target_thread_id=uuid.uuid4(),
        target_plan_id=uuid.uuid4(),
        target_item_id=uuid.uuid4(),
        target_job_id=uuid.uuid4(),
        target_artifact_id=uuid.uuid4(),
        target_receipt_id=uuid.uuid4(),
        unrelated_thread_id=uuid.uuid4(),
        unrelated_plan_id=uuid.uuid4(),
        unrelated_item_id=uuid.uuid4(),
        unrelated_job_id=uuid.uuid4(),
        unrelated_artifact_id=uuid.uuid4(),
        unrelated_receipt_id=uuid.uuid4(),
    )

    async with AsyncSessionLocal() as db:
        db.add(User(id=ids.user_id, email=f"creation-delete-{ids.user_id}@example.test"))
        db.add(
            Persona(
                id=uuid.uuid4(),
                user_id=ids.user_id,
                persona_status="ready",
                persona={"summary": "test creator"},
                questionnaire={},
            )
        )
        await db.flush()
        persona_id = (
            await db.execute(select(Persona.id).where(Persona.user_id == ids.user_id))
        ).scalar_one()

        db.add_all(
            [
                ContentPlan(
                    id=ids.target_plan_id,
                    user_id=ids.user_id,
                    persona_id=persona_id,
                    plan_status="ready",
                ),
                ContentPlan(
                    id=ids.unrelated_plan_id,
                    user_id=ids.user_id,
                    persona_id=persona_id,
                    plan_status="ready",
                ),
            ]
        )
        await db.flush()

        target_item = PlanItem(
            id=ids.target_item_id,
            content_plan_id=ids.target_plan_id,
            position=1,
            idea="target project",
            item_status="awaiting_clips",
            clip_gcs_paths=[],
        )
        unrelated_item = PlanItem(
            id=ids.unrelated_item_id,
            content_plan_id=ids.unrelated_plan_id,
            position=1,
            idea="unrelated project",
            item_status="awaiting_clips",
            clip_gcs_paths=[],
        )
        db.add_all([target_item, unrelated_item])
        await db.flush()

        db.add_all(
            [
                Job(
                    id=ids.target_job_id,
                    user_id=ids.user_id,
                    status="done",
                    job_type="default",
                    raw_storage_path=f"jobs/{ids.target_job_id}/source.mp4",
                    content_plan_item_id=ids.target_item_id,
                    assembly_plan={},
                    all_candidates={},
                ),
                Job(
                    id=ids.unrelated_job_id,
                    user_id=ids.user_id,
                    status="done",
                    job_type="default",
                    raw_storage_path=f"jobs/{ids.unrelated_job_id}/source.mp4",
                    content_plan_item_id=ids.unrelated_item_id,
                    assembly_plan={},
                    all_candidates={},
                ),
            ]
        )
        await db.flush()
        target_item.current_job_id = ids.target_job_id
        unrelated_item.current_job_id = ids.unrelated_job_id

        db.add_all(
            [
                CreationThread(
                    id=ids.target_thread_id,
                    creator_id=ids.user_id,
                    content_plan_id=ids.target_plan_id,
                    active_plan_item_id=ids.target_item_id,
                    active_job_id=ids.target_job_id,
                    state={},
                ),
                CreationThread(
                    id=ids.unrelated_thread_id,
                    creator_id=ids.user_id,
                    content_plan_id=ids.unrelated_plan_id,
                    active_plan_item_id=ids.unrelated_item_id,
                    active_job_id=ids.unrelated_job_id,
                    state={},
                ),
            ]
        )
        grant_id = uuid.uuid4()
        db.add(
            InternalAccountGrant(
                id=grant_id,
                creator_id=ids.user_id,
                granted_by="postgres-regression-test",
                idempotency_key=f"grant-{ids.user_id}",
            )
        )
        await db.flush()
        db.add_all(
            [
                _artifact(
                    artifact_id=ids.target_artifact_id,
                    creator_id=ids.user_id,
                    plan_item_id=ids.target_item_id,
                    job_id=ids.target_job_id,
                    grant_id=grant_id,
                ),
                _artifact(
                    artifact_id=ids.unrelated_artifact_id,
                    creator_id=ids.user_id,
                    plan_item_id=ids.unrelated_item_id,
                    job_id=ids.unrelated_job_id,
                    grant_id=grant_id,
                ),
                _receipt(
                    receipt_id=ids.target_receipt_id,
                    creator_id=ids.user_id,
                    plan_item_id=ids.target_item_id,
                    job_id=ids.target_job_id,
                    grant_id=grant_id,
                ),
                _receipt(
                    receipt_id=ids.unrelated_receipt_id,
                    creator_id=ids.user_id,
                    plan_item_id=ids.unrelated_item_id,
                    job_id=ids.unrelated_job_id,
                    grant_id=grant_id,
                ),
            ]
        )
        await db.commit()

    try:
        yield ids
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(CreationThread).where(CreationThread.creator_id == ids.user_id))
            await db.execute(
                update(Job)
                .where(Job.id.in_((ids.target_job_id, ids.unrelated_job_id)))
                .values(content_plan_item_id=None)
            )
            await db.execute(
                delete(PlanItem).where(PlanItem.id.in_((ids.target_item_id, ids.unrelated_item_id)))
            )
            await db.execute(
                delete(Job).where(Job.id.in_((ids.target_job_id, ids.unrelated_job_id)))
            )
            await db.execute(
                delete(ContentPlan).where(
                    ContentPlan.id.in_((ids.target_plan_id, ids.unrelated_plan_id))
                )
            )
            await db.execute(delete(User).where(User.id == ids.user_id))
            await db.commit()


@pytest.mark.asyncio
async def test_delete_thread_removes_project_job_learning_rows_and_preserves_unrelated_data(
    seeded_projects: _SeededProjects,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion must beat Job.SET NULL while preserving the append-only boundary."""
    monkeypatch.setattr(
        "app.tasks.account_lifecycle.purge_job_storage.apply_async",
        Mock(),
    )

    async with AsyncSessionLocal() as db:
        user = await db.get(User, seeded_projects.user_id)
        assert user is not None
        trigger_rows = (
            await db.execute(
                text(
                    "SELECT tgrelid::regclass::text, tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname IN "
                    "('edit_artifacts_append_only', 'edit_interaction_receipts_append_only')"
                )
            )
        ).all()
        assert set(trigger_rows) == {
            ("edit_artifacts", "edit_artifacts_append_only"),
            ("edit_interaction_receipts", "edit_interaction_receipts_append_only"),
        }
        response = await delete_thread(
            request=Request({"type": "http", "method": "DELETE", "path": "/creation-threads"}),
            thread_id=str(seeded_projects.target_thread_id),
            user=user,
            db=db,
            expected_revision=0,
        )
        assert response.status_code == 204

        assert await db.get(CreationThread, seeded_projects.target_thread_id) is None
        assert await db.get(PlanItem, seeded_projects.target_item_id) is None
        assert await db.get(Job, seeded_projects.target_job_id) is None
        assert await db.get(EditArtifact, seeded_projects.target_artifact_id) is None
        assert await db.get(EditInteractionReceipt, seeded_projects.target_receipt_id) is None

        assert await db.get(CreationThread, seeded_projects.unrelated_thread_id) is not None
        assert await db.get(PlanItem, seeded_projects.unrelated_item_id) is not None
        assert await db.get(Job, seeded_projects.unrelated_job_id) is not None
        unrelated_artifact = await db.get(EditArtifact, seeded_projects.unrelated_artifact_id)
        unrelated_receipt = await db.get(
            EditInteractionReceipt, seeded_projects.unrelated_receipt_id
        )
        assert unrelated_artifact is not None
        assert unrelated_artifact.job_id == seeded_projects.unrelated_job_id
        assert unrelated_receipt is not None
        assert unrelated_receipt.job_id == seeded_projects.unrelated_job_id

        with pytest.raises(DBAPIError, match="edit learning records are append-only"):
            await db.execute(
                update(EditArtifact)
                .where(EditArtifact.id == seeded_projects.unrelated_artifact_id)
                .values(render_hash="must-be-rejected")
            )
        await db.rollback()

        trigger_names = (
            (
                await db.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'edit_artifacts'::regclass AND NOT tgisinternal"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "edit_artifacts_append_only" in trigger_names
